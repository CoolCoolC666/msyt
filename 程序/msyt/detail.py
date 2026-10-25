# -*- coding: utf-8 -*-
"""
链路二：详情页采集
================================================================
用户给的三条硬要求，全部在这里落地：

1. **先旧后新**：按发帖时间升序取帖（`claim_next_detail` 的 ORDER BY）。
   为什么不能按「上次到第 N 页」当水位：列表页会漂移，新帖插到前面会把页码挤走，
   拿页码当水位会漏采或重采。**按时间排序才是稳的。**

2. **断点续传**（详情页传到一半把程序关掉）：
   粒度是「页」，落库是「每页一个事务」。
   · 进程被杀 → 队列里那条是 running，下次开机当成 pending 接着从 pages_done+1 继续
   · 已经写进去的楼层是**校验过 pid 集合**的正确数据，不用重采
   · 帖子本身在头/尾都齐了之前**不标 done**，所以「半截帖子」永远看得见

3. **头/尾完整才算完整**：
   · 头 = 楼主那一楼（floor == 1）
   · 尾 = 最后一楼（floor 最大）
   · 一帖有 N 页时，头在第 1 页、尾在末页 —— 所以完整性判据必须**跨页**定义
   · 再加一条独立交叉验证：列表页给出的「回复数 + 1」= 应有楼层数，
     实收楼层数少于它 ⇒ 不完整（这条不依赖任何解析，是最硬的证据）

⚠ 防「静默串帖」：
   实测 `ajax_getpost.php` 的 `tid` 参数**完全不参与内容选择**，token 才是钥匙。
   用 A 帖的 token 配 B 帖的 tid，服务端照样返回 A 帖的楼层且 code=1，**不报错**。
   所以每一页都必须校验：ajax 的 data 键集合 == 该页 postmsg 锚点集合，**精确相等**。
   不等就是失败，**宁可不收，不可收错**。
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

from .config import AJAX_URL, BASE, Config, THREAD_URL
from .db import DB
from .http_client import Client, HttpError
from .parsers import (ThreadPage, parse_ajax_posts, parse_site_notice,
                     parse_thread_page)
from .textutil import extract_images, html_to_text


#: 少贴补采的**次数上限**（2026-10-23 加）
#:   有些帖是站方计数口径本来就有偏差（replies 含楼主之类），
#:   补采多少次都补不齐 → 会 stale→重采→stale 死循环。
#:   超过这个次数就只保留告警，不再自动入队。
GAP_RETRY_MAX = 2
#: 补采次数在 detail_warn 里的记号
GAP_RETRY_MARK = "已补采"

#: 单帖页数的**硬天花板**（2026-10-24 加）
#:   防的是「站方给出一个离谱的总页数」这种异常，不是限制正常长帖。
#:   实测站方盖楼活动置顶帖 tid=8333204 有 **81 页**（站方自己标的），
#:   默认上限 50 页会把它卡在一半（750/1207 楼）——
#:   而且断点续传也救不了：续传从 pages_done+1 起算，一样越过不了 50。
DETAIL_PAGE_HARD_CAP = 500


def is_short_and_retryable(expect_posts: int, got_posts: int,
                            floors: Optional[List[int]] = None) -> bool:
    """这个帖是不是「**少贴**」→ 该保留原帖 + 标记 + 进增量补采队列。

    ★ 判据（2026-10-23 定）：
      少贴 = 站方称的楼层数 **>** 我们实收的楼层数，
      **且楼层号是连续的**（没有中间缺口）。

      为什么要有「楼层号连续」这个附加条件：
      楼层号中间缺（如 [1,3,4] 缺 2）是**站方删楼/置顶** ——
      那楼已经不存在了，重采多少次都补不上。
      只有「楼层连续但总数少」（尾部少）才是**站方还有楼我们没采到** → 该补。

      换句话说：
        · 尾部少   → 增量，补采有意义 ✓
        · 中间缺   → 站方的行为，补采没意义 ✗
    """
    if not expect_posts or got_posts >= expect_posts:
        return False
    fl = sorted(set(floors or []))
    if not fl:
        return True                      # 没有楼层信息，只能按数量判断
    mx = max(fl)
    gap = [i for i in range(1, mx + 1) if i not in fl]
    return not gap


class DetailCollector:
    def __init__(self, db: DB, client: Client, cfg: Config):
        self.db = db
        self.client = client
        self.cfg = cfg

    # ------------------------------------------------------------------
    def collect_one(self, tid: int, pages_done: int = 0,
                    pages_total: int = 0, max_attempts: int = 3,
                    should_stop=None) -> Dict:
        """采一帖的详情。返回结果字典。

        `should_stop`：可选回调，返回 True 则**在下一页之前**停下。
        成员端 agent 用它做「与服务器失联就自动停止」——
        采一帖要发好几次请求，中途失联时不能把整帖跑完。
        """
        expect_posts = self._expected_posts(tid)
        seen: List[int] = self._known_pids(tid)
        seen_set = set(seen)
        pages_ok = 0
        page_no = max(1, pages_done + 1)
        # ★ 预算至少要够「进得去循环」：续传时 pages_done 可能已经 ≥ 默认上限
        #   （实测盖楼活动置顶帖 81 页，默认上限 50），
        #   若预算照旧是 50，while page_no(51) <= 50 直接不成立 → **循环一次都不跑**，
        #   续传等于空转。真正的上限在下面按站方自己标的总页数放开。
        page_budget = max(self.cfg.detail_max_pages, pages_done + 1)
        flow_saved = False
        last_error = ""
        stopped = False
        restricted = ""            # 站方限制提示（正文不公开）
        # 站方给出的总页数（分页块）。单页帖没有分页块 → 1。
        # **必须用它封顶**：单页帖请求 page=2 会回绕返回第 1 页，
        # 盲翻会把第 1 页当第 2 页重复采一遍。
        site_total_pages = int(pages_total or 0)

        while page_no <= page_budget:
            if should_stop is not None and should_stop():
                stopped = True
                last_error = "收到停止指令（外部要求），已中止"
                break
            try:
                html = self.client.get(THREAD_URL.format(tid=tid, page=page_no),
                                       referer=THREAD_URL.format(tid=tid, page=1)).text
            except HttpError as e:
                last_error = f"page{page_no} 取页失败: {e}"
                break

            # ★ 站方限制优先判定：历史帖子只给「提示信息」页，
            #   没有楼层锚点也没有 token —— 继续往下走只会得到
            #   「page1 没取到 token」这种看不出真相的报错。
            notice = parse_site_notice(html)
            if notice:
                # ★★ **必须复验**（2026-10-25 加）
                #   站方会**间歇性**返回「提示信息」页（实测：并发/密集访问时出现，
                #   同样的帖单独再请求一次又是正常的）。
                #   如果直接采信，就会把**明明可见**的帖判成「站方限制」——
                #   而这个状态是**终态**（队列里摘出去、还写进正文当标记），
                #   一次瞬时异常 = 永久误标 = 数据污染。
                #   → 复验：再请求一次同 URL；能拿到楼层/token → 是临时故障，不是限制。
                v_url = THREAD_URL.format(tid=tid, page=page_no)
                try:
                    html2 = self.client.get(v_url, referer=v_url).text
                except HttpError as e2:
                    html2 = ""
                    self.db.log("detail", str(tid), "restricted_retry_err", False, str(e2))
                if html2 and not parse_site_notice(html2):
                    tp2 = parse_thread_page(html2, tid=tid, page_no=page_no)
                    if tp2.token and tp2.pids:
                        # 复验通过 → **不是**站方限制，是临时故障
                        html = html2
                        tp = tp2
                        last_error = (f"首次请求返回提示页（疑似临时故障），"
                                      f"复验后正常取得 {len(tp2.pids)} 层")
                        self.db.log("detail", str(tid), "notice_flaky", True,
                                    last_error)
                    else:
                        restricted = notice
                        last_error = notice
                        self._note_restricted_evidence(tid, html, page_no, "复验也不正常")
                        break
                else:
                    restricted = notice
                    last_error = notice
                    self._note_restricted_evidence(tid, html, page_no, "复验仍是提示页")
                    break

            tp = parse_thread_page(html, tid=tid, page_no=page_no)
            if not tp.token:
                last_error = f"page{page_no} 没取到 token"
                break
            if site_total_pages and page_no > site_total_pages:
                break
            if not tp.pids:
                # 没有楼层锚点 = 到底了
                if page_no == 1:
                    last_error = "第 1 页没有任何楼层锚点"
                break
            if page_no == 1 and not site_total_pages:
                site_total_pages = tp.total_pages
            elif tp.total_pages and tp.total_pages > site_total_pages:
                # ★ 续传时是从第 pages_done+1 页起的，**不会经过 page_no==1**，
                #   所以总页数必须在**每一页**都更新一次，
                #   否则续传的帖永远拿不到站方标的总页数 → 预算放不开。
                site_total_pages = tp.total_pages
            # ★ 站方**自己标了**总页数，且超过了默认上限 → 放开预算，
            #   采到站方说的那一页为止（另设硬天花板防异常）。
            #   旧逻辑一刀切在 detail_max_pages=50 → 81 页的盖楼帖只采一半，
            #   而且**断点续传也接不上**（续传从 pages_done+1 起算，
            #   一样落在 50 之外）→ 永远补不齐。
            if site_total_pages > page_budget:
                page_budget = min(site_total_pages, DETAIL_PAGE_HARD_CAP)
                self.client.log(
                    f"[detail] tid={tid} 站方总页数 {site_total_pages} > 默认上限，"
                    f"本次预算放宽到 {page_budget} 页")

            # ★ token 即用：紧接着发，中间不得插入任何 sleep
            try:
                ar = self.client.get_nowait(
                    AJAX_URL,
                    params={"token": tp.token, "tid": str(tid), "src": "pc"},
                    referer=THREAD_URL.format(tid=tid, page=page_no))
            except HttpError as e:
                last_error = f"page{page_no} ajax 请求失败: {e}"
                break

            try:
                payload = ar.json()
            except Exception:
                last_error = f"page{page_no} ajax 返回不是合法 JSON"
                break

            if payload.get("code") != 1:
                last_error = f"page{page_no} ajax code={payload.get('code')} " \
                             f"msg={payload.get('msg', '')}"
                self.db.log("detail", str(tid), "ajax_code", False, last_error)
                break

            posts_html = parse_ajax_posts(payload)

            # ★★ pid 集合校验：唯一能挡住静默串帖的一步
            if set(posts_html.keys()) != set(tp.pids):
                only_html = sorted(set(posts_html) - set(tp.pids))[:5]
                only_anchor = sorted(set(tp.pids) - set(posts_html))[:5]
                last_error = (f"page{page_no} pid 集合不一致 "
                              f"(ajax独有={only_html}, 锚点独有={only_anchor})")
                self.db.log("detail", str(tid), "pid_mismatch", False, last_error)
                break

            # ---- 落库：单页一个事务 ----
            rows = []
            for pid in tp.pids:
                fm = tp.floors.get(pid)
                content_html = posts_html.get(pid, "")
                text = html_to_text(content_html, BASE)
                rows.append({
                    "pid": pid,
                    "floor": fm.floor if fm else 0,
                    "author": fm.author if fm else "",
                    "author_uid": fm.author_uid if fm else "",
                    "post_time": fm.post_time if fm else "",
                    "post_ts": fm.post_ts if fm else 0,
                    "content": text,
                    "chars": len(text),
                    "images": extract_images(content_html, BASE),
                })
            self.db.save_posts(tid, page_no, rows)
            self.db.record_page(tid, page_no, tp.pids, "ok")
            pages_ok += 1

            new_pids = [p for p in tp.pids if p not in seen_set]
            if page_no == 1:
                if not flow_saved and tp.flow:
                    self.db.save_flows(tid, [
                        {"step": f.step, "name": f.name, "time": f.time, "ts": f.ts}
                        for f in tp.flow])
                    flow_saved = True
            seen_set.update(tp.pids)

            if site_total_pages and page_no >= site_total_pages:
                break
            if not new_pids and page_no > 1:
                break
            page_no += 1

        # ------------------------------------------------------------------
        # 完整性检验
        #   硬判据（不过 = 不算完整，拒绝入库为 done）：
        #     ① 头（floor==1 楼主楼）在库   ② 尾（最大楼层号那楼）在库
        #     ③ 该帖应有的页全部取到        ④ 没有空正文的楼层
        #   软警告（记录但不否决）：
        #     · 楼层号有缺口 —— 站方删楼/置顶造成，是站方的行为，不是我们漏采
        #     · 列表页的「回复数+1」与实收楼层对不上 —— 站方自己的计数不自洽
        #       （实测 tid=8332684：列表说 5 条回复，实际就 5 个楼层）
        head_pid, tail_pid = self._head_tail(tid)
        unique = self._unique_pids(tid)
        floors = self._floors(tid)
        pages_fetched = self._pages_fetched(tid)
        chars = self.db.conn.execute(
            "SELECT COALESCE(SUM(chars),0) FROM posts WHERE tid=?", (tid,)).fetchone()[0]
        content_missing = self.db.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=? AND COALESCE(chars,0)=0", (tid,)).fetchone()[0]

        problems: List[str] = []
        warnings: List[str] = []
        # ★ 「少贴 → 待补采」的动作标志（见下方 GAP_RETRY_MAX 说明）
        #   None = 没有少贴；(tid, reason, tried) = 写完 done 之后要标 stale
        _gap_retry = None

        # ---- 站方限制：正文不公开，**不是失败**，也不算采到 ----
        if restricted:
            self.db.mark_restricted(tid, restricted)
            self.db.log("detail", str(tid), "restricted", False, restricted)
            return {"tid": tid, "ok": False, "restricted": True,
                    "reason": restricted, "pages": 0, "posts": 0, "chars": 0,
                    "error": restricted, "problems": [], "warnings": [],
                    "head_pid": None, "tail_pid": None, "stopped": stopped}

        if pages_fetched == 0 and not last_error:
            last_error = "一页都没采到"
        if not head_pid:
            problems.append("缺头（楼主楼）")
        if not tail_pid:
            problems.append("缺尾（末楼）")
        if content_missing:
            problems.append(f"{content_missing} 个楼层正文为空")
        if site_total_pages and pages_fetched < site_total_pages:
            problems.append(f"只采到 {pages_fetched}/{site_total_pages} 页")

        if floors:
            mx = max(floors)
            gap = [i for i in range(1, mx + 1) if i not in floors]
            if gap:
                warnings.append(f"楼层号缺口 {gap}（站方删楼/置顶，非漏采；"
                               f"这类不补采——那楼已经不存在了）")
        if is_short_and_retryable(expect_posts, len(unique), floors):
            # ★★ 「少贴」→ 保留原帖 + 标记 + 进增量补采队列
            #   （用户 2026-10-23：「保留原贴并且标记，主要是作增量的补采」）
            #
            #   旧实现只 append 一句 warning → 归到「软警告：不算漏采也不算增量」
            #   → 这个帖**永远不会被补采**，缺口永远留在库里。
            #
            #   实测（抽查 400 个 done 帖）：392 个「站方称 replies+1」是对得上的，
            #   8 个对不上且**楼层号连续** → 是尾部少了 → 站方还有楼我们没采到 → 该补采。
            _short = expect_posts - len(unique)
            _reason = (f"少贴：站方称应有 {expect_posts} 楼，实收 {len(unique)} 楼"
                       f"（少 {_short} 楼）→ 已标记待补采")
            # 补采过几次？（防止无限重采）
            _prev = self.db.get_detail_warn(tid) or ""
            _m = re.search(r"已补采\s*(\d+)\s*次", _prev)
            _tried = int(_m.group(1)) if _m else 0
            if _tried < GAP_RETRY_MAX:
                # ★ 这里**只算不动作** —— 真正的 mark_detail_stale() 要等到
                #   写完 detail_state='done' **之后**再做，
                #   否则会被后面的 set_thread_detail(state='done') 覆盖掉。
                _gap_retry = (tid, _reason, _tried)
                warnings.append(_reason)
            else:
                _reason2 = (f"{_reason}；但已补采 {_tried} 次仍对不齐"
                            f"（疑为站方计数口径差异，不再自动补采）")
                warnings.append(_reason2)
                self.db.log("detail", str(tid), "warn", True, _reason2)

        if problems:
            if not last_error:
                last_error = "；".join(problems)
            self.db.log("detail", str(tid), "incomplete", False, last_error)
            return {"tid": tid, "ok": False, "pages": pages_fetched, "posts": len(unique),
                    "chars": chars, "error": last_error, "problems": problems,
                    "warnings": warnings, "head_pid": head_pid, "tail_pid": tail_pid,
                    "stopped": stopped}

        self.db.set_thread_detail(
            tid, detail_state="done", first_post_id=head_pid, last_post_id=tail_pid,
            pages_total=pages_fetched, posts_n=len(unique), chars=chars)
        self.db.set_detail_state(tid, "done", "", pages_done=pages_fetched,
                                 pages_total=pages_fetched)
        # ★★ 「少贴」的标记动作放在**这里**（写完 done 之后）——
        #   放前面会被上面的 set_thread_detail(state="done") 覆盖掉（实测踩过）。
        #   效果：原帖**保留**、状态标成 stale（界面显示「待更新」）、
        #   丢回 detail_queue → 下次详情采集自动捡走重采。
        if _gap_retry:
            _t_id, _t_reason, _t_tried = _gap_retry
            self.db.mark_detail_stale([_t_id], reason=_t_reason)
            self.db.log("detail", str(_t_id), "gap_retry", True,
                        f"{_t_reason}（第 {_t_tried + 1} 次）")
        if warnings:
            # ★ 把「已补采 N 次」记进告警文本（2026-10-23）：
            #   下一轮采集靠它判断「这个帖补过没有」→ 防止无限重采。
            _tried = 0
            _prev0 = self.db.get_detail_warn(tid) or ""
            _mm = re.search(r"已补采\s*(\d+)\s*次", _prev0)
            if _mm:
                _tried = int(_mm.group(1))
            if any("少贴" in w for w in warnings) and \
                    not any("已补采" in w for w in warnings):
                _tried += 1
                warnings.append(f"已补采 {_tried} 次")
            self.db.set_detail_warn(tid, "；".join(warnings))
            self.db.log("detail", str(tid), "warn", True, "；".join(warnings))
        self.db.log("detail", str(tid), "done", True,
                    f"pages={pages_fetched} posts={len(unique)}")
        return {"tid": tid, "ok": True, "pages": pages_fetched, "posts": len(unique),
                "chars": chars, "head_pid": head_pid, "tail_pid": tail_pid,
                "warnings": warnings, "stopped": stopped}

    # ------------------------------------------------------------------
    def run(self, limit: int = 0, max_attempts: int = 3,
            should_stop=None, workers: int = 1) -> Dict:
        """从队列里连续取帖采。先旧后新。

        `should_stop`：可选回调，返回 True 则**在下一帖之前**停下。
        Web 前端用它实现"随时停止"。默认 None = 跑到队列空（原有行为不变）。

        `workers`：并发线程数（2026-10-25 加）。**并发单位是一帖**，
        不是「一个请求」—— 同帖的「详情页 → ajax」必须贴在一起
        （站方 token 有效期 < 30 秒，中间插入任何 sleep 都会失败）。
        ★ `workers <= 1` 走**原串行路径**，行为与并发改造前逐条一致。
        """
        self.db.reset_stale_running()
        n = max(1, int(workers or 1))
        if n == 1:
            return self._run_serial(limit, max_attempts, should_stop)
        return self._run_parallel(limit, max_attempts, should_stop, n)

    # ------------------------------------------------------------------
    def _run_serial(self, limit: int, max_attempts: int, should_stop) -> Dict:
        """串行采集（**默认路径**，与并发改造前行为逐条一致）。"""
        done = failed = restricted = 0
        stopped = False
        t_start = time.time()
        while True:
            if should_stop is not None and should_stop():
                stopped = True
                self.client.log("[detail] 收到停止指令，收尾中…")
                break
            if limit and done + failed + restricted >= limit:
                break
            item = self.db.claim_next_detail()
            if item is None:
                break
            tid = item["tid"]
            res = self.collect_one(tid, pages_done=item.get("pages_done") or 0,
                                   pages_total=item.get("pages_total") or 0,
                                   max_attempts=max_attempts)
            if res["ok"]:
                done += 1
                self.client.log(
                    f"[detail] tid={tid} ✓ {res['pages']} 页 / {res['posts']} 楼 / "
                    f"{res['chars']} 字")
            elif res.get("restricted"):
                # ★ 站方限制：正文不公开 —— 已标记 + 写入正文，但**不算失败**
                restricted += 1
                self.db.set_detail_state(tid, "restricted", res.get("reason", ""))
                self.client.log(f"[detail] tid={tid} ⊘ 站方限制：{res.get('reason')}"
                                f"（已标记，不计入已采详情帖）")
            else:
                failed += 1
                self.db.set_detail_state(tid, "failed", res.get("error", ""))
                self.db.set_thread_detail(tid, detail_state="failed")
                self.client.log(f"[detail] tid={tid} ✗ {res.get('error')}")
        el = time.time() - t_start
        self.client.log(f"[detail] 完成 {done} 帖，站方限制 {restricted} 帖，"
                        f"失败 {failed} 帖，用时 {el:.0f}s")
        return {"done": done, "failed": failed, "restricted": restricted,
                "elapsed": round(el, 1), "stopped": stopped}

    # ------------------------------------------------------------------
    def _run_parallel(self, limit: int, max_attempts: int, should_stop,
                      workers: int) -> Dict:
        """并发采集（每个 worker 一条线）。

        ★ 三条并发安全的前提（缺一不可）
          ① **领活互斥**：`claim_next_detail()` 在 `with self.lock:` 里，
             而 db.DB 的写锁是**进程级共享**的 → 两个 worker 领不到同一条。
          ② **写库串行**：所有写操作同样走那把共享锁 → 不会出现
             "database is locked"，也不会把读事务升级为写。
          ③ **每 worker 自己的连接与客户端**：
             sqlite3 的连接**不能跨线程**（check_same_thread 默认开），
             requests.Session 也不是线程安全的，
             而且限速状态是**实例级**的 → 每线程独立，
             限速才不会被别的线程"搭便车"绕过。
        """
        state = {"done": 0, "failed": 0, "restricted": 0, "picked": 0}
        lock = threading.Lock()
        stopped = False
        t_start = time.time()
        self.client.log(f"[detail] 并发采集启动：{workers} 路"
                        f"（每路独立限速 {self.cfg.interval}s）")

        def _worker() -> None:
            nonlocal stopped
            dc = self.for_thread()
            db = dc.db
            while True:
                if should_stop is not None and should_stop():
                    with lock:
                        stopped = True
                    return
                # ★ 限额要**原子地预占**，不能「先查后领」——
                #   多个 worker 同时查到「还没满」→ 一起领 → 采出 limit+几个帖
                #   （实测 6 帖的限额采出了 8 帖）。
                with lock:
                    if limit and state["picked"] >= limit:
                        return
                    state["picked"] += 1
                try:
                    item = db.claim_next_detail()
                except Exception as e:                    # noqa: BLE001
                    with lock:
                        state["picked"] -= 1
                    self.client.log(f"[detail] 领活失败：{e}")
                    return
                if item is None:
                    with lock:
                        state["picked"] -= 1      # 没领到 → 归还名额
                    return
                tid = item["tid"]
                try:
                    res = dc.collect_one(
                        tid, pages_done=item.get("pages_done") or 0,
                        pages_total=item.get("pages_total") or 0,
                        max_attempts=max_attempts, should_stop=should_stop)
                except Exception as e:                    # noqa: BLE001
                    with lock:
                        state["failed"] += 1
                    self.client.log(f"[detail] tid={tid} ✗ 采集中断：{e}")
                    continue
                # ① 计数（锁内，且**不**在锁内做库写入 —— 避免锁嵌套）
                with lock:
                    if res.get("ok"):
                        state["done"] += 1
                        kind = "ok"
                    elif res.get("restricted"):
                        state["restricted"] += 1
                        kind = "restricted"
                    else:
                        state["failed"] += 1
                        kind = "failed"
                # ② 状态落库（db 自己的共享写锁 → 串行、线程安全）
                if kind == "restricted":
                    db.set_detail_state(tid, "restricted", res.get("reason", ""))
                    msg = (f"[detail] tid={tid} ⊘ 站方限制：{res.get('reason')}"
                           f"（已标记，不计入已采详情帖）")
                elif kind == "failed":
                    db.set_detail_state(tid, "failed", res.get("error", ""))
                    db.set_thread_detail(tid, detail_state="failed")
                    msg = f"[detail] tid={tid} ✗ {res.get('error')}"
                else:
                    msg = (f"[detail] tid={tid} ✓ {res['pages']} 页 / "
                           f"{res['posts']} 楼 / {res['chars']} 字")
                # ③ 打日志（锁外，避免长时间持锁）
                self.client.log(msg)

        threads = []
        for i in range(workers):
            th = threading.Thread(target=_worker, name=f"detail-{i + 1}",
                                  daemon=True)
            th.start()
            threads.append(th)
        try:
            for th in threads:
                th.join()
        except KeyboardInterrupt:
            stopped = True
            self.client.log("[detail] 收到中断，等各路收尾…")
            for th in threads:
                th.join(timeout=10)
        el = time.time() - t_start
        done, failed = state["done"], state["failed"]
        restricted = state["restricted"]
        self.client.log(f"[detail] 完成 {done} 帖（{workers} 路并发），"
                        f"站方限制 {restricted} 帖，失败 {failed} 帖，用时 {el:.0f}s")
        return {"done": done, "failed": failed, "restricted": restricted,
                "elapsed": round(el, 1), "stopped": stopped, "workers": workers}

    # ------------------------------------------------------------------
    _tls = threading.local()

    def for_thread(self) -> "DetailCollector":
        """给「当前线程」用的采集器：**独立 client + 独立 DB 连接**。

        ★ 为什么要独立（2026-10-25 并发改造）
          · `requests.Session` **不是线程安全的**（连接池会被并发搅乱）
          · 限速状态（`_last_obj`）是**实例级**的 → 共用会让限速互相干扰，
            甚至出现「限速被绕过」（另一个线程刚请求过就把时间戳刷新了）
          · sqlite3 的连接**不能跨线程**（check_same_thread 默认开）
        """
        cached = getattr(self._tls, "collectors", None)
        if cached is None:
            cached = {}
            self._tls.collectors = cached
        key = str(self.db.path)
        c = cached.get(key)
        if c is None:
            c = DetailCollector(self._worker_db(),
                                Client(self.cfg, verbose=False), self.cfg)
            cached[key] = c
        return c

    def _worker_db(self) -> DB:
        """worker 自己的 DB 连接（2026-10-25 加）。

        ★ 为什么不能共用一个 DB 实例
          sqlite3 的连接**默认不允许跨线程**（check_same_thread=True），
          两个 worker 共用一条连接会直接抛 ProgrammingError。
        ★ 为什么并发写仍然安全
          db.DB 的写锁是**进程级共享**的（见 db.DB 类注释）：
          多条连接 → 同一把锁 → 写事务真正串行化。
        """
        key = str(self.db.path)
        cached = getattr(self._tls, "dbs", None)
        if cached is None:
            cached = {}
            self._tls.dbs = cached
        db = cached.get(key)
        if db is None:
            db = DB(key)
            cached[key] = db
        return db

    # ------------------------------------------------------------------
    def _note_restricted_evidence(self, tid: int, html: str, page_no: int,
                                  why: str) -> None:
        """受限判定时**留证**：把提示页的特征记进 fetch_log。

        ★ 为什么要留证
          「站方限制」是**终态**（队列里摘出去 + 正文写标记 + 不计入已采详情帖），
          判错的代价很大（把明明可见的帖永久误标）→ 必须能事后复查。
          记下：字节数 / 标题 / 楼层锚点数 / 复验结论。
        """
        import re as _re
        m = _re.search(r"<title>(.*?)</title>", html or "", _re.S)
        title = (m.group(1).strip() if m else "?")[:120]
        n_anchor = len(_re.findall(r'id="post_\d+"', html or ""))
        self.db.log("detail", str(tid), "restricted_evidence", False,
                    f"page={page_no} {why} 字节={len(html or '')} "
                    f"楼层锚点={n_anchor} title={title!r}")

    # ------------------------------------------------------------------
    def _expected_posts(self, tid: int) -> int:
        """列表页给出的「回复数 + 1」= 应有楼层数。

        这是一条**独立于任何解析**的交叉验证：即使 total_pages 解析错了，
        它也能把「少收了一半楼层」的帖子揪出来。
        """
        r = self.db.conn.execute(
            "SELECT replies FROM threads WHERE tid=?", (tid,)).fetchone()
        if not r or not r["replies"]:
            return 0
        return int(r["replies"]) + 1

    def _known_pids(self, tid: int) -> List[int]:
        rows = self.db.conn.execute(
            "SELECT pid FROM posts WHERE tid=?", (tid,)).fetchall()
        return [r["pid"] for r in rows]

    def _unique_pids(self, tid: int) -> List[int]:
        rows = self.db.conn.execute(
            "SELECT DISTINCT pid FROM posts WHERE tid=?", (tid,)).fetchall()
        return [r["pid"] for r in rows]

    def _floors(self, tid: int) -> set:
        """已入库楼层的楼层号集合。"""
        rows = self.db.conn.execute(
            "SELECT DISTINCT floor FROM posts WHERE tid=? AND floor>0", (tid,)).fetchall()
        return {r["floor"] for r in rows}

    def _pages_fetched(self, tid: int) -> int:
        """已成功入库的页数（从 thread_pages 数，不看本次跑了几页 ——
        断点续传时本次只跑后半段，但整帖的页数是累计的）。"""
        r = self.db.conn.execute(
            "SELECT COUNT(*) FROM thread_pages WHERE tid=? AND status='ok'",
            (tid,)).fetchone()
        return int(r[0])

    def _head_tail(self, tid: int) -> Tuple[Optional[int], Optional[int]]:
        """头 = 楼主楼（floor==1）；尾 = 楼层号最大的那一楼。

        拿不准时退回「按页序：第 1 页首个 / 末页末个」。
        """
        r = self.db.conn.execute(
            "SELECT pid FROM posts WHERE tid=? AND floor=1 ORDER BY page_no LIMIT 1",
            (tid,)).fetchone()
        head = r["pid"] if r else None
        if head is None:
            rows = self.db.conn.execute(
                "SELECT pids FROM thread_pages WHERE tid=? AND status='ok' "
                "ORDER BY page_no", (tid,)).fetchall()
            if rows:
                first = rows[0]["pids"].split(",")[0]
                head = int(first) if first else None
        r2 = self.db.conn.execute(
            "SELECT pid FROM posts WHERE tid=? AND floor>0 ORDER BY floor DESC LIMIT 1",
            (tid,)).fetchone()
        tail = r2["pid"] if r2 else None
        if tail is None:
            rows = self.db.conn.execute(
                "SELECT pids FROM thread_pages WHERE tid=? AND status='ok' "
                "ORDER BY page_no DESC", (tid,)).fetchall()
            if rows:
                lst = rows[0]["pids"].split(",")
                tail = int(lst[-1]) if lst and lst[-1] else None
        return head, tail

    # ------------------------------------------------------------------
    def integrity(self) -> Dict:
        """完整性体检：把库里所有详情逐条核对一遍。

        判据与 collect_one **完全一致**：
          硬问题（进 problems）—— 缺头 / 缺尾 / 页数不足 / 状态非 done
          软警告（进 warnings）—— 楼层号缺口、站方回复计数与实收楼层不自洽
        两类必须分开：站方自己的计数问题不是我们漏采，
        混进 problems 会让体检报告失去意义。

        ★ 另列一栏 **growth（已识别的增量）**：
          站方的「回复数」比**我们上次采集时**变大了 → 站方又回复了，
          本地那份正文**过时了** → 已经自动标记 `stale` 并丢回队列。
          这才是**真增量**（判据来自两次列表快照的对比）。

        ★ 「少贴」的处理（2026-10-23 用户要求：
          「有少贴的情况下，保留原贴并且标记，主要是作增量的补采」）：
          站方称 N+1 楼、我们只收 N 楼 —— 实测（抽查 400 个 done 帖）
          392 个是「replies 不含楼主、+1 是楼主」的口径差异（属正常），
          少数是真的**尾部少了**（楼层号连续但总数少）。
          真的少贴 → **保留原帖** + 标记 `stale` + 丢回队列 → 自动补采。
          楼层号**中间**缺（站方删楼/置顶）→ 只是软警告，补采没意义。

        ⚠ 「少贴」必须在这里（体检时）标记，不能只在采集时：
          「少贴」的帖是 done 状态，而详情采集只捡 pending/stale ——
          不在这里标，它永远不会被重新采集，也就永远不会被补采（死锁）。
        """
        out: Dict = {"checked": 0, "ok": 0, "problems": [], "warnings": [],
                     "growth": [], "restricted": []}
        # ★ 站方限制的帖**单独一栏**：正文不公开，不是硬问题、也不是软警告。
        #   它只是「这一帖永远采不到正文」的**事实记录**。
        for r in self.db.conn.execute(
                "SELECT tid, detail_warn FROM threads "
                "WHERE detail_state='restricted'").fetchall():
            out["restricted"].append({
                "tid": r["tid"],
                "reason": r["detail_warn"] or "站方限制：历史帖子仅限作者本人查看",
                "posts": 0, "pages": 0,
                "note": "已标记并写入正文标记，不计入已采详情帖"})
        rows = self.db.conn.execute(
            "SELECT tid, detail_state, replies, posts_n, first_post_id, "
            "last_post_id, pages_total FROM threads "
            "WHERE detail_state IS NOT NULL AND detail_state<>'' "
            "AND detail_state NOT IN ('pending','stale','restricted')").fetchall()
        # 「站方又回复了 → 待重采」的帖 —— 这才是**真正识别出来的增量**
        for r in self.db.conn.execute(
                "SELECT tid, replies, posts_n FROM threads "
                "WHERE detail_state='stale'").fetchall():
            out["growth"].append({
                "tid": r["tid"], "posts": r["posts_n"] or 0,
                "expect": (r["replies"] or 0) + 1,
                "reason": f"站方回复数比上次采集时变大（现 {r['replies']}）"
                          f"→ 已标记待重采"})
        for r in rows:
            out["checked"] += 1
            head, tail = self._head_tail(r["tid"])
            uniq = len(self._unique_pids(r["tid"]))
            floors = self._floors(r["tid"])
            pages = self._pages_fetched(r["tid"])
            probs: List[str] = []
            warns: List[str] = []
            if r["detail_state"] != "done":
                probs.append(f"状态={r['detail_state']}")
            else:
                if head is None:
                    probs.append("缺头（楼主楼）")
                if tail is None:
                    probs.append("缺尾（末楼）")
                if r["pages_total"] and pages < r["pages_total"]:
                    probs.append(f"页数不足 {pages}/{r['pages_total']}")
                if floors:
                    mx = max(floors)
                    gap = [i for i in range(1, mx + 1) if i not in floors]
                    if gap:
                        warns.append(f"楼层号缺口 {gap}（站方删楼/置顶）")
                if r["replies"] and uniq < r["replies"] + 1:
                    # ★ 少贴 vs 楼层号缺口 —— 两者要分开（2026-10-23）：
                    #   · 楼层连续但总数少 → 站方还有楼我们没采到 → **增量**，要补采
                    #   · 楼层号中间缺     → 站方删楼/置顶 → 补采没意义，只是软警告
                    if is_short_and_retryable(r["replies"] + 1, uniq, floors):
                        short = (r["replies"] + 1) - uniq
                        reason = (f"少贴：站方称应有 {r['replies'] + 1} 楼，"
                                  f"实收 {uniq} 楼（少 {short} 楼）"
                                  f"→ 已标记待补采")
                        # ★ 直接标记 + 丢回队列（解死锁）：
                        #   这个帖是 done 状态，详情采集只捡 pending/stale，
                        #   不在这里标，它永远不会被补采。
                        # ★ 读补采次数要在**写之前**（先算 _tried，再写新值）
                        _prev_w = self.db.get_detail_warn(r["tid"]) or ""
                        _mm = re.search(r"已补采\s*(\d+)\s*次", _prev_w)
                        _tried = int(_mm.group(1)) if _mm else 0
                        if _tried < GAP_RETRY_MAX:
                            n_marked = self.db.mark_detail_stale([r["tid"]], reason=reason)
                            if n_marked:
                                # ★★ 必须把补采次数**写进 detail_warn** ——
                                #   它是「补过几次」的唯一记录处。
                                #   不写 → 下一轮读出来永远是 0 → 上限失效 → 无限重采。
                                _tried += 1
                                self.db.set_detail_warn(
                                    r["tid"], f"{reason}；已补采 {_tried} 次")
                            out["growth"].append({
                                "tid": r["tid"], "posts": uniq,
                                "expect": r["replies"] + 1, "reason": reason,
                                "gap_retry": True})
                        else:
                            warns.append(
                                f"少贴 {short} 楼，但已补采 {_tried} 次仍对不齐"
                                f"（疑为站方计数口径差异，不再自动补采）")
                    else:
                        # 楼层号**中间**缺 → 站方删楼/置顶 → 补采没意义，只记软警告
                        warns.append(
                            f"站方回复计数 {r['replies']} + 1 与实收楼层 {uniq} 不自洽"
                            f"（楼层号有缺口 → 站方删楼/置顶，不是少贴，不补采）")
            if probs:
                out["problems"].append({
                    "tid": r["tid"], "state": r["detail_state"],
                    "posts": uniq, "pages": pages, "problems": probs})
            else:
                out["ok"] += 1
                if warns:
                    out["warnings"].append({
                        "tid": r["tid"], "posts": uniq, "warnings": warns})
        return out
