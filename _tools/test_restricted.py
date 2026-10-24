# -*- coding: utf-8 -*-
"""
test_restricted.py —— 「站方限制」状态的验收
================================================================================
背景（2026-10-23 实测发现）
---------------------------
站方对**历史帖子**只给「提示信息」页，页面里明写
「历史帖子仅限作者本人查看」—— 详情页正文对非作者不公开。
实测可见边界在 tid ≈ 8074300（8074362 可见 / 8074198 不可见）。

这个事实把协作 Beta 打懵了：
  · 建池按「先旧后新」选帖 → 池子里全是这种采不到正文的帖
  · 成员领到活 → 采回来是空 → 服务端记「失败」
  · 于是「完成 0 / 失败 500」，界面看着像程序坏了

用户 2026-10-23 拍板：**标记、写入正文，但不计入「已采详情帖」**。

本测试验的就是这条口径，以及它的每一处落地：
  ① 提示页能被认出来，且**不误判**正常帖
  ② 标记后：detail_state='restricted'、正文里写进标记行、posts_n=0
  ③ 「已采详情帖」计数**不含**受限帖（这是用户的原话）
  ④ 协作回传为空但带 restricted 标记 → 活记 'restricted'，**不是** 'failed'
  ⑤ 协作任务进度里 restricted 是**独立一栏**
  ⑥ 建池**先新后旧**（池子里的帖得是真能采的）
  ⑦ 体检页里受限帖**单独一栏**，不进硬问题、不进软警告

用法：python _tools/test_restricted.py [--base http://127.0.0.1:8765]
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

ADMIN = ("qa-admin@example.com", "qa-test-password")
MEMBER = ("qa-member@example.com", "qa-test-password")
results = []

# 站方提示页的**真实片段**（从实测页面里取的一小段，够触发识别即可）
NOTICE_HTML = (
    '<html><head><title>提示信息 - 民生e点通-天一论坛 - Powered by Discuz!</title></head>'
    '<body><div class="alert_error"><div class="alert_info">'
    '<h2>历史帖子仅限作者本人查看</h2>'
    '<a href="index.php">返回首页</a></div></div></body></html>'
)
# 一个**正常帖**的片段：里面有楼层锚点 —— 不能被误判成提示页
NORMAL_HTML = (
    '<html><head><title>正常帖 - 民生e点通</title></head><body>'
    '<div id="post_123456"><div class="pi">作者</div></div>'
    "<script>token:'abcdef0123456789abcdef'</script></body></html>"
)


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def call(base, path, method="GET", body=None, token=""):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
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


def login(base, cred):
    st, d = call(base, "/api/auth/login", "POST",
                 {"email": cred[0], "password": cred[1]})
    if st != 200:
        print(f"  ! 登录失败 {cred[0]}: {d}")
    return d.get("token") if st == 200 else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    args = ap.parse_args()
    base = args.base

    from msyt.assist import AssistService
    from msyt.db import DB
    from msyt.hub import Hub
    from msyt.parsers import parse_site_notice

    print("=== ① 提示页识别 ===")
    notice = parse_site_notice(NOTICE_HTML)
    check("站方提示页能被认出来", bool(notice), notice[:60])
    check("★ 正常帖**不会**被误判", parse_site_notice(NORMAL_HTML) == "")
    check("普通页面返回空串", parse_site_notice("<html><body>普通页</body></html>") == "")
    check("★ 正文里提到那句话、但不是提示页 → 不误判",
          parse_site_notice('<html><title>正常帖</title><body>'
                            '历史帖子仅限作者本人查看'
                            '<div id="post_1">x</div></body></html>') == "")

    print("\n=== ② 标记落库（跑在库副本上） ===")
    tmp = tempfile.mkdtemp(prefix="msyt_restricted_")
    db_path = os.path.join(tmp, "t.db")
    try:
        db = DB(db_path)
        with db.lock:
            db.conn.execute(
                "INSERT INTO threads(tid,title,detail_state,posts_n,chars) "
                "VALUES(?,?,?,?,?)", (90000001, "测试受限帖", "pending", 0, 0))
            db.conn.commit()
        db.mark_restricted(90000001, "站方限制：历史帖子仅限作者本人查看")
        row = db.conn.execute(
            "SELECT detail_state,posts_n,chars,detail_warn FROM threads WHERE tid=?",
            (90000001,)).fetchone()
        check("detail_state 变成 restricted", row["detail_state"] == "restricted",
              row["detail_state"])
        check("★ posts_n 记 0（不是一层楼）", row["posts_n"] == 0, str(row["posts_n"]))
        check("detail_warn 记下站方提示", "历史帖子" in (row["detail_warn"] or ""))
        posts = db.conn.execute(
            "SELECT pid,floor,author,content FROM posts WHERE tid=?",
            (90000001,)).fetchall()
        check("★ 正文里写进了标记行", len(posts) == 1, f"{len(posts)} 行")
        if posts:
            check("标记行 pid 为负（不会撞真实楼层）", posts[0]["pid"] < 0,
                  str(posts[0]["pid"]))
            check("标记行 floor=0", posts[0]["floor"] == 0, str(posts[0]["floor"]))
            check("标记行正文写明站方限制",
                  "站方限制" in (posts[0]["content"] or ""),
                  (posts[0]["content"] or "")[:50])
        st = db.stats()
        check("★ 「已采详情帖」不含受限帖",
              st["detail_done"] == 0 and st["detail_restricted"] == 1,
              f"done={st['detail_done']} restricted={st['detail_restricted']}")
        check("★ 「楼层总数」只算真实楼层（标记行不算）",
              st["posts"] == 0 and st["posts_note"] == 1,
              f"posts={st['posts']} note={st['posts_note']}")

        print("\n=== ④⑥ 协作侧：回传受限于「restricted」而非 failed ===")
        hub = Hub()
        svc = AssistService(db, hub)
        with db.lock:
            db.conn.execute(
                "INSERT INTO members(id,email,pw_hash,role,status,node_id,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (99, "qa-restricted@example.com", "x", "member", "active",
                 "member:qa-restricted@example.com", "2026-10-23 00:00:00"))
            db.conn.execute(
                "INSERT INTO threads(tid,title,detail_state,pub_ts,dept_id) "
                "VALUES(?,?,?,?,?)",
                (90000002, "测试受限帖2", "pending", 2000000000, "999"))
            db.conn.execute(
                "INSERT INTO threads(tid,title,detail_state,pub_ts,dept_id) "
                "VALUES(?,?,?,?,?)",
                (90000003, "测试新帖", "pending", 2000000001, "999"))
            # 建池顺序检验用的三条：pub_ts 递增
            for i, ts in enumerate((100, 200, 300), start=90000010):
                db.conn.execute(
                    "INSERT INTO threads(tid,title,detail_state,pub_ts,dept_id) "
                    "VALUES(?,?,?,?,?)", (i, f"序{i}", "pending", ts, "999"))
            db.conn.execute(
                "INSERT INTO assist_tasks(code,title,dept_ids,total,done,state,"
                "created_by,created_at,updated_at) VALUES(?,?,?,?,0,'open','admin',?,?)",
                ("R1", "受限测试", "999", 2, "2026-10-23 00:00:00",
                 "2026-10-23 00:00:00"))
            db.conn.execute(
                "INSERT INTO assist_picks(task_code,tid,state) VALUES(?,?,'pending')",
                ("R1", 90000002))
            db.conn.commit()
        r = svc.submit("member:qa-restricted@example.com", "R1", 90000002,
                       [], [], {"ok": False, "restricted": True,
                               "reason": "历史帖子仅限作者本人查看"})
        check("回传返回 restricted 标记", r.get("restricted") is True, str(r)[:80])
        pr = db.conn.execute(
            "SELECT state FROM assist_picks WHERE task_code='R1' AND tid=90000002"
        ).fetchone()
        check("★ 活的状态是 restricted（**不是** failed）", pr["state"] == "restricted",
              pr["state"])
        th = db.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=90000002").fetchone()
        check("★ 该帖被标记为 restricted", th["detail_state"] == "restricted",
              th["detail_state"])
        t = svc.get("R1")
        check("★ 进度里 restricted 是独立一栏", t.get("restricted") == 1,
              str(t.get("restricted")))
        check("★ 受限不计入完成", t["done"] == 0, str(t["done"]))
        check("★ 受限不计入失败", t["failed"] == 0, str(t["failed"]))
        nd = (t.get("by_node_detail") or {}).get("member:qa-restricted@example.com") or {}
        check("各节点明细里也有 restricted 槽",
              nd.get("restricted") == 1 and nd.get("failed") == 0, json.dumps(nd))

        print("\n=== ⑥ 建池先新后旧 ===")
        t2 = svc.create("新池", [999], total=10, created_by="admin")
        rows = db.conn.execute(
            "SELECT tid FROM assist_picks WHERE task_code=? ORDER BY id", (t2["code"],)
        ).fetchall()
        check("新池至少选到了一条", len(rows) > 0, f"{len(rows)} 条")
        seq = [r["tid"] for r in rows if 90000010 <= r["tid"] <= 90000012]
        check("★ 建池是**先新后旧**（pub_ts 大的排前面）",
              seq == [90000012, 90000011, 90000010], str(seq))
        check("★ 已判受限的帖不再进新池",
              90000002 not in [r["tid"] for r in rows])
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== ③⑤⑦ 线上接口（真实服务） ===")
    atok = login(base, ADMIN)
    if not atok:
        check("管理员登录", False, "拿不到 token")
    else:
        st, d = call(base, "/api/stats", "GET", None, atok)
        check("stats 里有 detail_restricted 字段", st == 200 and
              "detail_restricted" in d, str(st))
        st, d = call(base, "/api/integrity", "GET", None, atok)
        check("integrity 里有 restricted 栏", st == 200 and "restricted" in d, str(st))
        if st == 200:
            rid = [x["tid"] for x in d.get("restricted") or []]
            pd = [x["tid"] for x in d.get("problems") or []]
            check("★ 受限帖不进硬问题", not (set(rid) & set(pd)),
                  f"受限 {len(rid)} 条，硬问题交集 {len(set(rid) & set(pd))}")
        st, d = call(base, "/api/assist/tasks", "GET", None, atok)
        check("协作任务进度带 restricted 字段",
              st == 200 and all("restricted" in t for t in (d.get("items") or [])),
              str(st))
        st, d = call(base, "/api/assist/online", "GET", None, atok)
        check("在线节点带 restricted 字段",
              st == 200 and all("restricted" in x for x in (d.get("items") or [])),
              str(st))
        mtok = login(base, MEMBER)
        if mtok:
            st, _ = call(base, "/api/integrity", "GET", None, mtok)
            check("★ 成员仍看不到体检页（接口层 403）", st == 403, str(st))

    ok = sum(1 for _, c, _ in results if c)
    bad = [r for r in results if not r[1]]
    print(f"\n共 {len(results)} 项 · 通过 {ok} · 失败 {len(bad)}")
    for n, _, e in bad:
        print(f"  FAIL: {n}  {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
