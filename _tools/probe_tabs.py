# -*- coding: utf-8 -*-
"""
probe_tabs.py —— 核查部门目录那 5 个 tab 的集合关系
================================================================
要回答的问题（用户的直觉：「总感觉落下了什么」）：

  ① 每个 tab 到底有多少个部门？（tab1 标题写「107家」）
  ② tab2..tab5 的并集 与 tab1 的集合 是否相等？差在谁？
  ③ 有没有部门同时属于多个分组？（重叠）
  ④ 站方 tab 标题里的数字 与 实际条目数 是否一致？
  ⑤ 程序库里的 depts 表 与页面实测 是否一致？

跑法：
    python _tools\\probe_tabs.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "程序"))

import requests                                   # noqa: E402
from bs4 import BeautifulSoup                    # noqa: E402

from msyt.config import LIST_URL                 # noqa: E402
from msyt.taxonomy import PANE_GROUPS, parse_depts   # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def main() -> int:
    r = requests.get(LIST_URL, headers={"User-Agent": UA,
                                        "Accept-Language": "zh-CN,zh;q=0.9"}, timeout=25)
    r.encoding = "utf-8"
    html = r.text
    print(f"列表页 HTTP {r.status_code} / {len(html):,} 字节\n")

    soup = BeautifulSoup(html, "html.parser")
    block = soup.select_one("#rankingwz")
    if block is None:
        print("[!] 页面里没有 #rankingwz 区块 —— 结构变了？")
        return 1

    # ---------- ① tab 标题原文（含站方自己写的数字） ----------
    print("=" * 72)
    print("① 5 个 tab 的标题原文")
    print("=" * 72)
    tab_titles = {}
    for i in range(1, 6):
        h = block.select_one(f"h3#tab{i}")
        title = h.get_text(" ", strip=True) if h else "(缺失)"
        tab_titles[i] = title
        num = re.findall(r"\d+", title)
        print(f"  tab{i}: {title!r}   标题里的数字={num or '无'}")

    # ---------- ② 每个 pane 的成员集合 ----------
    panes = {}
    for i in range(1, 6):
        pane = soup.select_one(f"#listranking{i}")
        if pane is None:
            print(f"  [WARN] #listranking{i} 不存在")
            panes[i] = []
            continue
        members = []
        for a in pane.select('a[href*="typeid="]'):
            m = re.search(r"typeid=(\d+)", a.get("href") or "")
            if m:
                members.append((m.group(1), a.get_text(strip=True)))
        panes[i] = members
        ids = [t for t, _ in members]
        print(f"  pane{i} ({PANE_GROUPS.get(i, ('?', '?'))[1]}): "
              f"{len(members)} 条 / 去重 {len(set(ids))}")

    # ---------- ③ 集合关系 ----------
    print()
    print("=" * 72)
    print("② 集合关系：tab1（全集） vs tab2..tab5（分组）")
    print("=" * 72)
    set1 = {t for t, _ in panes.get(1, [])}
    set2345 = set()
    for i in (2, 3, 4, 5):
        set2345 |= {t for t, _ in panes.get(i, [])}

    print(f"  tab1 条目数        : {len(panes.get(1, []))}  (去重 {len(set1)})")
    print(f"  tab2..5 并集       : {len(set2345)}")
    print(f"  tab1 ∩ tab2..5     : {len(set1 & set2345)}")
    only1 = set1 - set2345
    only2345 = set2345 - set1
    print(f"  只在 tab1 的       : {len(only1)}  {sorted(only1)}")
    print(f"  只在 tab2..5 的    : {len(only2345)}  {sorted(only2345)}")
    if only1:
        name1 = {t: n for t, n in panes[1]}
        for t in sorted(only1):
            print(f"      └ {t} {name1.get(t, '?')}")
    if only2345:
        for i in (2, 3, 4, 5):
            for t, n in panes.get(i, []):
                if t in only2345:
                    print(f"      └ {t} {n}（来自 tab{i}）")

    # ---------- ④ 重叠：哪些部门属于多个分组 ----------
    print()
    print("=" * 72)
    print("③ 同时属于多个分组的部门（这解释了分组数相加 > 全集数）")
    print("=" * 72)
    name_of = {}
    for i in (1, 2, 3, 4, 5):
        for t, n in panes.get(i, []):
            name_of.setdefault(t, n)
    multi = {}
    for i in (2, 3, 4, 5):
        for t, n in panes.get(i, []):
            multi.setdefault(t, []).append((i, PANE_GROUPS[i][1]))
    for t, gs in sorted(multi.items(), key=lambda kv: -len(kv[1])):
        if len(gs) > 1:
            print(f"  {t:>5} {name_of.get(t, '?'):<24} → "
                  + " / ".join(f"tab{i}({n})" for i, n in gs))
    overlap_extra = sum(len(g) - 1 for g in multi.values() if len(g) > 1)
    print(f"  多重归属造成的「多计」数 = {overlap_extra}")

    # ---------- ⑤ 分组计数 vs 标题数字 ----------
    print()
    print("=" * 72)
    print("④ 站方标题数字 vs 实际条目数")
    print("=" * 72)
    for i in range(1, 6):
        claimed = re.findall(r"(\d+)\s*家", tab_titles.get(i, ""))
        claimed = int(claimed[0]) if claimed else None
        actual = len(set(t for t, _ in panes.get(i, [])))
        flag = "" if claimed is None or claimed == actual else "   ← 对不上"
        print(f"  tab{i}: 标题称 {claimed if claimed is not None else '（无数字）':>6} 家 / "
              f"实际 {actual:>4} 个{flag}")

    # ---------- ⑥ 程序解析结果 vs 页面 ----------
    print()
    print("=" * 72)
    print("⑤ 程序 parse_depts() 的结果 vs 页面实测")
    print("=" * 72)
    depts = parse_depts(html)
    print(f"  parse_depts 给出 {len(depts)} 个部门")
    all_ids = {d.typeid for d in depts}
    print(f"  页面 tab1 去重 {len(set1)} / tab2..5 并集 {len(set2345)}")
    print(f"  程序集合 == tab1 集合 ? {all_ids == set1}")
    print(f"  程序集合 == tab2..5 并集 ? {all_ids == set2345}")
    miss = (set1 | set2345) - all_ids
    extra = all_ids - (set1 | set2345)
    if miss:
        print(f"  页面有、程序没有 : {len(miss)} {sorted(miss)}")
        for t in sorted(miss):
            print(f"      └ {t} {name_of.get(t, '?')}")
    if extra:
        print(f"  程序有、页面没有 : {len(extra)} {sorted(extra)}")

    gcount = {}
    for d in depts:
        for g in (d.group_code or "").split(","):
            if g:
                gcount[g] = gcount.get(g, 0) + 1
    print(f"  各分组部门数（含重叠）: {gcount}")
    print(f"  各分组相加 = {sum(gcount.values())}，去重后 = {len(all_ids)}")

    # ---------- ⑦ 库里的 depts 表 ----------
    print()
    print("=" * 72)
    print("⑥ 本地库 depts 表")
    print("=" * 72)
    import sqlite3
    db = ROOT / "程序" / "data" / "msyt.db"
    if db.exists():
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT typeid,name,group_code FROM depts").fetchall()
        print(f"  库中 {len(rows)} 条")
        g2 = {}
        for r in rows:
            for g in (r["group_code"] or "").split(","):
                if g:
                    g2[g] = g2.get(g, 0) + 1
        print(f"  库内分组计数: {g2}")
        db_ids = {r["typeid"] for r in rows}
        print(f"  库集合 == 页面 tab1 ? {db_ids == set1}")
        d = db_ids - set1
        if d:
            print(f"  库有、页面 tab1 没有 : {len(d)} {sorted(d)}")
        d2 = db_ids - set2345
        if d2:
            print(f"  库有、页面 tab2..5 没有 : {len(d2)} {sorted(d2)}")
    else:
        print("  （库不存在）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
