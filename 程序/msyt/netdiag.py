# -*- coding: utf-8 -*-
"""成员端网络诊断的共用件：代理发现 / 错误分类 / 地址族回落。

★ 为什么单独抽一个模块（2026-10-24 组员实测）：
  组员的现象是 `ConnectionResetError(10054)`（TCP 连上后被**中途重置**），
  而且「Edge 能打开网站、Python 却连不上」，系统代理里**什么都没有**。

  三条硬教训：
  ① **系统代理 ≠ 所有代理**。Clash / v2rayN / sing-box 常常**只代理浏览器**
     （系统代理开关没开、或用 PAC/扩展），注册表里查不到，
     但 127.0.0.1 上的代理端口**确实在监听** → 必须**扫端口**。
  ② **「连不上」和「连上被重置」是两回事**：
     RST(10054) = 链路建立后被中途掐断 → 典型是本地网络/运营商/校园网拦截，
     或 IPv6 路由坏了；**不是**服务端挂了。结论必须分开给。
  ③ **IPv6 路由坏掉**很常见：TCP 层 IPv6 能连（握手成功），
     但传输中途被重置 → 这时**强制走 IPv4** 就能通。
"""
from __future__ import annotations

import socket
from typing import List, Optional, Tuple

#: 常见的本地代理端口（HTTP 代理形态）
#: Clash / Mihomo 7890、Clash Verge 7897、v2rayN 10809、sing-box 2080 ……
COMMON_PROXY_PORTS: List[int] = [
    7890, 7891, 7892, 7897, 7899, 1080, 1081, 10808, 10809, 10810,
    8080, 8888, 20171, 33210, 4780, 4781,
]

_UA = "msyt-netdiag/1.0"


# ---------------------------------------------------------------- 代理
def sys_proxy() -> str:
    """Windows **系统代理**（Edge 读的就是这套注册表）。没有就返回空串。"""
    try:
        import urllib.request
        p = urllib.request.getproxies()
    except Exception:                                 # noqa: BLE001
        p = {}
    for k in ("https", "http", "all"):
        if p.get(k):
            return str(p[k])
    return ""


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        return s.connect_ex((host, port)) == 0
    except Exception:                                 # noqa: BLE001
        return False
    finally:
        try:
            s.close()
        except Exception:                             # noqa: BLE001
            pass


def local_proxy_candidates(ports: Optional[List[int]] = None) -> List[str]:
    """扫**常见本地代理端口**，只返回**真的在监听**的那些。

    ★ 这是「Edge 能开、Python 连不上」最常见的一环：
      代理软件在跑，但系统代理开关没开 → 注册表查不到 → 只能靠扫端口。
    """
    out = []
    for p in (ports if ports is not None else COMMON_PROXY_PORTS):
        if _port_listening(p):
            out.append(f"http://127.0.0.1:{p}")
    return out


# ---------------------------------------------------------------- 错误分类
def classify_error(exc: BaseException) -> Tuple[str, str]:
    """把一个网络异常翻译成「类别 + 该做什么」。

    返回 (类别, 结论+动作)。
    """
    name = type(exc).__name__
    txt = str(exc)
    low = txt.lower()

    # ① 连接被重置（RST）—— 最难认的一种
    if isinstance(exc, ConnectionResetError) or "10054" in txt \
            or "connection reset" in low or "connection aborted" in low:
        return ("连接被重置(RST)",
                "TCP 连上了但被**中途掐断**。这几乎都是**你这边网络**的问题"
                "（运营商/校园网拦截、或 IPv6 路由坏了），不是服务器的问题。")

    # ② 域名解析不了
    if "name or service not known" in low or "nodename nor servname" in low \
            or "getaddrinfo" in low or "temporary failure in name resolution" in low:
        return ("域名解析失败",
                "连域名都解析不出来 → 检查 DNS，或换网络（手机热点）再试。")

    # ③ 主动拒绝
    if isinstance(exc, ConnectionRefusedError) or "connection refused" in low:
        return ("对方拒绝连接",
                "目标明确拒绝 → 端口/服务不对，或对方服务没起。")

    # ④ 超时
    if isinstance(exc, socket.timeout) or "timed out" in low or "timeout" in low:
        return ("连接超时",
                "连出去没回音 → 网络被限速/丢包，或被墙。")

    # ⑤ TLS 阶段断
    if "ssl" in low or "certificate" in low or "handshake" in low:
        return ("TLS/证书阶段失败",
                "连上了但 HTTPS 握手失败 → 常见于中间设备劫持或证书不信任。")

    # ⑥ 兜底
    return (name, txt[:160])


# ---------------------------------------------------------------- 地址族
def tcp_ok(host: str, port: int, family: int, timeout: float = 6.0) -> Tuple[bool, str]:
    """测某个地址族的 TCP 能不能连上。返回 (通不通, 错误描述)。"""
    try:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    except Exception as e:                             # noqa: BLE001
        return (False, type(e).__name__)
    if not infos:
        return (False, "无解析结果")
    last = ""
    for fam, _t, _p, _c, sa in infos:
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(sa)
            s.close()
            return (True, sa[0])
        except Exception as e:                         # noqa: BLE001
            last = f"{type(e).__name__}"
            try:
                s.close()
            except Exception:                         # noqa: BLE001
                pass
    return (False, last)


# ---------------------------------------------------------------- 强制 IPv4
_orig_getaddrinfo = socket.getaddrinfo
_IPV4_ONLY = False


def set_ipv4_only(on: bool) -> None:
    """强制 socket 只解析 IPv4（默认关闭）。

    ★ 什么时候需要：TCP 层 IPv6「连得上」但传输被重置 ——
      很多国内宽带的 IPv6 出口能握手、不能传数据。
      这时把解析结果限成 IPv4 就能通。
    """
    global _IPV4_ONLY
    if on == _IPV4_ONLY:
        return
    _IPV4_ONLY = on
    if on:
        def _ga(host, port, family=0, type=0, proto=0, flags=0):  # type: ignore
            try:
                return _orig_getaddrinfo(host, port, socket.AF_INET,
                                         type, proto, flags)
            except socket.gaierror:
                return _orig_getaddrinfo(host, port, family, type, proto, flags)
        socket.getaddrinfo = _ga                        # type: ignore[assignment]
    else:
        socket.getaddrinfo = _orig_getaddrinfo          # type: ignore[assignment]


def ipv4_only() -> bool:
    return _IPV4_ONLY


# ---------------------------------------------------------------- 小助手端口
def local_helper_port(default: int = 8766) -> int:
    """本机小助手监听端口。

    ★ 顺序：环境变量 `MSYT_HELPER_PORT` → 包里的 `helper-port.txt` → 默认值。
      （包里的那个文件是**成员自己**改端口的地方：改完保存 → 重启助手即可。）
    """
    import os
    v = os.environ.get("MSYT_HELPER_PORT", "").strip()
    if v.isdigit() and 1024 <= int(v) <= 65535:
        return int(v)
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.dirname(here), os.getcwd()):
        p = os.path.join(d, "helper-port.txt")
        try:
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    n = int(line)
                    if 1024 <= n <= 65535:
                        return n
                    break
        except Exception:                             # noqa: BLE001
            continue
    return default


# ---------------------------------------------------------------- 探测
def looks_reachable(r) -> bool:
    """这个 HTTP 响应算不算「**控制面可达**」。

    ★ 为什么不能「有响应就算通」（2026-10-24 修）：
      代理连不上源站时，**代理自己**会回一个 502/504 的 HTML 错误页；
      本机某个**根本不是代理**的服务（开发服务器等）也可能回 404/200。
      只看「有没有收到响应」→ 把这些当成「可达」
      → agent 一头撞进死路还自以为通了。
      现在只认：
        · **401/403** —— 我们源站自己的「没带令牌」→ 路径确实通
          （`/api/agent/me` 没带令牌时**必定**是 401，这是接口契约）
        · 2xx 且响应是 **JSON** —— 也算通
      其它一律**不算**：网关 302 登录页、代理 502、别的服务 404 都不算。
    """
    try:
        code = int(getattr(r, "status_code", 0))
    except Exception:                                 # noqa: BLE001
        return False
    if code in (401, 403):
        return True
    if 200 <= code < 300:
        try:
            body = r.json()
        except Exception:                             # noqa: BLE001
            return False
        return isinstance(body, (dict, list))
    return False


def probe_http(base: str, proxy: str = "", timeout: float = 20.0) -> Tuple[bool, str]:
    """探一次控制面。返回 (通不通, 说明)。"""
    try:
        import requests
    except ImportError:
        return (False, "没装 requests")
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    try:
        r = s.get(base + "/api/agent/me", timeout=timeout)
        if looks_reachable(r):
            return (True, f"HTTP {r.status_code}")
        return (False, f"HTTP {r.status_code}（网关/代理的错误页，不是源站）")
    except Exception as e:                             # noqa: BLE001
        kind, _ = classify_error(e)
        return (False, kind)
