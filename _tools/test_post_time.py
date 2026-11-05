# -*- coding: utf-8 -*-
"""闸：楼层发帖时间必须能解析（2026-11-05 · 对应优化报告 P0-1）。

★ 为什么要有这个闸
  站方楼层锚点原文形如「发表于 2025-5-23 19:55来自浙江」——
  **月日不补零**。旧正则要求月日各两位 → 匹配失败 →
  全库 482,266 个楼层（86.1%）的发帖时间被丢掉，
  而课件字段①「发布/显示时间」正是靠它。

  → 这个闸锁死两件事：
     ① 各种真实写法都能解析，且输出统一补零成 `YYYY-MM-DD HH:MM`
     ② **反向验证**：把正则改回旧写法（要求两位），同一批断言必须报红

用法：python _tools/test_post_time.py
"""
from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.parsers import parse_thread_page, _POST_TIME_RE   # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


# --------------------------------------------------------------- 真实写法样本
# 全部来自站方详情页原文（实测 tid=8236013 / 8294530 / 8333204 的 authorposton 文本）
CASES = [
    ("发表于 2025-5-23 19:55来自浙江", "2025-05-23 19:55"),
    ("发表于 2026-9-2 14:49来自浙江", "2026-09-02 14:49"),
    ("发表于 2026-3-11 18:10来自浙江", "2026-03-11 18:10"),
    ("发表于 2024-12-1 08:00来自浙江", "2024-12-01 08:00"),
    ("发表于 2013-8-30 18:20", "2013-08-30 18:20"),
    ("发表于 2026-10-29 15:22", "2026-10-29 15:22"),          # 补零写法也要中
]

#: 旧实现（要求月日各两位）—— 反向验证用
OLD_RE = re.compile(
    r"发表于\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}(?::[0-9]{2})?)")

#: 站方真实 HTML 片段（tid=8236013 第 1 页首层，裁剪自实测页面）
REAL_HTML = (
    '<div id="post_113263783"><table id="pid113263783"><tr><td>'
    '<a id="postnum113263783" href="#pid113263783"><em>1</em><sup>#</sup></a>'
    '<em id="authorposton113263783">发表于 2025-5-23 19:55来自浙江</em>'
    '</td></tr></table></div>')


def main() -> int:
    print("=" * 62)
    print("闸：楼层发帖时间解析")
    print("=" * 62)

    # ---- ① 各种真实写法 ----
    print("\n--- ① 各种真实写法都要中，且补零归一化 ---")
    for raw, want in CASES:
        m = _POST_TIME_RE.search(raw)
        got = ("%04d-%02d-%02d %02d:%02d" % tuple(int(x) for x in m.groups())
               ) if m else ""
        check(got == want, f"{raw!r} -> {want!r}（实得 {got!r}）")

    # ---- ② 输出格式与存量一致 ----
    print("\n--- ② 输出格式与存量对齐（长度 16 = YYYY-MM-DD HH:MM）---")
    for raw, want in CASES:
        m = _POST_TIME_RE.search(raw)
        got = "%04d-%02d-%02d %02d:%02d" % tuple(int(x) for x in m.groups())
        check(len(got) == 16, f"{want} 长度 {len(got)}（应为 16）")

    # ---- ③ 真实 HTML 走整条解析链 ----
    print("\n--- ③ 真实 HTML 走 parse_thread_page ---")
    tp = parse_thread_page(REAL_HTML, tid=1, page_no=1)
    check(len(tp.pids) == 1, f"解析出 1 个楼层（实得 {len(tp.pids)}）")
    fm = list(tp.floors.values())[0] if tp.floors else None
    check(fm is not None and fm.post_time == "2025-05-23 19:55",
          f"楼层发帖时间 = 2025-05-23 19:55（实得 {fm.post_time if fm else None!r}）")
    check(fm is not None and fm.post_ts > 0,
          f"post_ts 已换算成时间戳（实得 {fm.post_ts if fm else None}）")

    # ---- ④ 反向验证：旧正则必须匹配不到不补零的写法 ----
    print("\n--- ④ ★反向验证：旧正则（要求两位）对同一批样本必须失效 ---")
    old_miss = [raw for raw, _ in CASES
                if not OLD_RE.search(raw) and "-" in raw]
    check(len(old_miss) >= 4,
          f"旧正则在 {len(old_miss)}/{len(CASES)} 个真实写法上失效"
          f"（证明闸不是空的）")
    for raw in old_miss[:3]:
        check(True, f"旧正则确实匹配不到：{raw!r}")

    # ---- ⑤ 静态检查：源码里不得再留着旧写法 ----
    print("\n--- ⑤ 静态检查：parsers.py 不得再有旧的两位月日正则 ---")
    src = io.open(os.path.join(PROJ, "程序", "msyt", "parsers.py"),
                  encoding="utf-8").read()
    bad = re.findall(r"发表于\\s\*\(\[0-9\]\{4\}-\[0-9\]\{2\}-\[0-9\]\{2\}", src)
    check(not bad, f"旧正则已从源码移除（残留 {len(bad)} 处）")
    check("_POST_TIME_RE" in src, "新正则以命名常量存在（可被闸直接引用）")

    print("\n" + "=" * 62)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
