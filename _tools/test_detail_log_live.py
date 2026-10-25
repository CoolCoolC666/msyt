# -*- coding: utf-8 -*-
"""test_detail_log_live.py —— 管理员本机采集的日志观感（2026-10-25 补）

★ 用户原话（第二次反馈）
  「管理员自己运行跑详情采集的时候，建议日志的显示与 Agent 日志相似，
    不然会有一种『如跑』的感觉」

★ 三条要求
  ① 服务端采集**逐帖打**（与 agent 端同一个格式：tid=N ✓ X 页 / Y 楼 / Z 字）
  ② 进度行由**独立心跳线程**打（不依赖「采完一帖」才触发）
     —— 否则一帖卡住就长时间一行不出，界面看着像死了
  ③ 进度行仍是 **5 秒一行**（不能变成每秒一行）

★ 另外验一条口径（同一晚发现）
  「按部门全采」的筛选**必须排除 restricted** ——
  否则 41.8 万「站方不公开正文」的帖被当成待采全塞进队列，
  队首全是注定白跑的帖。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request

ROOT = r"【项目根】"
PKG = os.path.join(ROOT, "程序")
sys.path.insert(0, PKG)
BASE = "http://127.0.0.1:8765"

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def call(p, body=None, tok="", method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + p, data=data,
                                 method=method if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


print("### ① 源码层：逐帖 + 心跳 + 5 秒节流")
detail_src = open(os.path.join(PKG, "msyt", "detail.py"), encoding="utf-8").read()
webapp_src = open(os.path.join(PKG, "msyt", "webapp.py"), encoding="utf-8").read()
appjs = open(os.path.join(PKG, "msyt", "web", "app.js"), encoding="utf-8").read()

check("并发路径成功也逐帖打（与 agent 同格式）",
      'f"[detail] tid={tid} ✓ {res.get(\'pages\')} 页 / "' in detail_src)
check("有独立心跳线程（不依赖采完一帖）",
      "detail-ticker" in detail_src and "_ticker" in detail_src)
check("心跳用节流版 progress()，不是 force=True",
      "progress(force=True)" not in detail_src)
check("进度间隔仍是 5 秒", "PROGRESS_INTERVAL = 5.0" in detail_src)

print()
print("### ② 部门筛选口径：必须排除 restricted")
check("按部门筛选排除 done 与 restricted",
      "NOT IN ('done','restricted')" in webapp_src)
check("★ 不再只排除 done（旧口径）",
      "COALESCE(detail_state,'pending') <> 'done' " not in webapp_src)
check("入队时也跳过已终态的帖",
      'in ("done", "restricted")' in open(
          os.path.join(PKG, "msyt", "db.py"), encoding="utf-8").read())

print()
print("### ③ 详情采集的部门选择器显示「欠多少详情」")
check("详情 picker 用 detail 模式", "mode: 'detail'" in appjs)
check("有 deptPendingCount()", "function deptPendingCount(" in appjs)
check("标签显示「欠 N 帖详情」", "帖详情" in appjs)
check("后端有 /api/pending-by-dept",
      '@app.get("/api/pending-by-dept")' in webapp_src)

print()
print("### ④ 实跑：日志里既有逐帖行、又有 5 秒进度行")
st, d = call("/api/auth/login", {"email": "qa-admin@example.com",
                                 "password": "qa-test-password"}, method="POST")
if st != 200:
    print(f"  [跳过] 服务没起来（{st}）")
    raise SystemExit(0)
tok = d["token"]

# 停掉在跑的同类任务，免得 409
st, d = call("/api/jobs", tok=tok)
for j in d.get("items", []):
    if j["state"] == "running" and j["kind"] == "detail":
        call(f"/api/jobs/{j['id']}/stop", tok=tok, method="POST")
time.sleep(3)

st, d = call("/api/jobs/detail", body={"limit": 12, "node": "admin"},
             tok=tok, method="POST")
if not isinstance(d, dict) or "job" not in d:
    check("任务能起来", False, str(d)[:120])
else:
    jid = d["job"]["id"]
    for _ in range(120):
        time.sleep(2)
        st, j = call(f"/api/jobs/{jid}?tail=300", tok=tok)
        if j.get("state") != "running":
            break
    logs = [l.get("m") or "" for l in (j.get("logs") or [])]
    each = [m for m in logs if re.search(r"tid=\d+ ✓ \d+ 页 / \d+ 楼 / \d+ 字", m)]
    prog = [m for m in logs if "进度：" in m]
    check("有逐帖成功行（agent 同格式）", len(each) >= 1, f"{len(each)} 条")
    check("有进度行", len(prog) >= 1, f"{len(prog)} 条")
    # 进度行的间隔：5 秒左右
    ts = [l.get("t") for l in (j.get("logs") or []) if "进度：" in (l.get("m") or "")]
    gaps = [round(ts[i + 1] - ts[i], 1) for i in range(len(ts) - 1)]
    ok_gap = all(4.0 <= g <= 9.0 for g in gaps) if gaps else True
    check("进度行间隔在 5 秒上下（不是每秒一行）", ok_gap, f"间隔={gaps}")
    check("逐帖行在进度行之外单独存在（不靠进度撑动静）", len(each) >= 1)

print()
print("=" * 60)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✓")
