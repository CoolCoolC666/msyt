# -*- coding: utf-8 -*-
"""成员端「连通性自检」—— 双击 `自检.cmd` 就能跑。

★ 为什么需要（2026-10-23 晚上组员实测）：
  组员只看到一句 `ConnectionError`，完全不知道是 DNS、TCP、TLS、
  还是代理、还是服务端的问题 —— 只能一遍遍试，越试越懵。
  这个脚本把**每一环单独测一遍**，最后给出**可执行的结论**。

★ 典型场景（实测）：
  「Edge 能打开网站，Python 却 ConnectionError」
  → Edge 读 Windows 注册表的系统代理，**requests 不读** → 直连不通。
  这个脚本会把「系统代理」单独列出来并告诉你怎么用。

用法：python 自检.py [控制面地址]
      不给地址就用同目录的 server.txt
"""
from __future__ import annotations

import os
import socket
import sys
import time
from urllib import parse as urlparse_mod

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "msyt-selfcheck/1.0"
#: 最近一次探测的原始异常（给结论部分做分类用）
LAST_ERROR: Exception = None


def out(line=""):
    print(line, flush=True)


def read_server() -> str:
    for name in ("server.txt", "server-alt.txt"):
        p = os.path.join(HERE, name)
        try:
            if os.path.exists(p):
                v = open(p, encoding="utf-8").read().strip()
                if v and not v.startswith("__MSYT_"):
                    return v
        except Exception:                             # noqa: BLE001
            pass
    return "https://agentapi.example.com"


def sys_proxy() -> str:
    try:
        import urllib.request
        p = urllib.request.getproxies()
    except Exception:                                 # noqa: BLE001
        p = {}
    for k in ("https", "http", "all"):
        if p.get(k):
            return str(p[k])
    return ""


def probe(base: str, proxy: str = "") -> str:
    """探一个地址；返回一句话结论。"""
    global LAST_ERROR
    try:
        import requests
    except ImportError:
        return "✗ 没装 requests（先双击「启动助手.cmd」，它会装依赖）"
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    t0 = time.time()
    try:
        r = s.get(base + "/api/agent/me", timeout=20)
        try:
            from . import netdiag
            ok = netdiag.looks_reachable(r)
        except Exception:                             # noqa: BLE001
            try:
                import netdiag                        # type: ignore
                ok = netdiag.looks_reachable(r)
            except Exception:                         # noqa: BLE001
                ok = r.status_code < 500
        if ok:
            return f"✓ 通（HTTP {r.status_code}，{(time.time() - t0) * 1000:.0f}ms）"
        return (f"✗ 不通（HTTP {r.status_code} —— 这是网关/代理的错误页，"
                "不是我们服务器）")
    except Exception as e:                             # noqa: BLE001
        LAST_ERROR = e
        return f"✗ {type(e).__name__}: {str(e)[:90]}"


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else "").strip() or read_server()
    out("=" * 66)
    out("  成员端连通性自检")
    out("=" * 66)
    out(f"控制面：{base}")
    out()

    u = urlparse_mod.urlparse(base)
    host = u.hostname or ""
    port = u.port or (443 if u.scheme == "https" else 80)

    # ---- ① DNS ----
    out("① DNS 解析")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        out(f"   ✓ {host} → {', '.join(ips[:6])}")
    except Exception as e:                             # noqa: BLE001
        out(f"   ✗ 解析失败（{type(e).__name__}）")
        out("     → 你的网络连这个域名都解析不了。")
        out("     → 换一个网络（手机热点）再试一次。")
        out()
        return 1

    # ---- ② TCP（IPv4 / IPv6 分开）----
    out("② TCP 连通")
    for fam, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        try:
            s = socket.socket(fam, socket.SOCK_STREAM)
            s.settimeout(6)
            s.connect((host, port))
            s.close()
            out(f"   ✓ {label} 连得上")
        except Exception as e:                         # noqa: BLE001
            out(f"   ✗ {label} {type(e).__name__}")
    out()

    # ---- ③ 直连 / ④ 代理 ----
    out("③ 直连 HTTP")
    direct = probe(base)
    out(f"   {direct}")
    out()

    out("④ 系统代理（Edge 用的就是这套，requests 默认**不**用）")
    px = sys_proxy()
    if px:
        out(f"   系统代理：{px}")
        out(f"   经代理 HTTP：{probe(base, px)}")
    else:
        out("   未检测到系统代理")
    out()

    # ★ ④b 常见本地代理端口（2026-10-24 加）
    #   组员实测：系统代理是空的，但 Edge 能开网站 →
    #   代理软件在跑，只是**没开「系统代理」开关** → 只能扫端口。
    out("④b 本机常见代理端口（Clash 7890 / v2rayN 10809 / …）")
    locals_ = []
    try:
        from . import netdiag
        locals_ = netdiag.local_proxy_candidates()
    except Exception:                                 # noqa: BLE001
        try:
            import netdiag                            # type: ignore
            locals_ = netdiag.local_proxy_candidates()
        except Exception:                             # noqa: BLE001
            locals_ = []
    if locals_:
        out(f"   发现 {len(locals_)} 个在监听：{', '.join(locals_)}")
        for lx in locals_:
            out(f"   经 {lx} HTTP：{probe(base, lx)}")
    else:
        out("   没发现常见代理端口在监听")
    out()

    # ---- ⑤ 本机助手（网页要靠它才能启动 agent）----
    try:
        from . import netdiag
        hport = netdiag.local_helper_port()
    except Exception:                                 # noqa: BLE001
        try:
            import netdiag                            # type: ignore
            hport = netdiag.local_helper_port()
        except Exception:                             # noqa: BLE001
            hport = 8766
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{hport}/status", timeout=4) as r:
            out(f"   ✓ 助手在跑（127.0.0.1:{hport}）")
    except Exception:                                 # noqa: BLE001
        out(f"   ✗ 助手没在跑（127.0.0.1:{hport}）→ 双击包里的「启动助手.cmd」")
        out(f"     （端口在包里的 helper-port.txt 里改；网页那边也要用同一个端口）")
    out()

    # ---- 结论 ----
    out("=" * 66)
    out("  结论")
    out("=" * 66)
    helper_up = False
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{hport}/status", timeout=4):
            helper_up = True
    except Exception:                                 # noqa: BLE001
        helper_up = False

    # 找出**真的能通**的那条代理路径
    working_proxy = ""
    for cand in ([px] if px else []) + list(locals_):
        if cand and "✓" in probe(base, cand):
            working_proxy = cand
            break

    if "✓" in direct:
        out("★ 控制面**直连就是通的** —— agent 应该能连上。")
        if not helper_up:
            out("  但**本机助手没在跑** → 双击包里的「启动助手.cmd」")
            out("  （网页要靠助手才能启动 agent；助手起来后网页才能看到日志）")
        else:
            out("  助手也在跑 → 回网页点「启动本机 agent」即可。")
    elif working_proxy:
        out("★ 直连不通，但**经代理是通的**！")
        out("  → 你的网络需要走代理。启动 agent 时指定：")
        out(f"     python -m msyt agent --server {base} --proxy {working_proxy}")
        out("  （新版 agent 会自动扫这些端口，通常不用手动加）")
    else:
        # ★ 分类：RST / DNS / 超时 …… 结论必须不同（2026-10-24）
        kind, advice = "未知原因", ""
        try:
            from . import netdiag
            if LAST_ERROR is not None:
                kind, advice = netdiag.classify_error(LAST_ERROR)
        except Exception:                             # noqa: BLE001
            try:
                import netdiag                        # type: ignore
                if LAST_ERROR is not None:
                    kind, advice = netdiag.classify_error(LAST_ERROR)
            except Exception:                         # noqa: BLE001
                pass
        out("★ 直连和代理**都不通** —— 你的网络到不了这个服务器。")
        out(f"  原因分类：**{kind}**")
        if advice:
            out(f"  {advice}")
        out()
        out("  请按顺序试：")
        out("   ① 如果你开着代理软件（Clash / v2rayN / 加速器）：")
        out("      - 打开它的「系统代理」开关，再重跑本程序；或")
        out("      - 手动指定端口（Clash 一般是 7890）：")
        out(f"        python -m msyt agent --server {base} --proxy http://127.0.0.1:7890")
        out("   ② 换网络试一下（手机热点最省事）。")
        out("   ③ 公司/校园网可能限制境外域名 —— 这种情况必须走代理。")
        out("   ④ 仍然不通的话，把这份输出发给管理员（账号和凭证都没问题，")
        out("      不用重新下载启动包）。")
    out()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
