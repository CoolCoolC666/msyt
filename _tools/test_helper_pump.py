# -*- coding: utf-8 -*-
"""回归：helper 的「日志泵」与「同机 agent 互斥」（2026-10-26）。

★ 为什么要有这个脚本
  用户反馈 agent「启动 1 秒就退出」。实测根因是**日志泵崩了**，不是 agent 坏了：
    helper 的 log() 兜底写成 `msg.encode("utf-8").decode("ascii","replace")`，
    replace 会造出 U+FFFD（�），而 **U+FFFD 在 GBK 里同样编不了**
    → 兜底的兜底再抛 UnicodeEncodeError
    → 逃出 pump 的 for 循环 → 被 `except Exception: pass` 静默吞掉
    → finally 打「agent 已退出」——**可 agent 进程其实活得好好的**。
  后果：网页显示「已停止」→ 管理员再点一次启动 → 堆出一堆同 node_id 的 agent
  互相抢活、互相退活 → 池子被占死、界面一直显示「在等活」。

  这三条一起锁住：
    ① log() 的编码兜底不能自己再抛（GBK 控制台实跑）
    ② pump 中断时**不能**误报「agent 已退出」（进程还活着就是还活着）
    ③ 同机重复启动要被拦住（只允许一个 agent）
"""
from __future__ import annotations

import io
import os
import re
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
#: 项目根 = _tools 的上一级；程序目录（helper 与 msyt 包）在这里面
ROOT = os.path.dirname(HERE)
PROG = os.path.join(ROOT, "程序")
if not os.path.exists(os.path.join(PROG, "msyt-helper.py")):
    # 兜底：万一目录名变了，往下找一层
    for sub in ("程序", "程序 ", "program"):
        cand = os.path.join(ROOT, sub)
        if os.path.exists(os.path.join(cand, "msyt-helper.py")):
            PROG = cand
            break
sys.path.insert(0, PROG)

RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}"
          + (f"  —— {detail}" if detail else ""))


def _load_helper():
    """把 msyt-helper.py 当模块加载（它是脚本不是包）。"""
    import importlib.util
    path = os.path.join(PROG, "msyt-helper.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("_msyt_helper_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- ① 编码兜底
def test_log_encoding_fallback() -> None:
    """helper.log() 在 GBK 控制台下遇到「✓ ↻ ⚠」不能自己再抛。"""
    h = _load_helper()
    if h is None:
        check("helper 可加载", False, "找不到 msyt-helper.py")
        return
    # 把 helper 的 stdout 换成 **GBK**（模拟 Windows 控制台）
    old = sys.stdout
    buf = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    sys.stdout = buf
    raised = None
    try:
        for s in ("✓ 可达", "↻ 服务端给了 2 个新地址", "⚠ 注意",
                  "· 圆点", "进度：本批 16/50 帖"):
            h.log(s)
        buf.flush()
    except Exception as e:                       # noqa: BLE001
        raised = e
    finally:
        sys.stdout = old
    check("log() 在 GBK 控制台下不抛异常", raised is None,
          f"抛了 {type(raised).__name__}: {raised}" if raised else
          "✓ / ↻ / ⚠ / · 都写进去了")

    # 再确认兜底**真的写出了东西**（不是静默吞掉）
    out = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    old = sys.stdout
    sys.stdout = out
    try:
        h.log("✓ 可达")
        out.flush()
        data = out.buffer.getvalue() if hasattr(out, "buffer") else b""
    except Exception:                            # noqa: BLE001
        data = b""
    finally:
        sys.stdout = old
    check("log() 的 GBK 兜底确实写出了内容", len(data) > 0,
          f"写出 {len(data)} 字节")


# ---------------------------------------------------------------- ② pump 误报
def test_pump_no_false_exit() -> None:
    """pump 中断且**进程还活着**时，不能打「agent 已退出」。"""
    h = _load_helper()
    if h is None:
        check("helper 可加载", False)
        return
    src = open(os.path.join(PROG, "msyt-helper.py"), encoding="utf-8").read()
    # ★ 用源码结构来断言（不依赖运行时的端口/进程状态）
    has_alive_branch = "alive = proc.poll() is None" in src
    check("pump 里判断了「进程是否真的还在」", has_alive_branch)

    # 「agent 已退出」这句**不能**是无条件打的
    # 取 _pump 方法体：从 def _pump 到下一个同缩进的 def
    mp = re.search(r"def _pump\(self, proc\):(.*?)\n    def ",
                   src, re.S)
    block = mp.group(1) if mp else ""
    check("能定位到 _pump 方法体", bool(block))
    unconditional = bool(re.search(r"^\s*log\(\"agent 已退出\"\)", block, re.M))
    # 允许它出现在 else 分支里（那是「进程真结束」才走的）
    in_else = "else:" in block and "log(\"agent 已退出\")" in block
    check("「agent 已退出」不是无条件输出", (not unconditional) or in_else,
          "进程还活着时改为「日志读取中断，但 agent 仍在运行」")

    check("pump 捕获异常后不再静默 pass",
          "except Exception as e:" in block and "crashed = " in block,
          "异常被记下来，写进日志")
    check("进程还活着时不清空 agent_state",
          "if alive:" in block and 'STATE["agent_state"] = ""' in block,
          "否则网页会以为 agent 没在跑")


# ---------------------------------------------------------------- ③ 同机互斥
def test_local_agent_pids() -> None:
    """_local_agent_pids() 能认出本机的 msyt agent 进程。"""
    h = _load_helper()
    if h is None:
        check("helper 可加载", False)
        return
    check("helper 里有 _local_agent_pids()", hasattr(h, "_local_agent_pids"))
    check("helper 里有 _kill_local_agents()", hasattr(h, "_kill_local_agents"))
    src = open(os.path.join(PROG, "msyt-helper.py"), encoding="utf-8").read()
    check("互斥检查接在 /start 上",
          "_local_agent_pids()" in src and "already" in src,
          "本机已有 agent 时不再起第二个")
    check("stop 会清理本机全部 agent",
          "_kill_local_agents(" in src)


# ---------------------------------------------------------------- ④ 包内地址
def test_bundle_primary_address() -> None:
    """包里主地址 = 下载者此刻访问的地址（不再用死地址当主）。"""
    import json
    import urllib.request

    base = "http://127.0.0.1:8765"
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=6) as r:
            if r.status != 200:
                raise RuntimeError("health 非 200")
    except Exception as e:                       # noqa: BLE001
        check("服务端可访问", False, f"{type(e).__name__}: {e} —— 跳过地址断言")
        return

    try:
        req = urllib.request.Request(
            base + "/api/auth/login",
            data=json.dumps({"email": "qa-admin@example.com",
                             "password": "qa-test-password"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.loads(r.read().decode()).get("token", "")
    except Exception as e:                       # noqa: BLE001
        check("能登录拿令牌", False, f"{type(e).__name__}: {e}")
        return

    def fetch(host: str, proto: str = "http", fwd: str = ""):
        import io as _io
        import zipfile
        hdrs = {"host": host, "User-Agent": "msyt-agent/1.1",
                "Authorization": "Bearer " + tok}
        if fwd:
            hdrs["x-forwarded-proto"] = proto
            hdrs["x-forwarded-host"] = fwd
        req = urllib.request.Request(base + "/api/agent/bundle.zip",
                                     headers=hdrs)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        z = zipfile.ZipFile(_io.BytesIO(data))
        srv = z.read("server.txt").decode("utf-8").strip()
        alt = [x.strip() for x in
               z.read("server-alt.txt").decode("utf-8").splitlines() if x.strip()]
        return srv, alt

    srv_local, alt_local = fetch("127.0.0.1:8765")
    check("本地下载 → 主地址是 127.0.0.1（agent 直连本机，不绕公网）",
          srv_local == "http://127.0.0.1:8765", f"实际 {srv_local}")
    check("本地下载 → 备用地址池无重复",
          len(alt_local) == len(set(alt_local)),
          f"{len(alt_local)} 条: {alt_local}")
    check("本地下载 → 备用地址里没有本机地址",
          "127.0.0.1" not in " ".join(alt_local), f"{alt_local}")

    eps = []
    try:
        with urllib.request.urlopen(base + "/api/agent/endpoints",
                                    timeout=10) as r:
            eps = json.loads(r.read().decode()).get("endpoints") or []
    except Exception:                            # noqa: BLE001
        pass
    if eps:
        host = eps[0].split("://", 1)[-1].strip("/")
        srv_pub, alt_pub = fetch(host, proto="https", fwd=host)
        check("公网下载 → 主地址 = 我们正在用的域名",
              srv_pub == eps[0], f"实际 {srv_pub}（期望 {eps[0]}）")
        check("公网下载 → 备用地址池无重复",
              len(alt_pub) == len(set(alt_pub)), f"{alt_pub}")
        check("公网下载 → 备用地址不含主地址自己",
              srv_pub not in alt_pub, f"{alt_pub}")
    else:
        check("拿到当前公网入口列表", False, "地址池为空，跳过公网断言")


def main() -> int:
    print("== helper 日志泵 / 同机互斥 / 包内地址 回归 ==")
    test_log_encoding_fallback()
    test_pump_no_false_exit()
    test_local_agent_pids()
    test_bundle_primary_address()
    n = len(RESULTS)
    ok = sum(1 for _, o, _ in RESULTS if o)
    bad = n - ok
    print(f"共 {n} 项 · 通过 {ok} · 失败 {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
