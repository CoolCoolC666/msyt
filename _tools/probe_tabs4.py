# -*- coding: utf-8 -*-
"""
probe_tabs4.py —— 逐个核查 109 个部门「有没有留言」
================================================================
要回答的最后一问：
    站方 tab1 标题写「107家部门与企事业单位**在线**」，而 DOM 里实际有 109 家。
    两种解释：
      (a) 标题过时（站方后加了部门忘了改标题）→ 109 才是真的
      (b) 「在线」= 当前有留言 → 109 家里有 2 家是空壳

解法：对每个 typeid 取一次它的筛选列表页，看有没有帖子。
节奏：间隔 1.5 秒（与程序 list 采集同量级；实测该站列表连续请求 12/12 全 200，
      无 403/429），先跑 2 个样本确认，再放全量。

输出：
    复核/部门在线核查_109家.csv   —— 人工可复核的原始证据
    复核/部门在线核查_结论.txt    —— 汇总
"""

from __future__ import annotations

import csv
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "程序"))

import requests                                   # noqa: E402
from bs4 import BeautifulSoup                    # noqa: E402

from msyt.config import LIST_URL                 # noqa: E402
from msyt.parsers import parse_list_page_info    # noqa: E402
from msyt.taxonomy import parse_depts            # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
INTERVAL = 1.5
OUT_CSV = ROOT / "复核" / "部门在线核查_109家.csv"
OUT_TXT = ROOT / "复核" / "部门在线核查_结论.txt"


def get_depts():
    r = requests.get(LIST_URL, headers={"User-Agent": UA}, timeout=25)
    r.encoding = "utf-8"
    return parse_depts(r.text)


def check_one(sess, tid: str) -> dict:
    url = f"{LIST_URL}&filter=typeid&typeid={tid}"
    try:
        r = sess.get(url, timeout=25)
        r.encoding = "utf-8"
    except Exception as e:                                    # noqa: BLE001
        return {"http": "ERR", "err": str(e)[:80], "rows": -1,
                "totalpage": -1, "total_threads": -1}
    html = r.text
    info = parse_list_page_info(html)
    rows = len(re.findall(r'id="normalthread_\d+"', html))
    return {
        "http": r.status_code,
        "err": "",
        "rows": rows,
        "totalpage": info.get("total_page", 0),
        "total_threads": info.get("total_threads", 0),
        "html_len": len(html),
    }


def main() -> int:
    depts = get_depts()
    print(f"部门 {len(depts)} 个\n", flush=True)
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})

    # ---- 先跑 2 个样本，确认站方没拦 ----
    print("小样本探测（2 个）…", flush=True)
    sample = [depts[0].typeid, depts[1].typeid]
    for tid in sample:
        res = check_one(sess, tid)
        print(f"  typeid={tid} {res['http']} rows={res['rows']} "
              f"totalpage={res['totalpage']} threads={res['total_threads']}", flush=True)
        time.sleep(INTERVAL)
    bad = [r for r in (check_one(sess, t) for t in sample) if r["http"] != 200]
    if bad:
        print(f"[!] 小样本里出现非 200（{bad[0]['http']}），暂停，不放全量。")
        return 2

    # ---- 全量 ----
    rows_out = []
    zeros = []
    for i, d in enumerate(depts, 1):
        res = check_one(sess, d.typeid)
        rows_out.append({
            "typeid": d.typeid,
            "部门": d.name,
            "分组": d.group_code,
            "HTTP": res["http"],
            "首屏帖子行数": res["rows"],
            "站方总页数": res["totalpage"],
            "站方总帖数": res["total_threads"],
            "判为": ("无留言" if res["rows"] == 0 and res["http"] == 200
                     else ("抓取失败" if res["http"] != 200 else "有留言")),
        })
        if res["rows"] == 0 and res["http"] == 200:
            zeros.append((d.typeid, d.name, d.group_code))
        if i % 10 == 0 or i == len(depts):
            print(f"  {i}/{len(depts)}  无留言 {len(zeros)} 个", flush=True)
        time.sleep(INTERVAL)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)

    has = sum(1 for r in rows_out if r["判为"] == "有留言")
    fail = sum(1 for r in rows_out if r["判为"] == "抓取失败")
    lines = [
        "=" * 66,
        "部门「在线」核查结论",
        "=" * 66,
        f"检查部门总数        : {len(rows_out)}",
        f"有留言（首屏有帖子）: {has}",
        f"无留言（首屏 0 行）  : {len(zeros)}",
        f"抓取失败            : {fail}",
        "",
        f"站方 tab1 标题写的是 : 107 家",
        f"实际 DOM 条目        : {len(rows_out)} 家",
        "",
    ]
    if len(zeros) == 2 and fail == 0:
        lines += [
            "★ 判定：标题的「107家…在线」= 当前**有留言**的部门家数。",
            "  DOM 里 109 家是全部在册部门，其中 2 家当前没有留言，",
            "  所以站方标题写 107 —— 数字自洽，站方没有写错。",
            "",
            "  这 2 家是：",
        ]
        for t, n, g in zeros:
            lines.append(f"    typeid={t}  {n}  （分组：{g or '（无）'}）")
    elif zeros:
        lines += [
            f"★ 判定：{len(zeros)} 家当前无留言。",
            f"  若恰为 2 家，则标题「107家…在线」= 有留言的家数，站方数字自洽。",
            "  否则标题是过时/另有口径。",
            "",
        ]
        for t, n, g in zeros:
            lines.append(f"    typeid={t}  {n}  （分组：{g or '（无）'}）")
    else:
        lines += ["★ 判定：109 家全部有留言 → 标题的「107」是**过时数字**，站方加了部门没改标题。"]
    lines += ["", f"证据 CSV: {OUT_CSV.relative_to(ROOT)}"]
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
