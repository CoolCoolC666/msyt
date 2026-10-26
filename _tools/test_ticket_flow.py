# -*- coding: utf-8 -*-
"""
test_ticket_flow.py —— 「网站登录 → 签发一次性凭证 → 组员免密开跑」全链路
================================================================================
这是用户要的那条主线：
    网站（Access 邮箱验证码 + 程序账号密码）→ 信任一天
      → 签发一次性启动凭证（30 分钟、只能用一次）
      → 组员下载启动包、解压、双击 → **不用输密码**，直接开跑

要验的：
  1) 凭证只能在**登录之后**签发（未登录 → 401）—— 这就是「配合网站登录」
  2) 凭证只能用一次（第二次兑换 → 401）
  3) 凭证过期/作废后，agent 会**自动退回**问账号密码（不会卡死）
  4) 带凭证的 agent 连**公网**能登录成功、角色/节点正确
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

LOCAL = "http://127.0.0.1:8765"
API = "https://agentapi.example.com"
ADMIN = ("qa-admin@example.com", "qa-test-password")
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def call(base, path, body=None, token="", method=None):
    m = method or ("POST" if body is not None else "GET")
    req = urllib.request.Request(base + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method=m)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "msyt-agent/1.1")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def main() -> int:
    print("=" * 78)
    print("启动凭证（一次性票据）全链路验收")
    print("=" * 78)

    # ★ 公网不可达 → 跳过（2026-10-26）
    #   本机所在网络若拦了该域名的 TLS 握手，票据兑换就无从谈起 ——
    #   那是网络环境的问题，不是「票据流程坏了」。
    from net_probe import skip_if_unreachable
    skip_if_unreachable()

    st, d = call(LOCAL, "/api/auth/login",
                 {"email": ADMIN[0], "password": ADMIN[1]})
    tok = d.get("token", "")

    # ---------- 1. 未登录不能签发 ----------
    st, d = call(LOCAL, "/api/agent/ticket", {}, method="POST")
    check("★ 未登录签发凭证 → 401（凭证必须配合网站登录）", st == 401, f"HTTP {st}")

    # ---------- 2. 登录后能签发 ----------
    st, d = call(LOCAL, "/api/agent/ticket", {}, token=tok)
    check("登录后能签发凭证", st == 200 and d.get("ticket"), f"HTTP {st}")
    ticket = d.get("ticket", "")
    check("凭证带过期时间", d.get("expires_in") == 1800, str(d.get("expires_in")))

    # ---------- 3. 凭证只能用一次 ----------
    st, d = call(API, "/api/agent/redeem", {"ticket": ticket})
    check("★ 第一次兑换成功", st == 200 and d.get("token"), f"HTTP {st}")
    st2, d2 = call(API, "/api/agent/redeem", {"ticket": ticket})
    check("★ 第二次兑换被拒（一次性）", st2 == 401, f"HTTP {st2}")

    # ---------- 4. 带凭证的 agent 连公网跑（免密） ----------
    tmpdir = tempfile.mkdtemp(prefix="msyt-tk-")
    try:
        # bundle.zip 是二进制，单独用 urllib 取
        req = urllib.request.Request(LOCAL + "/api/agent/bundle.zip")
        req.add_header("Authorization", "Bearer " + tok)
        with urllib.request.urlopen(req, timeout=60) as r:
            blob = r.read()
        zpath = os.path.join(tmpdir, "b.zip")
        with open(zpath, "wb") as f:
            f.write(blob)
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            check("★ 包里有 agent-ticket.txt（免密凭证）", "agent-ticket.txt" in names,
                  "" if "agent-ticket.txt" in names else str(names))
            z.extractall(tmpdir)
        work = os.path.join(tmpdir, "work")
        os.makedirs(work, exist_ok=True)
        shutil.copytree(os.path.join(tmpdir, "msyt"), os.path.join(work, "msyt"),
                        dirs_exist_ok=True)
        for extra in ("agent-ticket.txt", "server.txt"):
            src = os.path.join(tmpdir, extra)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(work, extra))
        runner = os.path.join(work, "_run.py")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(
                "import sys\n"
                f"sys.path.insert(0, {work!r})\n"
                "from msyt.agent import Agent\n"
                # ★ 不给 email / password / token —— 只有包里的票据
                f"ag = Agent(server={API!r}, verbose=True)\n"
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
        check("★ agent 自动用票据登录（没问密码）",
              "凭证已兑换" in out, out[-160:].replace("\n", " | "))
        check("★ 公网登录成功、角色节点正确",
              "角色 admin" in out and "节点 admin" in out,
              out[-160:].replace("\n", " | "))
        check("没有问密码", "密码" not in out, "")

        # ---------- 5. 凭证作废 → 明确报「不可用」，交回 CLI 去问密码 ----------
        with open(os.path.join(work, "agent-ticket.txt"), "w", encoding="utf-8") as f:
            f.write("this-ticket-is-already-used\n")
        runner2 = os.path.join(work, "_run2.py")
        with open(runner2, "w", encoding="utf-8") as f:
            f.write(
                "import sys\n"
                f"sys.path.insert(0, {work!r})\n"
                "from msyt.agent import Agent, AgentError\n"
                f"ag = Agent(server={API!r}, verbose=True)\n"
                "try:\n"
                "    ag.login()\n"
                "    print('NO_ERROR')\n"
                "except AgentError as e:\n"
                "    print('NEED_CREDS', getattr(e, 'need_credentials', False), e)\n")
        p2 = subprocess.run([sys.executable, runner2], cwd=work, env=env,
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180)
        out2 = (p2.stdout or "") + (p2.stderr or "")
        for line in out2.splitlines()[:6]:
            print("  | " + line)
        check("★ 凭证作废 → 报「不可用」并要求账号密码",
              "NEED_CREDS True" in out2 and "凭证不可用" in out2,
              out2[-160:].replace("\n", " | "))
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
