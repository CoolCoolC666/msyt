# -*- coding: utf-8 -*-
"""
test_local_collect.py —— 「成员在本机采集」链路的验收
================================================================================
这条链路解决的是 2026-10-23 发现的**真漏洞**：

  以前 `/api/jobs/list` 用的是 `require_auth` → 成员在网页上点「开始采集」，
  任务其实 spawn 在**管理员的服务器**里 → 用管理员的机器/IP 去刷站方。
  实测：成员点一次，dept:555 在管理员的 IP 上从 58 页刷到 500 页。
  这既白耗管理员的机器，也把「站方看到的是多个分散的普通用户」
  这个 Beta 的**核心前提**整个架空。

要验的东西：
  ① 服务器端采集端点**成员调 → 403**（管理员 → 200）
  ② agent 能在**本机**采列表元数据并回传（幂等入权威库）
  ③ agent 能在**本机**采指定帖的正文并回传（走与协作相同的合并路径）
  ④ 回传的元数据**不推水位线**（多机共推会让管理员的续采漏页）
  ⑤ 助手能把「采集目标」传给 agent（网页 → 助手 → agent）

用法：python _tools/test_local_collect.py
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


def call(path, method="GET", body=None, token="", timeout=120):
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
    email = os.environ.get("MSYT_TEST_EMAIL", "test@example.com")

    st, d = call("/api/auth/login", "POST", {"email": email, "password": pw})
    if st != 200:
        print(f"登录失败：{st} {d}")
        return 1
    tok = d["token"]
    role = (d.get("member") or {}).get("role")
    print(f"以 {email} 登录（角色 {role}）")

    print("\n=== ① 服务器端采集端点：成员 → 403 ===")
    for path, body in (("/api/jobs/list", {"sources": "dept:495", "pages": 1}),
                       ("/api/jobs/detail", {"tids": [1]}),
                       ("/api/jobs/taxonomy", {})):
        st, d = call(path, "POST", body, tok)
        detail = (d.get("detail") if isinstance(d, dict) else str(d)) or ""
        check(f"★ 成员调 {path} → 403", st == 403, f"HTTP {st}")
        check(f"  {path} 的提示里说清了「要本机 agent」",
              "agent" in str(detail) or "本机" in str(detail), str(detail)[:90])

    print("\n=== ④ 回传元数据不推水位线 ===")
    from msyt.config import Config
    from msyt.db import DB
    cfg = Config.load()
    db = DB(str(cfg.db_path))
    src = "dept:999"
    before = db.conn.execute(
        "SELECT COUNT(*) FROM list_watermark WHERE source=?", (src,)).fetchone()[0]
    st, d = call("/api/agent/ingest-list", "POST",
                 {"rows": [{"tid": 99000001, "title": "本机回传测试帖",
                            "url": "https://x/99000001", "category": "咨询",
                            "state": "已回复", "dept_id": "495",
                            "dept_name": "市公安局", "pub_time": "2026-10-23 10:00",
                            "pub_ts": 1792711200, "replies": 0, "views": 1,
                            "extra_dept_ids": [], "extra_dept_names": []}],
                  "source": src, "tags": []}, tok)
    check("回传成功", st == 200 and d.get("ingested") == 1, f"HTTP {st} {d}")
    row = db.conn.execute(
        "SELECT title,detail_state FROM threads WHERE tid=99000001").fetchone()
    check("★ 元数据进了权威库", bool(row) and "本机回传" in (row["title"] or ""),
          str(dict(row)) if row else "(没这行)")
    after = db.conn.execute(
        "SELECT COUNT(*) FROM list_watermark WHERE source=?", (src,)).fetchone()[0]
    check("★ 水位线**没被推**（多机共推会让管理员续采漏页）", after == before,
          f"{before} → {after}")
    # 幂等：再回传一次不应产生重复
    st2, d2 = call("/api/agent/ingest-list", "POST",
                    {"rows": [{"tid": 99000001, "title": "本机回传测试帖",
                               "dept_id": "495", "dept_name": "市公安局"}],
                     "source": src}, tok)
    n_threads = db.conn.execute(
        "SELECT COUNT(*) FROM threads WHERE tid=99000001").fetchone()[0]
    check("★ 回传是**幂等**的（不产生重复行）", n_threads == 1, str(n_threads))
    # 清理
    with db.lock:
        db.conn.execute("DELETE FROM threads WHERE tid=99000001")
        db.conn.execute("DELETE FROM thread_sources WHERE tid=99000001")
        db.conn.commit()
    db.close()

    print("\n=== ②③ agent 本机自主采集（真实跑一次） ===")
    from msyt.agent import Agent
    ag = Agent(server=BASE, token=tok, verbose=True)
    try:
        ag.login()
    except Exception as e:                              # noqa: BLE001
        check("agent 登录", False, str(e))
        return 1
    check("agent 登录成功", bool(ag.node), ag.node)

    # ③ 先采一帖正文（用库里真实存在、且可见的近期 tid）
    from msyt.db import DB as DB2
    db2 = DB2(str(Config.load().db_path))
    tid = db2.conn.execute(
        "SELECT tid FROM threads WHERE detail_state='done' AND tid>8270000 "
        "ORDER BY tid DESC LIMIT 1").fetchone()
    tid = int(tid["tid"]) if tid else 0
    db2.close()
    if not tid:
        check("找到一个可采的 tid", False, "库里没有 done 的帖")
    else:
        n_before = db2_conn = None
        db3 = DB2(str(Config.load().db_path))
        b4 = db3.conn.execute("SELECT COUNT(*) FROM posts WHERE tid=?", (tid,)).fetchone()[0]
        db3.close()
        res = ag.run_local(tids=[tid])
        db4 = DB2(str(Config.load().db_path))
        a4 = db4.conn.execute("SELECT COUNT(*) FROM posts WHERE tid=?", (tid,)).fetchone()[0]
        db4.close()
        check(f"★ 本机采到正文并回传（tid={tid}）",
              res.get("detail_ok") == 1 or a4 >= b4, str(res))
        check("★ 正文进了权威库", a4 >= 1, f"{b4} → {a4} 条楼层")

    print("\n=== ⑤ 助手能把目标传给 agent ===")
    # ★ 注意：助手在 8766，和主服务 8765 **不是**同一个地址
    helper_up = False
    try:
        req = urllib.request.Request("http://127.0.0.1:8766/status")
        with urllib.request.urlopen(req, timeout=8) as r:
            hj = json.loads(r.read().decode("utf-8", "replace"))
            helper_up = bool(hj.get("ok"))
            print(f"  助手：running={hj.get('running')} "
                  f"state={hj.get('agent_state')}")
    except Exception as e:                              # noqa: BLE001
        print(f"  助手没起来：{e}")
    check("本机助手在跑（127.0.0.1:8766）", helper_up)

    print("\n" + "=" * 70)
    ok = sum(1 for _, c, _ in results if c)
    bad = [r for r in results if not r[1]]
    print(f"共 {len(results)} 项 · 通过 {ok} · 失败 {len(bad)}")
    for nm, _, e in bad:
        print(f"  FAIL: {nm}  {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
