# -*- coding: utf-8 -*-
"""
anno.py —— 人工标注台（人工标注功能）

★ 定位：**在现有系统里自建标注台**，不引入 Label Studio / doccano 那套独立系统
  （要 Docker + 第二套账号 + 数据导出再导回，两处很容易不同步）。
  交互照它们的成熟做法来：任务队列 → 逐条标注 → 提交 → 复核 → 进度统计。

★ 核心口径（用户 2026-11-06 拍板）：**人工标签优先覆盖规则标签，规则值留痕可追溯**
  → 导出写两列：「三维标签」= 人工优先后的最终值；「规则三维标签」= 规则算的原值。

★ 人工标签三种状态：
  submitted  已标注待复核 → **已生效**（覆盖规则值）
  approved   已复核通过
  rejected   已打回       → **失效**，导出回落到规则标签（记录仍留着，不删）

★ 签署位纪律（沿用 2026-10-21「复核人留空」）：
  组员标注时**不动** reviewed_by（复核人是人工签署位，不能代签）；
  管理员**本人逐条**点「通过/打回」时才记他自己的 email —— 那是他刚做的动作。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from .db import DB
from .hub import Hub

#: 三个维度的中文名（界面用；标签取值来自 tags.py，不另造一套）
DIM_CN = {
    "relation": "关系",
    "process": "过程",
    "impact": "影响",
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def anno_tag_map_text(conn) -> Dict[int, str]:
    """{tid: 人工标签1 / 人工标签2} —— **已生效**的人工标签（被打回的除外）。"""
    out: Dict[int, str] = {}
    for tid, tag in conn.execute(
            "SELECT tid, tag FROM thread_anno_tags WHERE status<>'rejected' "
            "ORDER BY tid, dim, tag"):
        tid = int(tid)
        out[tid] = (out[tid] + " / " + tag) if tid in out else tag
    return out


def merge_tags(rule: Dict[int, str], anno: Dict[int, str]) -> Dict[int, str]:
    """**人工优先**：人工标签存在就用人工的，否则用规则的。"""
    out = dict(rule)
    for tid, text in (anno or {}).items():
        if text:
            out[tid] = text
    return out


class AnnoService:
    def __init__(self, db: DB, hub: Hub):
        self.db = db
        self.hub = hub

    # ---------------------------------------------------------------- 队列
    def next_code(self) -> str:
        """标注任务编号：L1 / L2…（与协作采集的 A1 / A2 区分开）。"""
        n = self.db.conn.execute("SELECT COUNT(*) FROM anno_queue").fetchone()[0]
        i = n + 1
        while self.db.conn.execute("SELECT 1 FROM anno_queue WHERE task_code=?",
                                   (f"L{i}",)).fetchone():
            i += 1
        return f"L{i}"

    def enqueue(self, tids: List[Any], assignee: str = "",
                task_code: str = "") -> Dict[str, Any]:
        """把帖子放进标注队列。

        ★ **只收已采到正文的帖** —— 没正文根本标不了，
          标一条空帖只会得到一个空结论，浪费标注员的时间。
        """
        want = []
        for t in (tids or []):
            try:
                want.append(int(t))
            except (TypeError, ValueError):
                continue
        if not want:
            raise ValueError("没挑到帖子")
        ok = []
        for t in want:
            r = self.db.conn.execute(
                "SELECT detail_state FROM threads WHERE tid=?", (t,)).fetchone()
            if r and (r["detail_state"] or "") == "done":
                ok.append(t)
        if not ok:
            raise ValueError("挑的帖都还没采到正文，标不了")
        code = (task_code or "").strip() or self.next_code()
        now = _now()
        with self.db.lock:
            self.db.conn.executemany(
                "INSERT INTO anno_queue(tid,assignee,state,task_code,added_at) "
                "VALUES(?,?,'queued',?,?) "
                "ON CONFLICT(tid) DO UPDATE SET assignee=excluded.assignee, "
                "state='queued', task_code=excluded.task_code, done_at=''",
                [(t, assignee or "", code, now) for t in ok])
            self.db.conn.commit()
        return {"task_code": code, "queued": len(ok),
                "skipped": len(want) - len(ok)}

    def queue(self, assignee: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        """标注队列：待标的排前面，已标的排后面。"""
        sql = ("SELECT q.tid, q.assignee, q.state, q.task_code, q.added_at, q.done_at, "
               "t.title, t.dept_name, t.detail_state "
               "FROM anno_queue q LEFT JOIN threads t ON t.tid=q.tid")
        args: List[Any] = []
        if assignee:
            sql += " WHERE q.assignee=?"
            args.append(assignee)
        sql += " ORDER BY q.state='done', q.added_at, q.tid LIMIT ?"
        args.append(max(1, int(limit)))
        return [dict(r) for r in self.db.conn.execute(sql, args).fetchall()]

    # ---------------------------------------------------------------- 标注
    def item(self, tid: int) -> Dict[str, Any]:
        """一条待标注的内容：帖子 + 楼层 + 规则标签 + 已有的人工标注。"""
        t = self.db.conn.execute("SELECT * FROM threads WHERE tid=?",
                                 (int(tid),)).fetchone()
        if not t:
            return {}
        d = dict(t)
        d["posts"] = [dict(r) for r in self.db.conn.execute(
            "SELECT pid, floor, author, post_time, content, chars FROM posts "
            "WHERE tid=? ORDER BY pid", (int(tid),))]
        d["rule_tags_by_dim"] = {}
        for dim, tag in self.db.conn.execute(
                "SELECT dim, tag FROM thread_dim_tags WHERE tid=? ORDER BY dim, tag",
                (int(tid),)):
            d["rule_tags_by_dim"].setdefault(str(dim), []).append(str(tag))
        d["rule_tags"] = " / ".join(
            t for tags in d["rule_tags_by_dim"].values() for t in tags)
        d["anno_tags"] = [dict(r) for r in self.db.conn.execute(
            "SELECT dim, tag, annotator, annotated_at, status FROM thread_anno_tags "
            "WHERE tid=? ORDER BY dim, tag", (int(tid),))]
        d["review"] = self.db.get_review(int(tid))
        return d

    def submit(self, node: str, tid: int, verdict: str = "", reason: str = "",
               tags: List[List[str]] = None) -> Dict[str, Any]:
        """提交一条人工标注。

        ★ 三件事一起做（一条标注是一个整体）：
          ① 核验结论写 sample_review（**理由必填**）
          ② 人工三维标签写 thread_anno_tags（status=submitted → **已生效**）
          ③ 队列里这条置 done

        ★ 标签是**整帖替换**：人改标签 = 重标这一帖的标签，
          不是往规则标签上再叠一层（叠着会让「人工值」和「规则值」混成一团）。
        """
        tid = int(tid)
        verdict = (verdict or "").strip()
        reason = (reason or "").strip()
        if verdict and verdict not in DB.VERDICTS:
            raise ValueError("核验结论只能是 %s，收到 %r" % (DB.VERDICTS, verdict))
        if verdict and not reason:
            raise ValueError("有核验结论就必须写理由")
        clean: List[Tuple[str, str]] = []
        for pair in (tags or []):
            try:
                dim = str(pair[0]).strip()
                tag = str(pair[1]).strip()
            except (IndexError, TypeError):
                continue
            if not dim or not tag:
                continue
            if dim not in DIM_CN:
                raise ValueError("维度只能是 %s，收到 %r" % (list(DIM_CN), dim))
            if (dim, tag) not in clean:
                clean.append((dim, tag))
        now = _now()
        with self.db.lock:
            if verdict:
                self.db.conn.execute(
                    "INSERT INTO sample_review(tid,verdict,reason,method,annotator,"
                    "annotated_at,status,updated_at) "
                    "VALUES(?,?,?,'人工',?,?,'submitted',?) "
                    "ON CONFLICT(tid) DO UPDATE SET verdict=excluded.verdict, "
                    "reason=excluded.reason, method='人工', annotator=excluded.annotator, "
                    "annotated_at=excluded.annotated_at, status='submitted', "
                    "updated_at=excluded.updated_at",
                    (tid, verdict, reason, node, now, now))
            for dim in DIM_CN:
                self.db.conn.execute(
                    "DELETE FROM thread_anno_tags WHERE tid=? AND dim=?", (tid, dim))
            self.db.conn.executemany(
                "INSERT OR REPLACE INTO thread_anno_tags(tid,dim,tag,annotator,"
                "annotated_at,status,updated_at) VALUES(?,?,?,?,?,'submitted',?)",
                [(tid, d, g, node, now, now) for d, g in clean])
            self.db.conn.execute(
                "UPDATE anno_queue SET state='done', done_at=? WHERE tid=?", (now, tid))
            self.db.conn.commit()
        return {"ok": True, "tid": tid, "verdict": verdict,
                "tags": [{"dim": d, "tag": g} for d, g in clean]}

    def review(self, tid: int, approved: bool, note: str = "",
               reviewer: str = "") -> Dict[str, Any]:
        """管理员复核一条标注。

        ★ 通过 → status=approved；打回 → status=rejected（**意见必填**），
          人工标签**失效** → 导出回落到规则标签（但记录还在，不删）。
        ★ 复核人/复核日期只有**管理员本人点了**才写 —— 那是他的签署动作。
        """
        tid = int(tid)
        note = (note or "").strip()
        if not approved and not note:
            raise ValueError("打回必须写意见")
        now = _now()
        today = time.strftime("%Y-%m-%d")
        status = "approved" if approved else "rejected"
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE sample_review SET status=?, review_note=?, reviewed_by=?, "
                "reviewed_at=?, updated_at=? WHERE tid=?",
                (status, note, (reviewer or "").strip(), today, now, tid))
            self.db.conn.execute(
                "UPDATE thread_anno_tags SET status=?, reviewer=?, review_note=?, "
                "updated_at=? WHERE tid=?",
                (status, (reviewer or "").strip(), note, now, tid))
            self.db.conn.commit()
        return {"ok": True, "tid": tid, "status": status}

    # ---------------------------------------------------------------- 统计
    def stats(self) -> Dict[str, Any]:
        """进度：队列、标注状态、核验结论三块。"""
        c = self.db.conn
        q = {r["state"]: int(r["n"]) for r in c.execute(
            "SELECT state, COUNT(*) n FROM anno_queue GROUP BY state")}
        a = {r["status"]: int(r["n"]) for r in c.execute(
            "SELECT status, COUNT(DISTINCT tid) n FROM thread_anno_tags GROUP BY status")}
        pending_review = int(c.execute(
            "SELECT COUNT(DISTINCT tid) FROM thread_anno_tags WHERE status='submitted'"
        ).fetchone()[0])
        return {"queue": q, "anno": a, "pending_review": pending_review,
                "review": self.db.review_stats()}

    def pending(self, limit: int = 100) -> List[Dict[str, Any]]:
        """待复核的标注（管理员用）。"""
        return [dict(r) for r in self.db.conn.execute(
            "SELECT a.tid, a.dim, a.tag, a.annotator, a.annotated_at, "
            "t.title, t.dept_name FROM thread_anno_tags a "
            "LEFT JOIN threads t ON t.tid=a.tid "
            "WHERE a.status='submitted' "
            "ORDER BY a.annotated_at DESC, a.tid LIMIT ?",
            (max(1, int(limit)),)).fetchall()]

    # ---------------------------------------------------------------- 领活
    def claim(self, assignee: str = "", limit: int = 20) -> Dict[str, Any]:
        """**领活**：从候选池领一批帖进自己的标注队列。

        ★ 为什么要有这个：标注台是**自己干活的地方**，
          但入队只有「管理员派活」和「手打 TID」两条路 ——
          组员打开标注台看到的是**空队列**，什么也干不了。
          协作采集那边早就有「领活」了（assist.pick），
          标注台照同样做法：**活先到池子里，谁都能领**。

        ★ 候选池 = 「已采到正文 + 没人标过 + 不在任何人队列里」的帖。
          排除「已在队列里」的 → 不会把别人的活抢走（tid 是主键，
          抢了就是把对方的 assignee 改掉）。
          排除「已标注过」的 → 不重复标同一条。

        ★ 领到的活归到一个**自己的批次**（L 编号），这样批次视图里
          能看出「这批是谁的、标到哪了」。
        """
        limit = max(1, min(int(limit or 20), 200))
        # 候选池查询：已采正文 + 不在队列 + 没人工标注
        rows = self.db.conn.execute(
            "SELECT t.tid FROM threads t "
            "LEFT JOIN anno_queue q ON q.tid=t.tid "
            "LEFT JOIN thread_anno_tags a ON a.tid=t.tid "
            "WHERE t.detail_state='done' AND q.tid IS NULL AND a.tid IS NULL "
            "ORDER BY t.pub_ts DESC, t.tid DESC LIMIT ?",
            (limit,)).fetchall()
        tids = [int(r["tid"]) for r in rows]
        if not tids:
            return {"task_code": "", "claimed": 0,
                    "hint": "候选池空了：没有「已采到正文、还没人标过、"
                            "也不在任何人队列里」的帖"}
        r = self.enqueue(tids, assignee)
        r["claimed"] = r["queued"]
        return r

    # ---------------------------------------------------------------- 批量派活
    def enqueue_filter(self, assignee: str = "", task_code: str = "",
                        limit: int = 50, **filters: Any) -> Dict[str, Any]:
        """按**筛选条件**批量入队（替代手打一串 TID）。

        ★ 复用 db.search() 的筛选能力（同一套口径，不另写一套 SQL）：
          管理员在检索页看到「这些帖该标」，直接按同样的条件批量派给某人。

        ★ 只收**已采到正文**的帖 —— 与 enqueue() 同口径，
          没正文的标不了，标空帖只是浪费标注员时间。
        """
        limit = max(1, min(int(limit or 50), 500))
        res = self.db.search(limit=limit, scope="thread", **filters)
        want = [int(it["tid"]) for it in (res.get("items") or [])]
        ok = []
        for t in want:
            r = self.db.conn.execute(
                "SELECT detail_state FROM threads WHERE tid=?", (t,)).fetchone()
            if r and (r["detail_state"] or "") == "done":
                ok.append(t)
        if not ok:
            raise ValueError("这个条件下没有「已采到正文」的帖，标不了")
        code = (task_code or "").strip() or self.next_code()
        now = _now()
        with self.db.lock:
            self.db.conn.executemany(
                "INSERT INTO anno_queue(tid,assignee,state,task_code,added_at) "
                "VALUES(?,?,'queued',?,?) "
                "ON CONFLICT(tid) DO UPDATE SET assignee=excluded.assignee, "
                "state='queued', task_code=excluded.task_code, done_at=''",
                [(t, assignee or "", code, now) for t in ok])
            self.db.conn.commit()
        return {"task_code": code, "queued": len(ok),
                "skipped": len(want) - len(ok),
                "matched": int(res.get("total") or 0)}

    # ---------------------------------------------------------------- 任务批次
    def batches(self, assignee: str = "") -> List[Dict[str, Any]]:
        """标注任务批次视图：按 task_code 分组看进度。

        ★ Label Studio 的做法：一个「项目」下有多个任务，每个任务派给某人，
          任务之间互不干扰。本项目的对应物就是 task_code（L1 / L2…）。

        ★ 为什么按**批次**而不是按帖看：组员做完一批要知道「这批还剩几条」，
          管理员要知道「每个任务派给了谁、完成了多少」——
          只列帖子列表看不出这个。
        """
        sql = ("SELECT task_code, assignee, state, COUNT(*) n "
               "FROM anno_queue")
        args: List[Any] = []
        if assignee:
            sql += " WHERE assignee=?"
            args.append(assignee)
        sql += " GROUP BY task_code, assignee, state"
        rows = self.db.conn.execute(sql, args).fetchall()
        agg: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            code = str(r["task_code"] or "")
            b = agg.setdefault(code, {
                "task_code": code, "total": 0, "done": 0, "queued": 0,
                "assignees": {},
            })
            n = int(r["n"])
            b["total"] += n
            if str(r["state"]) == "done":
                b["done"] += n
            else:
                b["queued"] += n
            who = str(r["assignee"] or "")
            if who:
                b["assignees"][who] = b["assignees"].get(who, 0) + n
        out = list(agg.values())
        for b in out:
            b["assignee"] = "、".join(
                "%s(%d)" % (k, v) for k, v in sorted(b["assignees"].items()))
            b.pop("assignees", None)
            b["pct"] = (int(round(100.0 * b["done"] / b["total"]))
                        if b["total"] else 0)
        out.sort(key=lambda x: x["task_code"])
        return out

    # ---------------------------------------------------------------- 一致性
    def agreement(self) -> Dict[str, Any]:
        """人工标签 vs 规则标签 的吻合情况（**不引入独立标注系统**）。

        ★ 对标对象是**规则标签**（机器算的），不是另一个标注员 ——
          本系统一条帖只存一份有效人工值（P2-4 口径），
          所以「人跟人比」在当前模型下没有数据基础。
          「机器 vs 人」反而更贴本项目的论点：
          规则标签到底准不准？人工改了多少？

        ★ 口径：
          · 只看**有人工标注**的帖（否则分母会被全库稀释，没意义）
          · 每个维度分别比：人工选的标签集合 vs 规则算的标签集合
            → 完全一致 / 部分重合 / 不一致
          · 帖级：三个维度**全都一致**才算「整帖一致」
          · 被打回的人工标签（status='rejected'）**不计入**——
            它已经失效、导出回落到规则值，拿它比就是自己跟自己比
        """
        # 有效人工标签（已生效的：submitted / approved）
        human: Dict[int, Dict[str, set]] = {}
        for tid, dim, tag in self.db.conn.execute(
                "SELECT tid, dim, tag FROM thread_anno_tags WHERE status<>'rejected'"):
            human.setdefault(int(tid), {}).setdefault(str(dim), set()).add(str(tag))
        if not human:
            return {"annotated": 0, "thread_same": 0, "thread_diff": 0,
                    "thread_pct": 0, "dims": {}, "changed_tags": []}
        # 规则标签（只查这些帖，别拉全库两百万条）
        tids = list(human.keys())
        rule: Dict[int, Dict[str, set]] = {}
        CH = 900  # SQLite 参数上限，批查
        for i in range(0, len(tids), CH):
            chunk = tids[i:i + CH]
            qm = ",".join("?" * len(chunk))
            for tid, dim, tag in self.db.conn.execute(
                    "SELECT tid, dim, tag FROM thread_dim_tags WHERE tid IN ("
                    + qm + ")", chunk):
                rule.setdefault(int(tid), {}).setdefault(str(dim), set()).add(str(tag))
        dim_stat: Dict[str, Dict[str, int]] = {}
        thread_same = 0
        thread_diff = 0
        changed: List[Dict[str, Any]] = []
        for tid, hmap in human.items():
            rmap = rule.get(tid, {})
            dims = set(hmap) | set(rmap)
            same_all = True
            for dim in dims:
                h = hmap.get(dim, set())
                r = rmap.get(dim, set())
                st = dim_stat.setdefault(dim, {"same": 0, "partial": 0, "diff": 0})
                if h == r:
                    st["same"] += 1
                elif h & r:
                    st["partial"] += 1
                    same_all = False
                else:
                    st["diff"] += 1
                    same_all = False
                if h and h != r:
                    changed.append({"tid": tid, "dim": dim,
                                    "human": " / ".join(sorted(h)),
                                    "rule": " / ".join(sorted(r))})
            if same_all:
                thread_same += 1
            else:
                thread_diff += 1
        n = len(human)
        return {
            "annotated": n,
            "thread_same": thread_same,
            "thread_diff": thread_diff,
            "thread_pct": int(round(100.0 * thread_same / n)) if n else 0,
            "dims": dim_stat,
            # ★ 只回**前若干条**差异明细：全量可能上万条，前端表格塞不下
            "changed_tags": sorted(changed, key=lambda x: x["tid"])[:200],
            "changed_total": len(changed),
        }

    def vocab(self) -> Dict[str, Any]:
        """候选标签清单：各维度**已经出现过的取值**。

        ★ 不给空白输入框：标签空间一旦自由发挥，「关系：外星人」这种值会
          混进库里，统计就没法看了。想要的取值先由规则产生，或由管理员加词表。
        """
        out: Dict[str, List[Dict[str, Any]]] = {}
        for dim, tag, n in self.db.conn.execute(
                "SELECT dim, tag, COUNT(DISTINCT tid) FROM thread_dim_tags "
                "GROUP BY dim, tag ORDER BY dim, tag"):
            out.setdefault(str(dim), []).append({"tag": str(tag), "n": int(n)})
        return {"dims": DIM_CN, "candidates": out}
