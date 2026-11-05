# -*- coding: utf-8 -*-
"""**抽取式摘要**：把一条样本（一个帖）压成一句人能读的话。

★ 为什么不用 AI 摘要
  课件字段④要的是「原文摘要」，但这份报告的第一目标是**结论可追溯**。
  AI 摘要有两个硬伤：
    · **不可复现** —— 换模型、换版本，同一条样本给出不同摘要
    · **不可追溯** —— 摘要本身没有出处
  → 所以主口径用**抽取式**：本地规则，**同输入必同输出**，可入库、可导出、可复核。
  → AI 摘要只能当**阅读辅助**（且必须标注模型与生成时间），
    真要做的时候另开一条路，别混进主口径。

★ 规则（全部本地、零依赖）
  1. 取**楼主正文**（第一页第一楼）作为诉求主体；
  2. 去掉图片标记（`[图:…]`）与多余空白；
  3. 取前 2 句（按 。！？；与换行断句），超长截断；
  4. 前面挂**锚点**（部门 · 类别 · 受理状态），后面挂**规模**（楼层数 / 字数）。

用法：from msyt.summarize import make_summary
"""
from __future__ import annotations

import re

#: 楼层正文里的图片标记：`[图:https://…]`
_IMG_RE = re.compile(r"\[图:[^\]]*\]")
#: 连续空白
_WS_RE = re.compile(r"[ \t　]+")
#: 断句符
_SENT_END = "。！？；!?;\n"


def clean_text(text: str) -> str:
    """去掉图片标记与多余空白（不改一个字的内容）。"""
    t = _IMG_RE.sub("", text or "")
    t = _WS_RE.sub(" ", t)
    return "\n".join(line.strip() for line in t.splitlines()).strip()


def first_sentences(text: str, n: int = 2) -> str:
    """取前 n 句（按 。！？；与换行断句），句末标点保留。"""
    out: list[str] = []
    buf = ""
    for ch in text or "":
        buf += ch
        if ch in _SENT_END:
            s = buf.strip()
            if s:
                out.append(s)
            buf = ""
            if len(out) >= n:
                break
    if buf.strip() and len(out) < n:
        out.append(buf.strip())
    return "".join(out)


def make_summary(title: str = "", dept_name: str = "", category: str = "",
                 state: str = "", first_text: str = "", n_posts: int = 0,
                 n_chars: int = 0, max_len: int = 120) -> str:
    """生成一条样本的抽取式摘要。

    ★ **同输入必同输出**（不依赖时间、不依赖随机、不依赖外部服务）。
    """
    body = clean_text(first_text)
    if not body:
        # 纯图片帖 / 正文为空 → 明确说清，**不拿标题凑**
        # （标题本来就是独立的一列，重复塞进摘要里会让人误以为那是正文）
        core = "（无文字正文）"
    else:
        core = first_sentences(body, 2)
        if len(core) > max_len:
            core = core[:max_len].rstrip() + "…"

    anchors = [a for a in (dept_name, category, state) if a]
    head = ("【" + " · ".join(anchors) + "】") if anchors else ""
    tail = ""
    if n_posts:
        tail = "（%d 楼" % n_posts
        tail += "，%d 字）" % n_chars if n_chars else "）"
    return (head + core + tail).strip()
