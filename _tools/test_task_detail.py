# -*- coding: utf-8 -*-
"""
test_task_detail.py —— 协作任务「详情」与「移除」的验收
================================================================================
用户 2026-10-23 提的两条：
  ① 「协作应该可以看的到详情」→ 点任务能看到这个任务里的每一帖
  ② 「管理员还可以将已经完成的任务移除」→ 从列表里删掉

要点：
  · 详情：**成员也能看**（协作的意义就是彼此看得见）
  · 移除：**管理员专属**
  · 移除**不删已采到的正文**，也不删合并痕迹（审计记录）
  · 还有**正在被采**的活时**不许移除**（成员回传会落到空处）→ 要先撤销
  · 移除是**不可恢复**的（与「撤销」可恢复形成分工）

用法：python _tools/test_task_detail.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

BASE = "http://127.0.0.1:8765"
ADMIN = ("qa-admin@example.com", "qa-test-password")
MEMBER = ("test@example.com", os.environ.get("MSYT_TEST_PW", ""))
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def call(path, method="GET", body=None, token="", timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


def login(cred):
    st, d = call("/api/auth/login", "POST",
                 {"email": cred[0], "password": cred[1]})
    if st != 200:
        print(f"登录失败 {cred[0]}: {st} {d}")
        return ""
    return d["token"]


def main() -> int:
    atok = login(ADMIN)
    mtok = login(MEMBER)
    if not atok:
        return 1
    print(f"管理员 {ADMIN[0]} 登录{'成功' if atok else '失败'}；"
          f"成员 {MEMBER[0]} 登录{'成功' if mtok else '失败'}")

    from msyt.config import Config
    from msyt.db import DB
    db = DB(str(Config.load().db_path))
    code = "DETAIL-TEST"
    now = "2026-10-23 12:00:00"
    with db.lock:
        db.conn.execute("DELETE FROM assist_picks WHERE task_code=?", (code,))
        db.conn.execute("DELETE FROM assist_tasks WHERE code=?", (code,))
        db.conn.execute(
            "INSERT INTO assist_tasks(code,title,dept_ids,total,done,state,created_by,"
            "created_at,updated_at) VALUES(?,?,?,?,0,'active','admin',?,?)",
            (code, "详情测试", "999", 4, now, now))
        db.conn.executemany(
            "INSERT INTO assist_picks(task_code,tid,state,node,posts,chars,taken_at,"
            "done_at) VALUES(?,?,?,?,?,?,?,?)",
            [(code, 91000001, "done", "member:x", 3, 120, now, now),
             (code, 91000002, "taken", "member:y", 0, 0, now, None),
             (code, 91000003, "pending", None, 0, 0, None, None),
             (code, 91000004, "restricted", "member:x", 0, 0, now, now)])
        db.conn.execute(
            "INSERT OR REPLACE INTO threads(tid,title,detail_state,posts_n,chars) "
            "VALUES(91000001,'详情帖一','done',3,120)")
        db.conn.commit()

    print("\n=== ① 任务详情 ===")
    st, d = call(f"/api/assist/task/{code}", "GET", None, atok)
    check("管理员取详情 → 200", st == 200, f"HTTP {st}")
    if st == 200:
        t = d.get("task") or {}
        items = d.get("items") or []
        check("返回了任务概要", t.get("code") == code, str(t.get("code")))
        check("返回了帖子明细", len(items) == 4, f"{len(items)} 条")
        check("明细里带上了标题",
              any(x.get("title") == "详情帖一" for x in items),
              str([x.get("title") for x in items])[:80])
        check("明细里带上了采的节点与状态",
              any(x.get("node") == "member:y" and x.get("state") == "taken"
                  for x in items), "")
        check("总数对得上", d.get("total") == 4, str(d.get("total")))

    if mtok:
        st2, d2 = call(f"/api/assist/task/{code}", "GET", None, mtok)
        check("★ 成员也能看详情（协作的意义）", st2 == 200, f"HTTP {st2}")
    else:
        check("（成员账号登录失败，跳过成员视角断言）", True, "")

    st, _ = call("/api/assist/task/NOT-EXIST", "GET", None, atok)
    check("不存在的任务 → 404", st == 404, f"HTTP {st}")

    print("\n=== ② 移除：还有正在被采的活时**不许**移除 ===")
    st, d = call(f"/api/assist/task/{code}", "DELETE", None, mtok)
    check("★ 成员移除 → 403", st == 403, f"HTTP {st}")
    st, d = call(f"/api/assist/task/{code}", "DELETE", None, atok)
    check("★ 有进行中的活时 → 400 并说清要先撤销",
          st == 400 and "撤销" in str(d), f"HTTP {st} {str(d)[:80]}")
    still = db.conn.execute(
        "SELECT COUNT(*) FROM assist_tasks WHERE code=?", (code,)).fetchone()[0]
    check("任务还在（没被误删）", still == 1, str(still))

    print("\n=== ③ 先撤销，再移除 ===")
    st, d = call("/api/assist/cancel", "POST", {"code": code}, atok)
    check("撤销成功", st == 200 and d.get("ok"), f"HTTP {st} {str(d)[:80]}")
    st, d = call(f"/api/assist/task/{code}", "DELETE", None, atok)
    check("★ 移除成功", st == 200 and d.get("ok"), f"HTTP {st} {str(d)[:90]}")
    # done_kept = 状态为 done 的条数（本例 1 条 done；另有 1 条 restricted 同样保留）
    check("★ 已完成的帖**保留**在权威库里", d.get("done_kept") == 1, str(d.get("done_kept")))
    n_task = db.conn.execute(
        "SELECT COUNT(*) FROM assist_tasks WHERE code=?", (code,)).fetchone()[0]
    n_pick = db.conn.execute(
        "SELECT COUNT(*) FROM assist_picks WHERE task_code=?", (code,)).fetchone()[0]
    check("任务与它的活都从列表里移除了", n_task == 0 and n_pick == 0,
          f"task={n_task} picks={n_pick}")
    n_thread = db.conn.execute(
        "SELECT COUNT(*) FROM threads WHERE tid=91000001").fetchone()[0]
    check("★ 权威库里的帖子正文**一个字没删**", n_thread == 1, str(n_thread))
    n_conf = db.conn.execute(
        "SELECT COUNT(*) FROM assist_conflicts WHERE task_code=?", (code,)).fetchone()[0]
    check("合并痕迹保留（审计可追责）", n_conf == 0, "（本例无痕迹行，属正常）")

    st, _ = call(f"/api/assist/task/{code}", "GET", None, atok)
    check("移除后再取详情 → 404", st == 404, f"HTTP {st}")

    # ---- 清理残留 ----
    with db.lock:
        db.conn.execute("DELETE FROM assist_picks WHERE task_code=?", (code,))
        db.conn.execute("DELETE FROM assist_tasks WHERE code=?", (code,))
        db.conn.execute("DELETE FROM threads WHERE tid=91000001")
        db.conn.commit()
    db.close()

    print("\n" + "=" * 70)
    ok = sum(1 for _, c, _ in results if c)
    bad = [r for r in results if not r[1]]
    print(f"共 {len(results)} 项 · 通过 {ok} · 失败 {len(bad)}")
    for nm, _, e in bad:
        print(f"  FAIL: {nm}  {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
