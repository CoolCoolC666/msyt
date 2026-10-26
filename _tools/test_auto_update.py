# -*- coding: utf-8 -*-
"""验收：助手能**自动更新**本机的采集代码。

★ 用户 2026-10-23：「修一下分发包机制」
  今天已经让组员重下三次包了 —— 助手启动 agent 前自己拉一次最新的，
  组员以后就不用管这件事。

测法：
  ① 把本机包里的 `msyt/agent.py` 改成**旧的**（塞一个标记）
  ② 调助手的 /start（带 JWT）
  ③ 检查 `msyt/agent.py` 是否**自动变回**最新的
  ④ 检查凭证文件 `agent-ticket.txt` **没被覆盖**
  ⑤ 检查两个 .cmd **没被覆盖**
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = r"【项目根】"
# ★ 启动包位置**不写死**（2026-10-25：它从桌面搬到了 02 同级）
from bundle_path import find_bundle
PKG = find_bundle()
if not PKG:
    print('本机没有成员端启动包 → 跳过')
    sys.exit(0)
BASE = "http://127.0.0.1:8765"
EMAIL, PW = "test@example.com", "test-password"

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


def req(url, data=None, token=None, method=None):
    body = json.dumps(data).encode("utf-8") if data is not None else None
    r = urllib.request.Request(url, data=body,
                               method=method or ("POST" if body else "GET"))
    if body:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    r.add_header("User-Agent", "msyt-helper/1.0")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


print("=" * 72)
print("助手「自动更新」验收")
print("=" * 72)

st, d = req(BASE + "/api/auth/login", {"email": EMAIL, "password": PW})
if st != 200:
    print(f"★ 登录失败 HTTP {st} {d}")
    sys.exit(1)
tok = d["token"]

ag_path = os.path.join(PKG, "msyt", "agent.py")
ticket_path = os.path.join(PKG, "agent-ticket.txt")
cmd_path = os.path.join(PKG, "启动采集.cmd")

# ---------------------------------------------------------------- ① 改旧
print("\n① 把本机 agent.py 改成「旧的」（塞标记）")
orig = io.open(ag_path, encoding="utf-8").read()
io.open(ag_path, "w", encoding="utf-8", newline="\n").write(
    "# __STALE_MARKER__ 故意改旧的，看助手会不会自动更新回来\n" + orig)
check("已写入旧标记", "__STALE_MARKER__" in io.open(ag_path, encoding="utf-8").read())

ticket_before = io.open(ticket_path, encoding="utf-8").read()
cmd_before = io.open(cmd_path, encoding="utf-8", errors="replace").read()

# ---------------------------------------------------------------- ② 启动
print("\n② 调助手的 /start（会自动更新代码，再拉起 agent）")
try:
    st, r = req("http://127.0.0.1:8766/start", {"token": tok}, method="POST")
except Exception as e:                                   # noqa: BLE001
    print(f"★ 助手调用失败：{e}")
    sys.exit(1)
check("助手 /start 有响应", st in (200, 400), f"HTTP {st}")
print(f"   返回：{str(r)[:140]}")

# 等更新完成（下载+覆盖）
time.sleep(2.5)

# ---------------------------------------------------------------- ③ 验证
print("\n③ 检查代码是否自动更新回来了")
now = io.open(ag_path, encoding="utf-8").read()
check("★ 旧标记已被清掉（代码被覆盖）", "__STALE_MARKER__" not in now)
check("★ agent.py 含最新逻辑（HTTP_TIMEOUT = 45）", "HTTP_TIMEOUT = 45" in now)
check("★ agent.py 含回传重试（_submit_with_retry）", "_submit_with_retry" in now)

print("\n④ 凭证与脚本**没被覆盖**")
check("agent-ticket.txt 没变",
      io.open(ticket_path, encoding="utf-8").read() == ticket_before)
check("启动采集.cmd 没变",
      io.open(cmd_path, encoding="utf-8", errors="replace").read() == cmd_before)

print("\n⑤ 助手状态")
st, h = req("http://127.0.0.1:8766/status", method="GET")
logs = [l.get("m", "") for l in (h.get("log") or [])]
upd = [m for m in logs if "自动更新" in m]
check("助手日志里有「自动更新」的记录", bool(upd), (upd[-1] if upd else "（无）")[:120])
check("助手报了 server / server_alt", bool(h.get("server")) and bool(h.get("server_alt")),
      f"{h.get('server')} | {h.get('server_alt')}")

# 停掉刚起的 agent，别一直采站方
try:
    req("http://127.0.0.1:8766/stop", method="POST")
except Exception:                                       # noqa: BLE001
    pass

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)

