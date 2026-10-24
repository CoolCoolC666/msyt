# -*- coding: utf-8 -*-
"""
test_pool.py —— 协作池子语义的验收
================================================================================
这块的坑，全是「**看起来对、其实把别人的活搞坏了**」那一类，所以必须钉死：

  ① **回收不能碰已完成的活**
     2026-10-23 踩过一个自己写的 SQL 括号 bug：
     `WHERE state='taken' AND (A) OR (B)` —— AND 优先级高于 OR，
     第二段不受 state='taken' 约束 →
     某节点心跳断了 2 分钟，它名下 **438 条已采完**的活全被退回池子，
     完成数从 466 掉回 56（正文还在库里，只是状态乱了）。
     这条测试就是为了钉死它。

  ② 离线节点的活要能回收（关窗口是成员最常见的死法）
  ③ 超时未回传的活要能回收
  ④ 主动退活只退「还没回传」的
  ⑤ 建池是**先新后旧**（站方对历史帖不公开正文，先旧的全是白跑）
  ⑥ 已判「站方限制」的帖不再进新池

用法：python _tools/test_pool.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def main() -> int:
    from msyt.assist import AssistService
    from msyt.db import DB
    from msyt.hub import Hub

    tmp = tempfile.mkdtemp(prefix="msyt_pool_")
    try:
        db = DB(os.path.join(tmp, "t.db"))
        svc = AssistService(db, Hub())
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
        with db.lock:
            db.conn.executemany(
                "INSERT INTO members(id,email,pw_hash,role,status,node_id,last_seen,"
                "created_at) VALUES(?,?,?,'member','active',?,?,?)",
                [(1, "m1@example.com", "x", "member:live", now, now),
                 (2, "m2@example.com", "x", "member:dead", old, now)])
            db.conn.executemany(
                "INSERT INTO threads(tid,title,detail_state,pub_ts,dept_id) "
                "VALUES(?,?,'pending',?,'999')",
                [(91000001, "t1", 100), (91000002, "t2", 200), (91000003, "t3", 300),
                 (91000004, "t4", 400)])
            db.conn.execute(
                "INSERT INTO assist_tasks(code,title,dept_ids,total,done,state,"
                "created_by,created_at,updated_at) VALUES('P1','池','999',4,0,'open',"
                "'admin',?,?)", (now, now))
            # 4 条活：done / taken(在线节点) / taken(离线节点) / taken(超时)
            db.conn.executemany(
                "INSERT INTO assist_picks(task_code,tid,state,node,taken_at,done_at) "
                "VALUES('P1',?,?,?,?,?)",
                [(91000001, "done", "member:live", now, now),
                 (91000002, "taken", "member:live", now, None),
                 (91000003, "taken", "member:dead", now, None),
                 (91000004, "taken", "member:live", old, None)])
            db.conn.commit()

        print("=== ① 回收不许碰已完成的活（括号 bug 的回归测试）===")
        n = svc.reclaim_stale(max_age_sec=1800, offline_sec=120)
        states = {r["tid"]: r["state"] for r in db.conn.execute(
            "SELECT tid,state FROM assist_picks WHERE task_code='P1'")}
        check("已完成的活仍是 done", states.get(91000001) == "done",
              str(states.get(91000001)))
        check("★ 离线节点的活被回收", states.get(91000003) == "pending",
              str(states.get(91000003)))
        check("★ 超时的活被回收", states.get(91000004) == "pending",
              str(states.get(91000004)))
        check("★ 在线节点的活**不该**被回收（它可能正在采）",
              states.get(91000002) == "taken", str(states.get(91000002)))
        check("回收条数 = 2", n == 2, str(n))

        print("\n=== ② 回收不动 done（再来一轮，确认幂等）===")
        n2 = svc.reclaim_stale(max_age_sec=1800, offline_sec=120)
        states2 = {r["tid"]: r["state"] for r in db.conn.execute(
            "SELECT tid,state FROM assist_picks WHERE task_code='P1'")}
        check("★ 第二次回收后 done 仍是 done", states2.get(91000001) == "done",
              str(states2.get(91000001)))
        check("第二次回收 0 条", n2 == 0, str(n2))

        print("\n=== ③ 主动退活只退还没回传的 ===")
        n3 = svc.return_picks("member:live")
        states3 = {r["tid"]: r["state"] for r in db.conn.execute(
            "SELECT tid,state FROM assist_picks WHERE task_code='P1'")}
        check("★ 已完成的活不受退活影响", states3.get(91000001) == "done",
              str(states3.get(91000001)))
        check("在途的活被退回", states3.get(91000002) == "pending",
              str(states3.get(91000002)))
        check("退活条数 = 1", n3 == 1, str(n3))

        print("\n=== ④ 建池先新后旧 + 排除已判受限的帖 ===")
        with db.lock:
            db.conn.execute(
                "UPDATE threads SET detail_state='restricted' WHERE tid=91000004")
            db.conn.execute(
                "UPDATE assist_tasks SET state='closed' WHERE code='P1'")
            db.conn.commit()
        t = svc.create("新池", [999], total=10, created_by="admin")
        tids = [r["tid"] for r in db.conn.execute(
            "SELECT tid FROM assist_picks WHERE task_code=? ORDER BY id", (t["code"],))]
        check("★ 新池按 pub_ts 倒序（先新后旧）",
              tids == [91000003, 91000002, 91000001], str(tids))
        check("★ 已判受限的帖不进新池", 91000004 not in tids, str(tids))

        print("\n=== ⑤ 领活把 pending 变成 taken ===")
        r = svc.pick("member:live", limit=2, code=t["code"])
        check("领到 2 条", len(r.get("picks") or []) == 2, str(len(r.get("picks") or [])))
        got = {p["tid"] for p in r["picks"]}
        check("★ 领到的是**最新**的两条", got == {91000003, 91000002}, str(got))
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for _, c, _ in results if c)
    bad = [r for r in results if not r[1]]
    print(f"\n共 {len(results)} 项 · 通过 {ok} · 失败 {len(bad)}")
    for nm, _, e in bad:
        print(f"  FAIL: {nm}  {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
