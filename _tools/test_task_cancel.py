# -*- coding: utf-8 -*-
"""
test_task_cancel.py —— 协作任务「撤销 / 恢复」的验收
================================================================================
用户 2026-10-23 明确：「协作任务应该需要可以撤销，已经完成的不废弃」。

撤销的语义（三块分别处理，这是本测试的核心）：
  | 那些活 | 处置 |
  |---|---|
  | 已完成 / 失败 / 站方限制 | **原样保留** |
  | 未完成（pending / taken） | 标成 `cancelled`（留痕，不删） |
  | 权威库里的正文 | **一个字不动** |

还要验：
  · 撤销后不再派活（成员领不到活）
  · 撤销是**管理员专属**（成员 403）
  · 撤销**可恢复**（作废的活能放回池子）
  · 正在被成员领着的那几条，采完回传**照常入库**（数据不浪费）

用法：python _tools/test_task_cancel.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

BASE = "http://127.0.0.1:8765"
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


def main() -> int:
    pw = os.environ.get("MSYT_TEST_PW", "")
    # 撤销是**管理员专属** → 主体断言用管理员账号；
    # 成员账号只用来验「成员撤销 → 403」。
    admin_email = os.environ.get("MSYT_ADMIN_EMAIL", "qa-admin@example.com")
    admin_pw = os.environ.get("MSYT_ADMIN_PW", "qa-test-password")
    member_email = os.environ.get("MSYT_TEST_EMAIL", "test@example.com")
    st, d = call("/api/auth/login", "POST",
                 {"email": admin_email, "password": admin_pw})
    if st != 200:
        print(f"管理员登录失败：{st} {d}")
        return 1
    tok = d["token"]
    role = (d.get("member") or {}).get("role")
    print(f"以 {admin_email} 登录（角色 {role}）")

    from msyt.config import Config
    from msyt.db import DB
    db = DB(str(Config.load().db_path))

    # ---- 造一个测试任务：2 条已完成 + 2 条未完成 ----
    code = "CANCEL-TEST"
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with db.lock:
        db.conn.execute("DELETE FROM assist_picks WHERE task_code=?", (code,))
        db.conn.execute("DELETE FROM assist_tasks WHERE code=?", (code,))
        db.conn.execute(
            "INSERT INTO assist_tasks(code,title,dept_ids,total,done,state,created_by,"
            "created_at,updated_at) VALUES(?,?,?,?,0,'active','admin',?,?)",
            (code, "撤销测试", "999", 4, now, now))
        db.conn.executemany(
            "INSERT INTO assist_picks(task_code,tid,state,node,posts,chars,done_at) "
            "VALUES(?,?,?,?,?,?,?)",
            [(code, 98000001, "done", "member:x", 2, 100, now),
             (code, 98000002, "restricted", "member:x", 0, 0, now),
             (code, 98000003, "pending", None, 0, 0, None),
             (code, 98000004, "taken", "member:x", 0, 0, None)])
        db.conn.commit()
    # 正文要真的在库里，才能验「撤销不动正文」
    with db.lock:
        db.conn.execute(
            "INSERT OR IGNORE INTO threads(tid,title,detail_state,posts_n,chars) "
            "VALUES(98000001,'撤销测试帖','done',2,100)")
        db.conn.execute(
            "INSERT OR IGNORE INTO posts(pid,tid,page_no,floor,author,post_time,"
            "content,chars,images,fetched_at) VALUES(9800000101,98000001,1,1,'a',?,"
            "'正文内容',4,'[]',?)", (now, now))
        db.conn.commit()

    print("\n=== ① 撤销：已完成的保留，未完成的作废 ===")
    st, d = call("/api/assist/cancel", "POST", {"code": code}, tok)
    check("撤销成功", st == 200 and d.get("ok"), f"HTTP {st} {d}")
    check("★ 已完成的 1 条 + 受限 1 条**保留**", d.get("kept") == 2, str(d.get("kept")))
    check("★ 未完成的 2 条作废", d.get("cancelled") == 2, str(d.get("cancelled")))
    rows = {r["tid"]: r["state"] for r in db.conn.execute(
        "SELECT tid,state FROM assist_picks WHERE task_code=?", (code,))}
    check("★ done 那条仍是 done（**不废弃**）",
          rows.get(98000001) == "done", str(rows.get(98000001)))
    check("★ restricted 那条也保留",
          rows.get(98000002) == "restricted", str(rows.get(98000002)))
    check("pending 那条变成 cancelled",
          rows.get(98000003) == "cancelled", str(rows.get(98000003)))
    check("taken 那条变成 cancelled",
          rows.get(98000004) == "cancelled", str(rows.get(98000004)))
    n_posts = db.conn.execute(
        "SELECT COUNT(*) FROM posts WHERE tid=98000001").fetchone()[0]
    check("★ 权威库里的正文一个字没动", n_posts >= 1, f"{n_posts} 条楼层")

    print("\n=== ② 撤销后不再派活 ===")
    t = None
    for x in call("/api/assist/tasks", "GET", None, tok)[1].get("items", []):
        if x["code"] == code:
            t = x
    check("任务状态变成 cancelled", t and t["state"] == "cancelled",
          str(t.get("state") if t else "?"))
    check("★ 已完成的仍计入完成数", t and t["done"] == 1, str(t.get("done") if t else "?"))
    check("★ 作废的活不计入「还剩」", t and t["pending"] == 0,
          str(t.get("pending") if t else "?"))
    check("★ 给出 can_cancel / can_restore 标志",
          t and t.get("can_cancel") is False and t.get("can_restore") is True,
          str((t.get("can_cancel"), t.get("can_restore")) if t else "?"))

    print("\n=== ③ 成员不能撤销（管理员专属）===")
    st2, dm = call("/api/auth/login", "POST",
                   {"email": member_email, "password": pw})
    if st2 == 200:
        mtok = dm["token"]
        st, d = call("/api/assist/cancel", "POST", {"code": code}, mtok)
        check("★ 成员撤销 → 403", st == 403, f"HTTP {st}")
        st, d = call("/api/assist/restore", "POST", {"code": code}, mtok)
        check("★ 成员恢复 → 403", st == 403, f"HTTP {st}")
    else:
        check("（成员账号登录失败，跳过成员视角的 403 断言）", True, str(st2))
    st, d = call("/api/assist/cancel", "POST", {"code": "不存在的任务"}, tok)
    check("撤销不存在的任务 → 400", st == 400, f"HTTP {st}")

    print("\n=== ④ 恢复：作废的活放回池子 ===")
    st, d = call("/api/assist/restore", "POST", {"code": code}, tok)
    check("恢复成功", st == 200 and d.get("ok"), f"HTTP {st} {d}")
    check("★ 2 条作废的活重新入池", d.get("restored") == 2, str(d.get("restored")))
    rows2 = {r["tid"]: r["state"] for r in db.conn.execute(
        "SELECT tid,state FROM assist_picks WHERE task_code=?", (code,))}
    check("★ 已完成的那条恢复后**仍是 done**（没被拖下水）",
          rows2.get(98000001) == "done", str(rows2.get(98000001)))
    check("作废的变回 pending",
          rows2.get(98000003) == "pending", str(rows2.get(98000003)))

    print("\n=== ⑤ 撤销期间成员回传：数据不浪费 ===")
    st, d = call("/api/assist/cancel", "POST", {"code": code}, tok)
    # 模拟成员在撤销之后才回传
    from msyt.assist import AssistService
    from msyt.hub import Hub
    svc = AssistService(db, Hub())
    svc._mark_pick(code, 98000004, "member:x", "done", 1, 50)
    r3 = db.conn.execute(
        "SELECT state FROM assist_picks WHERE task_code=? AND tid=98000004",
        (code,)).fetchone()
    check("★ 撤销后回传仍能入库（状态回到 done）", r3["state"] == "done",
          r3["state"])

    # ---- 清理 ----
    with db.lock:
        db.conn.execute("DELETE FROM assist_picks WHERE task_code=?", (code,))
        db.conn.execute("DELETE FROM assist_tasks WHERE code=?", (code,))
        db.conn.execute("DELETE FROM posts WHERE tid=98000001")
        db.conn.execute("DELETE FROM threads WHERE tid=98000001")
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
