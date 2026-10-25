# -*- coding: utf-8 -*-
"""test_integrity_manual.py —— 体检必须**手动触发**、且跑在后台（2026-10-25）

★ 用户原话：「体检界面不要打开自动体检，容易卡」

★ 背景
  原来前端切到「完整性体检」页签就立刻 `GET /api/integrity`，
  而那个接口是**同步扫全库**（60 万帖量级）→ 浏览器请求一直挂着，
  页面看着像死了。

★ 本测试断言两件事
  ① 切页签**不会**自动触发体检（前端源码层）
  ② 手动触发走**后台任务**接口（页面不挂死），且同节点互斥
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "程序")
sys.path.insert(0, PKG)

BASE = "http://127.0.0.1:8765"
ADMIN = ("qa-admin@example.com", "qa-test-password")

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def call(p, body=None, tok="", method="GET", timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + p, data=data,
                                 method=method if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as x:
            return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except Exception as e:                                   # noqa: BLE001
        return 0, str(e)[:200]


print("### ① 前端：切页签不再自动触发体检")
appjs = open(os.path.join(PKG, "msyt", "web", "app.js"), encoding="utf-8").read()
check("切页签时不再调 runIntegrity()",
      "btn.dataset.tab === 'integrity') runIntegrity()" not in appjs)
check("切页签改成显示空闲态提示",
      "btn.dataset.tab === 'integrity') showIntegrityIdle()" in appjs)
check("有空闲态提示函数", "function showIntegrityIdle(" in appjs)
check("手动触发走后台接口（不再直接 GET /api/integrity）",
      "api('/api/integrity/run'" in appjs
      and "await api('/api/integrity')" not in appjs)
check("后台任务用轮询取结果", "/api/jobs/' + jid" in appjs)
check("体检期间按钮禁用（防连点）", "btn.disabled = true" in appjs)

print()
print("### ② 服务端：有后台体检接口")
webapp_src = open(os.path.join(PKG, "msyt", "webapp.py"), encoding="utf-8").read()
check("有 POST /api/integrity/run", '@app.post("/api/integrity/run")' in webapp_src)
check("后台任务用 JOBS.spawn 跑", "JOBS.spawn(job, _run)" in webapp_src)
check("体检任务互斥（不许并发两个）", "已有一个体检任务在跑" in webapp_src)
check("同步版接口保留（脚本/测试用）", '@app.get("/api/integrity")' in webapp_src)

print()
print("### ③ 实际调接口")
st, d = call("/api/auth/login", {"email": ADMIN[0], "password": ADMIN[1]},
             method="POST")
if st != 200:
    print(f"  [跳过] 服务没起来（{st}）")
    raise SystemExit(0)
tok = d["token"]

st, d = call("/api/integrity/run", body={}, tok=tok, method="POST")
check("手动触发返回任务号", st == 200 and isinstance(d, dict) and "job" in d,
      str(d)[:100])
if st == 200 and "job" in d:
    jid = d["job"]["id"]
    check("任务 kind='integrity'", d["job"]["kind"] == "integrity")
    import time
    for _ in range(120):
        time.sleep(2)
        st2, j = call(f"/api/jobs/{jid}", tok=tok)
        if j.get("state") != "running":
            break
    check("体检任务跑完", j.get("state") == "done", str(j.get("state")))
    r = j.get("result") or {}
    check("结果里有检查数与三类清单",
          "checked" in r and "problems" in r and "warnings" in r
          and "growth" in r, str(list(r.keys()))[:120])

print()
print("=" * 60)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✓")
