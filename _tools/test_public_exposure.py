# -*- coding: utf-8 -*-
"""
test_public_exposure.py —— **公网暴露面**验收（2026-10-26 新增）
================================================================================
背景
----
公网入口是 Cloudflare **quick tunnel**，它上面**没有 Cloudflare Access**
（Access 只能挂在命名隧道的固定 Public Hostname 上）→ 匿名访客能直接摸到
Web 应用。加固前实测匿名能拿到：

  · `/docs` + `/openapi.json` → **58 个接口的完整地图**（含字段名）
  · `/api/health` → **本机绝对路径**（含用户名、盘符、项目名）
  · `/` 首页 → **采集目标站名**（天一论坛 · 群众留言板）
  · `/api/auth/login` → **不限速**，可无限撞库
  · 登录失败提示会区分「不在名单 / 待审核 / 已停用」→ 可**枚举成员邮箱**
  · 登录限速取 `request.client.host` → 经隧道进来**恒为 127.0.0.1**
    → 全网访客共用一个计数器（外人撞 10 次，全组被锁）

本脚本把这些逐条钉成回归项。

★ 怎么模拟「公网请求」
  不用真打公网地址（隧道地址会变）—— **伪造 Host 头**即可：
  服务端判「是不是本机」看的正是 Host（`127.0.0.1` vs 域名），
  伪造 Host = 走的是**和公网完全相同**的那条代码路径。

用法：python _tools/test_public_exposure.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

LOCAL = "http://127.0.0.1:8765"
FAKE_HOST = "probe-exposure.trycloudflare.com"
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """**不跟随重定向** —— 否则 302→/gate 会被 urllib 自动跟掉，
    断言时看到的是 200（/gate 的 200），看起来像「没拦住」。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def req(path, body=None, headers=None, host=FAKE_HOST, method=None):
    """打本机服务，但**把 Host 换掉** → 走公网那条路径。

    返回 (status, body_text)
    """
    h = {"User-Agent": "msyt-agent/1.1"}
    if host:
        h["Host"] = host
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(LOCAL + path, data=data,
                               method=method or ("POST" if data else "GET"))
    for k, v in h.items():
        r.add_header(k, v)
    try:
        with _OPENER.open(r, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                    # noqa: BLE001
        return 0, str(e)


def main() -> int:
    print("=" * 78)
    print("公网暴露面验收（伪造 Host 头 = 走公网代码路径）")
    print("=" * 78)

    # ---------------------------------------------------------------- ① 指纹
    st, body = req("/api/health")
    check("① /api/health 匿名可探活（隧道守护要靠它）", st == 200, f"status={st}")
    check("① /api/health 公网**不泄露** db_path", "db_path" not in body, body[:80])
    check("① /api/health 公网**不泄露** 本机绝对路径",
          "Deskbox" not in body and ":\\" not in body, body[:80])
    check("① /api/health 公网**不泄露** 条数/版本号",
          "threads" not in body and "version" not in body, body[:80])

    st, _ = req("/docs")
    check("① Swagger /docs 公网关闭（404）", st == 404, f"status={st}")
    st, _ = req("/redoc")
    check("① ReDoc /redoc 公网关闭（404）", st == 404, f"status={st}")
    st, body = req("/openapi.json")
    check("① openapi.json 公网关闭（404，接口地图不外泄）",
          st == 404 and "paths" not in body, f"status={st}")

    st, body = req("/")
    if st == 200:
        check("① 首页匿名不含**采集目标站名**",
              "天一" not in body and "留言板" not in body, "")
    else:
        # 开了口令门时首页会 302 到 /gate（更安全）
        check("① 首页匿名被口令门拦下（302 → /gate）", st == 302, f"status={st}")

    # ------------------------------------------------------------ ② 鉴权边界
    for path in ("/api/stats", "/api/members", "/api/threads", "/api/jobs",
                 "/api/integrity"):
        st, _ = req(path)
        check(f"② {path} 匿名 → 401（数据不外泄）", st == 401, f"status={st}")

    for ep in ("pick", "submit", "return", "login"):
        st, _ = req(f"/api/agent/{ep}", body={})
        check(f"② /api/agent/{ep} 匿名 → 401（agent 写接口不裸奔）",
              st == 401, f"status={st}")

    st, _ = req("/api/agent/endpoints")
    check("② /api/agent/endpoints 仍可达（组员 agent 的换址自愈不能被堵）",
          st == 200, f"status={st}")

    # ---------------------------------------------------------- ③ 本机不受影响
    st, body = req("/api/health", host="127.0.0.1:8765")
    check("③ 本机访问 /api/health 仍拿得到完整信息（db_path 在）",
          st == 200 and "db_path" in body, f"status={st}")
    st, _ = req("/api/health", host="localhost:8765")
    check("③ localhost 同样按本机处理", st == 200, f"status={st}")

    # -------------------------------------------------------------- ④ 登录加固
    fake_ip = {"CF-Connecting-IP": "203.0.113.77"}
    st, body = req("/api/auth/login",
                   body={"email": "no-such-member@example.com", "password": "x"},
                   headers=fake_ip)
    check("④ 登录失败提示**统一**（不泄露邮箱是否在名单里）",
          st == 401 and "邮箱或密码不对" in body, f"{st} {body[:60]}")

    st, _ = req("/api/auth/login",
                body={"email": "no-such-member@example.com", "password": "x"},
                headers={"CF-Connecting-IP": "203.0.113.78"})
    check("④ 不同 IP 各自计数（限速按真实客户端 IP，不是 127.0.0.1）",
          st == 401, f"status={st}")

    # 同一 IP 连撞 → 应触发 429
    last = 0
    for _ in range(12):
        last, _ = req("/api/auth/login",
                      body={"email": "no-such-member@example.com", "password": "x"},
                      headers={"CF-Connecting-IP": "203.0.113.99"})
        if last == 429:
            break
    check("④ 同一 IP 连续失败 → 触发限速 429", last == 429, f"status={last}")

    # ---------------------------------------------------------- ⑤ 口令门（可选）
    key_file = os.path.join(PROG, "data", "gate-key.txt")
    gate_on = os.path.isfile(key_file)
    if not gate_on:
        check("⑤ 口令门未启用（data/gate-key.txt 不存在）→ 属预期状态", True, "")
    else:
        key = open(key_file, encoding="utf-8").read().strip()
        st, body = req("/api/stats")
        check("⑤ 口令开启时 /api/stats 匿名 → 401「需要访问口令」",
              st == 401 and "需要访问口令" in body, f"{st} {body[:40]}")
        st, _ = req("/", headers={"X-Msys-Gate": key})
        check("⑤ 带正确口令可进入首页", st == 200, f"status={st}")
        st, _ = req("/", headers={"X-Msys-Gate": "wrong"})
        check("⑤ 错误口令进不去首页（302 → /gate）", st == 302, f"status={st}")
        st, _ = req("/static/style.css", headers={"X-Msys-Gate": "wrong"})
        check("⑤ 静态资源也要口令（不裸奔）", st == 302, f"status={st}")
        st, _ = req("/api/agent/endpoints")
        check("⑤ 口令门**不拦**组员 agent 的换址自愈通道", st == 200, f"status={st}")
        st, body = req("/api/auth/gate", body={"key": key})
        check("⑤ 口令校验接口认口令", st == 200 and '"ok"' in body, f"{st}")
        st, _ = req("/api/auth/gate", body={"key": "wrong"})
        check("⑤ 口令校验接口拒错口令", st == 401, f"status={st}")

    print("\n" + "=" * 78)
    npass = sum(1 for _, ok, _ in results if ok)
    nfail = len(results) - npass
    for name, ok, extra in results:
        if not ok:
            print(f"  FAIL {name}  {extra}")
    print(f"共 {len(results)} 项 · 通过 {npass} · 失败 {nfail}")
    print("=" * 78)
    return 0 if nfail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
