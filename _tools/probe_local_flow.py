# -*- coding: utf-8 -*-
"""probe_local_flow.py —— 实测「网页 → 本地助手 → agent → 本机采集 → 回传」整条链。

这条链是 2026-10-23 新加的：成员的采集必须发生在**他自己机器**上
（站方不提供 CORS，而且服务器上采会让站方只看到管理员那一个 IP）。

本脚本模拟网页做的事：拿成员 JWT → POST 127.0.0.1:8766/start（带 tid）→
等 agent 跑完 → 核对权威库里那条帖的楼层。

用法：python _tools/probe_local_flow.py <tid> [--sources dept:555 --pages 2]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
HELPER = "http://127.0.0.1:8766"
DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def call(url, path, method="GET", body=None, token="", timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method)
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
    import sqlite3
    pw = os.environ.get("MSYT_TEST_PW", "")
    email = os.environ.get("MSYT_TEST_EMAIL", "test@example.com")
    args = sys.argv[1:]
    # 先解析参数：**--pages 的值不是 tid**（别把它当 tid 采了）
    tids = []
    sources = ""
    pages = 2
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--sources" and i + 1 < len(args):
            sources = args[i + 1]
            i += 2
            continue
        if a == "--pages" and i + 1 < len(args):
            pages = int(args[i + 1])
            i += 2
            continue
        if str(a).isdigit():
            tids.append(int(a))
        i += 1

    # 没给 tid 就自己挑一个「可见但还没采详情」的近期帖
    if not tids and not sources:
        import sqlite3
        c = sqlite3.connect(str(DB))
        c.row_factory = sqlite3.Row
        r = c.execute(
            "SELECT tid FROM threads WHERE detail_state='pending' AND tid>8200000 "
            "ORDER BY tid DESC LIMIT 2").fetchall()
        c.close()
        tids = [int(x["tid"]) for x in r]
        print(f"自动挑的 tid：{tids}")

    st, d = call(BASE, "/api/auth/login", "POST",
                 {"email": email, "password": pw})
    if st != 200:
        print(f"登录失败：{st} {d}")
        return 1
    tok = d["token"]
    print(f"以 {email} 登录")

    payload = {"token": tok}
    if tids:
        payload["tids"] = ",".join(str(t) for t in tids)
    if sources:
        payload["sources"] = sources
        payload["pages"] = pages
    print(f"→ 呼叫本机助手：{payload}")
    st, d = call(HELPER, "/start", "POST", payload, timeout=40)
    print(f"   助手回：{d}")
    if st != 200 or not (d or {}).get("ok"):
        return 1

    c = sqlite3.connect(str(DB))
    before = {}
    for t in tids:
        before[t] = c.execute("SELECT COUNT(*) FROM posts WHERE tid=?",
                               (t,)).fetchone()[0]
    c.close()

    for i in range(40):
        time.sleep(3)
        st, h = call(HELPER, "/status", "GET", None, "", timeout=15)
        if not h or not h.get("running"):
            print(f"   agent 已退出（第 {i * 3}s）")
            break
        if i % 3 == 0:
            print(f"   [{i * 3:>3}s] state={h.get('agent_state')} "
                  f"已完成={h.get('stat_done')} 失败={h.get('stat_failed')} "
                  f"受限={h.get('stat_restricted')}")
    st, h = call(HELPER, "/status", "GET", None, "", timeout=15)
    print("\n=== 助手日志（末尾） ===")
    for x in (h or {}).get("log", [])[-12:]:
        print("  ", x["m"])

    c = sqlite3.connect(str(DB))
    print("\n=== 权威库核对 ===")
    for t in tids:
        a = c.execute("SELECT COUNT(*) FROM posts WHERE tid=?", (t,)).fetchone()[0]
        stt = c.execute("SELECT detail_state FROM threads WHERE tid=?",
                        (t,)).fetchone()
        print(f"  tid={t} 楼层 {before.get(t)} → {a}  状态={stt[0] if stt else '?'}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
