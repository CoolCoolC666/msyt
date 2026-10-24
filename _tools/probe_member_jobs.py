# -*- coding: utf-8 -*-
"""probe_member_jobs.py —— 实测：成员能不能在**管理员的机器**上启动采集任务。

为什么需要它
------------
协作 Beta 的初衷是「站方看到的是多个分散的普通用户，不是一个服务器在集中爬」。
但 `/api/jobs/list`、`/api/jobs/detail` 用的是 `require_auth`（不是 require_admin），
成员点「开始采集」→ 任务 spawn 在**web 服务进程里** → 用的还是管理员那台机器、那个 IP。

这会让：
  · 站方看到的流量形态**没有分散开**（风控视角上的初衷落空）
  · 管理员的机器与带宽被成员消耗
  · 成员以为在自己机器上采，实际不是

本脚本用**成员账号**发一个只有 1 页的列表采集，然后核对：
  ① 任务是否真的被创建、node 挂在谁名下
  ② 水位线 / fetch_log 是否真的动了（= 说服务器真的去请求了站方）

用法：python _tools/probe_member_jobs.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
DB = Path(__file__).resolve().parent.parent / "程序" / "data" / "msyt.db"


def call(path, method="GET", body=None, token=""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
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
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return 0, str(e)


def main() -> int:
    pw = os.environ.get("MSYT_TEST_PW", "")
    email = sys.argv[1] if len(sys.argv) > 1 else "test@example.com"
    source = sys.argv[2] if len(sys.argv) > 2 else "dept:495"
    st, d = call("/api/auth/login", "POST", {"email": email, "password": pw})
    if st != 200:
        print(f"登录失败：{st} {d}")
        return 1
    tok = d["token"]
    role = (d.get("member") or {}).get("role")
    print(f"以 {email} 登录（角色 {role}）  源 = {source}")

    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    before = c.execute(
        "SELECT COALESCE(MAX(last_page),0) p FROM list_watermark WHERE source=?",
        (source,)).fetchone()["p"]
    nlog_before = c.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
    c.close()

    # 只采 1 页，最小化副作用
    st, d = call("/api/jobs/list", "POST",
                 {"sources": source, "pages": 1}, tok)
    print(f"POST /api/jobs/list → HTTP {st}")
    if st != 200:
        print("  ", str(d)[:200])
        return 1
    jid = (d.get("job") or {}).get("id")
    node = (d.get("job") or {}).get("node")
    print(f"  任务 id={jid}  node={node}")

    time.sleep(6)
    st, d = call(f"/api/jobs/{jid}", "GET", None, tok)
    if st == 200:
        j = d.get("job") or d
        print(f"  任务状态：{j.get('state')}  node={j.get('node')}")
        tail = (j.get("log") or [])[-4:]
        for x in tail:
            print("   |", x)

    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    after = c.execute(
        "SELECT COALESCE(MAX(last_page),0) p FROM list_watermark "
        "WHERE source='dept:495'").fetchone()["p"]
    nlog_after = c.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
    last = c.execute("SELECT * FROM fetch_log ORDER BY id DESC LIMIT 1").fetchone()
    c.close()
    print(f"\n  水位线 dept:495  {before} → {after}")
    print(f"  fetch_log 条数  {nlog_before} → {nlog_after}")
    if last:
        print(f"  最近一条日志：{dict(last)}")
    if after > before:
        print("\n  ★★ 结论：成员的点击**真的在服务器上发了站方请求**"
              "（即用管理员的机器 / IP 在采）")
    else:
        print("\n  结论：水位线没动（未真正请求站方）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
