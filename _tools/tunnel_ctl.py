#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公网隧道守护的启动器（供根目录的 .cmd 调用）。

★ 为什么不直接 `pwsh -File 程序\tunnel.ps1`：
  `.cmd` 必须**纯 ASCII**（cmd.exe 会把非 ASCII 字节解析坏），
  而「程序」这个目录名是中文 → 所以由这个 Python 脚本负责解析路径，
  再去调 `tunnel.ps1`。跟 `_tools\rebuild_report_docx.py` 一个套路。

★ 隧道本身（`程序/tunnel.ps1`）做的事：
  · 用**空配置**顶掉 `~/.cloudflared/config.yml`（否则 quick tunnel 会 404）
  · 拉起 N 条隧道、掉线自动重启
  · 把当前公网地址写进 `data/agent-api-base.txt`、`data/agent-endpoints.json`
    以及成员启动包的 `server-alt.txt`
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent                      # 02 民生e网通
TUNNEL = PROJ / "程序" / "tunnel.ps1"


def main() -> int:
    ap = argparse.ArgumentParser(description="msyt public tunnel control")
    ap.add_argument("action", nargs="?", default="start",
                    choices=["start", "stop", "status"])
    ap.add_argument("--count", type=int, default=2, help="并行隧道条数")
    ap.add_argument("--port", type=int, default=8765, help="后端端口")
    args = ap.parse_args()

    if not TUNNEL.is_file():
        print("tunnel.ps1 not found:", TUNNEL)
        return 1

    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        print("pwsh / powershell not found in PATH")
        return 1

    cmd = [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(TUNNEL), args.action,
           "-Count", str(args.count), "-Port", str(args.port)]
    print("[tunnel] " + " ".join(cmd))
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        print("\n[tunnel] interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
