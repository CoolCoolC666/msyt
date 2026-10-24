# -*- coding: utf-8 -*-
"""
dump_postmsg_anchors.py —— 把详情页的 `postmsg_{pid}` 锚点单独抽成一个 CSV，供人工复核
=========================================================================================
为什么要有这个文件
------------------
程序里有一条**唯一**能挡住「静默串帖」的判据：

    每页 ajax_getpost.php 返回的 pid 键集合  ==  该页 HTML 里 postmsg_{pid} 锚点集合（精确相等）

`tid` 参数**完全不参与内容选择**，token 才是唯一钥匙（实测：A 帖 token 配 B 帖 tid，
服务端照样返回 A 帖楼层且 code=1，不报错）。所以这条判据成立与否，直接决定
「宁可不收，不可收错」是不是真的成立。

判据是程序自己判的 —— 人工要复核，就得能看到**原始证据**，而不是程序的结论。
本脚本输出的每一行都是**页面里原样取出来的**，未经加工：

  · 锚点HTML原文   —— 锚点在源 HTML 里的真实样子（前后各截一段）
  · 楼层号原文/HTML —— 楼主 / <em>5</em><sup>#</sup> / 顶来自 2# 三种形态原样保留
  · ajax是否返回   —— 这一楼到底有没有拿到正文
  · 正文开头/结尾  —— 用来肉眼确认正文有没有被截断
  · 重复位置       —— 置顶回复会在第 1 页顶部**再渲染一次**，同一个 pid 出现两回

用法
----
    python _tools\\dump_postmsg_anchors.py --tid 8339985
    python _tools\\dump_postmsg_anchors.py --tid 8339985 8343362
    python _tools\\dump_postmsg_anchors.py --tid 8339985 --out 复核\\xxx.csv

输出默认落在 `复核/postmsg锚点_人工复核.csv`（UTF-8-BOM，Excel 双击可开）。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # ...\02 民生e网通
sys.path.insert(0, str(ROOT / "程序"))                   # 让 import msyt 生效

from bs4 import BeautifulSoup                              # noqa: E402

from msyt.config import AJAX_URL, THREAD_URL, Config      # noqa: E402
from msyt.http_client import Client                        # noqa: E402
from msyt.parsers import (parse_ajax_posts, parse_thread_page)  # noqa: E402
from msyt.textutil import html_to_text                    # noqa: E402

#: 一帖最多翻多少页（保护上限；正常 1~3 页）
MAX_PAGES = 20


def anchor_snippet(html: str, pid: int, before: int = 160, after: int = 300) -> str:
    """截出 `id="postmsg_{pid}"` 在源 HTML 里的原样片段。

    保留上下文是为了让人看出**锚点本身长什么样**：
    实测它是个**空容器**（`<span id="postmsg_xxx"></span>`），
    正文是站方 JS 用 `jQuery('#postmsg_'+pid).replaceWith(html)` 事后塞进去的。
    """
    m = re.search(r'id="postmsg_%d"' % pid, html)
    if not m:
        return ""
    s = max(0, m.start() - before)
    e = min(len(html), m.end() + after)
    return re.sub(r"\s+", " ", html[s:e])


def floor_labels(html: str) -> dict:
    """pid → (楼层号文本, 楼层号内部 HTML)。

    三种形态都要原样留：
        "楼主"                    → 第 1 楼
        "<em>5</em><sup>#</sup>"  → 5#
        "顶来自 2#" / "来自 2#"    → 被顶替/置顶的楼层
    """
    out = {}
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select("a[id^=postnum]"):
        pid_s = (el.get("id") or "")[len("postnum"):]
        if not pid_s.isdigit():
            continue
        out[int(pid_s)] = (el.get_text(" ", strip=True),
                           re.sub(r"\s+", " ", el.decode_contents()).strip())
    return out


def fetch_tid(cli: Client, tid: int):
    """抓一帖的所有页。返回 pages 列表，每项 (page_no, ThreadPage, {pid:html}, raw_html)。"""
    pages = []
    total_pages = 0
    page_no = 1
    while page_no <= MAX_PAGES:
        url = THREAD_URL.format(tid=tid, page=page_no)
        try:
            html = cli.get(url, referer=THREAD_URL.format(tid=tid, page=1)).text
        except Exception as e:                                   # noqa: BLE001
            print(f"[!] tid={tid} page{page_no} 取页失败: {e}")
            break
        tp = parse_thread_page(html, tid=tid, page_no=page_no)
        if not tp.token:
            print(f"[!] tid={tid} page{page_no} 没取到 token，停止")
            break
        if not total_pages:
            total_pages = tp.total_pages
        if page_no > total_pages:
            print(f"[i] tid={tid} 已到末页（站点分页块显示共 {total_pages} 页），停止")
            break
        if not tp.pids:
            print(f"[i] tid={tid} page{page_no} 无楼层锚点，停止")
            break

        # ★ token 即用：中间不得插入任何 sleep
        try:
            ar = cli.get_nowait(AJAX_URL,
                                params={"token": tp.token, "tid": str(tid), "src": "pc"},
                                referer=url)
            payload = ar.json()
        except Exception as e:                                   # noqa: BLE001
            print(f"[!] tid={tid} page{page_no} ajax 失败: {e}")
            break

        posts_html = parse_ajax_posts(payload)
        code = payload.get("code")
        print(f"[·] tid={tid} page{page_no}: 锚点 {len(tp.pids)} 个 / "
              f"ajax 返回 {len(posts_html)} 个 (code={code})")
        pages.append((page_no, tp, posts_html, html))
        page_no += 1
    return pages, total_pages


def main() -> int:
    ap = argparse.ArgumentParser(description="抽出 postmsg_{pid} 锚点供人工复核")
    ap.add_argument("--tid", type=int, nargs="+", required=True, help="帖子 tid（可多个）")
    ap.add_argument("--out", default="", help="输出 CSV 路径")
    ap.add_argument("--head-chars", type=int, default=110, help="正文开头保留字数")
    ap.add_argument("--tail-chars", type=int, default=110, help="正文结尾保留字数")
    args = ap.parse_args()

    cfg = Config.load()
    cli = Client(cfg, verbose=True)

    rows = []
    summaries = []

    for tid in args.tid:
        pages, total_pages = fetch_tid(cli, tid)
        if not pages:
            print(f"[!] tid={tid} 一页都没取到，跳过")
            continue

        title = pages[0][1].title
        all_floors = []
        for _, tp, _, _ in pages:
            all_floors.extend(f.floor for f in tp.floors.values() if f.floor > 0)
        max_floor = max(all_floors) if all_floors else 0

        # ---- 先扫一遍：每个 pid 在全帖出现几次、分别在哪儿、正文是否一致 ----
        occurrences = {}          # pid → [(page_no, idx)]
        first_text = {}           # pid → 首次出现的正文文本
        for pno, tp, posts_html, _ in pages:
            for idx, pid in enumerate(tp.pids, 1):
                occurrences.setdefault(pid, []).append((pno, idx))
                if pid not in first_text:
                    h = posts_html.get(pid)
                    first_text[pid] = html_to_text(h, "") if h is not None else ""

        for pno, tp, posts_html, raw in pages:
            labels = floor_labels(raw)
            n_anchor = len(tp.pids)
            n_ajax = len(posts_html)
            equal = "是" if set(posts_html.keys()) == set(tp.pids) else "否"
            only_anchor = sorted(set(tp.pids) - set(posts_html.keys()))
            only_ajax = sorted(set(posts_html.keys()) - set(tp.pids))

            for idx, pid in enumerate(tp.pids, 1):
                fm = tp.floors.get(pid)
                label_txt, label_html = labels.get(pid, ("", ""))
                content_html = posts_html.get(pid)
                got = "是" if content_html is not None else "否"
                text = html_to_text(content_html, "") if content_html is not None else ""
                chars = len(text)

                occ = occurrences.get(pid, [])
                dup_n = len(occ)
                dup_pos = " ".join(f"页{p}/序{i}" for p, i in occ)
                same = ""
                if dup_n > 1:
                    same = "是" if text == first_text.get(pid) else "否（正文不一致！）"

                # 角色：头 / 尾 / 置顶 / 普通
                role = []
                if fm and fm.floor == 1:
                    role.append("头楼(楼主)")
                if fm and fm.floor == max_floor:
                    role.append("尾楼")
                if "顶" in label_txt:
                    role.append("置顶")
                if not role:
                    role.append("中间楼")

                # 楼层号说明：本页标签没号、但同 pid 在别处有号 → 指向那里
                floor_note = ""
                if (fm is None or fm.floor == 0) and dup_n > 1:
                    other = [o for o in occ if o != (pno, idx)]
                    floor_note = ("本页标签无楼层号（置顶渲染）；同 pid 的真楼层号见 "
                                  + " ".join(f"页{p}/序{i}" for p, i in other))
                elif fm and fm.floor > 0:
                    floor_note = f"本页即真楼层号 {fm.floor}"

                rows.append({
                    "tid": tid,
                    "页码": pno,
                    "页内文档序": idx,
                    "pid": pid,
                    "锚点HTML原文": anchor_snippet(raw, pid),
                    "楼层号原文": label_txt,
                    "楼层号HTML": label_html,
                    "楼层号": (fm.floor if fm else 0),
                    "楼层号说明": floor_note,
                    "是否楼主": (1 if fm and fm.is_author else 0),
                    "作者": (fm.author if fm else ""),
                    "作者uid": (fm.author_uid if fm else ""),
                    "发帖时间": (fm.post_time if fm else ""),
                    "ajax是否返回": got,
                    "正文字数": chars,
                    "正文开头": text[:args.head_chars],
                    "正文结尾": text[-args.tail_chars:] if text else "",
                    "本页锚点数": n_anchor,
                    "本页ajax数": n_ajax,
                    "本页pid集合精确相等": equal,
                    "锚点独有(ajax缺)": ",".join(str(x) for x in only_anchor),
                    "ajax独有(锚点缺)": ",".join(str(x) for x in only_ajax),
                    "该pid全帖出现次数": dup_n,
                    "重复位置": dup_pos if dup_n > 1 else "",
                    "本页正文与首次出现是否相同": same,
                    "角色": "/".join(role),
                    "人工复核备注": "",
                })

        got_n = sum(1 for r in rows if r["tid"] == tid and r["ajax是否返回"] == "是")
        my_rows = [r for r in rows if r["tid"] == tid]
        empty_n = sum(1 for r in my_rows if r["正文字数"] == 0)
        all_equal = all(r["本页pid集合精确相等"] == "是" for r in my_rows)
        floors = sorted({r["楼层号"] for r in my_rows if r["楼层号"] > 0})
        gaps = [i for i in range(1, max_floor + 1) if i not in floors] if floors else []
        dup_pids = sorted({r["pid"] for r in my_rows if r["该pid全帖出现次数"] > 1})

        summaries.append({
            "tid": tid, "title": title, "pages": len(pages), "total_pages": total_pages,
            "anchors": len(my_rows), "got": got_n, "empty": empty_n,
            "all_equal": all_equal, "floors": floors, "gaps": gaps, "dup_pids": dup_pids,
        })

    if not rows:
        print("[!] 没东西可导出")
        return 1

    # ---------------------------------------------------------------- 写盘
    out = Path(args.out) if args.out else ROOT / "复核" / "postmsg锚点_人工复核.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # ---------------------------------------------------------------- 摘要
    print()
    print("=" * 74)
    for s in summaries:
        print(f"tid            : {s['tid']}")
        print(f"标题           : {s['title']}")
        print(f"抓到的页       : {s['pages']} 页（站点分页块上限 {s['total_pages']}）")
        print(f"锚点出现次数   : {s['anchors']}（去重后 {s['anchors'] - (len(s['dup_pids']))} 楼）")
        print(f"ajax 命中      : {s['got']} / {s['anchors']}")
        print(f"空正文楼层     : {s['empty']}")
        print(f"每页 pid 精确相等: {'全部是' if s['all_equal'] else '有不相等'}")
        print(f"楼层号         : {s['floors']}")
        print(f"楼层号缺口     : {s['gaps'] if s['gaps'] else '无'}")
        print(f"重复渲染的 pid  : {s['dup_pids'] if s['dup_pids'] else '无'}")
        print("-" * 74)
    print(f"CSV            : {out}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
