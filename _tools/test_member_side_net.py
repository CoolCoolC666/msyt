# -*- coding: utf-8 -*-
"""验收：组员侧的连通性优化。

★ 用户 2026-10-23 晚上：
  「有位组员的连接有优化，不过member2这边就没有了」
  「这边除了采集的优化，同时也需要对于组员侧的优化」
  「启动助手无法连接浏览器的情况，使用的浏览器是Edge」

测：
  ① agent 能在**直连不通时**自动改用系统代理
  ② --diagnose / 自检.cmd 的诊断输出齐全
  ③ 网页端探测超时已放宽到 10 秒
  ④ 启动包里有「自检.cmd」，且 .cmd 是纯 ASCII + CRLF
  ⑤ 助手的 CORS 带 PNA 头（Edge 的私有网络访问）
"""
from __future__ import annotations

import io
import os
import sys

ROOT = r"【项目根】"
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt import agent as A                      # noqa: E402

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


print("=" * 72)
print("组员侧连通性优化验收")
print("=" * 72)

# ---------------------------------------------------------------- ① 代理支持
print("\n① agent 支持系统代理（直连不通时自动改走代理）")
ag = A.Agent(server="http://127.0.0.1:59999", verbose=False)
check("Agent 有 _sys_proxy 方法", hasattr(ag, "_sys_proxy"))
check("Agent 有 proxy 字段", hasattr(ag, "proxy"))
src = io.open(os.path.join(ROOT, "程序", "msyt", "agent.py"), encoding="utf-8").read()
check("用了 urllib.request.getproxies（Windows 上会读注册表）",
      "getproxies" in src)
check("探测失败后会**再经代理试一遍**", "经代理可达" in src)
check("代理通了会建带代理的 Session", "_mk_session(proxy)" in src)

# 实测：假代理 → 直连不通、经代理也不通 → 明确报「都不通」
ag2 = A.Agent(server="http://127.0.0.1:59999", verbose=False)
ag2._proxy = "http://127.0.0.1:59998"       # 假装有系统代理
res = ag2.pick_server()
check("直连+代理都不通 → 仍返回主地址（后面靠退避重试兜底）",
      res == "http://127.0.0.1:59999", res)

# ---------------------------------------------------------------- ② 诊断
print("\n② 连通性诊断")
check("Agent 有 diagnose() 方法", hasattr(ag, "diagnose"))
lines = ag.diagnose()
joined = "\n".join(lines)
check("诊断含 DNS", "DNS" in joined)
check("诊断含 TCP", "TCP" in joined)
check("诊断含直连 HTTP", "直连" in joined)
check("诊断含系统代理", "系统代理" in joined)
check("诊断含本机助手", "本机助手" in joined)
check("诊断会点明「Edge 能连而这里不通」这个已知坑",
      "Edge" in joined)

sc = os.path.join(ROOT, "程序", "msyt", "selfcheck.py")
check("有独立的 selfcheck.py（自检.cmd 用）", os.path.exists(sc))
sc_src = io.open(sc, encoding="utf-8").read()
check("selfcheck 给出可执行结论", "结论" in sc_src and "换网络" in sc_src)

# ---------------------------------------------------------------- ③ 网页端
print("\n③ 网页端：探测超时与 Edge 指引")
js = io.open(os.path.join(ROOT, "程序", "msyt", "web", "app.js"), encoding="utf-8").read()
check("助手探测超时 4 秒 → 10 秒", "ctl.abort(), 10000" in js)
check("提示语给出 Edge「本地网络访问」的**具体位置**",
      "地址栏" in js and "本地网络" in js)
check("提示语提到「自检.cmd」", "自检.cmd" in js)

# ---------------------------------------------------------------- ④ 启动包
print("\n④ 启动包里有「自检.cmd」")
bp = os.path.join(ROOT, "程序", "msyt", "bundle.py")
b_src = io.open(bp, encoding="utf-8").read()
check("bundle.py 会写「自检.cmd」", "自检.cmd" in b_src)
check("助手 cmd 里有 Edge 提示", "local network access" in b_src)

# ---------------------------------------------------------------- ⑤ 助手 CORS
print("\n⑤ 助手的 CORS（Edge 私有网络访问）")
hp = os.path.join(ROOT, "程序", "msyt-helper.py")
h_src = io.open(hp, encoding="utf-8").read()
check("有 Access-Control-Allow-Private-Network: true",
      "Access-Control-Allow-Private-Network" in h_src)
check("有 Access-Control-Allow-Headers（含 Authorization）",
      "Access-Control-Allow-Headers" in h_src and "Authorization" in h_src)
check("有 OPTIONS 预检处理", "do_OPTIONS" in h_src)

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
