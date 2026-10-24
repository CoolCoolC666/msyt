# -*- coding: utf-8 -*-
"""
采集源注册表：把站方自带的标签轴变成「可独立分页、可独立续传」的采集源
================================================================
站方把同一批留言切成很多个维度（见 #rankingwz 的 5 个 tab 与 .qxsa 网格）：
    部门/单位（109 个 typeid）· 分组（职能/区县市/民生企事业/其他）
    区（县、市）（10 个）· 类别（7 类）· 受理状态（3 类）
    留言箱（2 个）· 时效灯（4 档）· 时间窗（dateline）
这些维度**不是互斥的**，一条留言同时落在好几条轴上。

所以本模块把「一个采集源」定义成 = 一条独立的分页列表：
    global            全站视图（500 页顶）
    dept:495          市公安局（该部门自己的分页，可能远小于 500）
    group:func        职能部门和群团组织（展开成 64 个 dept 源）
    district:737      鄞州区（展开成该区县自己的 typeid 源）
    category:1        类别=投诉
    state:2           状态=已受理
    mailbox:23        市委书记留言箱
    dateline:2592000  最近 30 天（打破 500 页上限的已验证手段之一）

每个源有**独立的 list_watermark 行**，所以：
  · 断点续传是「按源」的 —— 采到第 137 页断了，下次从 138 页接着来
  · 同一批帖子可以被多个源发现，入库去重靠 tid，命中关系记进 thread_sources
  · 源本身就是一条标签轴（thread_sources 表），可回答「这批帖子是从哪个维度看到的」
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import BASE, FID
from .taxonomy import CATEGORIES, LIGHTS, MAILBOXES, STATES


@dataclass
class Source:
    key: str          # 唯一键，也是 list_watermark.source
    dim: str          # global/dept/group/district/category/state/mailbox/light/dateline
    code: str
    name: str
    url: str          # 含 {page} 占位

    def url_for(self, page: int) -> str:
        return self.url.format(page=page)


def _lp(extra: str = "") -> str:
    """列表页 URL 模板（extra 为附加查询串，不含前导 &）。"""
    base = f"{BASE}/forum.php?mod=forumdisplay&fid={FID}"
    tail = f"&{extra}" if extra else ""
    return base + tail + "&page={page}"


#: 各维度的 URL 模板工厂
_TEMPLATES = {
    "global":    lambda code: _lp(),
    "dept":      lambda code: _lp(f"filter=typeid&typeid={code}"),
    "category":  lambda code: _lp(f"filter=tieziclass&tieziclass={code}"),
    "state":     lambda code: _lp(f"filter=govchecked&govchecked={code}"),
    "mailbox":   lambda code: _lp(f"filter=sortid&sortid={code}"),
    "light":     lambda code: _lp(f"filter=light&light={code}"),
    "dateline":  lambda code: _lp(f"filter=dateline&dateline={code}"),
}

#: 各维度 code → 名称
_DIM_NAMES: Dict[str, Dict[str, str]] = {
    "category": {c["code"]: c["name"] for c in CATEGORIES},
    "state": {s["code"]: s["name"] for s in STATES},
    "mailbox": {m["code"]: m["name"] for m in MAILBOXES},
    "light": {l["code"]: l["name"] for l in LIGHTS},
    "dateline": {
        "2592000": "最近30天", "7776000": "最近90天",
        "31536000": "最近365天", "63072000": "最近730天",
    },
}


def _parse_spec(spec: str) -> List[tuple]:
    """把源描述解析成 [(dim, codes)] 列表。

    语义：**逗号分隔的是「码表」，冒号分隔的是「维度」**。
        dept:495,972          -> dept 的码表是 495 与 972
        group:func,group:enterprise
                              -> 两项：group 的 func、group 的 enterprise
        dept:495|972          -> 同上（| 也当分隔符）

    ⚠ 这里必须**先按项归属、再拆 codes**。旧实现直接 `spec.split(",")`，
    于是 `dept:495,972` 被切成 ["dept:495", "972"] —— 第二段 "972" 丢了 dim 归属，
    解析时命中 `dim not in _TEMPLATES` 被**静默丢弃**，
    结果只采到 495，972 从没被采过（不报错，最难发现的一类 bug）。
    """
    items: List[List] = []
    for seg in (spec or "").split(","):
        seg = seg.strip()
        if not seg:
            continue
        if ":" in seg:
            dim, _, codes = seg.partition(":")
            items.append([dim.strip().lower(), codes.strip()])
        elif items:
            # 上一项的后续码（dept:495,972 里的 972）
            items[-1][1] = (items[-1][1] + "," + seg).strip(",")
        else:
            # 裸维度（如 "global"）
            items.append([seg.lower(), ""])
    return [(d, c) for d, c in items]


def make_source(dim: str, code: str, name: str = "") -> Source:
    tmpl = _TEMPLATES[dim]
    return Source(
        key=f"{dim}:{code}" if dim != "global" else "global",
        dim=dim, code=code,
        name=name or _DIM_NAMES.get(dim, {}).get(code, code),
        url=tmpl(code),
    )


def expand_sources(spec: str, depts: Optional[List[Dict]] = None) -> List[Source]:
    """把命令行里的源描述展开成具体的 Source 列表。

    spec 语法（逗号分隔，每项 dim:code 或 dim:code1,code2）：
        global
        dept:495,972
        group:func
        district:737
        category:1,2
        state:2
        mailbox:23
        dateline:2592000

    group / district 是**派生维度**：它们展开成该组下每个部门的 dept 源，
    因为「按区县市采」在站方就是「按该区县的 typeid 采」。
    """
    depts = depts or []
    by_group: Dict[str, List[Dict]] = {}
    for d in depts:
        for g in (d.get("group_code") or "").split(","):
            if g:
                by_group.setdefault(g, []).append(d)

    out: List[Source] = []
    seen = set()

    def add(src: Source):
        if src.key in seen:
            return
        seen.add(src.key)
        out.append(src)

    for dim, codes in _parse_spec(spec):
        if dim == "global":
            add(make_source("global", ""))
            continue

        if dim in ("group", "district"):
            members = by_group.get(codes, [])
            if dim == "district":
                members = [d for d in depts if d.get("is_district")]
                if codes:
                    members = [d for d in members if d["typeid"] == codes]
            for d in members:
                add(make_source("dept", d["typeid"], d["name"]))
            continue

        if dim not in _TEMPLATES:
            continue
        for c in re.split(r"[|,]", codes or ""):
            c = c.strip()
            if not c:
                continue
            add(make_source(dim, c))

    return out


def describe(sources: List[Source]) -> str:
    lines = []
    for s in sources:
        lines.append(f"  {s.key:<22} {s.name:<24} {s.url_for(1)}")
    return "\n".join(lines)
