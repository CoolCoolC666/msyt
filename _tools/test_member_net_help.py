# -*- coding: utf-8 -*-
"""验收：成员端**网络自救**能力（2026-10-24 组员实测后加的一组）。

★ 背景（组员 member2 的实测截图）：
  `ConnectionResetError(10054)` —— TCP 连上了却被中途重置，
  系统代理是空的，Edge 能打开网站而 Python 连不上。
  旧版 agent 只会说「网络抖动」，然后重试 3 次就**退出**。

★ 这一组验收盯住四件事：
  ① 错误**分类**：RST / DNS / 超时 / 拒绝 必须给出**不同**的结论
  ② 「可达」判定要严：代理的 502 错误页、别的服务的 404 都**不算**通
  ③ 能**扫到常见本地代理端口**（Clash 7890 / v2rayN 10809 …）
  ④ 网络不通时**不退出**，在原地等网络恢复
"""
from __future__ import annotations

import io
import os
import socket
import sys
import threading
import zipfile

ROOT = r"【项目根】"
PKG = os.path.join(ROOT, "程序")
sys.path.insert(0, PKG)

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
print("成员端网络自救能力验收")
print("=" * 72)

from msyt import netdiag                                  # noqa: E402

# ---------------------------------------------------------------- ① 错误分类
print("\n① 错误分类（RST / DNS / 超时 / 拒绝 必须是四种结论）")
cases = [
    (ConnectionResetError(10054, "远程主机强迫关闭了一个现有的连接。"), "连接被重置"),
    (OSError("[Errno -2] Name or service not known"), "域名解析失败"),
    (socket.timeout("timed out"), "连接超时"),
    (ConnectionRefusedError("refused"), "拒绝连接"),
]
kinds = []
for exc, want in cases:
    k, advice = netdiag.classify_error(exc)
    kinds.append(k)
    check(f"{want} → 有独立分类", want in k, f"得到「{k}」")
    check(f"{want} → 有可执行建议", len(advice) > 8, advice[:40])
check("四类错误给出四种不同结论", len(set(kinds)) == 4, str(kinds))

# ---------------------------------------------------------------- ② 可达判定
print("\n② 「可达」判定要严（代理错误页 / 别人的 404 都不算通）")


class FakeResp:
    def __init__(self, code, body=None, is_json=False):
        self.status_code = code
        self._body = body
        self._is_json = is_json

    def json(self):
        if not self._is_json:
            raise ValueError("not json")
        return self._body


check("401 → 可达（我们源站的『没带令牌』）", netdiag.looks_reachable(FakeResp(401)))
check("403 → 可达", netdiag.looks_reachable(FakeResp(403)))
check("200 + JSON → 可达", netdiag.looks_reachable(FakeResp(200, {"ok": True}, True)))
check("502（代理/网关错误页）→ 不算通", not netdiag.looks_reachable(FakeResp(502, "<html>", False)))
check("404（别的本地服务）→ 不算通", not netdiag.looks_reachable(FakeResp(404, "<html>", False)))
check("200 但不是 JSON → 不算通", not netdiag.looks_reachable(FakeResp(200, "<html>", False)))

# ---------------------------------------------------------------- ③ 代理端口扫描
print("\n③ 常见本地代理端口扫描")
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
real_port = srv.getsockname()[1]


def _accept_loop():
    try:
        for _ in range(4):
            srv.accept()
    except OSError:
        pass        # 测试结束时 socket 已关 → 正常收尾


threading.Thread(target=_accept_loop, daemon=True).start()
found = netdiag.local_proxy_candidates(ports=[real_port, 59999])
check("能扫到正在监听的端口", f"http://127.0.0.1:{real_port}" in found, str(found))
check("不会报出没监听的端口", "http://127.0.0.1:59999" not in found, str(found))
srv.close()

src_agent = open(os.path.join(PKG, "msyt", "agent.py"), encoding="utf-8").read()
check("agent 会扫本地代理端口", "_local_proxies" in src_agent
      and "local_proxy_candidates" in src_agent)
check("agent 源码里用了 netdiag", "from . import netdiag" in src_agent)

# ---------------------------------------------------------------- ④ IPv4 回落
print("\n④ IPv6 坏掉时的 IPv4 回落")
netdiag.set_ipv4_only(True)
try:
    infos = socket.getaddrinfo("example.com", 443, socket.AF_INET, socket.SOCK_STREAM)
    check("强制后只解析 IPv4", all(i[0] == socket.AF_INET for i in infos))
finally:
    netdiag.set_ipv4_only(False)
check("能还原回原样", socket.getaddrinfo is netdiag._orig_getaddrinfo)

# ---------------------------------------------------------------- ⑤ 不退出
print("\n⑤ 网络不通时不退出（在原地等恢复）")
from msyt.agent import Agent                               # noqa: E402

check("Agent 有 net_wait_minutes 参数",
      "net_wait_minutes" in Agent.__init__.__code__.co_varnames)
check("有 _wait_for_network 方法", hasattr(Agent, "_wait_for_network"))
check("有 _is_connection_failure 方法", hasattr(Agent, "_is_connection_failure"))
check("失败判定会区分『连不上』与『被拒绝』",
      Agent._is_connection_failure(Agent, None) is False)

a = Agent(server="https://x.invalid", verbose=False, net_wait_minutes=0)
from msyt.agent import LinkLost                          # noqa: E402
check("连接类错误 → 判为『连不上』",
      a._is_connection_failure(LinkLost("与 x 的连接失败：ConnectionError")))
check("5xx → 不算『连不上』（那是服务端问题）",
      not a._is_connection_failure(LinkLost("服务端暂时不可用（HTTP 502）")))

src_cli = open(os.path.join(PKG, "msyt", "cli.py"), encoding="utf-8").read()
check("CLI 有 --net-wait 参数", "--net-wait" in src_cli)
check("CLI 把 net_wait 传给 Agent", "net_wait_minutes=" in src_cli)

# ---------------------------------------------------------------- ⑥ 排查指引
print("\n⑥ 连不通时给出**可执行**的排查指引")
check("有『连不上控制面』的排查段落", "_report_network_dead_end" in src_agent)
check("指引里提到自检.cmd", "自检.cmd" in src_agent)
check("指引里提到 --proxy", "--proxy" in src_agent)
check("指引里说『不用重新下载包』", "不用重新下载包" in src_agent)

src_sc = open(os.path.join(PKG, "msyt", "selfcheck.py"), encoding="utf-8").read()
check("自检会扫本地代理端口", "local_proxy_candidates" in src_sc)
check("自检结论会分类原因", "classify_error" in src_sc)
check("自检会给出可直接复制的命令", "--proxy http://127.0.0.1:" in src_sc)

# ---------------------------------------------------------------- ⑦ 进包
print("\n⑦ 新模块要进启动包（组员拿到的包必须带它）")
from msyt import bundle                                   # noqa: E402

zbytes, _ = bundle.build_bundle(server="https://agentapi.example.com",
                                 ticket="TICKET",
                                 msyt_dir=PKG,
                                 helper_src=os.path.join(PKG, "msyt-helper.py"))
with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
    names = z.namelist()
    check("包里有 netdiag.py", "msyt/netdiag.py" in names, str(names[:6]))
    check("包里有 agent.py", "msyt/agent.py" in names)
    for fn in names:
        if fn.lower().endswith(".cmd"):
            txt = z.read(fn).decode("ascii", "replace")
            check(f"「{fn}」不含 '->'（防闪退）", "->" not in txt)

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
