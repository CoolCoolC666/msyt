# -*- coding: utf-8 -*-
"""验收：agent 的「失联判定节奏」。

★ 用户 2026-10-23 的反馈（组员实测日志）：
  「连接超时似乎设置长了，感觉以网络连接，个人觉得可以 30s 以上一次，
    不然可能容易连接超时」
  组员的 agent 因为一次瞬时 ReadTimeout，**107 秒就判失联停止**了。

本测试验证：
  ① 单次请求 timeout 收紧到 20 秒（辅助请求 10 秒）
  ② 失败后的重试间隔 **≥30 秒**（退避 30/45/60…）
  ③ 失联阈值从 3 提到 4，且心跳**单独计数**
  ④ 网络恢复后能自动继续，不用重开 agent
"""
from __future__ import annotations

import os
import sys
import time

ROOT = r"os.path.dirname(os.path.dirname(os.path.abspath(__file__)))"
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt import agent as A                      # noqa: E402

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


print("=" * 70)
print("失联判定节奏验收")
print("=" * 70)

print("\n① 参数取值")
# ★ 2026-10-23 晚上又调宽了一档：组员走**公网**（多好几跳），
#   比管理员本机直连更容易触发超时 → timeout 20→45、阈值 4→6、心跳 6→8。
check("单次请求 timeout ≥45 秒（公网链路慢）", A.HTTP_TIMEOUT >= 45, f"{A.HTTP_TIMEOUT}s")
check("辅助请求 timeout ≥15 秒", A.HTTP_TIMEOUT_AUX >= 15, f"{A.HTTP_TIMEOUT_AUX}s")
check("重试间隔第 1 次 ≥30 秒", A.RETRY_BACKOFF[0] >= 30, f"{A.RETRY_BACKOFF}")
check("重试间隔单调不减", all(A.RETRY_BACKOFF[i] <= A.RETRY_BACKOFF[i + 1]
                          for i in range(len(A.RETRY_BACKOFF) - 1)),
      str(A.RETRY_BACKOFF))
check("失联阈值 ≥6（组员更容易触发）", A.FAIL_LIMIT >= 6, str(A.FAIL_LIMIT))
check("心跳独立阈值 ≥8", A.HB_FAIL_LIMIT >= 8, str(A.HB_FAIL_LIMIT))

print("\n② agent 实例的默认值")
ag = A.Agent(server="https://example.invalid", heartbeat=12.0)
check("实例 fail_limit 用新默认值", ag.fail_limit == A.FAIL_LIMIT, str(ag.fail_limit))
check("心跳计数与主请求分开", hasattr(ag, "_hb_fail_streak")
      and ag._hb_fail_streak == 0, f"hb_streak={ag._hb_fail_streak}")

print("\n③ 退避序列：实际等多久")
waits = []
real_sleep = time.sleep


def fake_sleep(s):
    waits.append(s)
    return real_sleep(0)      # 真的不睡，省时间


A.time.sleep = fake_sleep
try:
    ag2 = A.Agent(server="https://example.invalid")
    for _ in range(5):
        ag2._backoff_wait()
finally:
    A.time.sleep = real_sleep

check("每次等待都 ≥30 秒", all(w >= 30 for w in waits), str(waits))
check("第 1 次 = 30", waits[0] == 30, str(waits[0]))
check("退避递增后封顶", waits[:3] == [30, 45, 60], str(waits[:3]))
check("封顶后不再增长", waits[3] == waits[4] == 60, str(waits[3:]))

print("\n④ 心跳失败不污染主请求计数")
ag3 = A.Agent(server="https://example.invalid")
ag3._mark_hb_fail("模拟心跳失败")
check("心跳失败后主请求计数仍为 0", ag3._fail_streak == 0, f"主={ag3._fail_streak}")
check("心跳计数为 1", ag3._hb_fail_streak == 1, f"心跳={ag3._hb_fail_streak}")
check("心跳未达阈值不判失联", ag3.link_lost is False)

print("\n⑤ 任一方成功会清零另一方")
ag4 = A.Agent(server="https://example.invalid")
ag4._mark_fail("模拟主请求失败")
ag4._mark_hb_fail("模拟心跳失败")
check("主请求计数 = 1", ag4._fail_streak == 1)
check("心跳计数 = 1", ag4._hb_fail_streak == 1)
ag4._mark_ok()
check("一次成功 → 主请求计数清零", ag4._fail_streak == 0)
check("一次成功 → 心跳计数也清零", ag4._hb_fail_streak == 0)
check("退避序号也重置", ag4._backoff_i == 0, str(ag4._backoff_i))

print("\n⑥ 达阈值才判失联")
ag5 = A.Agent(server="https://example.invalid")
for i in range(A.FAIL_LIMIT - 1):
    ag5._mark_fail("模拟")
check(f"连续 {A.FAIL_LIMIT - 1} 次还没判失联", ag5.link_lost is False)
ag5._mark_fail("模拟")
check(f"连续 {A.FAIL_LIMIT} 次才判失联", ag5.link_lost is True)

print("\n⑦ 容忍时长估算")
seq = A.RETRY_BACKOFF[:A.FAIL_LIMIT - 1]
total = A.FAIL_LIMIT * A.HTTP_TIMEOUT + sum(seq)
check("总容忍 ≥5 分钟（组员走公网，要更宽裕）", total >= 300,
      f"≈ {total} 秒（{total // 60} 分 {total % 60} 秒）")
check("比旧实现（107 秒）宽裕得多", total > 107 * 3, f"{total} vs 107")

print()
print("=" * 70)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
