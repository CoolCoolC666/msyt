# -*- coding: utf-8 -*-
"""
test_drift.py —— 离线验证「页码漂移」的检测与补漏机制
================================================================
不碰运行中的服务、不碰站方：用**合成列表页** + **临时库副本**跑 msyt 的列表采集。

验三件事：
  ① 正常采集：水位线正确、无误报漂移
  ② 漂移检测：续采时首行 tid 接不上上批末行 → drift_hits+1
  ③ 补漏回扫：源采完后回扫前几页，把期间新增的帖捞回来 → rescan_rows>0

跑法：
    python _tools\\test_drift.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "程序"))

from msyt.config import Config                  # noqa: E402
from msyt.db import DB                   # noqa: E402
from msyt.listpage import ListCollector  # noqa: E402
from msyt.sources import make_source     # noqa: E402


# --------------------------------------------------------------- 合成页面
def make_page(rows, total_page, total_threads):
    """合成一页列表 HTML：rows = [(tid, title, pub_ts_str)]"""
    trs = []
    for tid, title, pub in rows:
        trs.append(f"""
<tbody id="normalthread_{tid}">
 <tr>
  <th class="common">
   <a href="forum.php?mod=forumdisplay&amp;fid=1176&amp;filter=typeid&amp;typeid=495">[投诉]</a>
   <span class="typewrap"><em><a href="forum.php?mod=forumdisplay&amp;fid=1176&amp;filter=typeid&amp;typeid=495">市公安局</a></em></span>
   <a href="forum.php?mod=viewthread&amp;tid={tid}" class="s xst">{title}</a>
  </th>
  <td class="by"><cite><a href="home.php?mod=space&amp;uid=1">u{tid}</a></cite>
      <em><span>2026-01-01 10:00</span></em></td>
  <td class="num"><a href="x">3</a><em>100</em></td>
 </tr>
</tbody>""")
    return (f'<div id="rankingwz"></div><input totalpage="{total_page}" total="{total_threads}">'
            + "".join(trs))


class FakeResp:
    def __init__(self, text):
        self.text = text


class FakeClient:
    """按页返回合成内容。

    `shift_at`    ：该页永久后移多少行（模拟"一开始就有的漂移"）
    `late_shift`  ：**第 shift_after 次调用之后**该页才后移（模拟"采集途中来了新帖"）
    `shift_after` ：前多少次调用还算"漂移之前"
    """

    def __init__(self, per_page=5, total_pages=3, shift_at=None,
                 late_shift=None, shift_after=0):
        self.per_page = per_page
        self.total_pages = total_pages
        self.shift_at = shift_at or {}
        self.late_shift = late_shift or {}
        self.shift_after = shift_after
        self.calls = []
        self.logs = []

    def log(self, msg):
        self.logs.append(str(msg))

    def get(self, url, throttle=None, **kw):
        m = re.search(r"page=(\d+)", url)
        page = int(m.group(1)) if m else 1
        self.calls.append(page)
        insert = 0
        if self.shift_after and len(self.calls) > self.shift_after:
            insert = self.late_shift.get(page, 0)
        base = (page - 1) * self.per_page
        # 漂移的**真实模型**：新帖（或被新回复顶上来的老帖）插到**最前面**，
        # 于是整条序列后移 —— 各页之间仍然**连续**（不重不漏），
        # 真正被挤出的是**尾部最老的一批**（只在触顶时才有损失）。
        rows = []
        for i in range(insert):
            rows.append((80000 + page * 100 + i, f"新帖{page}-{i}", "2026-01-01 10:00"))
        for i in range(self.per_page - insert):
            idx = base + i + self.shift_at.get(page, 0)
            rows.append((90000 + idx, f"帖{idx}", "2026-01-01 10:00"))
        return FakeResp(make_page(rows, self.total_pages, self.total_pages * self.per_page))


def _mk(cfg, db):
    lc = ListCollector(db, FakeClient(), cfg)
    return lc


def _fresh_db(tmpdir) -> DB:
    db = DB(str(Path(tmpdir) / "t.db"))
    db.save_depts([{"typeid": "495", "name": "市公安局", "group_code": "func",
                    "group_name": "职能部门和群团组织", "is_district": 0}])
    return db


def main() -> int:
    cfg = Config.load()
    cfg.db_path = str(Path(tempfile.mkdtemp()) / "t.db")
    src = make_source("dept", "495", "市公安局")
    fails = []

    def check(cond, msg):
        print(("  [OK]  " if cond else "  [FAIL] ") + msg)
        if not cond:
            fails.append(msg)

    # ---------------------------------------------------------------- ①
    print("=" * 66)
    print("① 正常采集：无漂移误报")
    print("=" * 66)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = _fresh_db(td)
        client = FakeClient(per_page=5, total_pages=3)
        lc = ListCollector(db, client, cfg)
        res = lc.collect(src, max_pages=10, resume=True)
        wm = db.get_watermark("dept:495")
        check(res["status"] == "done", f"状态=done（实际 {res['status']}）")
        check(res["pages"] == 3, f"采了 3 页（实际 {res['pages']}）")
        check(res["drift_hits"] == 0, f"无漂移误报（drift_hits={res['drift_hits']}）")
        check(wm and wm.get("last_tid"), f"水位线记了 last_tid（{wm.get('last_tid') if wm else None}）")
        check(wm and "drift_hits" in wm and "rescan_rows" in wm,
              "水位线表有 drift_hits / rescan_rows 两列")
        db.close()

    # ---------------------------------------------------------------- ②
    print()
    print("=" * 66)
    print("② 漂移检测：续采时首行接不上 → drift_hits+1")
    print("=" * 66)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = _fresh_db(td)
        # 先采 2 页就停（每页 5 行）—— 不能把整源采完，
        # 否则第二次会走「已完成，跳过」分支，测不到续采。
        c1 = FakeClient(per_page=5, total_pages=9)
        lc1 = ListCollector(db, c1, cfg)
        lc1.collect(src, max_pages=10, resume=True, stop_after=2)
        wm1 = db.get_watermark("dept:495")
        last_tid_1 = wm1.get("last_tid")
        print(f"     上批末行 tid = {last_tid_1}")

        # 续采：模拟"前面插入了 3 条新帖" → 窗口后移，第 3 页首行不再是 90009
        c2 = FakeClient(per_page=5, total_pages=9, shift_at={3: 3})
        lc2 = ListCollector(db, c2, cfg)
        res2 = lc2.collect(src, max_pages=10, resume=True)
        check(res2["drift_hits"] == 1,
              f"检测到 1 次漂移（实际 {res2['drift_hits']}）")
        check(any("漂移" in m for m in c2.logs), "日志里写明了漂移")
        wm2 = db.get_watermark("dept:495")
        check(wm2.get("drift_hits") == 1, f"水位线累计 drift_hits=1（实际 {wm2.get('drift_hits')}）")
        db.close()

    # ---------------------------------------------------------------- ③
    print()
    print("=" * 66)
    print("③ 补漏回扫：期间新增的帖被捞回来")
    print("=" * 66)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = _fresh_db(td)
        # 一次采集：前 3 页正常采完；**采完之后**（第 4 次请求起）第 1 页多出 2 条新帖
        # → 回扫时就能把它们捞回来
        c1 = FakeClient(per_page=5, total_pages=3, late_shift={1: 2}, shift_after=3)
        lc1 = ListCollector(db, c1, cfg)
        res2 = lc1.collect(src, max_pages=10, resume=True)
        n_after = db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        n_before = n_after - res2.get("rescan_new", 0)
        print(f"     回扫前 {n_before} 帖 → 回扫后 {n_after} 帖")
        check(res2["rescan_new"] == 2, f"补回 2 条（实际 {res2['rescan_new']}）")
        check(n_after == n_before + 2, f"库里真的多了 2 条（{n_before} → {n_after}）")
        check(any("补漏回扫" in m for m in c1.logs), "日志里写明了补漏回扫")
        wm = db.get_watermark("dept:495")
        check(wm.get("rescan_rows") == 2, f"水位线记 rescan_rows=2（实际 {wm.get('rescan_rows')}）")
        db.close()

    print()
    print("=" * 66)
    if fails:
        print(f"失败 {len(fails)} 项：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
