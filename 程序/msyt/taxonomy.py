# -*- coding: utf-8 -*-
"""
分类体系：109 个部门 + 4 个分组 + 10 个区县市 + 类别/状态字典
================================================================
这套标签是站方自带的，直接拿来当分析维度，比自己造标签可靠得多。

结构（用浏览器实地勘察确认，见 docs/分类体系勘察.md）：
  #rankingwz 区块
    ├── .reakHd h3#tab1..tab5      5 个 tab
    │     tab1 全部 / tab2 职能部门和群团组织 / tab3 区（县、市）及开发园区
    │     tab4 民生企事业单位 / tab5 其他
    ├── #listranking1..5           5 个 pane，**同一批部门的不同分组**
    │     每个 pane 里的 a[href*=typeid=] 就是该组的成员
    └── #deptSearch                部门搜索框（站方用 pinyin_tools.js 前端拼音检索）

  .qxsa 区块
    └── .yzq2                     10 个区县市，各带「上月留言/上月受理」

⚠ 区县市的统计数字在 HTML 里是 `document.write(ms_hs);` —— 真正的数在
  站方独立脚本 html/yp/ms_sjszxsqtotal.js 里，必须单独取。
  不取那个脚本，采集到的就是字面量「document.write(ms_hs);」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from .config import STATS_JS

# ---------------------------------------------------------------- 维度字典
CATEGORIES = [
    {"code": "1", "name": "投诉", "param": "tieziclass=1"},
    {"code": "2", "name": "建议", "param": "tieziclass=2"},
    {"code": "3", "name": "咨询", "param": "tieziclass=3"},
    {"code": "4", "name": "点赞", "param": "tieziclass=4"},
    {"code": "5", "name": "市民巡访", "param": "tieziclass=5"},
    {"code": "6", "name": "追问", "param": "tieziclass=6"},
    {"code": "7", "name": "求助", "param": "tieziclass=7"},
    {"code": "999", "name": "其他", "param": "tieziclass=999"},
]

STATES = [
    {"code": "1", "name": "已回复", "param": "govchecked=1"},
    {"code": "2", "name": "已受理", "param": "govchecked=2"},
    {"code": "999", "name": "未受理", "param": "govchecked=999"},
]

#: 时效灯。**代码取实测到的 svg 名**，不是猜的编号：
#:   实测已回复的帖才带灯；已受理/未受理的帖 th 里没有图标（只有两个空 span）
#:   → 空值不是解析失败，是「尚未点亮」
LIGHTS = [
    {"code": "green", "name": "3个工作日内回复", "alias": "1", "param": "light=1"},
    {"code": "yellow", "name": "5个工作日内回复", "alias": "3", "param": "light=3"},
    {"code": "grey", "name": "5个工作日外回复", "alias": "5", "param": "light=5"},
    {"code": "warn", "name": "24小时未受理", "alias": "9", "param": "light=9"},
]

MAILBOXES = [
    {"code": "23", "name": "市委书记留言箱", "param": "sortid=23"},
    {"code": "24", "name": "市长留言箱", "param": "sortid=24"},
]

#: pane 序号 → 分组名（tab1 是「全部」，不算真实分组）
PANE_GROUPS = {
    1: ("all", "全部"),
    2: ("func", "职能部门和群团组织"),
    3: ("district", "区（县、市）及开发园区"),
    4: ("enterprise", "民生企事业单位"),
    5: ("other", "其他"),
}

#: 区县市 typeid → 统计脚本变量前缀
DISTRICT_VARS = {
    "736": ("ms_hs", "海曙区"), "727": ("ms_jb", "江北区"),
    "734": ("ms_zh", "镇海区"), "728": ("ms_bl", "北仑区"),
    "737": ("ms_yz", "鄞州区"), "731": ("ms_fh", "奉化区"),
    "733": ("ms_yy", "余姚市"), "735": ("ms_cx", "慈溪市"),
    "732": ("ms_nh", "宁海县"), "730": ("ms_xs", "象山县"),
}


@dataclass
class Dept:
    typeid: str
    name: str
    group_code: str
    group_name: str
    is_district: int = 0


@dataclass
class DistrictStat:
    typeid: str
    name: str
    last_month_msgs: Optional[int] = None
    last_month_accepted: Optional[int] = None
    total_msgs: Optional[int] = None
    total_accepted: Optional[int] = None


@dataclass
class SiteStats:
    last_month: Optional[int] = None
    this_year: Optional[int] = None
    total: Optional[int] = None
    last_month_accepted: Optional[int] = None
    this_year_accepted: Optional[int] = None
    total_accepted: Optional[int] = None


# ---------------------------------------------------------------- 解析
_TYPEID_RE = re.compile(r"typeid=(\d+)")


def parse_depts(html: str) -> List[Dept]:
    """从列表页 HTML 解析部门目录（5 个 pane 合并 + 分组归属）。

    分组归属**只取 tab2..tab5**（tab1「107家部门与企事业单位在线」是全集，
    不是分组）。一个部门可能同时属于多个分组（如「综合」512 四个分组都有），
    group_code 用逗号连接，按 tab 顺序排列，第一个是主分组。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    block = soup.select_one("#rankingwz")
    if block is None:
        return []

    out: Dict[str, Dept] = {}
    for pane_no, (gcode, gname) in PANE_GROUPS.items():
        pane = soup.select_one(f"#listranking{pane_no}")
        if pane is None:
            continue
        for a in pane.select('a[href*="typeid="]'):
            m = _TYPEID_RE.search(a.get("href") or "")
            name = a.get_text(strip=True)
            if not m or not name:
                continue
            tid = m.group(1)
            is_dist = 1 if tid in DISTRICT_VARS else 0
            prev = out.get(tid)
            if prev is None:
                out[tid] = Dept(tid, name, "", "", is_dist)
                prev = out[tid]
            if pane_no == 1:
                continue          # tab1 是全集，不算分组
            groups = [g for g in prev.group_code.split(",") if g]
            if gcode not in groups:
                groups.append(gcode)
            prev.group_code = ",".join(groups)
            prev.group_name = prev.group_code
    return list(out.values())


def parse_districts(html: str) -> List[DistrictStat]:
    """解析区县市网格（10 个），统计数字此时还是 document.write 占位。"""
    soup = BeautifulSoup(html or "", "html.parser")
    box = soup.select_one(".qxsa")
    if box is None:
        return []
    out: List[DistrictStat] = []
    for item in box.select(".yzq2"):
        a = item.select_one('a[href*="typeid="]')
        if not a:
            continue
        m = _TYPEID_RE.search(a.get("href") or "")
        if not m:
            continue
        out.append(DistrictStat(tid := m.group(1), a.get_text(strip=True)))
        if not out[-1].name:
            out.pop()
    return out


def parse_stats_js(js_text: str) -> tuple[SiteStats, Dict[str, DistrictStat]]:
    """解析站方统计脚本 html/yp/ms_sjszxsqtotal.js。

    这是全站真实量级的唯一来源：
      ms_all = 744636  历史留言总量（74 万条）
    而列表分页只到 500 页（约 5 万条）—— 两者差着 90% 以上，
    报告里必须把这个缺口写清楚，不能把「分页够不到」说成「数据不存在」。
    """
    def num(var: str) -> Optional[int]:
        m = re.search(rf"\b{var}\s*=\s*(\d+)", js_text or "")
        return int(m.group(1)) if m else None

    site = SiteStats(
        last_month=num("ms_lastmonth"),
        this_year=num("ms_thisyear"),
        total=num("ms_all"),
        last_month_accepted=num("ms_lastmonth_reply"),
        this_year_accepted=num("ms_thisyear_reply"),
        total_accepted=num("ms_all_reply"),
    )
    districts: Dict[str, DistrictStat] = {}
    for tid, (var, name) in DISTRICT_VARS.items():
        districts[tid] = DistrictStat(
            typeid=tid, name=name,
            last_month_msgs=num(var),
            last_month_accepted=num(var + "_reply"),
        )
    return site, districts


def fetch_taxonomy(client, stats_js_url: str = STATS_JS):
    """一次性把分类体系拉全：列表页 HTML + 统计脚本。"""
    from .config import LIST_URL
    r = client.get(LIST_URL, throttle="list")
    depts = parse_depts(r.text)
    districts = parse_districts(r.text)
    site_stats = SiteStats()
    try:
        jr = client.get(stats_js_url, throttle="list")
        site_stats, dstats = parse_stats_js(jr.text)
        for d in districts:
            s = dstats.get(d.typeid)
            if s:
                d.last_month_msgs = s.last_month_msgs
                d.last_month_accepted = s.last_month_accepted
    except Exception:
        pass
    return {
        "depts": depts,
        "districts": districts,
        "site_stats": site_stats,
        "categories": CATEGORIES,
        "states": STATES,
        "lights": LIGHTS,
        "mailboxes": MAILBOXES,
    }


def dept_to_dict(d: Dept) -> Dict:
    return asdict(d)
