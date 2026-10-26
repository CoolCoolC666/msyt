# -*- coding: utf-8 -*-
"""net_probe.py —— 公网可达性探测（给那些**必须走公网**的测试用）

★ 为什么要有这个（2026-10-25）
  实测踩过：本机所在网络把 `*.example-tld` 的 TLS 握手**重置**了
  （SSL Labs 从外部看该域名一切正常 → Cloudflare 侧没问题）
  → 于是 `test_public_e2e` / `test_pack_credential` 这类
  **必须走公网**的测试集体失败。

  ★ 但「公网不通」**不是被测对象坏了** —— 把它算作 FAIL 会让回归报告
    变成噪音，真正的回归问题反而被淹没。
  → 规矩：**环境不满足 → 明确跳过并写清原因；被测对象坏了 → 才算失败。**
"""
from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request

#: agent 唯一能直连的公网域名（Access 对它 bypass）
AGENT_API = "https://agentapi.example.com"
#: 网页 UI 的公网域名（受 Access 保护）
PUBLIC = "https://msyt.example.com"


def probe(url: str = AGENT_API, timeout: int = 15) -> tuple[bool, str]:
    """探测某个公网地址是否可达。返回 (可达, 说明)。

    ★ 判据要看**响应体**，不能只看 HTTP 状态码：
      Cloudflare Access 的登录页也是 **200**，
      拿到它不代表「我们的服务在回话」。
    """
    req = urllib.request.Request(url + "/api/health",
                                 headers={"User-Agent": "msyt-agent/1.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
            if "Sign in" in body or "Cloudflare Access" in body:
                return False, f"拿到的是 Access 登录页（不是我们的服务）"
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        # 403/404 也说明**请求到了我们的程序**（它自己在挡）→ 链路是通的
        return True, f"HTTP {e.code}（程序自己的鉴权/路由在挡 → 链路通）"
    except ssl.SSLError as e:
        return False, f"TLS 握手失败：{e}"
    except ConnectionResetError:
        return False, "连接被重置（网络侧拦了该域名的 TLS 握手）"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ConnectionResetError):
            return False, "连接被重置（网络侧拦了该域名的 TLS 握手）"
        return False, f"{type(reason).__name__}: {reason}"
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def skip_if_unreachable(url: str = AGENT_API) -> None:
    """不可达就打印原因并**正常退出**（跳过 ≠ 失败）。"""
    ok, why = probe(url)
    if ok:
        print(f"公网可达（{why}）→ 继续跑公网验收")
        return
    print("=" * 70)
    print("⏭ 跳过：公网不可达 —— 这不是被测对象的问题")
    print(f"   探测：{url}")
    print(f"   原因：{why}")
    print("   判据提示：Cloudflare Access 的登录页也是 HTTP 200，")
    print("             所以「能拿到响应」≠「服务在回话」，要连响应体一起看。")
    print("=" * 70)
    raise SystemExit(0)


if __name__ == "__main__":
    for u in (AGENT_API, PUBLIC):
        ok, why = probe(u)
        print(f"{u} → {'可达' if ok else '不可达'}：{why}")
