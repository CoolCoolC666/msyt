# -*- coding: utf-8 -*-
"""
test_agent_linkloss.py —— agent 与控制面失联 → **自动停止**
================================================================================
为什么要单独测这一条：
  控制面是协作的**唯一出口**（领活要问它、回传要给它）。
  一旦断了联系还继续采，采到的东西送不回来 —— 那是白采，也是白打站方。

测法（**不碰站方**）：
  起一个假的控制面（只实现 login / pick / heartbeat），让 agent 连上去；
  然后**把假控制面关掉** —— agent 应当自己发现失联并停下。

判据：
  · 日志里出现「判定为失联」
  · 日志里出现「与控制面失联 → 停止领活与采集」
  · 进程自己退出（不是靠我们杀它）
  · 退出时汇报里带 stopped_by_link
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


class Fake(BaseHTTPRequestHandler):
    hits = {"login": 0, "pick": 0, "heartbeat": 0}

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path in ("/api/auth/login", "/api/agent/login"):
            Fake.hits["login"] += 1
            self._send({"token": "fake-token", "member": {
                "email": "fake@example.com", "role": "member",
                "node_id": "member:fake@example.com"}})
        elif self.path in ("/api/assist/pick", "/api/agent/pick"):
            Fake.hits["pick"] += 1
            # 不给活：让 agent 进入轮询状态，便于观察心跳与失联
            self._send({"picks": []})
        elif self.path in ("/api/agent/heartbeat",):
            Fake.hits["heartbeat"] += 1
            self._send({"ok": True, "node": "member:fake@example.com"})
        else:
            self._send({"ok": True})


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    print("=" * 78)
    print("agent 失联自动停止 —— 验收")
    print("=" * 78)

    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Fake)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    print(f"假控制面已起： http://127.0.0.1:{port}")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = PROG
    cmd = [sys.executable, "-m", "msyt", "agent",
           "--server", f"http://127.0.0.1:{port}",
           "--email", "fake@example.com", "--password", "whatever",
           "--interval", "1", "--heartbeat", "2", "--fail-limit", "3"]
    print("agent 启动：" + " ".join(cmd[2:]) + "\n")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    out_lines = []

    def reader():
        for line in proc.stdout:
            out_lines.append(line.rstrip())
            print("  | " + line.rstrip())

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # ---- 等它跑起来 ----
    time.sleep(6)
    check("agent 成功登录假控制面", Fake.hits["login"] >= 1,
          f"login={Fake.hits['login']}")
    check("agent 在轮询领活", Fake.hits["pick"] >= 1, f"pick={Fake.hits['pick']}")
    check("心跳已发出", Fake.hits["heartbeat"] >= 1,
          f"heartbeat={Fake.hits['heartbeat']}")
    check("agent 还在跑（没自己停）", proc.poll() is None, "")

    # ---- 关掉假控制面：模拟「与服务器切断联系」 ----
    print("\n  >>> 现在把假控制面关掉（模拟与服务器失联）\n")
    srv.shutdown()
    srv.server_close()

    # ---- 等 agent 自己发现并停下 ----
    t_end = time.time() + 60
    while time.time() < t_end and proc.poll() is None:
        time.sleep(1)
    rc = proc.poll()
    check("★ agent 自己退出了（不是被我们杀的）", rc is not None, f"rc={rc}")

    text = "\n".join(out_lines)
    check("★ 日志里有「判定为失联」", "判定为失联" in text, "")
    check("★ 日志里有「停止领活与采集」",
          "与控制面失联" in text and "停止" in text, "")
    check("★ 退出时汇报了因失联停止",
          "因与控制面失联而自动停止" in text, text[-160:].replace("\n", " | "))
    check("心跳失败被连续计数（≥3 次）",
          text.count("连续第") >= 3, f"次数={text.count('连续第')}")

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
