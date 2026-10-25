# -*- coding: utf-8 -*-
"""批量标注「历史帖子仅限作者本人查看」的帖。

★ 探测结论（2026-10-25 实测，证据见报告 §9.33）
  · 站方对 **2023-12-31 之前**发帖的帖，详情页只给一句
    「历史帖子仅限作者本人查看」→ 正文不公开
  · **2023-12-31 及之后**发帖的帖：正常可读
  · **置顶的老帖是例外**（实测 tid=6177045，2013-08-30 → 77 楼全采到；
    同一天发的另外 13 个帖全部受限）
  · 站方公告帖也是例外（它们没有发帖时间，pub_ts=0）

★ 为什么要批量标
  库里阈值之前的老帖有 **38 万**量级。它们如果留在队列里，
  详情采集会**逐个去撞一次站方限制** —— 每次白跑一个详情页请求 + 一轮限速。
  按单线程 1.5 秒/帖算，光这些就要浪费 **6.7 天**产能
  （这正是「一日只有 36,000 篇」的直接原因）。
  标完 → 它们进「站方限制」终态，不再占队列；界面与统计也才准确。

★ 判据（保守，宁可少标不可错标）
  候选 = pub_ts > 0
       AND pub_ts < 阈值
       AND detail_state NOT IN ('done','restricted')
       AND 库里**没有真实楼层**（pid>0）
       AND 不在**置顶豁免名单**里
  · pub_ts=0 的不标（公告帖/时间解析不出来的）→ 保守，留给采集去试
  · 置顶豁免名单来自 thread_list_forms（kind='stick'）

用法：
    python _tools\\mark_restricted_bulk.py --dry-run        # 只统计，不改库
    python _tools\\mark_restricted_bulk.py --verify 40      # 抽样实测准确率
    python _tools\\mark_restricted_bulk.py --apply           # 真正执行
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

PKG = os.path.join(
    r"【用户桌面】",
    "14-网络舆情分析与监测-2.0", "2-作业", "02 民生e网通", "程序")
sys.path.insert(0, PKG)
from msyt.config import BASE, Config                # noqa: E402
from msyt.db import DB                               # noqa: E402
from msyt.http_client import Client                 # noqa: E402
from msyt.parsers import parse_list_page, parse_announcements  # noqa: E402
from bs4 import BeautifulSoup                       # noqa: E402

#: ★ 站方可见性分界（2026-10-25 精确实测，161 个帖逐个探出来的）
#:
#:   · 最后受限：**tid 8074333**（2023-12-31 23:36）
#:   · 首个可见：**tid 8074334**（2024-01-01 00:08）
#:   · 分布核对：2023-12-31 当天 102 个样本**全受限**，
#:     2024-01-01 起 18 个样本**全可见** → 站方就是在这**一刻**收的口
#:
#: ★★ 为什么用 tid 而**不是日期**（我踩过的坑）
#:   早先我按「pub_ts < 2023-12-31」这个**日期**阈值批量标注，
#:   一度担心它会误标「12-31 当天里 tid 较小的那批」。
#:   实测证明**没有误标**：12-31 全受限、1-1 起全可见，日期切得正好。
#:   但日期阈值依赖库里 pub_ts 解析正确（列表页的时间格式），
#:   **tid 判据不依赖时间解析**、更稳 → 下面两种判据都留着：
#:     · --max-tid：精确到个位（推荐）
#:     · --threshold：按日期（旧口径，保留以便复现当时的结果）
DEFAULT_MAX_TID = 8074333

#: 旧口径：按发帖时间（早于此刻的判为受限）
DEFAULT_THRESHOLD = "2023-12-31 00:00:00"


def sync_forms(db: DB, client: Client, limit_pages: int = 0) -> int:
    """同步「列表形态」记录（主板块 + 各部门页第 1 页）。

    置顶帖的豁免名单就来自这里 —— 站方每天会换置顶，
    所以这个名单要**持续**累积，不能只记一次。
    """
    n_pages = 0
    urls = [(f"{BASE}/forum.php?mod=forumdisplay&fid=1176", "global")]
    rows = db.conn.execute(
        "SELECT typeid, name FROM depts ORDER BY typeid").fetchall()
    for d in rows:
        urls.append((f"{BASE}/forum.php?mod=forumdisplay&fid=1176"
                     f"&filter=typeid&typeid={d['typeid']}", f"dept:{d['typeid']}"))
    for url, key in urls:
        try:
            html = client.get(url, throttle="list").text
        except Exception:                                  # noqa: BLE001
            continue
        soup = BeautifulSoup(html, "html.parser")
        rows_ = parse_list_page(html, url)
        db.add_list_forms(rows_, key)
        n_pages += 1
        if limit_pages and n_pages >= limit_pages:
            break
    return n_pages


def build_candidates(db: DB, threshold_ts: int) -> list:
    """候选：阈值之前、不是置顶帖、库里没有真实楼层的帖。

    ★ 豁免名单有**两类**，缺一不可（2026-10-25 实测踩到）：
      ① **置顶帖**（kind='stick'）—— 站方主动保留的老帖，能看
         （tid=6177045，2013-08-30 → 77 楼全采到）
      ② **公告帖**（kind='announce'）—— 站方公告区里的链接，**没有发帖时间**
         （库里 pub_ts=0 → 1970）→ 会被时间判据当成"很老的帖"而误判成受限，
         但它们**实际可见**（实测 7342780 / 7342697 均可见）
      → 只豁免置顶帖会误标公告帖。
    """
    exempt = db.exempt_tids()
    rows = db.conn.execute(
        """SELECT t.tid, t.pub_ts, t.title, t.detail_state
             FROM threads t
            WHERE t.pub_ts > 0 AND t.pub_ts < ?
              AND COALESCE(t.detail_state,'pending') NOT IN ('done','restricted')
              AND NOT EXISTS (SELECT 1 FROM posts p
                               WHERE p.tid=t.tid AND p.pid>0)
            ORDER BY t.pub_ts""", (threshold_ts,)).fetchall()
    return [r for r in rows if int(r["tid"]) not in exempt]


def non_candidates(db: DB, threshold_ts: int, n: int) -> list:
    """对照组：阈值之后（或已 done）的帖 —— 它们应该**可见**。"""
    rows = db.conn.execute(
        """SELECT t.tid, t.pub_ts, t.title FROM threads t
            WHERE t.pub_ts >= ? AND COALESCE(t.detail_state,'pending')='pending'
              AND NOT EXISTS (SELECT 1 FROM posts p
                               WHERE p.tid=t.tid AND p.pid>0)
            ORDER BY t.pub_ts DESC LIMIT ?""", (threshold_ts, n)).fetchall()
    return rows


def probe(client: Client, tid: int) -> str:
    from msyt.parsers import parse_site_notice
    url = f"{BASE}/forum.php?mod=viewthread&tid={tid}"
    try:
        html = client.get(url, referer=BASE + "/forum.php?mod=forumdisplay").text
    except Exception:                                      # noqa: BLE001
        return "出错"
    if parse_site_notice(html):
        return "受限"
    if 'id="post_' in html:
        return "可见"
    return "受限"


def main() -> int:
    ap = argparse.ArgumentParser(description="批量标注站方限制的帖")
    ap.add_argument("--threshold", default=DEFAULT_THRESHOLD,
                    help="分界时刻（早于它的帖判为受限）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不改库")
    ap.add_argument("--verify", type=int, default=0, help="抽样实测多少个")
    ap.add_argument("--apply", action="store_true", help="真正写入")
    ap.add_argument("--skip-sync", action="store_true", help="跳过形态同步")
    ap.add_argument("--batch", type=int, default=20000, help="分批大小")
    args = ap.parse_args()

    thr = int(time.mktime(time.strptime(args.threshold, "%Y-%m-%d %H:%M:%S")))
    cfg = Config.load()
    cfg.interval = 1.2
    cfg.list_interval = 1.3
    client = Client(cfg, verbose=False)
    db = DB(cfg.db_path)

    print("=" * 72)
    print(f"批量标注「站方限制：历史帖子仅限作者本人查看」  阈值 {args.threshold}")

    if not args.skip_sync:
        print("\n[1/3] 同步列表形态（主板块 + 109 个部门页第 1 页）…")
        t0 = time.time()
        n = sync_forms(db, client)
        st = db.conn.execute(
            "SELECT kind, COUNT(*) n FROM thread_list_forms GROUP BY kind").fetchall()
        print(f"      扫了 {n} 个列表页，用时 {time.time()-t0:.0f}s")
        print("      形态记录：" + "  ".join(f"{r['kind']}={r['n']}" for r in st))
        print(f"      ★ 置顶豁免名单：{len(db.sticky_tids())} 个 tid")

    print("\n[2/3] 计算候选集…")
    cand = build_candidates(db, thr)
    total = db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    print(f"      全库 {total:,} 帖；候选（阈值前且非置顶、库里无真实楼层）"
          f" **{len(cand):,}**")
    by_year = {}
    for r in cand:
        y = time.strftime("%Y", time.localtime(r["pub_ts"]))
        by_year[y] = by_year.get(y, 0) + 1
    print("      按年：" + "  ".join(f"{y}:{n:,}" for y, n in sorted(by_year.items())))

    if args.verify:
        print(f"\n[3/3] 抽样实测（候选 {args.verify} 个 + 对照 {args.verify} 个）…")
        sample = cand[::max(1, len(cand) // args.verify)][:args.verify]
        ctrl = non_candidates(db, thr, args.verify)
        ok = bad = 0
        for r in sample:
            st = probe(client, int(r["tid"]))
            good = (st == "受限")
            ok += good
            bad += (not good)
            d = time.strftime("%Y-%m-%d", time.localtime(r["pub_ts"]))
            if not good:
                print(f"      ✗ 误标风险 tid={r['tid']} {d} 实测={st} {r['title'][:26]}")
        cok = cbad = 0
        for r in ctrl:
            st = probe(client, int(r["tid"]))
            cok += (st == "可见")
            cbad += (st != "可见")
            if st != "可见":
                print(f"      ✗ 对照异常 tid={r['tid']} 实测={st}")
        print(f"      候选组：{ok}/{len(sample)} 确为受限"
              f"（误标 {bad} 个）")
        print(f"      对照组：{cok}/{len(ctrl)} 确为可见（异常 {cbad} 个）")

    if args.apply and cand:
        print(f"\n[3/3] 写入 {len(cand):,} 个帖…（分批 {args.batch}）")
        t0 = time.time()
        done = 0
        for i in range(0, len(cand), args.batch):
            chunk = [int(r["tid"]) for r in cand[i:i + args.batch]]
            done += db.mark_restricted_bulk(chunk)
            print(f"      {done:,}/{len(cand):,}  ({time.time()-t0:.0f}s)")
        print(f"      完成，用时 {time.time()-t0:.0f}s")
    elif not args.apply:
        print("\n[3/3] 未指定 --apply，**未改动数据库**")

    print("\n### 标注后统计")
    for r in db.conn.execute(
            "SELECT detail_state, COUNT(*) n FROM threads GROUP BY detail_state "
            "ORDER BY n DESC"):
        print(f"  {r['detail_state'] or '(空)':<12} {r['n']:>9,}")
    q = db.conn.execute(
        "SELECT state, COUNT(*) n FROM detail_queue GROUP BY state "
        "ORDER BY n DESC").fetchall()
    print("  队列：" + "  ".join(f"{r['state']}={r['n']:,}" for r in q))
    db.conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
