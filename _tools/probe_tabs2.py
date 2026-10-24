# -*- coding: utf-8 -*-
"""
probe_tabs2.py —— 深挖：站点搜索框索引的是不是另一份名单？
================================================================
上一轮（probe_tabs.py）已确认：tab1 与 tab2..5 并集**完全相等**（109 = 109），
程序没漏任何部门。但站方标题写「107家」，实际 109。

用户直觉「总感觉落下了什么」还有两个可能藏点：
  A. 搜索框（#deptSearch）索引的是**另一份**名单，比 5 个 pane 大
  B. pane 里有**被隐藏**的条目（display:none / class 隐藏），肉眼看不见但确实存在

跑法：
    python _tools\\probe_tabs2.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "程序"))

import requests                                   # noqa: E402
from bs4 import BeautifulSoup                    # noqa: E402

from msyt.config import LIST_URL                 # noqa: E402
from msyt.taxonomy import PANE_GROUPS            # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def main() -> int:
    r = requests.get(LIST_URL, headers={"User-Agent": UA,
                                        "Accept-Language": "zh-CN,zh;q=0.9"}, timeout=25)
    r.encoding = "utf-8"
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    block = soup.select_one("#rankingwz")
    print(f"列表页 HTTP {r.status_code} / {len(html):,} 字节\n")

    # ---------------------------------------------------------- A. 搜索框的数据源
    print("=" * 72)
    print("A. 部门搜索框（#deptSearch）的数据从哪来")
    print("=" * 72)
    box = soup.select_one("#deptSearch")
    if box is None:
        print("  没找到 #deptSearch")
    else:
        print(f"  搜索框 HTML: {str(box)[:400]}")
        # 搜索框附近的 script
        for sc in block.find_all("script"):
            txt = sc.get_text() or ""
            if not txt.strip():
                continue
            if re.search(r"typeid|dept|search|pinyin", txt, re.I):
                print("  --- 同区块 script（前 900 字）---")
                print("  " + txt.strip()[:900].replace("\n", "\n  "))

    # 全页搜可能的数据数组
    print()
    print("  --- 全页搜索 '部门数组/JSON' 类数据源 ---")
    for pat in [r"var\s+\w*dept\w*\s*=\s*[\[{]", r"\[\s*\{\s*['\"]?(id|typeid|name)",
                r"deptList|deptlist|dept_data|deptData|searchData|keywordData"]:
        hits = re.findall(pat, html)
        if hits:
            print(f"    命中 {pat} × {len(hits)}")
    # 所有指向 typeid 的链接总数（整页，不限 rankingwz）
    all_typeids = re.findall(r"typeid=(\d+)", html)
    print(f"    整页 typeid= 出现 {len(all_typeids)} 次，去重 {len(set(all_typeids))} 个")
    in_block = re.findall(r"typeid=(\d+)", str(block))
    print(f"    #rankingwz 内 typeid= 出现 {len(in_block)} 次，去重 {len(set(in_block))} 个")

    # ---------------------------------------------------------- B. 被隐藏的条目
    print()
    print("=" * 72)
    print("B. pane 里有没有「渲染了但被隐藏」的条目")
    print("=" * 72)
    for i in range(1, 6):
        pane = soup.select_one(f"#listranking{i}")
        if pane is None:
            continue
        hidden = []
        for a in pane.select('a[href*="typeid="]'):
            st = (a.get("style") or "").replace(" ", "").lower()
            cls = " ".join(a.get("class") or []).lower()
            if "display:none" in st or "visibility:hidden" in st or "hide" in cls:
                hidden.append(a.get_text(strip=True))
        # 整个 pane 的 style
        pst = (pane.get("style") or "")
        print(f"  pane{i}: 条目 {len(pane.select('a[href*=\"typeid=\"]'))} / "
              f"隐藏 {len(hidden)} {hidden if hidden else ''} / "
              f"pane style={pst.strip()!r}")

    # ---------------------------------------------------------- C. 「107」出现在哪
    print()
    print("=" * 72)
    print("C. 「107」这个数字出现在页面的哪些位置")
    print("=" * 72)
    for m in re.finditer(r"107", html):
        s = max(0, m.start() - 120)
        e = min(len(html), m.end() + 120)
        ctx = re.sub(r"\s+", " ", html[s:e])
        print(f"  …{ctx}…")
    if not re.search(r"107", html):
        print("  页面上没有 107 这个数字")

    # ---------------------------------------------------------- D. 各 tab 成员清单
    print()
    print("=" * 72)
    print("D. 5 个 tab 的完整成员清单（typeid + 名称）")
    print("=" * 72)
    panes = {}
    for i in range(1, 6):
        pane = soup.select_one(f"#listranking{i}")
        members = []
        if pane:
            for a in pane.select('a[href*="typeid="]'):
                m = re.search(r"typeid=(\d+)", a.get("href") or "")
                if m:
                    members.append((m.group(1), a.get_text(strip=True)))
        panes[i] = members

    # 导出成 JSON 供人工复核
    out = {}
    for i in range(1, 6):
        out[f"tab{i}"] = {"title": (soup.select_one(f"h3#tab{i}").get_text(" ", strip=True)
                                   if soup.select_one(f"h3#tab{i}") else ""),
                          "group": PANE_GROUPS.get(i, ("", ""))[1],
                          "count": len(panes[i]),
                          "members": [{"typeid": t, "name": n} for t, n in panes[i]]}
    pj = ROOT / "复核" / "部门目录_5个tab_成员清单.json"
    pj.parent.mkdir(parents=True, exist_ok=True)
    pj.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  成员清单已导出：{pj.relative_to(ROOT)}")

    for i in range(1, 6):
        print(f"\n  ── tab{i}  {out[f'tab{i}']['title']}  ({out[f'tab{i}']['count']} 个) ──")
        for t, n in panes[i]:
            print(f"     {t:>5}  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
