# -*- coding: utf-8 -*-
"""
assist.py —— 协作任务：管理员出题，成员领活，回传后并入权威库
================================================================
Beta 的协作闭环：

    管理员                        控制面（管理员机器）              成员设备
    ────                          ──────────────────              ────────
    发布协助请求（选部门）   →   assist_tasks + assist_picks
                                 任务编号 A1 / A2 …
                                 ├─ 成员 agent 领活：POST /api/assist/pick
                                 │     ← 分配具体 tid（先旧后新）
                                 ├─ 成员本地采集（内存 SQLite，不落盘）
                                 │     → POST /api/assist/submit（回传楼层 + 锚点凭据）
                                 └─ 合并进权威库，复用同一套完整性判据
                                 实时日志 → 双方都看得到（带任务编号）

★ 为什么回传能直接并入权威库：
   成员端跑的是**同一个** `DetailCollector.collect_one()`，
   产出的 posts / thread_pages 走服务端的 `save_posts` / `record_page` /
   `set_thread_detail` —— 与管理员本地采集**同一条校验路径**，
   不另写一套"简化版"，所以不会因为走网络而丢掉完整性判据。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .db import DB
from .hub import Hub


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _now_ts() -> float:
    return time.time()


class AssistService:
    def __init__(self, db: DB, hub: Hub):
        self.db = db
        self.hub = hub

    # ---------------------------------------------------------------- 建任务
    def create(self, title: str, dept_ids: List[int], total: int = 50,
               created_by: str = "admin") -> Dict[str, Any]:
        """管理员发布一个协助请求：按部门挑出**还没采详情**的帖，拆成单帖的活。

        ★ 选帖顺序：**先新后旧**（2026-10-23 改）
          原先按 `pub_ts ASC`（先旧后新）选 —— 结果成员领到的全是**老帖**，
          而站方对历史帖子只给「提示信息」页（"历史帖子仅限作者本人查看"），
          成员采一百条全是空 → 完成数永远是 0、失败数一路涨，
          界面看上去就像"程序没在采"。
          改成先新后旧，成员领到的活才是**真能采到正文**的。

        ★ 排除 `detail_state='restricted'` 的帖：
          已经被站方判定为「正文不公开」的，再派一次只是让成员白跑一趟。
        """
        ids = [str(x) for x in (dept_ids or []) if str(x).strip()]
        if not ids:
            raise ValueError("没选部门")
        parts, params = [], []
        for i in ids:
            parts.append("dept_id = ?")
            params.append(i)
            parts.append("instr(',' || COALESCE(extra_dept_ids,'') || ',', ?) > 0")
            params.append(f",{i},")
        rows = self.db.conn.execute(
            "SELECT tid FROM threads "
            "WHERE COALESCE(detail_state,'pending') NOT IN ('done','restricted') "
            f"AND ({' OR '.join(parts)}) "
            "ORDER BY COALESCE(pub_ts,0) DESC, tid DESC", params).fetchall()
        tids = [r["tid"] for r in rows]
        if total and total > 0:
            tids = tids[:total]
        if not tids:
            raise ValueError("这些部门下没有待采详情的帖了")

        code = self._next_code()
        now = _now()
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO assist_tasks(code,title,dept_ids,total,done,state,"
                "created_by,created_at,updated_at) VALUES(?,?,?,?,0,'open',?,?,?)",
                (code, title or "", ",".join(ids), len(tids), created_by, now, now))
            self.db.conn.executemany(
                "INSERT OR IGNORE INTO assist_picks(task_code,tid,state) "
                "VALUES(?,?,'pending')", [(code, t) for t in tids])
            self.db.conn.commit()
        self.hub.info("admin", f"发布协助请求 {code}：{len(tids)} 帖（{title or '未命名'}）",
                      task=code)
        return self.get(code)

    def _next_code(self) -> str:
        n = self.db.conn.execute("SELECT COUNT(*) FROM assist_tasks").fetchone()[0]
        i = n + 1
        while self.db.conn.execute("SELECT 1 FROM assist_tasks WHERE code=?",
                                   (f"A{i}",)).fetchone():
            i += 1
        return f"A{i}"

    # ---------------------------------------------------------------- 领活
    def reclaim_stale(self, max_age_sec: int = 1800,
                      offline_sec: int = 120) -> int:
        """**回收超时未回传的活**。

        ★ 为什么必须有这个：
          活被领走（state='taken'）后，只有回传才会变成 done/failed。
          如果成员领了活就关机、agent 崩了、网络断了 ——
          这些活会**永远**停在 'taken'，谁都领不到，池子等于被占死。
          实测踩过：测试脚本一次 `limit=999` 把 5000 条全领走，
          真实成员从此无活可领，而界面显示的是「各节点贡献 5000」，
          看起来像在采，其实一条没做、池子也空了。

        两条回收判据（2026-10-23 补上第二条）：
          ① **超时**：taken 超过 max_age_sec（默认 30 分钟）还没回传 → 退回。
          ② **节点已离线**：领活的节点**心跳断了**超过 offline_sec
             （默认 120 秒）→ **立刻**退回。
             ★ 为什么加这条：成员**关掉窗口**是最常见的死法，
               等 30 分钟太久 —— 期间整个池子对其他成员是空的。
               而 agent 只要活着就每 12 秒发一次心跳，
               心跳断了 2 分钟 → 它不可能还在采那条活 → 可以安全退回。

        ⚠⚠ **cutoff 必须是字符串**，不能是时间戳：
          `taken_at` 存的是 'YYYY-MM-DD HH:MM:SS' 文本，
          而 SQLite 的**类型亲和性**规定「整数永远小于文本」→
          `taken_at < 1792712345` 这个条件**恒为假**，回收一条都不生效。
          （踩过一次：写着回收逻辑，0 条被回收，还以为活都正常。）
          文本按字典序比较正好等价于时间序 —— 同格式的字符串直接比就行。
        """
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(time.time() - max_age_sec))
        off_cut = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(time.time() - offline_sec))
        # ★★ 括号**必须**把 OR 组整体括起来（2026-10-23 修的自己的 bug）
        #   写成 `state='taken' AND A OR B` 时，SQL 的 AND 优先级高于 OR，
        #   会被解析成 `(state='taken' AND A) OR B` —— 第二段 B **不受**
        #   state='taken' 约束 → **已完成的活（done）也被改成 pending**！
        #   实测后果：某节点心跳断了 2 分钟，它名下 400+ 条**已采完**的活
        #   全被退回池子 → 成员得重复劳动，完成数从 466 掉回 56。
        where = ("((taken_at IS NULL OR taken_at < ?) OR "
                 "(node IN (SELECT COALESCE(node_id,'') FROM members "
                 "WHERE last_seen IS NULL OR last_seen < ?)))")
        # ★ 先**只读**看一眼有没有要回收的（2026-10-23 补）
        #   UPDATE 即使一行都匹配不到，也会开启写事务、占住写锁 ——
        #   而服务端是多线程各自一条连接，撞上就是
        #   `database is locked` → HTTP 500 → agent 判「失联」而自停。
        #   实测踩过：界面每 15 秒轮询一次任务列表，那个「顺手回收一下」
        #   把写锁抢得死死的，正在回传的成员 agent 直接被打挂。
        #   所以：先查（只读），真有要回收的才写。
        try:
            n_need = self.db.conn.execute(
                f"SELECT COUNT(*) FROM assist_picks "
                f"WHERE state='taken' AND {where}",
                (cutoff, off_cut)).fetchone()[0]
        except Exception:                             # noqa: BLE001
            return 0
        if not n_need:
            return 0
        with self.db.lock:
            cur = self.db.conn.execute(
                "UPDATE assist_picks SET state='pending', node=NULL, taken_at=NULL "
                f"WHERE state='taken' AND {where}", (cutoff, off_cut))
            n = cur.rowcount or 0
            if n:
                self.db.conn.commit()
        return n

    def return_picks(self, node: str, code: Optional[str] = None) -> int:
        """把某节点**领走但还没回传**的活退回池子。

        ★ 为什么要有这个（2026-10-23）
          「超时才回收」不够快：成员**关掉窗口**是最常见的死法，
          等 30 分钟期间整个池子对其他成员是空的。
          有了主动退活，agent 退出时（Ctrl+C、跑完一轮、正常结束）
          立刻把手里没做完的活还回去，池子马上恢复可用。

        ★ 也给测试脚本用：测试用 limit=999 验证「不限配额」，
          领完必须还回去 —— 否则测试脚本自己把池子占死，
          真实成员无活可领（实测踩过两次）。

        只退**还没回传**的（state='taken'）；已完成的 done/failed 不动。
        """
        now = _now()
        with self.db.lock:
            if code:
                cur = self.db.conn.execute(
                    "UPDATE assist_picks SET state='pending', node=NULL, taken_at=NULL "
                    "WHERE state='taken' AND node=? AND task_code=?",
                    (node, code))
            else:
                cur = self.db.conn.execute(
                    "UPDATE assist_picks SET state='pending', node=NULL, taken_at=NULL "
                    "WHERE state='taken' AND node=?", (node,))
            n = cur.rowcount or 0
            if n:
                db2 = self.db
                db2.conn.commit()
        return n

    def pick(self, node: str, limit: int = 0,
             code: Optional[str] = None) -> Dict[str, Any]:
        """成员领活：领走若干个具体 tid（先旧后新）。

        ★ **不限配额**（2026-10-22 起）：
          原来的 `limit` 默认取成员的 `quota`（单次配额），上限 200。
          现在配额取消 —— `limit` 只是「这一轮领多少」的批量大小，
          不再受 members.quota 约束，上限放宽到 2000。
          真正的约束在**站方**那边（别把站方打疼），不在我们这边。
        """
        limit = max(1, min(int(limit or 50), 2000))
        # 先把超时没回传的活收回来，免得池子被占死
        self.reclaim_stale()
        now = _now()
        with self.db.lock:
            if code:
                rows = self.db.conn.execute(
                    "SELECT * FROM assist_picks WHERE task_code=? AND state='pending' "
                    "ORDER BY id LIMIT ?", (code, limit)).fetchall()
            else:
                rows = self.db.conn.execute(
                    "SELECT p.* FROM assist_picks p JOIN assist_tasks t "
                    "ON t.code=p.task_code "
                    "WHERE p.state='pending' AND t.state IN ('open','active') "
                    "ORDER BY p.id LIMIT ?", (limit,)).fetchall()
            picks = []
            for r in rows:
                self.db.conn.execute(
                    "UPDATE assist_picks SET node=?, state='taken', taken_at=? "
                    "WHERE id=?", (node, now, r["id"]))
                picks.append({"task_code": r["task_code"], "tid": r["tid"]})
            if picks:
                codes = sorted({p["task_code"] for p in picks})
                for c in codes:
                    self.db.conn.execute(
                        "UPDATE assist_tasks SET state='active', updated_at=? "
                        "WHERE code=?", (now, c))
            self.db.conn.commit()
        if picks:
            codes = sorted({p["task_code"] for p in picks})
            self.hub.info(node, f"领走 {len(picks)} 帖", task=",".join(codes))
        return {"picks": picks}

    # ---------------------------------------------------------------- 冲突
    def _conflict(self, code: str, tid: int, node: str, kind: str,
                  reason: str, local_val: str = "", remote_val: str = "") -> None:
        """记一条「回传与本地不一致」的痕迹。

        为什么要有这张表：合并规则是「**重复的以我们本地的为准**」，
        那就必须能回答一句「**哪些**被本地压住了、为什么」。
        只在日志里飘一句，等于没有留痕 —— 事后无法复盘。
        """
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO assist_conflicts(task_code,tid,node,kind,reason,"
                "local_val,remote_val,resolved,created_at) VALUES(?,?,?,?,?,?,?,0,?)",
                (code, int(tid), node or "", kind, reason[:500],
                 str(local_val)[:500], str(remote_val)[:500], _now()))
            self.db.conn.commit()

    def conflicts(self, code: str = "", limit: int = 200,
                  offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
        """回传冲突记录（分页）。

        ★ 为什么返回 (列表, 总数) 而不是只返回列表（2026-10-23 加）：
          界面要做**翻页**，就必须知道一共有多少条 ——
          只给 limit 的话，界面永远不知道「还有没有下一页」。
          总数用一次 COUNT 查（这张表不大），换取界面能显示「第 2 / 5 页」。
        """
        if code:
            total = int(self.db.conn.execute(
                "SELECT COUNT(*) FROM assist_conflicts WHERE task_code=?",
                (code,)).fetchone()[0])
            rows = self.db.conn.execute(
                "SELECT * FROM assist_conflicts WHERE task_code=? "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (code, limit, offset)).fetchall()
        else:
            total = int(self.db.conn.execute(
                "SELECT COUNT(*) FROM assist_conflicts").fetchone()[0])
            rows = self.db.conn.execute(
                "SELECT * FROM assist_conflicts ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()
        return [dict(r) for r in rows], total

    # ---------------------------------------------------------------- 回传
    def submit(self, node: str, code: str, tid: int,
               posts: List[Dict], pages: List[Dict],
               summary: Optional[Dict] = None) -> Dict[str, Any]:
        """成员回传一帖的采集结果 → **走与本地采集完全相同的合并路径**。

        ★★ 合并规则：**不跳过，采取并覆盖**（2026-10-23 用户明确）

          楼层级别：
            · 同 pid  → 用**成员版覆盖**本地版（本地原文被替换）
            · 新 pid  → 直接加进来（**增量**：站方追加的回复就这样进来）
            · 本地独有、成员没采到的 pid → **保留**（不因成员少采而丢数据）

          计数级别：楼层数 / 字数 / 头尾 pid / 页数**一律现算**合并后的真实值，
            不用成员报上来的数字（那只是它自己那一份）。

          留痕：被覆盖的楼层、被新增的楼层，都写进 `assist_conflicts`，
            界面上「合并冲突记录」可查 —— 覆盖了谁、覆盖前是什么、覆盖后是什么。

        ⚠ 为什么可以放心覆盖：`save_posts` 的 ON CONFLICT 分支只更新
          `content/chars/author/post_time`，而 `floor` 用 CASE 保护
          （Discuz 置顶回复在首页会渲染出 floor=0，不能把真楼层号覆盖掉）。
        """
        summary = summary or {}
        posts = posts or []
        pages = pages or []
        if not posts:
            # ★ 站方限制 ≠ 失败（2026-10-23 用户明确）
            #   站方对**历史帖子**只给「提示信息」页（"历史帖子仅限作者本人查看"），
            #   成员采回来自然没有正文 —— 这是**站方的可见性策略**，
            #   不是成员偷懒、也不是程序坏了。
            #   所以：标记该帖 + 把提示写进正文 + **不计入已采详情帖**，
            #   活的状态记成 'restricted' 而不是 'failed'。
            if summary.get("restricted"):
                reason = str(summary.get("reason") or
                             "站方限制：历史帖子仅限作者本人查看")
                self.db.mark_restricted(int(tid), reason)
                self._conflict(code, tid, node, "restricted", reason)
                self._mark_pick(code, tid, node, "restricted", 0, 0)
                self.hub.info(node, f"{code} tid={tid} 站方限制（正文不公开），"
                                    f"已标记不计入已采详情帖", task=code)
                return {"ok": False, "restricted": True, "reason": reason}
            self.hub.warn(node, f"{code} tid={tid} 回传为空", task=code)
            self._conflict(code, tid, node, "empty", "成员回传没有任何楼层")
            self._mark_pick(code, tid, node, "failed", 0, 0)
            return {"ok": False, "reason": "空结果"}

        remote_posts = int(summary.get("posts") or len(posts))
        remote_chars = int(summary.get("chars")
                           or sum(int(p.get("chars") or 0) for p in posts))

        # ---- 本地现状 ----------------------------------------------------
        local = self.db.conn.execute(
            "SELECT detail_state,posts_n,chars,pages_total FROM threads WHERE tid=?",
            (int(tid),)).fetchone()
        local_state = (local["detail_state"] if local else "") or "pending"
        local_posts = int(local["posts_n"] or 0) if local else 0
        local_chars = int(local["chars"] or 0) if local else 0

        # ---- 逐 pid 合并：**不跳过，采取并覆盖** -------------------------
        # ★ 规则（2026-10-23 用户明确）：组员传输的帖子如果涉及相关帖子，
        #   **不跳过，采取并覆盖**。
        #   具体到楼层：
        #     · 同 pid  → 用**成员版覆盖**本地版（本地原文被替换）
        #     · 新 pid  → 直接加进来（这就是**增量**：站方追加的回复）
        #     · 本地独有、成员没采到的 pid → **保留**（不因成员少采而丢数据）
        #   计数（楼层数/字数/头尾/页数）**一律现算**合并后的真实值。
        olds = {int(r["pid"]): (int(r["chars"] or 0), r["content"] or "")
                for r in self.db.conn.execute(
                    "SELECT pid,chars,content FROM posts WHERE tid=?", (int(tid),))}
        existing = set(olds)
        dup = [p for p in posts if int(p.get("pid") or 0) in existing]
        fresh = [p for p in posts if int(p.get("pid") or 0) not in existing]

        # 被覆盖且内容确实变了的楼层 → 留痕（覆盖前 vs 覆盖后）
        diff = []
        for p in dup:
            pid = int(p.get("pid") or 0)
            old_chars, old_text = olds.get(pid, (None, ""))
            if old_chars is None:
                continue
            if int(p.get("chars") or 0) != old_chars or (p.get("content") or "") != old_text:
                diff.append((p, old_chars))
        if diff:
            self._conflict(
                code, tid, node, "overwritten",
                f"{len(diff)} 个楼层被成员回传**覆盖**（同 pid，用成员版替换本地版）",
                local_val="、".join(f"pid{p.get('pid')}={c}字" for p, c in diff[:8]),
                remote_val="、".join(f"pid{p.get('pid')}={p.get('chars')}字"
                                     for p, _ in diff[:8]))
        if fresh:
            self._conflict(
                code, tid, node, "merged_new",
                f"新增 {len(fresh)} 个楼层（站方追加的回复 → 增量加入）",
                local_val=f"合并前 {len(existing)} 楼 / {local_chars} 字",
                remote_val=f"成员回传 {remote_posts} 楼 / {remote_chars} 字")

        # ---- 写入：按页分组，逐页一个事务 ------------------------------
        # ⚠ 必须**按楼层自己的 page_no 分组**再写。
        #   旧实现把「全部楼层」在每一页都写一遍，导致所有楼层的 page_no
        #   都被刷成最后一页 —— 页归属错了，完整性判据会跟着错。
        # ★ 这次写的是**全部** posts（dup + fresh）：
        #   dup 走 save_posts 的 ON CONFLICT 分支 → 成员版覆盖本地版。
        by_page: Dict[int, List[Dict]] = {}
        for p in posts:
            by_page.setdefault(int(p.get("page_no") or 1), []).append(p)
        for pno in sorted(by_page):
            self.db.save_posts(int(tid), pno, by_page[pno])
        if pages:
            for pg in pages:
                pno = int(pg.get("page_no") or 1)
                self.db.record_page(int(tid), pno,
                                    ",".join(str(x) for x in (pg.get("pids") or [])),
                                    "ok")
        else:
            for pno in sorted(by_page):
                pids = sorted(int(p.get("pid") or 0) for p in by_page[pno])
                self.db.record_page(int(tid), pno,
                                    ",".join(str(x) for x in pids), "ok")

        ok = bool(summary.get("ok"))
        # 计数**一律现算**，不用成员报上来的数字：
        #   合并后库里可能还有本地早就有的楼层，成员报的数只是它自己那一份，
        #   直接写进去会让 posts_n/chars 与实际对不上，完整性判据跟着失真。
        cnt = self.db.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(chars),0) c FROM posts WHERE tid=?",
            (int(tid),)).fetchone()
        n_pages = self.db.conn.execute(
            "SELECT COUNT(*) n FROM thread_pages WHERE tid=? AND status='ok'",
            (int(tid),)).fetchone()["n"]
        head_r = self.db.conn.execute(
            "SELECT pid FROM posts WHERE tid=? AND floor=1 ORDER BY page_no LIMIT 1",
            (int(tid),)).fetchone()
        tail_r = self.db.conn.execute(
            "SELECT pid FROM posts WHERE tid=? AND floor>0 ORDER BY floor DESC LIMIT 1",
            (int(tid),)).fetchone()
        self.db.set_thread_detail(
            int(tid),
            detail_state="done" if ok else "failed",
            first_post_id=head_r["pid"] if head_r else summary.get("head_pid"),
            last_post_id=tail_r["pid"] if tail_r else summary.get("tail_pid"),
            pages_total=n_pages or (summary.get("pages") or len(pages)),
            posts_n=cnt["n"],
            chars=cnt["c"])
        # ★★ 队列行也要收（2026-10-25 修）
        #   threads.detail_state 与 detail_queue.state 是**两张表**，
        #   改前者不会改后者 → 组员回传成功后，那行永远停在 'pending'，
        #   既白占产能（会被详情采集重复领走再采一遍），
        #   又让列表采集一直以为它「还没采详情」。
        #   实测库里积了 231 条这样的孤儿活（全部来自组员回传）。
        self.db.finish_queue(int(tid), "done" if ok else "failed",
                             "" if ok else str(summary.get("error") or "回传失败"))
        if summary.get("warnings"):
            self.db.set_detail_warn(int(tid), "；".join(summary["warnings"]))

        now = _now()
        self._mark_pick(code, tid, node, "done" if ok else "failed",
                        remote_posts, remote_chars)
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE assist_tasks SET done=(SELECT COUNT(*) FROM assist_picks "
                "WHERE task_code=? AND state='done'), updated_at=? WHERE code=?",
                (code, now, code))
            self.db.conn.execute(
                "UPDATE assist_tasks SET state='done' WHERE code=? AND "
                "done + (SELECT COUNT(*) FROM assist_picks WHERE task_code=? "
                "AND state='restricted') >= total", (code, code))
            self.db.conn.commit()

        mark = "✓" if ok else "✗"
        # 任务编号由日志徽标带（task=code），消息里不再重复
        extra = ""
        if fresh:
            extra += f"（新增 {len(fresh)} 楼）"
        if diff:
            extra += f"（覆盖 {len(diff)} 楼）"
        self.hub.info(node, f"tid={tid} {mark} "
                            f"{summary.get('pages') or len(pages)} 页 / "
                            f"{remote_posts} 楼 / {remote_chars} 字" + extra,
                      task=code)
        return {"ok": ok, "merged": True,
                "added": len(fresh), "overwritten": len(diff),
                "merged_total": cnt["n"],
                "local_before": {"posts": local_posts, "chars": local_chars},
                "task": code, "tid": int(tid)}

    def _mark_pick(self, code: str, tid: int, node: str, state: str,
                   posts: int, chars: int) -> None:
        now = _now()
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE assist_picks SET state=?, posts=?, chars=?, done_at=?, node=? "
                "WHERE task_code=? AND tid=?",
                (state, int(posts), int(chars), now, node, code, int(tid)))
            self.db.conn.commit()

    # ---------------------------------------------------------------- 查询
    # ---------------------------------------------------------------- 撤销
    def cancel(self, code: str, by: str = "admin") -> Dict[str, Any]:
        """**撤销**一个协作任务。

        ★ 语义（用户 2026-10-23 明确：「已经完成的不废弃」）
          撤销 = **这个任务作废，不再派活**。但分三块处理：

          | 那些活 | 处置 | 为什么 |
          |---|---|---|
          | 已完成（done）/ 失败 / 站方限制 | **原样保留** | 正文已经进权威库了，撤掉任务不该把已采到的东西一起废掉 |
          | 未完成（pending / taken） | 标成 `cancelled` | 不再有人领它们；但**不删** —— 留痕可查，也能事后恢复 |
          | 权威库里的正文 | **一个字不动** | 正文本来就独立于任务存在 |

        ★ 正在被成员领着的那几条怎么办
          成员手里那几条在他采完回传时，`_mark_pick` 会把状态改回 `done`
          → **数据不浪费**，已采到的正文照常入库。
          这正是「已完成的不废弃」最实在的一种情形。

        ★ 为什么要给未完成的活一个新状态，而不是直接删
          删了就没法回答「这个任务当初派了多少活、干了多少、剩多少」。
          留一条 `cancelled` 的痕迹，问题就还能问清楚。
        """
        now = _now()
        with self.db.lock:
            task = self.db.conn.execute(
                "SELECT * FROM assist_tasks WHERE code=?", (code,)).fetchone()
            if not task:
                return {"ok": False, "error": f"任务 {code} 不存在"}
            if task["state"] == "cancelled":
                return {"ok": False, "error": f"任务 {code} 已经撤销过了"}
            # 未完成的 → 作废（留痕）；已完成的 → 不碰
            cur = self.db.conn.execute(
                "UPDATE assist_picks SET state='cancelled', node=NULL, taken_at=NULL "
                "WHERE task_code=? AND state IN ('pending','taken')", (code,))
            undone = cur.rowcount or 0
            kept = self.db.conn.execute(
                "SELECT COUNT(*) FROM assist_picks WHERE task_code=? "
                "AND state IN ('done','failed','restricted')", (code,)).fetchone()[0]
            self.db.conn.execute(
                "UPDATE assist_tasks SET state='cancelled', updated_at=? WHERE code=?",
                (now, code))
            self.db.conn.commit()
        self.hub.info(by, f"撤销协作任务 {code}：未完成的 {undone} 帖作废，"
                          f"已完成的 {kept} 帖**保留**（正文仍在权威库）", task=code)
        return {"ok": True, "code": code, "cancelled": undone, "kept": kept}

    def restore(self, code: str, by: str = "admin") -> Dict[str, Any]:
        """**恢复**一个被撤销的任务：把作废的活放回池子，任务重新开。

        「撤销」不是终局 —— 撤销错了、或者成员还没采完想继续，都能恢复。
        已完成的那部分**不受影响**（本来就是保留的）。
        """
        now = _now()
        with self.db.lock:
            task = self.db.conn.execute(
                "SELECT * FROM assist_tasks WHERE code=?", (code,)).fetchone()
            if not task:
                return {"ok": False, "error": f"任务 {code} 不存在"}
            if task["state"] != "cancelled":
                return {"ok": False, "error": f"任务 {code} 不是撤销状态"}
            cur = self.db.conn.execute(
                "UPDATE assist_picks SET state='pending' "
                "WHERE task_code=? AND state='cancelled'", (code,))
            back = cur.rowcount or 0
            self.db.conn.execute(
                "UPDATE assist_tasks SET state='open', updated_at=? WHERE code=?",
                (now, code))
            self.db.conn.commit()
        self.hub.info(by, f"恢复协作任务 {code}：{back} 帖重新放回池子", task=code)
        return {"ok": True, "code": code, "restored": back}

    # ---------------------------------------------------------------- 详情
    def task_detail(self, code: str, limit: int = 200,
                    offset: int = 0) -> Dict[str, Any]:
        """一个协作任务的**详情**：概要 + 这个任务里的每一帖。

        ★ 为什么要有详情
          任务表只给一行汇总（完成 499/500、谁贡献了多少），
          但协作是**帖子级**的：到底哪几帖采到了、哪几帖还欠着、
          哪几帖被站方限制了 —— 这些都只能看明细。
        """
        info = self.get(code)
        if not info:
            return {}
        limit = max(1, min(int(limit or 200), 1000))
        offset = max(0, int(offset or 0))
        where = "p.task_code=?"
        args: List[Any] = [code]
        if info.get("state") == "cancelled":
            # 撤销后的任务：被作废的那些也要看得见（否则"撤销了哪些"查不到）
            where += " AND p.state<>'cancelled'"
        total = self.db.conn.execute(
            f"SELECT COUNT(*) FROM assist_picks p WHERE {where}", args).fetchone()[0]
        rows = self.db.conn.execute(
            f"SELECT p.tid, p.state, p.node, p.posts, p.chars, p.taken_at, p.done_at, "
            f"       t.title, t.detail_state, t.dept_name "
            f"FROM assist_picks p LEFT JOIN threads t ON t.tid=p.tid "
            f"WHERE {where} "
            f"ORDER BY (p.state='done') DESC, p.id LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["title"] = d.get("title") or ""
            d["detail_state"] = d.get("detail_state") or ""
            items.append(d)
        return {"task": info, "items": items,
                "total": int(total or 0), "limit": limit, "offset": offset}

    # ---------------------------------------------------------------- 移除
    def remove(self, code: str, by: str = "admin") -> Dict[str, Any]:
        """把一个协作任务**从列表里移除**（管理员）。

        ★ 与「撤销」的区别
          撤销 = 停止派活，未完成的作废，**可恢复**；
          移除 = 这个任务不再出现在列表里，**不可恢复**。

        ★ 删什么、不删什么
          删：assist_tasks（任务本身）+ assist_picks（这个任务的活）
          **不删**：assist_conflicts（合并痕迹）—— 那是"谁覆盖了什么"的**审计记录**，
                  与任务列表无关，删了就没法追责了。
          **不删**：权威库里的帖子正文 —— 正文本来就独立于任务存在，
                  任务移除 ≠ 把采到的数据扔掉。

        ★ 什么情况下不允许删
          还有**进行中**（taken）的活 —— 成员正在采，删了他回传会落到空处。
          这种要先「撤销」。
        """
        info = self.get(code)
        if not info:
            return {"ok": False, "error": f"任务 {code} 不存在"}
        inflight = int(info.get("inflight") or 0)
        if inflight:
            return {"ok": False,
                    "error": f"还有 {inflight} 帖正在被采，先「撤销」再移除"}
        done = int(info.get("done") or 0)
        pending = int(info.get("pending") or 0)
        now = _now()
        with self.db.lock:
            self.db.conn.execute("DELETE FROM assist_picks WHERE task_code=?", (code,))
            self.db.conn.execute("DELETE FROM assist_tasks WHERE code=?", (code,))
            self.db.conn.commit()
        self.hub.info(by, f"移除协作任务 {code}（已完成 {done} 帖**保留**在权威库，"
                          f"未完成的 {pending} 帖一并作废）", task=code)
        return {"ok": True, "code": code, "done_kept": done,
                "pending_dropped": pending}

    def get(self, code: str) -> Dict[str, Any]:
        r = self.db.conn.execute("SELECT * FROM assist_tasks WHERE code=?",
                                 (code,)).fetchone()
        if not r:
            return {}
        d = dict(r)
        d["picked"] = self.db.conn.execute(
            "SELECT COUNT(*) FROM assist_picks WHERE task_code=?", (code,)).fetchone()[0]
        d["by_node"] = {x["node"]: x["n"] for x in self.db.conn.execute(
            "SELECT node, COUNT(*) n FROM assist_picks WHERE task_code=? AND node IS NOT NULL "
            "GROUP BY node", (code,)).fetchall()}
        d["by_state"] = {x["state"]: x["n"] for x in self.db.conn.execute(
            "SELECT state, COUNT(*) n FROM assist_picks WHERE task_code=? GROUP BY state",
            (code,)).fetchall()}
        # ★ 拆开「已领」与「已完成」—— 之前只有一个 by_node，
        #   显示成「各节点贡献 5000」，看着像在采，其实一条都没完成。
        d["by_node_detail"] = self._by_node_detail(code)
        d["inflight"] = d["by_state"].get("taken", 0)
        d["pending"] = d["by_state"].get("pending", 0)
        d["failed"] = d["by_state"].get("failed", 0)
        # ★ 完成数**现算**（不要直接用 assist_tasks.done 那一列）：
        #   那一列只在「回传」路径上更新，而**撤销/恢复**不走回传 →
        #   列里的数会停在旧值（实测：撤销后明明有 1 帖已完成，界面却显示 0）。
        #   撤销恰恰是「已完成的不废弃」这条口径最需要看清楚的时候。
        d["done"] = d["by_state"].get("done", 0)
        # ★ 站方限制（正文不公开）：**单独一栏**，既不算完成也不算失败。
        #   不分开的话，成员会看着「失败」一路涨而「完成」永远是 0，
        #   误以为程序坏了 —— 这正是"无法确认是否被采集"的观感来源。
        d["restricted"] = d["by_state"].get("restricted", 0)
        # ★ 被撤销的活（撤销时未完成的那些）—— 留痕，不删
        d["cancelled"] = d["by_state"].get("cancelled", 0)
        d["can_cancel"] = d.get("state") in ("open", "active")
        d["can_restore"] = d.get("state") == "cancelled"
        d["last_activity"] = self.db.conn.execute(
            "SELECT MAX(done_at) t FROM assist_picks WHERE task_code=?", (code,)).fetchone()["t"]
        return d

    def _by_node_detail(self, code: str) -> Dict[str, Dict[str, int]]:
        """每个节点的 **完成/已领/失败/站方限制/已作废** 五个数。"""
        out: Dict[str, Dict[str, int]] = {}
        for x in self.db.conn.execute(
                "SELECT node, state, COUNT(*) n FROM assist_picks "
                "WHERE task_code=? AND node IS NOT NULL GROUP BY node, state",
                (code,)).fetchall():
            node = x["node"]
            slot = out.setdefault(node, {"done": 0, "taken": 0, "failed": 0,
                                         "restricted": 0, "cancelled": 0,
                                         "total": 0})
            st = x["state"]
            if st in slot:
                slot[st] += x["n"]
            slot["total"] += x["n"]
        return out

    def tasks(self) -> List[Dict[str, Any]]:
        # 列表也顺手回收一下超时未回传的活（好让界面及时显示真实池子）
        # ★ 但**回收失败绝不能让这个读接口 500**：回收是「顺手做的 housekeeping」，
        #   失败了就晚点再回收，不该把整个界面打挂
        #   （实测踩过：回收撞上并发写 → database is locked → 500 →
        #    正在回传的成员 agent 被打成「失联」而自停）。
        try:
            self.reclaim_stale()
        except Exception:                             # noqa: BLE001
            pass
        codes = [r["code"] for r in self.db.conn.execute(
            "SELECT code FROM assist_tasks ORDER BY id DESC").fetchall()]
        return [self.get(c) for c in codes]

    def online(self, stale_sec: int = 300) -> List[Dict[str, Any]]:
        """**谁还在线** —— 界面上要能一眼看出「谁在采、有没有卡住」。

        判据：心跳（`members.last_seen`）在 stale_sec 内算在线。
        每个节点再附上：领用中的活、已完成的活、失败数、最后一次回传时间。
        """
        out: List[Dict[str, Any]] = []
        now = time.time()
        for m in self.db.conn.execute(
                "SELECT email, role, node_id, status, last_seen FROM members "
                "ORDER BY id").fetchall():
            node = m["node_id"] or ("admin" if m["role"] == "admin"
                                    else "member:" + m["email"])
            ls = m["last_seen"]
            age = None
            if ls:
                try:
                    age = int(now - time.mktime(time.strptime(ls, "%Y-%m-%d %H:%M:%S")))
                except Exception:
                    age = None
            row = self.db.conn.execute(
                "SELECT SUM(state='done') d, SUM(state='taken') t, SUM(state='failed') f, "
                "SUM(state='restricted') r, MAX(done_at) la FROM assist_picks WHERE node=?",
                (node,)).fetchone()
            # ★ agent 自报的状态（心跳里带来的）
            #   没有它，界面只能靠「心跳停了多久」去猜节点在干嘛 ——
            #   「在等活」和「卡住了」看起来一模一样。
            astate = ""
            anote = ""
            try:
                raw = self.db.get_meta(f"agent_state:{node}")
                if raw:
                    d2 = json.loads(raw)
                    astate = str(d2.get("state") or "")
                    anote = str(d2.get("note") or "")
            except Exception:                         # noqa: BLE001
                pass
            out.append({
                "node": node,
                "email": m["email"],
                "role": m["role"],
                "status": m["status"],
                "online": bool(age is not None and age <= stale_sec),
                "last_seen": ls or "",
                "seconds_ago": age,
                "done": int(row["d"] or 0),
                "taken": int(row["t"] or 0),
                "failed": int(row["f"] or 0),
                # ★ 站方限制：正文不公开，不是成员的失败
                "restricted": int(row["r"] or 0),
                "agent_state": astate,
                "agent_note": anote,
                "last_activity": row["la"] or "",
            })
        return out
