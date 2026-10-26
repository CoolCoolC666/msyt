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
    p = None                      # ★ 必须先置空：超时分支里 p 从未被赋值，
                                   #   否则会沿用**上一个**脚本的 returncode → 误判通过
    try:
        p = subprocess.run([PY, os.path.join(HERE, fn)], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           env=ENV, timeout=600)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        out = "★ 超时（600s）—— 该脚本依赖真实链路/长任务，需单独排查"
    m = None
    for line in reversed(out.splitlines()):
        m = PAT.search(line)
        if m:
            break
    if m:
        n, ok, bad = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        # ★ 兜底（2026-10-26 补）：各脚本的汇总行写法不统一
        #   （「通过 18 / 失败 0」「结果：5 通过 / 0 失败」「全部通过」…），
        #   正则认不出时旧逻辑一律 bad=1 → 回归报告里全是 0/0 的假 FAIL，
        #   真正的回归问题反而被淹没。
        n = ok = 0
        rc = p.returncode if p is not None else -1
        bad = out.count("[FAIL]")
        ok = out.count("[OK ]") + out.count("[PASS]")
        if bad == 0 and ok == 0:
            # 连标记都没有：看正文是不是明说「全过」
            if ("\u5168\u90e8\u901a\u8fc7" in out) or re.search(r"\u5931\u8d25\s*0", out):
                ok, bad = 1, 0
            else:
                bad = 1
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

# ---- 收尾：把回归留下的**任务运行记录**清掉（2026-10-26 加）
#   回归里几十个脚本会起真实任务（详情/列表/体检）→ 全都留在
#   「任务运行情况」里刷屏，管理员根本看不见真正在跑的那个。
#   这里调一次 DELETE /api/jobs/clear 收干净。
#   ★ 清的是**运行记录**，不碰采集结果（库里的数据一行不动）。
try:
    import json as _json
    import urllib.request as _ur

    def _api(method, path, body=None, token=""):
        data = _json.dumps(body).encode() if body is not None else None
        req = _ur.Request("http://127.0.0.1:8765" + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "msyt-agent/1.1")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with _ur.urlopen(req, timeout=20) as r:
            return _json.loads(r.read().decode("utf-8", "replace"))

    _tok = _api("POST", "/api/auth/login",
                {"email": "qa-admin@example.com", "password": "qa-test-password"}).get("token", "")
    _r = _api("POST", "/api/jobs/clear", {}, _tok)
    print(f"  [收尾] 已清理回归留下的任务记录：{_r.get('removed', 0)} 条")
except Exception as _e:                                    # noqa: BLE001
    print(f"  [收尾] 清理任务记录失败（不影响回归结论）：{type(_e).__name__}: {_e}")

print(f"总计：{total_ok}/{total_n} 项通过，失败 {total_fail} 项")
sys.exit(0 if total_fail == 0 else 1)
