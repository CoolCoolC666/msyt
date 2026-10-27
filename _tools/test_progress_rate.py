# -*- coding: utf-8 -*-
"""test_progress_rate.py —— 速率/还剩必须是**本批口径**（2026-10-25 补）

★ 用户原话
  「关于总篇数/当前批次(50批)的算法下的篇采集时长算法不对
    应该调整为总篇次取余(%)与批次设置(如50)取余/当前批次用时 的时间
    不然的话，运行久了，可能可以看到1秒60篇」

★ 原来的算法（错在哪）
  速率 = **累计**已采 ÷ **本批**已用时
  → 分子跨批累加、分母每批重置 → 跑久了速率虚高
  → 「还剩」也错：拿累计去减本批总数 → 跨批后恒为 0
  实测：累计 122 帖 ÷ 本批 53 秒 = 2.3 帖/秒（其中 100 帖是上一批早采完的）

★ 现在的算法
  本批已采 = 累计 − 本批起点（`_batch_base`）
  还  剩   = 本批总数 − 本批已采
  本批用时 = now − 本批起点
  速  率   = 本批已采 ÷ 本批用时
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time

ROOT = r"os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt.agent import Agent                        # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")


def make_agent(base: int, elapsed: float = 10.0):
    """造一个只够跑 _progress_line 的轻量 Agent。"""
    a = Agent.__new__(Agent)
    a._lock = threading.Lock()
    a._last_progress = 0.0
    a._prog_start = time.time() - elapsed
    a._batch_base = base
    a._logs = []
    a.log = a._logs.append
    return a


print("### ① 跨批时：速率与「还剩」按本批算")
a = make_agent(base=100, elapsed=10.0)          # 本批起点时累计已 100
a._progress_line(122, 50, "协作")               # 现在累计 122
line = a._logs[0]
print("   " + line)
check("显示「本批 22/50 帖」", "本批 22/50 帖" in line, line)
check("「还剩」是本批的 28（不是累计减本批的 0）",
      "还剩 28 帖" in line, line)
check("「用时」是本批用时", "本批用时 10s" in line, line)
m = re.search(r"｜(\d+\.\d) 帖/秒", line)
check("速率 = 本批已采 ÷ 本批用时 = 22/10 ≈ 2.2",
      bool(m) and abs(float(m.group(1)) - 2.2) < 0.15,
      m.group(1) if m else "没匹配到")
check("累计单独列出来（不丢信息）", "累计 122 帖" in line, line)

print()
print("### ② 同一批内递增时，速率稳定")
a2 = make_agent(base=0, elapsed=20.0)
rates = []
for done in (10, 20, 30):
    a2._last_progress = 0.0                        # 绕过 5 秒节流
    a2._progress_line(done, 50, "协作")
    mm = re.search(r"｜(\d+\.\d) 帖/秒", a2._logs[-1])
    rates.append(float(mm.group(1)) if mm else None)
print(f"   速率序列：{rates}")
check("速率随本批进度线性上升", rates == [0.5, 1.0, 1.5], str(rates))

print()
print("### ③ ★ 跨批后速率不会爆炸（用户担心的「1 秒 60 篇」）")
a3 = make_agent(base=1000, elapsed=1.0)         # 累计已 1000，本批才刚开始
a3._progress_line(1030, 50, "协作")              # 累计 1030 → 本批 30
line3 = a3._logs[0]
print("   " + line3)
m3 = re.search(r"｜(\d+\.\d) 帖/秒", line3)
rate3 = float(m3.group(1)) if m3 else None
check("速率 = 本批 30 帖 ÷ 1 秒 = 30（不是 1030 帖/秒）",
      rate3 is not None and abs(rate3 - 30.0) < 0.2, str(rate3))
check("★ 速率不会超过「本批总数 ÷ 用时」",
      rate3 is not None and rate3 <= 50 / 1.0 + 0.01, str(rate3))

print()
print("### ④ 源码层：两套计数器不能串口径")
src = open(os.path.join(ROOT, "程序", "msyt", "agent.py"), encoding="utf-8").read()
check("有 _batch_base 字段", "_batch_base" in src)
check("_progress_reset 带 base 参数",
      "def _progress_reset(self, base: int = 0)" in src)
check("本机路径 reset 传 0", "self._progress_reset(0)" in src)
check("协作路径 reset 传当前累计",
      "self._progress_reset(self._c_done + self._c_failed" in src)
check("速率用本批口径（in_batch / el）", "rate = in_batch / el" in src)
check("「还剩」用本批口径",
      "left = max(0, int(total) - in_batch)" in src)

print()
print("### ⑤ 助手面板的正则不受影响")
helper = open(os.path.join(ROOT, "程序", "msyt-helper.py"),
              encoding="utf-8").read()
check("面板数「已领 N 帖」靠『领走 N 帖』",
      re.search(r"领走\\s\(\\d\+\)\\s\+帖", helper) is not None
      or "领走" in helper)
check("新进度行不含『领走』字样", "领走" not in line)
check("面板数「已完成 N 帖」靠『tid=N ✓』",
      "tid=" in helper and "✓" in helper)
check("新进度行不含 tid=…✓", not re.search(r"tid=\d+\s+✓", line))

print()
print("=" * 60)
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败：" + "；".join(FAIL))
    sys.exit(1)
print("全部通过 ✓")

