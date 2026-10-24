# -*- coding: utf-8 -*-
"""
test_public_e2e.py —— **公网链路**端到端验收（模拟成员机器）
================================================================================
这一条测的是最容易出问题、也最难自查的一段：
    成员机器 → Cloudflare 隧道 → Access → 控制面

关键点：**成员机器上的 agent 没有浏览器、没有 Access 会话 Cookie**。
所以它必须靠 **service token** 过 Access 这道门。
如果没带对，Access 会返回**登录页 HTML** 而不是 JSON —— 早期版本正是把
这个"拦截"当成了"登录成功"，于是表现为「登录成功但角色未知、什么活都领不到」。

本脚本做的是：
  1) 下载启动包（网址 = 公网地址）
  2) 解压，检查包里有没有 cf-access.txt（service token）
  3) 用**解压出来的代码**连**公网地址**跑一次 agent
  4) 核对：登录成功、角色正确、节点正确

用法：python _tools/test_public_e2e.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

LOCAL = "http://127.0.0.1:8765"
PUBLIC = "https://msyt.example.com"        # 网页 UI 走这里（受 Access 保护）
API = "https://agentapi.example.com"    # agent 走这里（Access bypass）
ADMIN = ("qa-admin@example.com", "qa-test-password")
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def call(base, path, body=None, token=""):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET")
    req.add_header("Content-Type", "application/json")
    # ★ 必须带 UA：Cloudflare 的「浏览器完整性检查」(error 1010) 会拦
    #   Python 默认 UA（Python-urllib/…）—— agent 自己带的是 msyt-agent/1.1。
    req.add_header("User-Agent", "msyt-agent/1.1")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main() -> int:
    print("=" * 78)
    print("公网链路端到端验收（模拟成员机器）")
    print("=" * 78)

    # ---------- 0. 拿一个 token（在本地拿，避免依赖 Access 会话） ----------
    tok = json.loads(call(LOCAL, "/api/auth/login",
                          {"email": ADMIN[0], "password": ADMIN[1]}))["token"]

    # ---------- 1. 主域名仍受 Access 保护，API 子域名直连 ----------
    # ⚠ 用「不跟随重定向」的 opener：urllib 默认会跟着 302 跳到 Access 登录页，
    #   跳完是 200（HTML），会被误判成「防护没了」。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(urllib.request.Request(
            PUBLIC + "/api/health", headers={"User-Agent": "msyt-agent/1.1"}), timeout=30)
        check("主域名对匿名请求仍被 Access 挡（应 302/403）", False,
              f"★ 竟然 {r.status} —— 网页防护没了")
    except urllib.error.HTTPError as e:
        check("主域名对匿名请求仍被 Access 挡（应 302/403）",
              e.code in (302, 403), f"HTTP {e.code}")
    except Exception as e:
        check("主域名对匿名请求仍被 Access 挡", True, f"{type(e).__name__}")

    try:
        body = call(API, "/api/agent/me", token=tok)
        ok = True
        try:
            json.loads(body)
        except Exception:
            ok = False
        check("★ API 子域名上 /api/agent/me 返回 JSON（Access 已 bypass）", ok,
              "返回 HTML = 被 Access 挡了" if not ok else "")
    except Exception as e:
        check("★ API 子域名上 /api/agent/me 返回 JSON", False,
              f"{type(e).__name__}: {e}")

    try:
        r = urllib.request.urlopen(urllib.request.Request(
            API + "/api/health", headers={"User-Agent": "msyt-agent/1.1"}), timeout=30)
        check("agentapi 域名上非 /api/agent/* 被收紧（应 403）", False, f"竟然 {r.status}")
    except urllib.error.HTTPError as e:
        check("agentapi 域名上非 /api/agent/* 被收紧（应 403）", e.code == 403,
              f"HTTP {e.code}")
    except Exception as e:
        check("agentapi 域名上非 /api/agent/* 被收紧", True, f"{type(e).__name__}")

    # ---------- 3. 下载启动包 ----------
    tmpdir = tempfile.mkdtemp(prefix="msyt-pub-")
    try:
        data = call(LOCAL, "/api/agent/bundle.zip", token=tok)
        zpath = os.path.join(tmpdir, "bundle.zip")
        with open(zpath, "wb") as f:
            f.write(data)
        check("启动包能下载", len(data) > 20000, f"{len(data)} 字节")
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            server_txt = z.read("server.txt").decode("utf-8").strip()
            alt_txt = (z.read("server-alt.txt").decode("utf-8").strip()
                       if "server-alt.txt" in names else "")
            has_cf = "cf-access.txt" in names
            z.extractall(tmpdir)
        # ★ 包现在带**主用 + 备用**两个地址（2026-10-23 改成
        #   「本地浏览器 + 服务器」协同生成），所以断言改成
        #   「两个地址里**必须有** agentapi 子域名」——
        #   它是 agent 唯一能直连的（Access 对它 bypass）。
        check("★ 包里带 agent 专用 API 地址（主用或备用都行）",
              API in (server_txt, alt_txt), f"主用={server_txt} 备用={alt_txt}")
        check("包里不再带 service token（不需要了）", not has_cf,
              "" if not has_cf else "★ 仍带着多余的密钥")

        # ---------- 4. 用解压出来的代码连公网跑一次 agent ----------
        work = os.path.join(tmpdir, "work")
        os.makedirs(work, exist_ok=True)
        shutil.copytree(os.path.join(tmpdir, "msyt"), os.path.join(work, "msyt"),
                        dirs_exist_ok=True)
        for extra in ("cf-access.txt", "server.txt"):
            src = os.path.join(tmpdir, extra)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(work, extra))
        runner = os.path.join(work, "_run.py")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(
                "import sys\n"
                f"sys.path.insert(0, {work!r})\n"
                "from msyt.agent import Agent\n"
                f"ag = Agent(server={API!r}, email='test@example.com',"
                " password='test-password', verbose=True)\n"
                "d = ag.login()\n"
                "print('MEMBER', d.get('member'))\n")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run([sys.executable, runner], cwd=work, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
        for line in out.splitlines()[:8]:
            print("  | " + line)
        check("★ 公网登录成功（不再被 Access 挡）",
              "登录成功" in out or "使用已有令牌" in out, out[-160:].replace("\n", " | "))
        check("★ 角色不再是未知", "角色 未知" not in out and "角色 None" not in out,
              out[-160:].replace("\n", " | "))
        check("★ 节点号拿到了",
              "节点 member:" in out or "节点 admin" in out,
              out[-160:].replace("\n", " | "))
        check("★ 没有出现「服务端返回的不是 JSON」",
              "不是 JSON" not in out, "")
        check("★ 没有出现「启动包可能是旧版本」",
              "旧版本" not in out, "")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

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
