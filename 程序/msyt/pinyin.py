# -*- coding: utf-8 -*-
"""拼音首字母（支持「输入名称、拼音或首字母」检索）。

★ 为什么要这个（2026-10-24 用户要求）：
  站方那个「107家部门与企事业单位在线」的搜索框支持
  **「输入部门名称、拼音或首字母」** —— 109 个部门用原生下拉框根本没法找。
  我们的检索页要**同款能力**，而且「其他标签同理」。

★ 两种来源
  ① **固定词典**（类别 / 状态 / 时效灯 / 留言箱 / 分组）：
     首字母**写死**在这里 —— 不依赖任何第三方库，克隆下来就能用。
  ② **部门 / 区县市**（109 + 10 条，来自站方，会变）：
     用 `pypinyin` 现场算；**没装 pypinyin 也不影响**（退化成只支持中文子串），
     算出来的结果会**存进库**（`py_initials` 列），所以装一次就长期有效。
"""
from __future__ import annotations

from typing import Dict, List

#: 固定词典的拼音首字母（手写，零依赖）
FIXED_INITIALS: Dict[str, str] = {
    # 类别
    "投诉": "TS", "建议": "JY", "咨询": "ZX", "点赞": "DZ",
    "市民巡访": "SMXF", "追问": "ZT", "求助": "QZ", "其他": "QT",
    # 受理状态
    "已回复": "YHF", "已受理": "YSL", "未受理": "WSL",
    # 时效灯
    "3个工作日内回复": "3GZYNHDHF", "5个工作日内回复": "5GZYNHDHF",
    "5个工作日外回复": "5GZYNWHDHF", "24小时未受理": "24XSSWL",
    # 留言箱
    "市委书记留言箱": "SJSJYXB", "市长留言箱": "CSJYXB",
    # 部门分组
    "职能部门和群团组织": "BMDWQTTZZ",
    "区（县、市）及开发园区": "QXSJSJKFQQ",
    "民生企事业单位": "MSQYDW",
}

try:                                                    # 可选依赖
    from pypinyin import lazy_pinyin as _lazy_pinyin     # type: ignore
    HAVE_PINYIN = True
except Exception:                                       # noqa: BLE001
    _lazy_pinyin = None                                 # type: ignore
    HAVE_PINYIN = False


def initials(text: str) -> str:
    """一段中文的拼音首字母（大写）；没有拼音库时返回空串。"""
    if not text:
        return ""
    fixed = FIXED_INITIALS.get(text.strip())
    if fixed:
        return fixed
    if not HAVE_PINYIN or not _lazy_pinyin:
        return ""
    try:
        return "".join(p[0] for p in _lazy_pinyin(text) if p and p[0]).upper()
    except Exception:                                   # noqa: BLE001
        return ""


def dict_items(items: List[Dict], group_key: str = "") -> List[Dict]:
    """给字典项补上 `py` 字段（前端拿去匹配输入的拼音/首字母）。"""
    out = []
    for it in items or []:
        d = dict(it)
        d["py"] = initials(str(d.get("name") or ""))
        if group_key and d.get(group_key):
            d["group"] = d[group_key]
        out.append(d)
    return out
