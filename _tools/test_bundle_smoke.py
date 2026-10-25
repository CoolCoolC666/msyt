# -*- coding: utf-8 -*-
"""冒烟：build_bundle 能不能真的跑通（上次就是这里 NameError → 500）。

★ 为什么要有这个
  语法检查**抓不到**运行时的 NameError（`agent_workers` vs `AGENT_WORKERS`），
  而它会让**生成启动包的接口 500** → 一串涉及启动包/助手的测试全挂。
  → 凡是改了 bundle.py，就跑一下这个。
"""
from __future__ import annotations

import os
import sys
import zipfile

PKG = os.path.join(
    r"【用户桌面】",
    "14-网络舆情分析与监测-2.0", "2-作业", "02 民生e网通", "程序")
sys.path.insert(0, PKG)
from msyt import bundle                                  # noqa: E402

OK = 0
FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {label}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  —— {extra}" if extra else ""))


msyt_dir = os.path.join(PKG, "msyt")
helper_src = os.path.join(PKG, "msyt-helper.py")
try:
    data, filename = bundle.build_bundle(
        "https://example.com", "TICKET-XXXX", msyt_dir,
        helper_src=helper_src, server_alt="https://alt.example.com",
        helper_port=8766)
    check("build_bundle 能跑通（不抛异常）", True, f"{len(data)} 字节 → {filename}")
except Exception as e:                                   # noqa: BLE001
    import traceback
    check("build_bundle 能跑通（不抛异常）", False,
          f"{type(e).__name__}: {e}")
    traceback.print_exc()
    sys.exit(1)

import io                                                 # noqa: E402
with zipfile.ZipFile(io.BytesIO(data)) as z:
    names = z.namelist()
    cmd = [n for n in names if n.endswith(".cmd")]
    check("包里有 .cmd 启动脚本", len(cmd) >= 2, str(cmd))
    # 检查 .cmd 里有没有 --workers
    hit = []
    for n in cmd:
        body = z.read(n).decode("utf-8", "replace")
        if "--workers" in body:
            hit.append(n)
    check("启动脚本带上了 --workers", bool(hit), str(hit))
    for n in cmd:
        body = z.read(n).decode("utf-8", "replace")
        for line in body.splitlines():
            if "msyt agent" in line and "--server" in line:
                print(f"      {n}: {line.strip()[:110]}")
                if "--diagnose" in line:
                    # 诊断命令不采集，不需要并发参数
                    check("诊断命令不强制带 --workers", "--workers" not in line)
                    continue
                check("agent 启动命令里带 --workers 3", "--workers 3" in line,
                      line.strip()[:90])

print()
print(f"结果：{OK} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
