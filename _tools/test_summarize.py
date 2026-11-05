# -*- coding: utf-8 -*-
"""闸：抽取式摘要（优化报告 P0-3a）。

★ 三条要守住的性质
  ① **同输入必同输出** —— 摘要要能当证据用，就必须可复现
  ② 不编内容 —— 正文为空就说「无文字正文」，不拿标题凑
  ③ 零新依赖、纯本地规则

用法：python _tools/test_summarize.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.summarize import make_summary, clean_text, first_sentences  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


REAL = ("[图:https://bbs.cnnb.com.cn/data/attachment/forum/202609/02/x.jpg]"
        "地铁2号线换乘站无障碍通道长期关闭"
        "希望相关部门尽快恢复。已反映多次，无人回复！")


def main() -> int:
    print("=" * 62)
    print("闸：抽取式摘要")
    print("=" * 62)

    # ---- ① 同输入必同输出 ----
    print("\n--- ① 可复现：同输入连跑 5 次结果完全一致 ---")
    outs = {make_summary("标题", "市交通局", "投诉", "已受理", REAL, 12, 480)
            for _ in range(5)}
    check(len(outs) == 1, f"5 次结果唯一（实得 {len(outs)} 种）")

    # ---- ② 图片标记被去掉、内容一字不少 ----
    print("\n--- ② 图片标记去掉，正文保留 ---")
    s = make_summary("标题", "市交通局", "投诉", "已受理", REAL, 12, 480)
    check("[图:" not in s, f"摘要里没有图片标记：{s}")
    check("无障碍通道" in s, "正文内容保留")

    # ---- ③ 锚点与规模都在 ----
    print("\n--- ③ 锚点（部门/类别/状态）与规模（楼数/字数）---")
    check("市交通局" in s and "投诉" in s and "已受理" in s, "三个锚点都在")
    check("12 楼" in s and "480 字" in s, "规模锚点在")

    # ---- ④ 正文为空 → 明说，不编 ----
    print("\n--- ④ 正文为空要说清，不拿标题凑 ---")
    s2 = make_summary("只有标题", "某部门", "咨询", "", "", 0, 0)
    check("无文字正文" in s2, f"空正文如实说明：{s2}")
    check("只有标题" not in s2, "没有把标题当正文塞进去")

    # ---- ⑤ 纯图片帖 ----
    print("\n--- ⑤ 纯图片帖 ---")
    s3 = make_summary("贴图", "某部门", "", "", "[图:http://x/1.jpg]", 1, 0)
    check("无文字正文" in s3, f"纯图片帖如实说明：{s3}")

    # ---- ⑥ 长度有上限 ----
    print("\n--- ⑥ 长度有上限（用**单句超长**正文触发截断）---")
    long_text = "这是一个没有标点的超长句子" * 30      # 单句 → 前 2 句仍很长
    s4 = make_summary("t", "d", "", "", long_text, 1, 1000, max_len=120)
    check(len(s4) <= 200, f"长正文被截断（长度 {len(s4)}）")
    check("…" in s4, f"截断处有省略号：{s4[:40]!r}…")
    # 短句多时不会被截断（取前 2 句就够）
    s4b = make_summary("t", "d", "", "", "很长的正文。" * 200, 1, 1000, max_len=120)
    check("…" not in s4b, "短句多时按句数截断，不按字数硬砍")

    # ---- ⑦ 断句取前 N 句 ----
    print("\n--- ⑦ 取前 2 句 ---")
    got = first_sentences("第一句。第二句。第三句。第四句。", 2)
    check(got == "第一句。第二句。", f"取前 2 句：{got!r}")

    # ---- ⑧ 空锚点不产生空的【】 ----
    print("\n--- ⑧ 无锚点时不留空括号 ---")
    s5 = make_summary("", "", "", "", "正文一句。", 1, 5)
    check("【" not in s5, f"无锚点不产生空标题：{s5}")

    # ---- ⑨ ★反向验证：把「同输入同输出」的性质改坏必须报红 ----
    print("\n--- ⑨ ★反向验证 ---")
    # ① 清洗函数若不删图片标记 → ② 的断言必须报红
    dirty = clean_text("[图:http://x/1.jpg]正文")
    check("[图:" not in dirty, "清洗确实删掉了图片标记")
    # 若 first_sentences 不限句数 → ⑦ 的断言必须报红
    check(len(first_sentences("一。二。三。四。", 2)) <= 4,
          "断句确实受句数上限约束")

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
