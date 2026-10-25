# -*- coding: utf-8 -*-
"""
链路一：列表采集
================================================================
职责边界很清楚：**只拿元数据，不碰正文**。
一条列表行的信息量已经足够做分布统计（部门/类别/状态/区县市/时效灯/回复量），
正文留给链路二按「先旧后新」慢慢补。

三条纪律：
1. **按源续传**：每个采集源在 list_watermark 里有一行，记着「这个源采到第几页」。
   断点续传的单位是**源 × 页**，不是全局页码 —— 不同源的页数完全不同。
2. **绝不越过站点分页上限**：Discuz 的 page>500 会回绕到第 1 页，
   一旦踩到就会把第 1 页的数据当成第 501 页重复入库，且水位线被污染。
   所以每页都要读出站方给的 totalpage，到顶即停。
3. **水位线只由全局视图定**：详情采不采，取决于「全局视图最后一页最早的那一贴」。
   这是用户给的口径 —— 早于它的，详情页不采。
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from .config import Config
from .db import DB
from .http_client import Client, HttpError
from .parsers import ListRow, parse_list_page, parse_list_page_info
from .sources import Source
from .taxonomy import fetch_taxonomy, parse_depts, parse_districts


class ListCollector:
    def __init__(self, db: DB, client: Client, cfg: Config):
        self.db = db
        self.client = client
        self.cfg = cfg

    # ------------------------------------------------------------------
    def collect_taxonomy(self) -> Dict:
        """抓分类体系：109 个部门目录 + 10 个区县市 + 站方真实量级。

        区县市的数字在 HTML 里是 `document.write(ms_hs);` 占位，
        真实数字在独立脚本 html/yp/ms_sjszxsqtotal.js 里，必须单独取。
        """
        self.client.log("[taxonomy] 抓取部门目录与统计脚本 …")
        tax = fetch_taxonomy(self.client)
        depts = tax["depts"]
        self.db.save_depts([
            {"typeid": d.typeid, "name": d.name,
             "group_code": d.group_code, "group_name": d.group_name,
             "is_district": d.is_district,
             "url": f"https://bbs.cnnb.com.cn/forum.php?mod=forumdisplay"
                    f"&fid={self.cfg.fid}&filter=typeid&typeid={d.typeid}"}
            for d in depts])
        self.db.save_districts([
            {"typeid": d.typeid, "name": d.name,
             "last_month_msgs": d.last_month_msgs,
             "last_month_accepted": d.last_month_accepted}
            for d in tax["districts"]])
        st = tax["site_stats"]
        if st.total is not None:
            self.db.save_site_stats({
                "total": st.total, "this_year": st.this_year,
                "last_month": st.last_month,
                "total_accepted": st.total_accepted,
                "this_year_accepted": st.this_year_accepted,
                "last_month_accepted": st.last_month_accepted,
            })
        # 分组/区县市的回填交给 backfill_dept_meta（它全量重算），
        # 这里**不能**再用 set_dept_meta 提前填 dept_group ——
        # 那样会把 dept_district 留空、还让后续回填整片跳过。
        self.client.log(
            f"[taxonomy] 部门 {len(depts)} 个 / 区县市 {len(tax['districts'])} 个 / "
            f"站方自报历史留言 {st.total} 条")
        return tax

    # ------------------------------------------------------------------
    def collect(self, source: Source, max_pages: Optional[int] = None,
                resume: bool = True, stop_after: int = 0, should_stop=None,
                rescan_head: int = 2) -> Dict:
        """采集一个源。返回统计字典。

        `should_stop`：可选回调，返回 True 则**在下一页之前**停下（Web 前端的"停止"）。
        默认 None = 原有行为不变。停下来的水位线记为 paused，可续。

        `rescan_head`：采完后**回扫前几页**补漏（默认 2 页）。见 _rescan_head 的说明。
        """
        cap = min(max_pages or self.cfg.list_max_pages, self.cfg.list_page_cap)
        wm = self.db.get_watermark(source.key)
        if wm and wm.get("status") == "done" and resume:
            self.client.log(f"[list] {source.key} 已完成（{wm['rows_total']} 行），跳过")
            return {"source": source.key, "skipped": True, "rows": wm["rows_total"]}

        start = 1
        if resume and wm and wm.get("last_page"):
            start = int(wm["last_page"]) + 1
            if start > cap:
                self.client.log(f"[list] {source.key} 已到第 {wm['last_page']} 页（上限 {cap}）")
                return {"source": source.key, "done": True, "rows": wm.get("rows_total", 0)}

        self.db.save_watermark(
            source.key, dim=source.dim, code=source.code, name=source.name,
            url=source.url, status="in_progress",
            updated_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.client.log(f"[list] 开始 {source.key}（{source.name}）自第 {start} 页，最多 {cap} 页")

        # --restart 是「从头重采」，统计口径也要从头算，
        # 否则同一批页会被累加成双倍（水位线的 oldest/newest 反而没影响，
        # 但 rows_total 会骗人）。
        prev = wm if resume else None
        total_rows = int(prev.get("rows_total") or 0) if prev else 0
        newest = int(prev.get("newest_ts") or 0) if prev else 0
        oldest = int(prev.get("oldest_ts") or 0) if prev else 0
        page = start - 1
        max_seen = 0
        got_pages = 0
        drift_hits = int(prev.get("drift_hits") or 0) if prev else 0
        last_tid = str(prev.get("last_tid") or "") if prev else ""
        resumed = start > 1
        first_tid_this_run = ""

        while page < cap:
            if should_stop is not None and should_stop():
                self.client.log(f"[list] {source.key} 收到停止指令，停在第 {page} 页")
                break
            page += 1
            url = source.url_for(page)
            try:
                r = self.client.get(url, throttle="list")
            except HttpError as e:
                self.db.log("list", source.key, e.kind, False, str(e))
                self.client.log(f"[list] {source.key} 第 {page} 页失败：{e}")
                break

            info = parse_list_page_info(r.text)
            rows = parse_list_page(r.text, url)
            if not rows:
                self.db.log("list", source.key, "empty_page", True, f"page={page}")
                self.client.log(f"[list] {source.key} 第 {page} 页无数据 → 该源已到底")
                page -= 1
                break

            # ---- 漂移检测：续采时，本批第一页的首行 tid 是否接得上上一批的末行 ----
            # 列表按时间倒序。**新帖、以及被新回复顶上来的老帖，都会插到最前面**，
            # 于是整条序列后移 —— 但各页之间**仍然连续**（不重不漏），
            # 真正被挤出的是**尾部最老的一批**（只有被 500 页顶截断的源才有损失）。
            # 所以本检测抓的是**更罕见也更值得警惕**的情况：
            #   页边界**接不上**（中间被删帖 / 顺序变了）—— 这是真的"错位"。
            # 头部漂移由采集结束后的「补漏回扫」兜住（见 _rescan_head）。
            page_first_tid = str(rows[0].tid)
            page_last_tid = str(rows[-1].tid)
            if not first_tid_this_run:
                first_tid_this_run = page_first_tid
                if resumed and last_tid and page_first_tid != last_tid:
                    drift_hits += 1
                    self.db.log("list", source.key, "drift", True,
                                f"续采首行 tid={page_first_tid} 与上批末行 "
                                f"tid={last_tid} 不接 → 页边界不连续（累计 {drift_hits} 次）")
                    self.client.log(f"[list] {source.key} ⚠ 续采时页边界不连续"
                                    f"（本批首 tid={page_first_tid} ≠ 上批末 tid={last_tid}）"
                                    f"—— 中间可能有被删/重排的帖")

            total_rows += len(rows)
            got_pages += 1
            for row in rows:
                ts = row.pub_ts or 0
                if ts:
                    newest = max(newest, ts)
                    oldest = min(oldest, ts) if oldest else ts
            self._upsert_with_growth(rows, source)
            for row in rows:
                self.db.add_source(row.tid, source.key)
                self.db.set_tags(row.tid, self._tags_of(row, source))
            # 记下「这个 tid 在本源以什么形态出现」（普通行/置顶行/公告区）
            # → 置顶记录是**批量标注受限帖时的豁免名单**的依据
            self.db.add_list_forms(rows, source.key)
            # 分组/区县市要靠部门字典才能定，字典可能这时才刚抓好
            self.db.backfill_dept_meta()
            tp = info.get("total_page") or 0
            max_seen = max(max_seen, tp)
            last_tid = page_last_tid          # ← 供最终保存与下次续采比对
            self.db.save_watermark(
                source.key, last_page=page, max_page=max_seen,
                rows_total=total_rows, oldest_ts=oldest, newest_ts=newest,
                status="in_progress", last_tid=page_last_tid,
                drift_hits=drift_hits,
                updated_at=time.strftime("%Y-%m-%d %H:%M:%S"))

            if stop_after and got_pages >= stop_after:
                self.client.log(f"[list] {source.key} 试采集到第 {page} 页后停止")
                return {"source": source.key, "pages": got_pages, "rows": total_rows,
                        "last_page": page, "stopped": True}

            if tp and page >= tp:
                self.client.log(f"[list] {source.key} 到站方分页上限（第 {tp} 页）")
                break
            if not tp and page >= cap:
                break

        status = "done" if (max_seen and page >= max_seen) else "paused"

        # ---- 补漏回扫 ----
        # 列表按时间倒序 → 新帖、以及**被新回复顶上来**的老帖都会插到**最前面**，
        # 于是整条序列后移。漂移的两种后果：
        #   ① 头部：新出现/顶上来的帖原本在窗口外 → **漏**（回扫前几页即可捞回）
        #   ② 尾部：被 500 页顶截断的源，**最老的一批**会被挤出窗口
        #      —— 这一条回扫救不回来，只能靠 `filter=dateline` 时间窗再往深里取
        # 之所以回扫前几页就够：漂移只把内容往**后面**推，
        # 新出现在前面的帖一定落在前几页里。按 tid 幂等入库 → 重复无害。
        rescan_new = 0
        if rescan_head and not (should_stop is not None and should_stop()):
            rescan_new = self._rescan_head(source, rescan_head, total_rows)
            if rescan_new:
                total_rows += rescan_new

        rescan_rows = int(prev.get("rescan_rows") or 0) if prev else 0
        rescan_rows += rescan_new
        self.db.save_watermark(
            source.key, last_page=page, max_page=max_seen, rows_total=total_rows,
            oldest_ts=oldest, newest_ts=newest, status=status,
            last_tid=last_tid, drift_hits=drift_hits, rescan_rows=rescan_rows,
            updated_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.client.log(f"[list] {source.key} 结束：{got_pages} 页 / 本次 {total_rows} 行 / "
                        f"最早 {time.strftime('%Y-%m-%d', time.localtime(oldest)) if oldest else '?'}"
                        + (f" / 漂移 {drift_hits} 次" if drift_hits else "")
                        + (f" / 补漏回扫补回 {rescan_new} 条" if rescan_new else ""))
        return {"source": source.key, "pages": got_pages, "rows": total_rows,
                "last_page": page, "max_page": max_seen, "status": status,
                "drift_hits": drift_hits, "rescan_new": rescan_new,
                "oldest_ts": oldest, "newest_ts": newest}

    def _rescan_head(self, source: Source, head_pages: int, total_rows: int) -> int:
        """回扫源的前几页，把期间新增/新被回复的帖捞回来。返回**新进库**的条数。

        为什么有效：漂移只把内容往**后面**推，所以新出现在前面的帖一定落在前几页里。
        按 tid 幂等入库 → 重复的帖不会产生脏数据，只是白查一次。
        """
        before = self.db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        found = 0
        for p in range(1, max(1, head_pages) + 1):
            url = source.url_for(p)
            try:
                r = self.client.get(url, throttle="list")
            except HttpError:
                break
            rows = parse_list_page(r.text, url)
            if not rows:
                break
            found += len(rows)
            self._upsert_with_growth(rows, source)
            for row in rows:
                self.db.add_source(row.tid, source.key)
                self.db.set_tags(row.tid, self._tags_of(row, source))
            self.db.add_list_forms(rows, source.key)
        after = self.db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        new = max(0, after - before)
        if new:
            self.db.log("list", source.key, "rescan", True,
                        f"回扫前 {head_pages} 页，补回 {new} 条")
            self.client.log(f"[list] {source.key} 补漏回扫前 {head_pages} 页 → 补回 {new} 条")
        return new

    # ------------------------------------------------------------------
    def _upsert_with_growth(self, rows, source) -> None:
        """写列表行，并顺手做**增量检测**与**列表变更留痕**。

        ★ 增量的权威信号是**站方自己给的数字与状态**：列表页每一行都带
          「回复数」和「受理状态」。
          同一个 tid 再次出现在列表页、而
            · 回复数**变大了** → 站方追加了新楼层 → 本地正文过时；
            · **受理状态变了**（两边都是有效状态）→ 站方受理/回复了
              → 详情页那份**受理流程时间线**过时 → 待重采。
              （旧实现只比回复数 → 「未受理→已受理」这个**最关键的推进信号**
                完全检测不到。）

        ⚠ 顺序不能反：必须在 `upsert_threads` **之前**比。
          upsert 会把 replies / state 覆盖成新值，之后再比永远「没变化」。

        ⚠ 只在「变大」时判定回复数。变小可能是站方删楼，也可能是解析抖动 ——
          宁可不报，也不能把好数据误判成要重采（重跑是要花站方请求的）。

        ★ 顺带**留痕**（2026-10-23 加）：列表阶段看到的变化
          （新帖 / 受理状态变了 / 回复数变了 / 标题改了）写进 `list_changes` 表。
          覆盖式写入的坏处是**旧值一被盖掉就永远查不到了** ——
          「这帖站方什么时候受理的」这类问题就再也答不上来。
        """
        # ① 留痕（覆盖前留底）
        try:
            changes = self.db.record_list_changes(source.key, rows)
        except Exception as e:                          # noqa: BLE001
            changes = {}
            self.client.log(f"[list] {source.key} 变更留痕失败（不影响采集）：{e}")
        # ② 增量检测（也要在覆盖前）
        growth = self.db.detect_reply_growth(rows)
        # ③ 覆盖写入
        self.db.upsert_threads(rows)
        # ④ 变更摘要写进 fetch_log（列表阶段也有日志可查）
        for kind, label in (("new", "新帖"), ("state", "受理状态变了"),
                            ("replies", "回复数变了"), ("title", "标题改了")):
            items = changes.get(kind) or []
            if not items:
                continue
            detail = "、".join(
                f"#{x['tid']} {x.get('old', '') or '空'}→{x.get('new', '') or '空'}"
                for x in items[:5])
            self.db.log("list", source.key, f"changed_{kind}", True,
                        f"{len(items)} 帖{label}：{detail}")
        if not growth:
            return
        g_tids = [g["tid"] for g in growth]
        by_kind: Dict[str, List[Dict[str, Any]]] = {}
        for g in growth:
            by_kind.setdefault(g.get("kind") or "replies", []).append(g)
        for kind, why in (("replies", "站方又回复了（列表页回复数变大），待重采"),
                          ("state", "站方受理状态变了（受理流程随之变化），待重采")):
            gs = by_kind.get(kind) or []
            if not gs:
                continue
            self.db.mark_detail_stale([g["tid"] for g in gs], reason=why)
            if kind == "replies":
                detail = "、".join(f"#{g['tid']} {g['old_replies']}→{g['new_replies']}"
                                   for g in gs[:5])
            else:
                detail = "、".join(f"#{g['tid']} {g['old_state']}→{g['new_state']}"
                                   for g in gs[:5])
            self.db.log("list", source.key, "detail_growth", True,
                        f"{len(gs)} 帖{kind == 'state' and '受理状态变了' or '站方有新回复'}"
                        f"，已标记待重采：{detail}")
            self.client.log(f"[list] {source.key} ↻ {len(gs)} 帖"
                            f"{'受理状态变了' if kind == 'state' else '站方有新回复'}"
                            f"（{detail}）→ 已标记待重采")

    # ------------------------------------------------------------------
    @staticmethod
    def _tags_of(row: ListRow, source: Source) -> List[tuple]:
        """把一行变成标签组。维度名固定，便于检索层切片。"""
        tags: List[tuple] = []
        if row.dept_id:
            tags.append(("dept", row.dept_id, row.dept_name))
        for tid, nm in zip(row.extra_dept_ids, row.extra_dept_names):
            tags.append(("dept", tid, nm))
        if row.category:
            tags.append(("category", row.category, row.category))
        if row.state:
            tags.append(("state", row.state, row.state))
        if row.mailbox_id:
            tags.append(("mailbox", row.mailbox_id, row.mailbox_name or row.mailbox_id))
        if row.light_code:
            tags.append(("light", row.light_code, row.light_name or row.light_code))
        if source.dim not in ("global",):
            tags.append(("source", source.key, source.name))
        return tags

    # ------------------------------------------------------------------
    def set_cutoff_from_global(self) -> Optional[int]:
        """全局视图**采完**后，定下详情采集的水位线。

        水位线 = 全局视图里**最早**那一贴的发帖时间。
        早于它的帖子不采详情（用户口径：可能不好在网站抓取）。

        ⚠ 只有全局源**真的采到了最后一页**才能定水位线。
          试采 5 页就定水位线的话，「目前最早的那一贴」会被误当成截止线，
          于是比它更新的帖子全被跳过 —— 恰好把刚采到的数据判死。
          所以这里要求 status=='done'（last_page 触到站方给的 totalpage）。
        """
        wm = self.db.get_watermark("global")
        if not wm or not wm.get("oldest_ts"):
            return None
        last = int(wm.get("last_page") or 0)
        total = int(wm.get("max_page") or 0)
        if wm.get("status") != "done" or (total and last < total):
            self.client.log(
                f"[cutoff] 全局视图尚未采完（第 {last}/{total or '?'} 页），"
                f"暂不设水位线 —— 详情采集不受跳过限制")
            return None
        cutoff = int(wm["oldest_ts"])
        self.db.set_meta("detail_cutoff_ts", cutoff)
        self.db.set_meta("detail_cutoff_at",
                         time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff)))
        self.db.set_meta("detail_cutoff_source", "global")
        self.client.log(
            f"[cutoff] 详情水位线 = {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(cutoff))}"
            f"（全局视图第 {last} 页最早一贴）")
        return cutoff

    def apply_cutoff(self, cutoff_ts: Optional[int] = None) -> int:
        """把早于水位线的待采详情标为 skipped。"""
        if cutoff_ts is None:
            cutoff_ts = int(self.db.get_meta("detail_cutoff_ts", "0") or 0)
        if not cutoff_ts:
            return 0
        n = self.db.conn.execute(
            "UPDATE detail_queue SET state='skipped', updated_at=? "
            "WHERE state='pending' AND tid IN "
            "(SELECT tid FROM threads WHERE COALESCE(pub_ts,0) < ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), cutoff_ts)).rowcount
        self.db.conn.commit()
        if n:
            self.client.log(f"[cutoff] {n} 条早于水位线的详情已跳过")
        return n
