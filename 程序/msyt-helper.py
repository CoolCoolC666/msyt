#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
msyt-helper.py —— 成员端**本地助手**（单文件，只用标准库）
================================================================================
它解决的是"命令在成员机器上跑不起来"这件事：
    成员的机器上没有 msyt 这个包 → `python -m msyt agent ...` 报
    `No module named msyt`。

所以助手的作用有两个：
  ① **宿主**：它待在解压出来的目录里，那个目录里就有 msyt 包，
     于是它能 `python -m msyt agent` 把采集器拉起来。
  ② **被网页呼叫**：它在 127.0.0.1:8766 上开一个极小的 HTTP 服务，
     浏览器里的采集页面可以 `fetch('http://127.0.0.1:8766/start')`
     —— 这就是"网页请求访问成员的设备"那条路。

★ 为什么本地助手允许跨域，而站方不允许
  站方**一个 CORS 头都不给**，所以浏览器里的网页不能直接采站方。
  但我们自己的本地助手是自己人：它只绑 127.0.0.1（外网访问不到），
  只接受本机发起的启动/停止/查状态，**不接受任何采集指令**，
  所以放行 CORS 没有风险。

★ 浏览器 → https 页面 → http://127.0.0.1 的两个坑
  ① 混合内容：Chrome 把 127.0.0.1/localhost 视为可信来源，不拦
  ② 私有网络访问（PNA，Chrome 104+）：会先发 OPTIONS 预检，
     服务端必须回 `Access-Control-Allow-Private-Network: true`
     —— 少了这个头，fetch 会直接失败，错误信息还很含糊

用法（一般不用手敲，网站下载的启动包里已经写好了）：
    python msyt-helper.py --server https://...

★ 凭证是怎么来的（2026-10-23 修的坑）
    助手是**常驻**进程，而启动包里那张票据是**一次性**的。
    原先助手把票据写死在命令行里 → 第一次启动用掉之后，
    第二次「启动本机 agent」就再也换不到 JWT → 助手看着在跑，其实没在采。
    现在凭证链是：
        ① 网页点「启动本机 agent」递过来的 JWT（最省事）
        ② 包内 agent-ticket.txt 里的票据 → **当场兑成 JWT 存进内存**，
           之后每次启动都用这个 JWT（JWT 7 天有效，票据只吃一次）
        ③ 账号密码（兜底，走环境变量，不进命令行）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: agent 日志里的四类行（用来累计计数，让网页能显示真实进度）
_RE_PICK = re.compile(r"领走\s+(\d+)\s+帖")
_RE_DONE = re.compile(r"tid=\d+\s+✓")
_RE_FAIL = re.compile(r"✗")
_RE_RESTRICTED = re.compile(r"⊘")

#: 这些占位符由服务端（msyt/bundle.py）在**下载时**替换掉 → 网址是动态的
SERVER = "__MSYT_SERVER__"
TICKET = "__MSYT_TICKET__"

PORT = 8766
STATE = {
    "proc": None,
    "log": deque(maxlen=400),
    "started_at": 0.0,
    "server": SERVER,
    "server_alt": "",     # ★ 备用控制面地址（2026-10-23：包带主/备两个）
    "ticket": "",
    "jwt": "",            # ★ 票据兑来的 JWT（常驻复用，不再重复吃一次性票据）
    "cred_src": "",       # 当前凭证的来源，写进 /status 让人一眼看懂
    "email": "",
    "password": "",
    "agent_state": "",      # agent 自报的状态：collecting / idle / starting
    "agent_state_note": "",
    "agent_state_at": 0.0,
    # ★ 累计计数（助手自己数，**不**受日志窗口长度影响）
    #   日志 deque 只留最近 400 条，面板若从"可见窗口"里正则数，
    #   数字会随窗口滚动而忽大忽小 —— 用户看着像"进度在跳"。
    "stat_picked": 0,
    "stat_done": 0,
    "stat_failed": 0,
    "stat_restricted": 0,
}
LOCK = threading.Lock()

UA = "msyt-helper/1.1"


def log(msg: str) -> None:
    with LOCK:
        STATE["log"].append({"t": time.time(), "m": str(msg)})
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:                       # Windows 控制台 GBK 会打挂 print
        print(msg.encode("utf-8", "replace").decode("ascii", "replace"), flush=True)


# ------------------------------------------------------------------ 与控制面说话
def _api(server: str, path: str, payload: dict, timeout: float = 20.0) -> dict:
    """向控制面发一个 JSON 请求。

    ★ 一定要带**非默认 UA**：Cloudflare 的浏览器完整性检查会拦 Python 默认 UA
      （403 error 1010），那种情况下拿回来的是拦截页而不是数据。
    ★ 收到**非 JSON** 一律当失败 —— 以前把 Access 登录页当成功，
      于是日志显示「登录成功」，实际什么都没干。
    """
    url = server.rstrip("/") + path
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            raise RuntimeError(f"HTTP {e.code}: {json.loads(raw).get('detail') or raw[:120]}")
        except json.JSONDecodeError:
            raise RuntimeError(f"HTTP {e.code}: {raw[:120]}") from None
    except Exception as e:                           # noqa: BLE001
        raise RuntimeError(f"连不上控制面：{e}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("控制面返回的不是 JSON（可能被网关/登录页拦了）") from None


def _auto_update(token: str) -> bool:
    """**自动更新**本机的采集代码（2026-10-23 加）。

    ★ 为什么需要
      启动包里的 `msyt/` 是**代码**，服务端一改，组员手上那份就旧了。
      今天已经让组员重下三次包（失联容忍 → 凭证 30 天 → 主备双地址），
      每一次都要「下载 → 覆盖目录 → 重启助手」，很折腾。
      助手启动 agent 前**自己拉一次最新的** —— 组员以后不用管这件事。

    ★ 只覆盖 `msyt/` 目录 + **msyt-helper.py 自己**，**不动**：
      · `agent-ticket.txt`（凭证，30 天可重复，别覆盖没了）
      · 两个 `.cmd`（用户可能改过）
      · `说明.txt` / `版本.txt`
      只换代码，最安全。

    ★ 为什么连 `msyt-helper.py` 自己也更新（2026-10-23）：
      这是「自举」的关键一环 —— 助手要能更新**自己**，
      才能把「自动更新」这个能力传给下一台机器。
      （Windows 上运行中的 .py 可以被覆盖：Python 已经把源码读进内存了，
        当前进程不受影响，下次启动就是新版。）

    ★ 更新失败**不能挡住采集**：拿不到就用旧代码继续跑。

    返回 True = 更新过（版本变了）。
    """
    if not token:
        return False
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        import urllib.request
        import zipfile
        import io as _io
        req = urllib.request.Request(
            STATE["server"] + "/api/agent/bundle.zip",
            headers={"Authorization": "Bearer " + token,
                     "User-Agent": "msyt-helper/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with zipfile.ZipFile(_io.BytesIO(data)) as z:
            # msyt/ 下的全部 + 助手自己
            targets = [n for n in z.namelist()
                       if n.startswith("msyt/") or n == "msyt-helper.py"]
            if not targets:
                return False
            changed = []
            for name in targets:
                if name.startswith("msyt/"):
                    rel = name[len("msyt/"):]           # msyt/agent.py → agent.py
                    if not rel or rel.endswith("/"):
                        continue
                    # ★ 目标路径要**保留 msyt 这一层**：
                    #   包里是 `msyt/agent.py` → 本地也必须是 `msyt/agent.py`。
                    #   早先写成 os.path.join(here, rel) 把它写到了包根目录，
                    #   结果「更新成功」但真正要更新的文件没动。
                    dst = os.path.join(here, "msyt", rel.replace("/", os.sep))
                else:
                    rel = name                          # msyt-helper.py → 自己
                    dst = os.path.join(here, name)
                try:
                    new = z.read(name)
                    old = b""
                    if os.path.exists(dst):
                        with open(dst, "rb") as f:
                            old = f.read()
                    if old == new:
                        continue
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "wb") as f:
                        f.write(new)
                    changed.append(rel)
                except Exception:                     # noqa: BLE001
                    continue          # 个别文件被占用就跳过，不影响其余
            if changed:
                log(f"已自动更新采集代码（{len(changed)} 个文件）："
                    f"{', '.join(changed[:4])}"
                    + ("…" if len(changed) > 4 else ""))
                return True
    except Exception as e:                            # noqa: BLE001
        log(f"自动更新跳过（{type(e).__name__}）—— 用现有代码继续")
    return False


def _read_ticket_file() -> str:
    """读同目录下的 `agent-ticket.txt`（**一次性**启动凭证）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "agent-ticket.txt")
    try:
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except Exception:                                 # noqa: BLE001
        pass
    return ""


def _ensure_jwt() -> str:
    """保证 STATE["jwt"] 里有可用的 JWT，返回它（拿不到就返回空串）。

    ★ 票据是一次性的：这里**当场兑成 JWT**并缓存住，
      之后助手重启 agent 都用这个 JWT —— 不再重复吃那张一次性票。
    """
    with LOCK:
        if STATE.get("jwt"):
            return STATE["jwt"]
    ticket = (STATE.get("ticket") or "").strip() or _read_ticket_file()
    if not ticket:
        return ""
    try:
        d = _api(STATE["server"], "/api/agent/redeem", {"ticket": ticket})
    except Exception as e:                            # noqa: BLE001
        log(f"启动凭证不可用（{e}）")
        return ""
    tok = str(d.get("token") or "")
    if not tok:
        return ""
    with LOCK:
        STATE["jwt"] = tok
        STATE["cred_src"] = "agent-ticket.txt（已兑换）"
    log("启动凭证已兑换成令牌，之后每次启动都用它")
    return tok


class Handler(BaseHTTPRequestHandler):
    server_version = "msyt-helper/1.0"

    def log_message(self, *a):        # 不刷屏
        pass

    # ---------------------------------------------------------- CORS
    def _cors(self) -> None:
        origin = self.headers.get("Origin") or "*"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        # ★ PNA：Chrome 从 https 页面访问 127.0.0.1 时会先预检，
        #   少了这个头浏览器直接拒绝，而且报的是含糊的网络错误。
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------------------------------------------------- 路由
    def do_GET(self):
        if self.path.startswith("/status"):
            self._json(self.status())
        elif self.path.startswith("/ping"):
            self._json({"ok": True, "server": STATE["server"]})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        body = {}
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:                            # noqa: BLE001
            body = {}
        if self.path.startswith("/start"):
            self._json(self.start(body))
        elif self.path.startswith("/stop"):
            self._json(self.stop())
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    # ---------------------------------------------------------- 动作
    def status(self):
        with LOCK:
            proc = STATE["proc"]
            running = proc is not None and proc.poll() is None
            return {"ok": True, "running": running,
                    "pid": proc.pid if proc else None,
                    "server": STATE["server"],
                    "server_alt": STATE.get("server_alt") or "",
                    "ticket_mode": bool(STATE.get("ticket")),
                    "has_jwt": bool(STATE.get("jwt")),
                    "cred_src": STATE.get("cred_src") or "",
                    # ★ agent 自报状态：让网页能区分「在采 / 在等活 / 卡住了」
                    "agent_state": STATE.get("agent_state") or "",
                    "agent_state_note": STATE.get("agent_state_note") or "",
                    "agent_state_at": STATE.get("agent_state_at") or 0.0,
                    # ★ 累计计数（助手自己数的，不受日志窗口影响）
                    "stat_picked": int(STATE.get("stat_picked") or 0),
                    "stat_done": int(STATE.get("stat_done") or 0),
                    "stat_failed": int(STATE.get("stat_failed") or 0),
                    "stat_restricted": int(STATE.get("stat_restricted") or 0),
                    "uptime": round(time.time() - STATE["started_at"], 1)
                    if STATE["started_at"] else 0,
                    "log": list(STATE["log"])[-80:]}

    def start(self, body=None):
        """启动 agent。

        `body` 里可以带**本机自主采集**的目标（2026-10-23 加）：
          · `sources`  列表页元数据的源（dept:495 / group:func / global…）
          · `tids`     指定帖的正文
          · `pages`    每个源最多采多少页
        带了这些 → 走 `--local` 模式（**采完就退**，不领协作活）；
        不带 → 走常驻的协作领活模式。

        ★ 为什么成员的采集必须在**本机**跑
          站方不提供 CORS → 浏览器不能直采；而在服务器上采 →
          站方看到的流量全来自管理员那一个 IP，
          「多个分散的普通用户」这个 Beta 的核心前提就落空了。
        """
        body = body if isinstance(body, dict) else {}
        token = str(body.get("token") or "")
        sources = str(body.get("sources") or "").strip()
        tids = str(body.get("tids") or "").strip()
        pages = int(body.get("pages") or 20)
        local_mode = bool(sources or tids)
        # ★ 自动更新放在**最前面**（2026-10-23 修）：
        #   原来放在拿到令牌之后、启动 agent 之前 ——
        #   但上面有「agent 还在跑就直接返回」的短路，
        #   于是 agent 没退干净的那次调用**根本走不到更新**。
        #   现在每次 /start 都先更新一次代码（agent 在跑也更新：
        #   Python 已经把源码读进内存了，覆盖 .py 不影响当前进程）。
        if token:
            _auto_update(token)
        with LOCK:
            proc = STATE["proc"]
            if proc is not None and proc.poll() is None:
                return {"ok": True, "already": True, "pid": proc.pid}
        here = os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, "-m", "msyt", "agent",
               "--server", STATE["server"]]
        # ★ 备用控制面地址（2026-10-23）：包由「本地浏览器 + 服务器」协同生成，
        #   带主/备两个地址；agent 启动时自己探，主地址不通就换备用。
        if STATE.get("server_alt"):
            cmd += ["--server-alt", STATE["server_alt"]]
        env = dict(os.environ)
        if local_mode:
            cmd.append("--local")
            if sources:
                cmd += ["--sources", sources]
                cmd += ["--pages", str(max(1, pages))]
            if tids:
                cmd += ["--tids", tids]
        # ① 网页递过来的 JWT（成员刚在网页登录过）→ 最省事，不用任何密码
        tok = token
        src = "网页递过来的令牌" if tok else ""
        if not tok:
            # ② 已兑好的 JWT（常驻复用）or 现兑包内的一次性票据
            tok = _ensure_jwt()
            src = STATE.get("cred_src") or "启动凭证"
            # ★ 没从网页拿到令牌时（走的票据兑换），这里补一次自动更新
            if tok:
                _auto_update(tok)
        if tok:
            cmd += ["--token", tok]
            with LOCK:
                STATE["cred_src"] = src
            # （自动更新已在方法开头做过 —— 那里才能覆盖到「agent 还在跑」的情况）
        else:
            # ③ 兜底：交互输入的账号密码 → 走**环境变量**传给子进程，
            #    不进命令行（命令行在进程列表里是公开的）
            email = STATE.get("email") or ""
            pw = STATE.get("password") or ""
            if email and pw:
                env["MSYT_EMAIL"] = email
                env["MSYT_PASSWORD"] = pw
                with LOCK:
                    STATE["cred_src"] = "账号密码"
            else:
                return {"ok": False,
                        "error": "没有可用凭据：请用「启动助手.cmd」启动（包内有启动凭证），"
                                 "或在网页点「在本机采集」由网页递令牌"}
        try:
            proc = subprocess.Popen(cmd, cwd=here, env=env,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace")
        except Exception as e:                       # noqa: BLE001
            log(f"启动失败：{e}")
            return {"ok": False, "error": str(e)}
        with LOCK:
            STATE["proc"] = proc
            STATE["started_at"] = time.time()
            # 新一轮开始 → 计数归零（同一个助手连续开跑多次时不会叠加）
            STATE["stat_picked"] = 0
            STATE["stat_done"] = 0
            STATE["stat_failed"] = 0
            STATE["stat_restricted"] = 0
        threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
        if local_mode:
            what = []
            if sources:
                what.append(f"列表源 {sources}（{max(1, pages)} 页）")
            if tids:
                n_tid = len([x for x in str(tids).replace("，", ",").split(",")
                             if x.strip()])
                what.append(f"正文 {n_tid} 帖")
            log(f"已启动本机采集（pid {proc.pid}）：{'、'.join(what)}"
                f"（凭证来源：{src or '未知'}）")
        else:
            log(f"已启动 agent（pid {proc.pid}，凭证来源：{src or '未知'}）")
        return {"ok": True, "pid": proc.pid, "cred_src": src, "local": local_mode}

    def _pump(self, proc):
        try:
            for line in proc.stdout:
                line = line.rstrip()
                # ★ agent 状态标记行（2026-10-23 加）
                #   agent 会打 `[agent] [[state]] idle · 池子里暂时没有可领的活…`
                #   助手据此告诉网页「这机是在**等活**，不是**卡住了**」。
                #   没有它，网页只能靠「多久没动静」去猜 ——
                #   于是"等活"被误报成"卡住"（实测踩过）。
                #   ⚠ agent 自己会给每行加 "[agent] " 前缀，
                #     所以要先剥掉前缀再判断标记。
                bare = line
                if bare.startswith("[agent] "):
                    bare = bare[len("[agent] "):].lstrip()
                if bare.startswith("[[state]]"):
                    body = bare[len("[[state]]"):].strip()
                    st = body.split("·")[0].strip() or "unknown"
                    note = body.split("·", 1)[1].strip() if "·" in body else ""
                    with LOCK:
                        STATE["agent_state"] = st
                        STATE["agent_state_note"] = note
                        STATE["agent_state_at"] = time.time()
                    continue
                # ★ 累计计数：领了多少 / 完成了多少 / 失败 / 站方限制
                #   自己数，不从"可见日志窗口"里数 —— 窗口会滚动，
                #   数字跟着跳就看不出真实进度了。
                m_pick = _RE_PICK.search(bare)
                if m_pick:
                    with LOCK:
                        STATE["stat_picked"] += int(m_pick.group(1))
                if _RE_DONE.search(bare):
                    with LOCK:
                        STATE["stat_done"] += 1
                if _RE_RESTRICTED.search(bare):
                    with LOCK:
                        STATE["stat_restricted"] += 1
                elif _RE_FAIL.search(bare):
                    with LOCK:
                        STATE["stat_failed"] += 1
                log("[agent] " + line)
        except Exception:                            # noqa: BLE001
            pass
        finally:
            log("agent 已退出")
            with LOCK:
                STATE["agent_state"] = ""
                STATE["agent_state_at"] = 0.0

    def stop(self):
        with LOCK:
            proc = STATE["proc"]
        if proc is None or proc.poll() is not None:
            return {"ok": True, "running": False}
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:                            # noqa: BLE001
            proc.kill()
        log("已要求 agent 停止")
        return {"ok": True, "running": False}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=SERVER, help="控制面地址")
    ap.add_argument("--server-alt", default="",
                    help="备用控制面地址（包由「本地浏览器+服务器」协同生成，"
                         "带主/备两个；主地址探不通 agent 会自动换这个）")
    ap.add_argument("--ticket", default="", help="一次性启动票据（一般不用给）")
    ap.add_argument("--email", default="", help="成员邮箱（没票据时用）")
    ap.add_argument("--password", default="", help="成员密码（没票据时用）")
    ap.add_argument("--port", type=int, default=PORT, help="本地端口（默认 8766）")
    args = ap.parse_args()

    # ★ 占位符（没被服务端替换过）当空处理 —— 否则会把
    #   "__MSYT_TICKET__" 这种字符串当成真票据传给 agent，白白失败一次。
    tk = (args.ticket or "").strip()
    if tk == TICKET or tk.startswith("__MSYT_"):
        tk = ""

    here = os.path.dirname(os.path.abspath(__file__))

    STATE["server"] = (args.server or SERVER).strip()
    if STATE["server"] == SERVER:
        STATE["server"] = ""
    if not STATE["server"]:
        # 兜底：读同目录的 server.txt（包里的控制面地址）
        try:
            p = os.path.join(here, "server.txt")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    STATE["server"] = f.read().strip()
        except Exception:                             # noqa: BLE001
            pass
    STATE["ticket"] = tk
    # ★ 备用控制面地址（2026-10-23）：命令行给、或读同目录的 server-alt.txt
    alt = (args.server_alt or "").strip()
    if not alt:
        try:
            p = os.path.join(here, "server-alt.txt")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    alt = f.read().strip()
        except Exception:                             # noqa: BLE001
            alt = ""
    if alt.startswith("__MSYT_"):
        alt = ""
    STATE["server_alt"] = alt
    STATE["email"] = args.email
    STATE["password"] = args.password

    has_file = os.path.exists(os.path.join(here, "agent-ticket.txt"))

    print("=" * 66)
    print("  成员端本地助手")
    print(f"  控制面 : {STATE['server'] or '(未指定)'}")
    print(f"  本地   : http://127.0.0.1:{args.port}")
    print(f"  凭证   : {'包内 agent-ticket.txt' if has_file else '无（需网页递令牌或账号密码）'}")
    print("=" * 66)
    print("  保持这个窗口开着；回到网页点「启动本机 agent」即可开跑。")
    print("  关掉这个窗口 = 停止协助采集。")
    print("=" * 66)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n助手已退出。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
