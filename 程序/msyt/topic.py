# -*- coding: utf-8 -*-
"""
topic.py —— 选题/任务命名规范（优化报告 P2-3）
================================================================
课件口径：「选题名称可由**监测范围、关键词、服务对象和报告规格**等构成」
→ 协作任务不再只有一个自由标题，而是有**结构化的四要素** + 自动生成的规范名。

四要素（顺序即规范名里的顺序，别乱改）：
    scope      监测范围（查哪儿：板块 / 部门 / 地域）
    keywords   关键词（查什么：词表命中口径）
    audience   服务对象（给谁看：报送单位 / 领导 / 公开平台）
    spec       报告规格（怎么交：周报 / 专题 / 简报，含篇幅）

规范名示例：
    监测范围：宁波公交｜关键词：晚点/拥挤/换乘｜服务对象：市交通局｜报告规格：周报

★ 为什么把「规范名」现算而不写死一列：
  规范名由四要素拼出来，四要素是事实、规范名是派生物。
  四要素改了（哪怕是从库里手工订正），规范名跟着变 —— 不会出现
  「标题写着周报、实际是专题」这种自相矛盾。
"""
from __future__ import annotations

from typing import Any, Dict, List

# 要素顺序 = 规范名里的顺序（跟课件一致）
TOPIC_FIELDS = ("scope", "keywords", "audience", "spec")

FIELD_CN = {
    "scope": "监测范围",
    "keywords": "关键词",
    "audience": "服务对象",
    "spec": "报告规格",
}

SEP = "｜"          # 全角竖线：不会跟词里的逗号/顿号混淆
MAX_KEYWORDS = 6   # 规范名里最多列 6 个词（多的截断，但**全部入库、仍可检索**）

# 关键词分隔符：认全组员手输的写法
_SEP_CHARS = ",，、/;；|\t\r\n "


def split_keywords(raw: str) -> List[str]:
    """把「晚点，拥挤、换乘 停运」拆成干净的词表。

    ★ 去重且**保序**（按第一次出现的顺序）。
    ★ 空格也算分隔符 —— 「宁波 公交」是两个词，不是带空格的短语。
    """
    out: List[str] = []
    seen = set()
    buf = ""
    for ch in (raw or "") + "\n":
        if ch in _SEP_CHARS:
            w = buf.strip()
            buf = ""
            if w and w not in seen:
                seen.add(w)
                out.append(w)
        else:
            buf += ch
    return out


def norm_keywords(raw: str) -> str:
    """入库用的关键词串：拆开后用 / 连接（便于检索与再显示）。"""
    return "/".join(split_keywords(raw))


def build_title(scope: str = "", keywords: str = "", audience: str = "",
                spec: str = "") -> str:
    """生成规范选题名。

    ★ 空要素**跳过** —— 不写「关键词：—」这种占位（占位会让名字看着完整、
      实则没信息，比空着更误导）。
    """
    parts: List[str] = []
    s = (scope or "").strip()
    if s:
        parts.append(f"{FIELD_CN['scope']}：{s}")
    kws = split_keywords(keywords)
    if kws:
        parts.append(f"{FIELD_CN['keywords']}：{'/'.join(kws[:MAX_KEYWORDS])}")
    a = (audience or "").strip()
    if a:
        parts.append(f"{FIELD_CN['audience']}：{a}")
    p = (spec or "").strip()
    if p:
        parts.append(f"{FIELD_CN['spec']}：{p}")
    return SEP.join(parts)


def display_title(custom: str = "", scope: str = "", keywords: str = "",
                  audience: str = "", spec: str = "") -> str:
    """界面上显示的标题 = 自定义标题（若有）+ 规范名。

    ★ 自定义标题放前面：它是管理员自己写的一句话（可能是「本周加急」），
      规范名是结构化的四要素 —— 两者都在，检索时也都能搜到。
    """
    t = (custom or "").strip()
    n = build_title(scope, keywords, audience, spec)
    if t and n:
        return f"{t}{SEP}{n}"
    return t or n


def search_haystack(task: Dict[str, Any]) -> str:
    """任务的**可检索文本**：编号 + 自定义标题 + 四要素 + 规范名。

    ★ 检索按「包含」做，不解析 —— 任务量是几十到几百条，
      现算比在 SQL 里拼 LIKE 更不容易漏（规范名是派生物，SQL 里算不出来）。
    """
    bits = [str(task.get("code") or ""), str(task.get("title") or "")]
    for k in TOPIC_FIELDS:
        bits.append(str(task.get(k) or ""))
    bits.append(display_title(
        str(task.get("title") or ""),
        str(task.get("scope") or ""), str(task.get("keywords") or ""),
        str(task.get("audience") or ""), str(task.get("spec") or "")))
    return " ".join(bits)
