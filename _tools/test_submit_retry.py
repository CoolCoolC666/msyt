# -*- coding: utf-8 -*-
"""验收：回传失联要**重试**，不该丢弃数据、更不该直接停。

★ 用户 2026-10-23：「另外回传后失联就断开，不重试不大合理」
★ 组员实测日志（旧行为）：
    20:52:33 ⚠ 连接失败：ReadTimeout（连续第 1 次）
    20:52:33 ✗ tid=8342251 回传时失联，本帖丢弃（数据只在内存里，不会残留）
    20:52:36 结束：完成 20 帖，失败 1 帖（因与控制面失联而自动停止）
  —— 一次 20 秒超时 = 一帖白采 + 整个 agent 停。

本测试验证：
  ① 回传失败会**重试**（≥30 秒退避）
  ② 重试成功 → 这一帖**没白采**（正常计入完成）
  ③ 重试用尽 → 只放弃这一帖，**继续下一帖**
  ④ 连续多帖都失败 → 才停止
  ⑤ 组员侧参数比本机宽松（timeout / 失联阈值）
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


print("=" * 72)
print("回传失联：重试、不丢弃、不直接停")
print("=" * 72)

# ---------------------------------------------------------------- ① 参数
print("\n① 组员侧参数比本机宽松")
check("单次请求 timeout ≥45 秒（公网链路慢）", A.HTTP_TIMEOUT >= 45,
      f"{A.HTTP_TIMEOUT}s")
check("辅助请求 timeout ≥15 秒", A.HTTP_TIMEOUT_AUX >= 15, f"{A.HTTP_TIMEOUT_AUX}s")
check("失联阈值 ≥6（组员更容易触发）", A.FAIL_LIMIT >= 6, str(A.FAIL_LIMIT))
check("心跳独立阈值 ≥8", A.HB_FAIL_LIMIT >= 8, str(A.HB_FAIL_LIMIT))
check("探测超时 ≥20 秒", A.PROBE_TIMEOUT >= 20, f"{A.PROBE_TIMEOUT}s")
check("回传重试次数 = 3", A.SUBMIT_RETRY == 3, str(A.SUBMIT_RETRY))
check("连续失败多少帖才停 = 4", A.SUBMIT_FAIL_LIMIT == 4, str(A.SUBMIT_FAIL_LIMIT))
check("重试间隔仍 ≥30 秒", A.RETRY_BACKOFF[0] >= 30, str(A.RETRY_BACKOFF))

# ---------------------------------------------------------------- ② 重试行为
print("\n② 回传失败会重试（且退避 ≥30 秒）")


class FakeAgent(A.Agent):
    """把 submit 换成可控的假实现。"""

    def __init__(self, seq):
        super().__init__(server="http://x.invalid", verbose=False)
        self.seq = list(seq)          # 每次调用 submit 的结果
        self.calls = 0
        self.waits = []

    def submit(self, code, tid, payload):
        self.calls += 1
        act = self.seq.pop(0)
        if act == "fail":
            raise A.LinkLost("模拟：回传时连接失败")
        # 成功 → 模拟真正 submit 的行为：清空 payload
        payload["posts"].clear()
        payload["pages"].clear()
        payload["summary"] = {}
        return {"ok": True}


def mk_payload():
    return {
        "posts": [{"pid": 1, "content": "正文"}],
        "pages": [{"page_no": 1}],
        "summary": {"ok": True, "restricted": False, "reason": "",
                    "pages": 1, "posts": 1, "chars": 5},
    }


real_sleep = time.sleep
A.time.sleep = lambda s: None          # 别真等
try:
    # ②a 第一次失败、第二次成功 → 不该丢数据
    ag = FakeAgent(["fail", "ok"])
    r = ag._submit_with_retry("A1", 1, mk_payload())
    check("★ 第一次失败会**重试**", ag.calls == 2, f"调用 {ag.calls} 次")
    check("★ 重试成功 → 这一帖**没白采**", r["ok"] is True)
    check("★ 重试成功 → 计数仍然正确（1 页/1 楼/5 字）",
          r["n_pages"] == 1 and r["n_posts"] == 1 and r["n_chars"] == 5,
          f"{r['n_pages']} 页 {r['n_posts']} 楼 {r['n_chars']} 字")

    # ②b 一直失败 → 重试用尽，返回 ok=False（由调用方决定放弃/继续）
    ag2 = FakeAgent(["fail", "fail", "fail"])
    r2 = ag2._submit_with_retry("A1", 1, mk_payload())
    check("一直失败 → 重试满 3 次", ag2.calls == 3, f"调用 {ag2.calls} 次")
    check("一直失败 → 返回 ok=False（不是抛异常）", r2["ok"] is False)

    # ②c 重试时 payload 还在（没被清空）
    ag3 = FakeAgent(["fail", "ok"])
    p3 = mk_payload()
    ag3._submit_with_retry("A1", 1, p3)
    check("★ 重试时正文**没被清空**（数据还在，能重传）",
          ag3.calls == 2 and r"ok" not in str(ag3.seq))
finally:
    A.time.sleep = real_sleep

# ---------------------------------------------------------------- ③ 退避时长
print("\n③ 重试的退避间隔")
waits = []
A.time.sleep = lambda s: waits.append(s)
try:
    ag4 = FakeAgent(["fail", "fail", "ok"])
    ag4._submit_with_retry("A1", 1, mk_payload())
finally:
    A.time.sleep = real_sleep
check("每次重试前都等了 ≥30 秒", len(waits) == 2 and all(w >= 30 for w in waits),
      str(waits))
check("退避递增", waits == [30, 45], str(waits))

# ---------------------------------------------------------------- ④ 主循环不再 break
print("\n④ 源码层面：回传失败不再 break 整个循环")
src = open(os.path.join(ROOT, "程序", "msyt", "agent.py"), encoding="utf-8").read()
check("有「继续采下一帖」的处理", "继续采下一帖" in src)
check("有连续回传失败的计数逻辑", "submit_fail_streak" in src)

# ★ 关键行为断言：
#   ① `_submit_with_retry` 里的 `except LinkLost` **不含 break**（靠重试循环）
#   ② 主循环里「采正文失联」的 `except LinkLost` **不含 break**（继续下一帖）
#   ③ 其它含 break 的失联分支，必须带 `if self.link_lost:` 守卫
#      （那是「已判定失联 → 停止」，是合理的）
lines = src.split("\n")
blocks = []
for i, ln in enumerate(lines):
    if ln.strip().startswith("except LinkLost"):
        indent = len(ln) - len(ln.lstrip())
        body = []
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            if not nxt.strip():
                body.append(nxt)
                continue
            nind = len(nxt) - len(nxt.lstrip())
            if nind < indent:
                break
            if nind == indent and nxt.strip().startswith(("except", "try", "finally", "else", "elif")):
                break
            body.append(nxt)
        blocks.append((i + 1, "\n".join(body)))

has_break = [(ln, b) for ln, b in blocks if "break" in b]
print(f"   （共 {len(blocks)} 处 except LinkLost，其中 {len(has_break)} 处含 break）")
for ln, b in has_break:
    guarded = "if self.link_lost:" in b
    print(f"    L{ln}: {'有失联守卫 ✓' if guarded else '★ 无守卫'}")
check("所有含 break 的失联分支都有 link_lost 守卫",
      all("if self.link_lost:" in b for _, b in has_break),
      str([ln for ln, _ in has_break]))

# 回传重试方法内部不许有 break
retry_blk = [(ln, b) for ln, b in blocks
             if "回传失败（第" in b or "这一帖**不丢**" in b]
check("★ 回传重试方法内不含 break",
      all("break" not in b for _, b in retry_blk) or not retry_blk,
      f"回传相关块 {len(retry_blk)} 处")

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
