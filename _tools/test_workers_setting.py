# -*- coding: utf-8 -*-
"""端到端验「采集并发数」这条链：服务端设置 → 助手本机 → 启动包 → 网页接口。

★ 要验的四处
  ① 服务端 GET/POST /api/agent/workers（成员可读、管理员可改）
  ② 助手 POST /workers（设本机值）+ /status 里回报 workers
  ③ 启动包里有 workers.txt，且值 == 服务端默认
  ④ 服务端采集任务真的用这个并发数（任务日志里打出「并发采集启动：N 路」）
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile

PKG = os.path.join(
    r"【用户桌面】",
    "14-网络舆情分析与监测-2.0", "2-作业", "02 民生e网通", "程序")
sys.path.insert(0, PKG)
BASE = "http://127.0.0.1:8765"
HELPER = "http://127.0.0.1:8766"
OK = 0
FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {label}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  —— {extra}" if extra else ""))


def call(url, body=None, tok="", method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                               method=method if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


atok = call(BASE + "/api/auth/login", {"email": "qa-admin@example.com",
                                      "password": "qa-test-password"},
            method="POST")[1]["token"]
mtok = call(BASE + "/api/auth/login", {"email": "qa-member@example.com",
                                      "password": "qa-test-password"},
            method="POST")[1]["token"]

print("### ① 服务端接口")
st, d = call(BASE + "/api/agent/workers", tok=atok)
check("管理员能读 /api/agent/workers", st == 200 and "workers" in d, f"{st} {d}")
w0 = d.get("workers")
check("返回带推荐值与提示", d.get("recommended") == 3 and bool(d.get("tip")),
      f"recommended={d.get('recommended')}")
check("提示文案里有「推荐 3」", "推荐 3" in (d.get("tip") or ""), d.get("tip"))
st, _ = call(BASE + "/api/agent/workers", tok=mtok)
check("成员也能读（助手要靠它取默认值）", st == 200, str(st))
st, _ = call(BASE + "/api/agent/workers", tok=mtok, body={"workers": 2},
             method="POST")
check("★ 成员改服务器默认 → 403", st == 403, str(st))

print("\n### ② 服务端设置真的生效（改 → 读回 → 采集任务用它）")
st, d = call(BASE + "/api/agent/workers", tok=atok, body={"workers": 2},
             method="POST")
check("管理员改服务器默认 → 200", st == 200 and d.get("workers") == 2, f"{st} {d}")
st, d = call(BASE + "/api/agent/workers", tok=atok)
check("读回是 2", d.get("workers") == 2, str(d.get("workers")))
# ★ 先停掉正在跑的 detail 任务：**同节点互斥**，不先停会 409
#   停止是**异步收尾**的 → 要轮询等它真的停下（实测停了仍会 409 几秒）
for _ in range(30):
    jobs = call(BASE + "/api/jobs", tok=atok)[1]
    running = [j for j in jobs.get("items", [])
               if j.get("state") == "running" and j.get("kind") == "detail"]
    if not running:
        break
    for j in running:
        call(BASE + f"/api/jobs/{j['id']}/stop", tok=atok, method="POST")
    time.sleep(2)
else:
    print("      （在跑的 detail 任务停不掉，跳过采集任务那部分）")

conn = sqlite3.connect(os.path.join(PKG, "data", "msyt.db"))
conn.row_factory = sqlite3.Row
tids = [int(r["tid"]) for r in conn.execute(
    """SELECT tid FROM threads WHERE detail_state='done'
        AND pub_ts >= strftime('%s','2023-12-31') ORDER BY tid DESC LIMIT 3""")]
conn.close()
st, d = call(BASE + "/api/jobs/detail", {"tids": tids, "node": "admin"},
             tok=atok, method="POST")
jid = d["job"]["id"]
for _ in range(40):
    time.sleep(2)
    j = call(BASE + f"/api/jobs/{jid}?tail=20", tok=atok)[1]
    if j["state"] != "running":
        break
logs = [l.get("m", "") for l in (j.get("logs") or [])]
hit = [l for l in logs if "并发采集启动" in l]
check("★ 采集任务用了设置里的 2 路", any("2 路" in l for l in hit), str(hit))
call(BASE + "/api/agent/workers", tok=atok, body={"workers": 3}, method="POST")
st, d = call(BASE + "/api/agent/workers", tok=atok)
check("已恢复推荐值 3", d.get("workers") == 3, str(d.get("workers")))

print("\n### ③ 启动包里的 workers.txt")
from msyt import bundle                                  # noqa: E402
data, _ = bundle.build_bundle(
    "https://example.com", "TICKET", os.path.join(PKG, "msyt"),
    helper_src=os.path.join(PKG, "msyt-helper.py"),
    server_alt="", helper_port=8766, agent_workers=3)
with zipfile.ZipFile(io.BytesIO(data)) as z:
    names = z.namelist()
    check("包里有 workers.txt", "workers.txt" in names)
    if "workers.txt" in names:
        txt = z.read("workers.txt").decode("utf-8")
        check("workers.txt 第一行是 3", txt.splitlines()[0].strip() == "3",
              txt.splitlines()[0])
        check("workers.txt 里有推荐提示", "推荐 3" in txt)

print("\n### ④ 助手：本机设置 + /status 回报")
try:
    st, s = call(HELPER + "/status")
    check("助手 /status 有 workers 字段", st == 200 and "workers" in s, str(st))
    check("助手 /status 带推荐值", s.get("workers_recommended") == 3,
          str(s.get("workers_recommended")))
    st, d = call(HELPER + "/workers", {"workers": 4}, method="POST")
    check("助手能设本机并发数", st == 200 and d.get("workers") == 4, f"{st} {d}")
    st, s = call(HELPER + "/status")
    check("助手 /status 回报本机值 4", s.get("workers") == 4, str(s.get("workers")))
    st, d = call(HELPER + "/workers", {"workers": 99}, method="POST")
    check("★ 越界值被拒", st == 400, str(st))
    call(HELPER + "/workers", {"workers": 3}, method="POST")
except Exception as e:                                   # noqa: BLE001
    check("助手可访问", False, str(e))

print()
print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
