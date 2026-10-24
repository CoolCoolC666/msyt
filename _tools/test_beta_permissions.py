# -*- coding: utf-8 -*-
"""
test_beta_permissions.py —— 协作 Beta 的验收脚本（权限 / 改密码 / 合并 / 无限额）
================================================================================
测的是**跑着的服务**，不是单元测试：

  ① 权限矩阵：成员能用什么、不能用什么（界面层 + 接口层两道）
  ② 改密码：本人可改；管理员重置后本人能自行改回来
  ③ 无限额：成员领活不受配额限制
  ④ 合并规则：**重复的以管理员本地为准**，且留冲突记录
  ⑤ 心跳：成员在线状态能被记录

不碰站方，也不往权威库里写测试数据（④的"写入"用例跑在**库副本**上）。

用法：
    python _tools/test_beta_permissions.py [--base http://127.0.0.1:8765]
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "程序"))

ADMIN = ("qa-admin@example.com", "qa-test-password")
MEMBER = ("qa-member@example.com", "qa-test-password")

OK = "PASS"
BAD = "FAIL"
results = []


def check(name: str, cond: bool, extra: str = "") -> None:
    results.append((name, bool(cond), extra))
    print(f"  [{OK if cond else BAD}] {name}" + (f"  —— {extra}" if extra else ""))


# ---------------------------------------------------------------- HTTP 小工具
def call(base: str, path: str, method: str = "GET", body=None, token: str = ""):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
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
        raise SystemExit(f"登录失败 {cred[0]}: {st} {d}")
    return d["token"], d["member"]


def try_login(base, cred):
    """不抛异常的登录：返回 (token, member)，失败时 token 为空串。"""
    st, d = call(base, "/api/auth/login", "POST",
                 {"email": cred[0], "password": cred[1]})
    if st != 200:
        return "", {"error": d}
    return d["token"], d["member"]


def return_picks(base, node, token):
    """把该节点领走但还没回传的活**退回池子**。

    ★ 为什么测试脚本必须退活（2026-10-23 修）
      这个脚本用 `limit=999` 验证「协作不限配额」，
      那一领就是**整个池子**。测完不退回 →
      活一直卡在 taken，**真实成员无活可领**，
      而界面上显示的是「各节点贡献 400」，看着像在采，其实池子空了。
      （这个坑实测踩过两次：一次是测试脚本，一次是另一个 agent 崩了。）
    """
    st, d = call(base, "/api/assist/return", "POST", {"node": node}, token=token)
    if st != 200:
        return -1
    return int(d.get("returned") or 0)


def normalize_member_pw(email: str, pw: str) -> None:
    """把测试成员的密码**归位**。

    为什么需要：这个脚本会改成员的密码（测「自行改密码」）。
    上一次跑失败中途退出时，密码就停在中间态 —— 再跑一次就登不进去。
    所以每次开跑前先**直接写库归位**，保证脚本可反复执行。
    """
    from msyt.auth import hash_pw       # noqa: E402
    from msyt.config import Config      # noqa: E402
    cfg = Config.load()
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE members SET pw_hash=? WHERE email=?", (hash_pw(pw), email))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 78)
    print("协作 Beta 验收 —— 权限 / 改密码 / 无限额 / 合并规则")
    print("=" * 78)

    normalize_member_pw(MEMBER[0], MEMBER[1])
    atok, amem = login(base, ADMIN)
    mtok, mmem = login(base, MEMBER)
    print(f"管理员 {amem['email']} 节点 {amem['node_id']}")
    print(f"成员   {mmem['email']} 节点 {mmem['node_id']}\n")

    # ---------------- ① 权限矩阵 ----------------
    print("① 权限矩阵（成员视角）")
    st, d = call(base, "/api/auth/me", token=mtok)
    check("成员可读自己的身份", st == 200 and d.get("role") == "member", f"{st}")
    check("成员知道自己是「无限配额」", d.get("quota_unlimited") is True, str(d.get("quota")))

    st, _ = call(base, "/api/stats", token=mtok)
    check("成员可看总览统计（界面层可见 = 接口层放行）", st == 200, f"HTTP {st}")
    st, _ = call(base, "/api/threads?limit=1", token=mtok)
    check("成员可检索帖子", st == 200, f"HTTP {st}")
    st, _ = call(base, "/api/fetchlog?limit=1", token=mtok)
    check("成员可看采集日志", st == 200, f"HTTP {st}")
    st, _ = call(base, "/api/pending?dept_ids=", token=mtok)
    check("成员可看待采统计（采集作业界面要用）", st == 200, f"HTTP {st}")

    st, _ = call(base, "/api/integrity", token=mtok)
    check("★ 成员调不通完整性体检（管理员专属）", st == 403, f"HTTP {st}")
    st, _ = call(base, "/api/members", token=mtok)
    check("★ 成员调不通成员名单（管理员专属）", st == 403, f"HTTP {st}")
    st, _ = call(base, "/api/members", "POST", {"email": "x@example.com"}, token=mtok)
    check("★ 成员建不了成员账号", st == 403, f"HTTP {st}")
    st, _ = call(base, "/api/assist/task", "POST",
                 {"title": "x", "dept_ids": [495], "total": 1}, token=mtok)
    check("★ 成员发不了协助请求（管理员派单）", st == 403, f"HTTP {st}")

    st, d = call(base, "/api/jobs", token=mtok)
    nodes = {j.get("node") for j in (d.get("items") or [])}
    check("★ 成员只看得到自己节点的任务", st == 200 and nodes <= {mmem["node_id"]},
          f"nodes={nodes}")
    st, d = call(base, "/api/jobs", token=atok)
    allnodes = sorted({j.get("node") for j in (d.get("items") or [])})
    check("管理员看得到全部节点的任务", st == 200, f"nodes={allnodes}")

    st, d = call(base, "/api/jobs/list", "POST",
                 {"dept_ids": [], "sources": "", "node": "admin"}, token=mtok)
    mine = (d.get("job") or {}).get("node")
    # ★ 2026-10-23：服务器端采集端点已收成**管理员专属**（成员 403）。
    #   原来的断言是「成员自报 node 会被强制改成自己的节点」——
    #   那是"防冒充 admin 节点"的旧防线。现在防得更彻底：
    #   成员**根本不能**在服务器上启动采集（那会��管理员的机器/IP 去刷站方，
    #   把「站方看到多个分散的普通用户」这个 Beta 核心前提架空）。
    #   成员的采集走**本机 agent**（见 test_local_collect.py）。
    check("★ 成员不能在服务器上启动采集（更严：连自报 node 都不给机会）",
          st == 403, f"HTTP {st}")
    check("★ 403 的提示里说清了「要本机 agent」",
          isinstance(d, dict) and ("agent" in str(d.get("detail") or "")
                                   or "本机" in str(d.get("detail") or "")),
          str(d)[:90] if isinstance(d, dict) else str(d)[:90])
    if st == 200:      # 万一真起来了，停掉
        call(base, f"/api/jobs/{d['job']['id']}/stop", "POST", token=mtok)

    st, _ = call(base, "/api/stats")
    check("未登录读不到数据（数据接口要登录）", st == 401, f"HTTP {st}")

    # ---------------- ③ 无限额 ----------------
    print("\n③ 协作配额：不限")
    st, d = call(base, "/api/assist/pick", "POST",
                 {"node": mmem["node_id"], "limit": 999}, token=mtok)
    check("成员领活返回 200（不限配额，limit 只是批量大小）", st == 200, f"HTTP {st}")
    check("领活接口不按 quota 截断", st == 200 and "picks" in d, json.dumps(d)[:80])
    # ★★★ 领完**必须立刻退活**（2026-10-23 修）
    #   这里用 limit=999 是为了验证「不限配额」，但它会把整个池子领空。
    #   以前测完就走 —— 活一直卡在 taken 状态，
    #   **真实成员从此无活可领**，而界面显示的是「各节点贡献 400」，
    #   看着像在采，其实池子已经空了（实测踩过两次）。
    #   所以：领完立刻还回去，别把池子留在「被占死」的状态。
    _returned = return_picks(base, mmem["node_id"], mtok)
    check("★ 测完把领走的活退回了（不占死池子）", _returned >= 0,
          f"退回 {_returned} 条")

    # ---------------- ⑤ 心跳 ----------------
    print("\n⑤ 成员在线状态（心跳）")
    st, d = call(base, "/api/agent/heartbeat", "POST",
                 {"node": mmem["node_id"]}, token=mtok)
    check("心跳接口可用", st == 200, f"HTTP {st}")
    st, d = call(base, "/api/members", token=atok)
    me = [m for m in d.get("items", []) if m["email"] == MEMBER[0]]
    check("管理员在成员名单上看到「在线」",
          bool(me) and me[0].get("online") is True,
          f"online={me[0].get('online') if me else '?'}")

    # ---------------- ② 改密码 ----------------
    print("\n② 改密码（本人可改；重置后能自行改回来）")
    newpw = "Qa-NewPass-2026"
    st, d = call(base, "/api/auth/password", "POST",
                 {"old_password": MEMBER[1], "new_password": newpw,
                  "confirm_password": newpw}, token=mtok)
    check("成员能改自己的密码", st == 200, f"HTTP {st} {d}")
    st, d = call(base, "/api/auth/password", "POST",
                 {"old_password": MEMBER[1], "new_password": "Another-2026",
                  "confirm_password": "Another-2026"}, token=mtok)
    check("用旧密码改不动（必须验旧密码）", st == 400, f"HTTP {st}")
    st, d = call(base, "/api/auth/password", "POST",
                 {"old_password": newpw, "new_password": "Another-2026",
                  "confirm_password": "Mismatch-2026"}, token=mtok)
    check("两次输入不一致会被拒", st == 400, f"HTTP {st}")
    # ⚠ login() 返回的是 (token, member)，不是 (status, body) —— 别写错解包
    tok2, mem2 = try_login(base, (MEMBER[0], newpw))
    check("★ 改完能用新密码登录", bool(tok2), str(mem2.get("error", ""))[:80])
    mtok = tok2 or mtok
    # 管理员重置 → 成员用临时密码登进去 → 自己再改
    # ⚠ **必须按邮箱查 id**：写死 id=2 会打到别人的账号上
    #   （这个坑真踩过：把用户自己的成员账号密码误重置了）
    st, d = call(base, "/api/members", token=atok)
    mid = None
    for m in d.get("items", []):
        if m["email"] == MEMBER[0]:
            mid = m["id"]
            break
    check("能在成员名单里按邮箱查到成员 id", mid is not None, str(mid))
    st, d = call(base, f"/api/members/{mid}/reset-password", "POST", token=atok)
    check("管理员重置成员密码", st == 200 and d.get("password"), f"HTTP {st}")
    tmp = d.get("password")
    check("重置后提示「让对方自行改密码」", "改" in (d.get("message") or ""),
          (d.get("message") or "")[:60])
    tok_tmp, mem_tmp = try_login(base, (MEMBER[0], tmp))
    check("★ 成员能用临时密码登录（不会登不进去）", bool(tok_tmp),
          str(mem_tmp.get("error", ""))[:80])
    if tok_tmp:
        st, d3 = call(base, "/api/auth/password", "POST",
                      {"old_password": tmp, "new_password": MEMBER[1],
                       "confirm_password": MEMBER[1]}, token=tok_tmp)
        check("★ 成员登录后能自行把临时密码改掉", st == 200, f"HTTP {st}")
        tok3, _ = try_login(base, (MEMBER[0], MEMBER[1]))
        check("改回后原密码可用", bool(tok3), "")

    # ---------------- ④ 合并规则（HTTP 层：真实回传 → 合并覆盖） ----------
    print("\n④ 合并规则：成员回传不跳过，采取并覆盖")
    from msyt.config import Config            # noqa: E402
    cfg = Config.load()
    # ★ 用**专用测试帖**（tid=999999001），不碰真实帖子：
    #   覆盖规则会真的改数据，所以不能拿库里的真帖来测。
    #   测完把这帖的所有行删掉。
    TID = 999999001
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    for sql in ("DELETE FROM posts WHERE tid=?", "DELETE FROM thread_pages WHERE tid=?",
                "DELETE FROM flows WHERE tid=?", "DELETE FROM detail_queue WHERE tid=?",
                "DELETE FROM assist_conflicts WHERE tid=?"):
        conn.execute(sql, (TID,))
    conn.execute("DELETE FROM threads WHERE tid=?", (TID,))
    conn.execute(
        "INSERT INTO threads(tid,title,detail_state,posts_n,chars,pages_total) "
        "VALUES(?,'【验收用测试帖】','done',2,6,1)", (TID,))
    conn.commit()
    # 造两楼本地数据
    from msyt.db import DB                    # noqa: E402
    _db = DB(cfg.db_path)
    _db.save_posts(TID, 1, [
        {"pid": 999000001, "floor": 1, "author": "本地甲", "author_uid": "",
         "post_time": "2026-01-01", "content": "本地第一楼", "chars": 5,
         "images": []},
        {"pid": 999000002, "floor": 2, "author": "本地乙", "author_uid": "",
         "post_time": "2026-01-02", "content": "本地第二楼", "chars": 5,
         "images": []},
    ])
    _db.record_page(TID, 1, "999000001,999000002", "ok")
    _db.set_thread_detail(TID, detail_state="done", posts_n=2, chars=10,
                          pages_total=1, first_post_id=999000001,
                          last_post_id=999000002)
    _db.close()
    try:
        before_n = conn.execute("SELECT COUNT(*) FROM posts WHERE tid=?",
                                (TID,)).fetchone()[0]
        before_chars = conn.execute(
            "SELECT COALESCE(SUM(chars),0) FROM posts WHERE tid=?", (TID,)).fetchone()[0]
        # 成员回传：同 pid 但内容不同（覆盖）+ 一个本地没有的新 pid（增量）
        st, d = call(base, "/api/assist/submit", "POST", {
            "node": mmem["node_id"], "code": "A-QA", "tid": TID,
            "posts": [
                {"pid": 999000002, "page_no": 1, "floor": 2, "author": "成员乙",
                 "author_uid": "", "post_time": "2026-01-02",
                 "content": "成员版第二楼", "chars": 6, "images": []},
                {"pid": 999000003, "page_no": 1, "floor": 3, "author": "成员丙",
                 "author_uid": "", "post_time": "2026-01-03",
                 "content": "站方追加的新楼", "chars": 7, "images": []},
            ],
            "pages": [{"page_no": 1, "pids": [999000001, 999000002, 999000003],
                       "n": 3}],
            "summary": {"ok": True, "pages": 1, "posts": 3, "chars": 13,
                        "head_pid": 999000001, "tail_pid": 999000003,
                        "warnings": []},
        }, token=mtok)
        check("★ 不跳过（merged=true）",
              st == 200 and d.get("merged") is True, json.dumps(d, ensure_ascii=False))
        check("★ 新楼层被加进来（增量）", d.get("added") == 1,
              json.dumps(d, ensure_ascii=False))
        check("★ 同 pid 被覆盖（overwritten）", d.get("overwritten") == 1,
              json.dumps(d, ensure_ascii=False))
        after_n = conn.execute("SELECT COUNT(*) FROM posts WHERE tid=?",
                               (TID,)).fetchone()[0]
        after_chars = conn.execute(
            "SELECT COALESCE(SUM(chars),0) FROM posts WHERE tid=?", (TID,)).fetchone()[0]
        check("★ 楼层数 2 → 3", after_n == before_n + 1, f"{before_n} → {after_n}")
        c2 = conn.execute(
            "SELECT content FROM posts WHERE tid=? AND pid=999000002",
            (TID,)).fetchone()["content"]
        check("★ 同 pid 的正文被**覆盖**成成员版", c2 == "成员版第二楼", c2)
        c1 = conn.execute(
            "SELECT content FROM posts WHERE tid=? AND pid=999000001",
            (TID,)).fetchone()["content"]
        check("★ 本地独有、成员没采的楼层**保留**", c1 == "本地第一楼", c1)
        th = conn.execute(
            "SELECT posts_n, chars, detail_state FROM threads WHERE tid=?",
            (TID,)).fetchone()
        check("★ 计数现算（posts_n）", (th["posts_n"] or 0) == after_n,
              f"posts_n={th['posts_n']} 实际={after_n}")
        check("★ 计数现算（chars）", (th["chars"] or 0) == after_chars,
              f"chars={th['chars']} 实际={after_chars}")
        st, d = call(base, "/api/assist/conflicts?limit=10", token=atok)
        items = d.get("items") or []
        kinds = {c["kind"] for c in items if int(c.get("tid") or 0) == TID}
        check("★ 覆盖与新增都留痕",
              "overwritten" in kinds and "merged_new" in kinds, str(kinds))
    finally:
        # ★ 清理要用**写数据那个连接**（_db），并且先 commit 掉未决事务。
        #   原来这里用 conn 删，但 posts 是 _db 写的：
        #   conn 在 298 行的 SELECT 之后一直持着一个读事务（Python sqlite3 的
        #   deferred 事务），后面的 DELETE 属于「读事务升级为写」——
        #   期间服务端 HTTP 提交也写过库，于是删除**没生效**，
        #   测试数据留在了真实库里（实测：tid=999999001 的两楼残留）。
        try:
            _db2 = DB(cfg.db_path)
            for sql in ("DELETE FROM posts WHERE tid=?", "DELETE FROM thread_pages WHERE tid=?",
                        "DELETE FROM flows WHERE tid=?", "DELETE FROM detail_queue WHERE tid=?",
                        "DELETE FROM assist_conflicts WHERE tid=?"):
                _db2.conn.execute(sql, (TID,))
            _db2.conn.execute("DELETE FROM threads WHERE tid=?", (TID,))
            _db2.conn.commit()
            _db2.close()
        except Exception as e:                        # noqa: BLE001
            print(f"  [WARN] 清理测试帖失败：{e}")
        conn.commit()
        conn.close()
        print("  （验收用测试帖已清理干净）")

    # ---------------- ④ 合并规则（单元层：库副本上跑写入用例） ----------
    print("\n④ 合并规则（库副本）：不跳过、合并覆盖")
    from msyt.db import DB                       # noqa: E402
    from msyt.assist import AssistService        # noqa: E402
    from msyt.hub import Hub                      # noqa: E402
    tmpdir = tempfile.mkdtemp(prefix="msyt-merge-")
    copy = os.path.join(tmpdir, "merge-test.db")
    shutil.copy2(cfg.db_path, copy)
    try:
        db = DB(copy)
        svc = AssistService(db, Hub())
        # ★ 用**独立** tid（999999003），不要跟上面 HTTP 段的 999999001、
        #   也不要跟本段后面那个 999999002 撞车。
        #   撞车的后果（实测踩过）：HTTP 段在真实库上造数据、清理不彻底，
        #   残留会被这里的「拷贝真实库」带进来 → 楼层数翻倍，
        #   测试莫名失败，而真实库还多了一份测试数据。
        tid = 999999003
        with db.lock:
            # 副本里也先把这个 tid 清干净（防御：万一真实库有残留）
            for sql in ("DELETE FROM posts WHERE tid=?", "DELETE FROM thread_pages WHERE tid=?",
                        "DELETE FROM flows WHERE tid=?", "DELETE FROM detail_queue WHERE tid=?",
                        "DELETE FROM assist_conflicts WHERE tid=?"):
                db.conn.execute(sql, (tid,))
            db.conn.execute("DELETE FROM threads WHERE tid=?", (tid,))
            db.conn.execute(
                "INSERT OR IGNORE INTO threads(tid,title,detail_state) "
                "VALUES(?,'合并测试帖','pending')", (tid,))
            db.conn.commit()
        payload = [
            {"pid": 900000001, "page_no": 1, "floor": 1, "author": "甲",
             "author_uid": "", "post_time": "2026-01-01", "content": "第一楼",
             "chars": 3, "images": []},
            {"pid": 900000002, "page_no": 1, "floor": 2, "author": "乙",
             "author_uid": "", "post_time": "2026-01-02", "content": "第二楼",
             "chars": 3, "images": []},
        ]
        r = svc.submit("member:qa", "A-UNIT", tid, payload,
                       [{"page_no": 1, "pids": [900000001, 900000002], "n": 2}],
                       {"ok": True, "pages": 1, "posts": 2, "chars": 6,
                        "head_pid": 900000001, "tail_pid": 900000002})
        check("首次回传：全部写入", r.get("merged") is True and r.get("added") == 2,
              json.dumps(r, ensure_ascii=False))
        n1 = db.conn.execute("SELECT COUNT(*) FROM posts WHERE tid=?",
                             (tid,)).fetchone()[0]
        check("库里确实有 2 个楼层", n1 == 2, str(n1))

        # 再回传一次同一帖（本地已 done）→ ★ **不跳过**，照样合并并覆盖
        payload2_early = [
            {"pid": 900000001, "page_no": 1, "floor": 1, "author": "甲",
             "author_uid": "", "post_time": "2026-01-01", "content": "被改过的内容",
             "chars": 6, "images": []},
        ]
        r2 = svc.submit("member:qa", "A-UNIT", tid, payload2_early,
                        [{"page_no": 1, "pids": [900000001], "n": 1}],
                        {"ok": True, "pages": 1, "posts": 1, "chars": 6,
                         "head_pid": 900000001, "tail_pid": 900000001})
        check("★ 本地已 done 也**不跳过**（仍合并）",
              r2.get("merged") is True, json.dumps(r2, ensure_ascii=False))
        check("★ 同 pid 被覆盖（overwritten）", r2.get("overwritten") == 1,
              json.dumps(r2, ensure_ascii=False))
        c_ov = db.conn.execute(
            "SELECT content FROM posts WHERE tid=? AND pid=900000001",
            (tid,)).fetchone()["content"]
        check("★ 本地原文被**覆盖**成成员版", c_ov == "被改过的内容", c_ov)

        # ---- 本地**还没 done**、已有部分楼层 → 覆盖 + 补缺，一样 ----
        tid2 = 999999002
        with db.lock:
            # 防御：这个 tid 也先清干净（万一以前跑过留下了残留）
            for sql in ("DELETE FROM posts WHERE tid=?", "DELETE FROM thread_pages WHERE tid=?",
                        "DELETE FROM flows WHERE tid=?", "DELETE FROM detail_queue WHERE tid=?",
                        "DELETE FROM assist_conflicts WHERE tid=?"):
                db.conn.execute(sql, (tid2,))
            db.conn.execute("DELETE FROM threads WHERE tid=?", (tid2,))
            db.conn.execute(
                "INSERT OR IGNORE INTO threads(tid,title,detail_state) "
                "VALUES(?,'合并测试帖2','failed')", (tid2,))
            db.conn.commit()
        db.save_posts(tid2, 1, [
            {"pid": 910000001, "floor": 1, "author": "甲", "author_uid": "",
             "post_time": "2026-01-01", "content": "本地第一楼", "chars": 5,
             "images": []}])
        payload3 = [
            {"pid": 910000001, "page_no": 1, "floor": 1, "author": "甲",
             "author_uid": "", "post_time": "2026-01-01", "content": "成员改写的内容",
             "chars": 7, "images": []},
            {"pid": 910000002, "page_no": 1, "floor": 2, "author": "乙",
             "author_uid": "", "post_time": "2026-01-02", "content": "成员第二楼",
             "chars": 5, "images": []},
        ]
        r3 = svc.submit("member:qa", "A-UNIT", tid2, payload3,
                        [{"page_no": 1, "pids": [910000001, 910000002], "n": 2}],
                        {"ok": True, "pages": 1, "posts": 2, "chars": 12,
                         "head_pid": 910000001, "tail_pid": 910000002})
        check("★ 重复楼层被**覆盖**（overwritten=1）", r3.get("overwritten") == 1,
              json.dumps(r3, ensure_ascii=False))
        check("★ 缺失楼层被补上（added=1）", r3.get("added") == 1,
              json.dumps(r3, ensure_ascii=False))
        c1 = db.conn.execute(
            "SELECT content FROM posts WHERE tid=? AND pid=910000001",
            (tid2,)).fetchone()["content"]
        check("★ 本地原文被覆盖成成员版", c1 == "成员改写的内容", c1)
        n3 = db.conn.execute("SELECT COUNT(*) FROM posts WHERE tid=?",
                             (tid2,)).fetchone()[0]
        check("补完共 2 个楼层", n3 == 2, str(n3))
        cf = db.conn.execute(
            "SELECT kind, COUNT(*) n FROM assist_conflicts WHERE tid=? GROUP BY kind",
            (tid2,)).fetchall()
        kinds = {r["kind"]: r["n"] for r in cf}
        check("★ 冲突留痕（overwritten / merged_new）",
              kinds.get("overwritten") and kinds.get("merged_new"), json.dumps(kinds))
        stt = db.conn.execute("SELECT detail_state FROM threads WHERE tid=?",
                              (tid2,)).fetchone()["detail_state"]
        check("详情状态为 done", stt == "done", stt)
        db.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 78)
    npass = sum(1 for _, ok, _ in results if ok)
    nfail = len(results) - npass
    for name, ok, extra in results:
        if not ok:
            print(f"  {BAD} {name}  {extra}")
    print(f"共 {len(results)} 项 · 通过 {npass} · 失败 {nfail}")
    print("=" * 78)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
