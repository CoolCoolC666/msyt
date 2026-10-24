# -*- coding: utf-8 -*-
"""全量回归：把所有 test_*.py 跑一遍，出汇总。

（验收资产是资产，不是一次性脚本 —— 别删。）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
ENV = dict(os.environ)
ENV["PYTHONIOENCODING"] = "utf-8"

PAT = re.compile(r"共\s*(\d+)\s*项\s*·\s*通过\s*(\d+)\s*·\s*失败\s*(\d+)")

rows = []
files = sorted(f for f in os.listdir(HERE)
               if f.startswith("test_") and f.endswith(".py"))
print(f"共 {len(files)} 个测试脚本\n" + "=" * 70)
total_ok = total_fail = total_n = 0
for fn in files:
    t0 = time.time()
    try:
        p = subprocess.run([PY, os.path.join(HERE, fn)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=ENV, timeout=600)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        out = "超时"
    m = None
    for line in reversed(out.splitlines()):
        m = PAT.search(line)
        if m:
            break
    if m:
        n, ok, bad = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        n = ok = 0
        bad = 1 if p.returncode != 0 else 0
        if "FAIL" in out:
            bad = out.count("[FAIL]")
            ok = out.count("[PASS]")
            n = ok + bad
    total_n += n
    total_ok += ok
    total_fail += bad
    dt = time.time() - t0
    flag = "OK " if bad == 0 else "FAIL"
    print(f"  [{flag}] {fn:<34} {ok}/{n} 项  ({dt:.1f}s)")
    if bad:
        for line in out.splitlines():
            if "[FAIL]" in line:
                print(f"        {line.strip()[:110]}")

print("=" * 70)
print(f"总计：{total_ok}/{total_n} 项通过，失败 {total_fail} 项")
sys.exit(0 if total_fail == 0 else 1)
