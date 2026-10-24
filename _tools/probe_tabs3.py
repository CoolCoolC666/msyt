# -*- coding: utf-8 -*-
"""
probe_tabs3.py —— 定位「107 家」与实际 109 个之间的那 2 个差
================================================================
已确认的事实（probe_tabs / probe_tabs2）：
  · 5 个 pane 全部在 HTML 里；tab2..5 只是 `display:none`（前端切换）
  · 每个 pane 隐藏条目数 = 0；整页 typeid 去重 = 109
  · tab1 集合 == tab2..5 并集（都是 109），程序没漏
  · 「107」在页面里**只出现在 tab1 的标题文字**里，是写死的

本脚本要验的唯一假设：
    标题写的是「107家部门与企事业单位**在线**」
    → 若「在线」= 该部门当前**有留言**，那 109 家里应有 2 家是 0 留言
    → 查 tab1 每个部门后面的 num（留言数）即可定案

跑法：
    python _tools\\probe_tabs3.py
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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def main() -> int:
    r = requests.get(LIST_URL, headers={"User-Agent": UA,
                                        "Accept-Language": "zh-CN,zh;q=0.9"}, timeout=25)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")

    pane = soup.select_one("#listranking1")
    rows = []
    for a in pane.select('a[href*="typeid="]'):
        m = re.search(r"typeid=(\d+)", a.get("href") or "")
        if not m:
            continue
        tid = m.group(1)
        name = a.get_text(strip=True)
        # 同一个 <li> 里的 num span = 该部门的留言数
        li = a.find_parent("li")
        num = None
        if li is not None:
            sp = li.select_one("span.xg1.num") or li.select_one("span.num")
            if sp:
                t = sp.get_text(strip=True)
                if t.isdigit():
                    num = int(t)
        # 该 li 里还可能有别的标记（如 "新"）
        rows.append({"typeid": tid, "name": name, "num": num})

    print("=" * 72)
    print(f"tab1 部门 {len(rows)} 个 —— 每个后面的留言数")
    print("=" * 72)
    zero = [x for x in rows if x["num"] == 0]
    nonum = [x for x in rows if x["num"] is None]
    nonzero = [x for x in rows if x["num"] is not None and x["num"] > 0]
    print(f"  有留言数且 >0 : {len(nonzero)}")
    print(f"  留言数 == 0   : {len(zero)}  {[x['name'] for x in zero]}")
    print(f"  没有 num 标记 : {len(nonum)}  {[x['name'] for x in nonum]}")
    print(f"  → 若「在线」= 有留言，则在线家数 = {len(nonzero)}"
          f"（站方标题写 107）")
    print(f"  → 109 - {len(nonzero)} = {109 - len(nonzero)}")

    print()
    print("  --- 留言数最小的 12 个（最像「不在线」的） ---")
    for x in sorted([x for x in rows if x["num"] is not None],
                    key=lambda y: y["num"])[:12]:
        print(f"     {x['typeid']:>5}  {x['name']:<26} {x['num']}")

    print()
    print("  --- 各分组里 num 最小的（看是不是同一个部门在多组） ---")
    for i in range(2, 6):
        p = soup.select_one(f"#listranking{i}")
        if p is None:
            continue
        items = []
        for a in p.select('a[href*="typeid="]'):
            m = re.search(r"typeid=(\d+)", a.get("href") or "")
            if not m:
                continue
            li = a.find_parent("li")
            num = None
            if li is not None:
                sp = li.select_one("span.xg1.num") or li.select_one("span.num")
                if sp:
                    t = sp.get_text(strip=True)
                    if t.isdigit():
                        num = int(t)
            items.append((m.group(1), a.get_text(strip=True), num))
        lo = sorted([x for x in items if x[2] is not None], key=lambda y: y[2])[:4]
        print(f"     tab{i}: 最小的 → " + ", ".join(f"{n}({v})" for _, n, v in lo))

    # 顺带：站方有没有在别处给出「在线」口径（如 JS 变量）
    print()
    print("=" * 72)
    print("附加：页面上与「在线/online」相关的文字")
    print("=" * 72)
    for m in re.finditer(r"在线", r.text):
        s = max(0, m.start() - 90)
        e = min(len(r.text), m.end() + 60)
        ctx = re.sub(r"\s+", " ", r.text[s:e])
        if "107" in ctx or "部门" in ctx or "单位" in ctx:
            print(f"  …{ctx}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
