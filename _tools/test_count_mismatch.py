# -*- coding: utf-8 -*-
"""闸：站方删帖 →「少贴」误判的修正（2026-10-28 初版 / 2026-10-29 改口径）

★ 口径演变（别被旧文档误导）
  2026-10-28 初版：实测 <= 实收 → 标 `count_mismatch`（终态、**不计入**「已采详情帖」）。
  2026-10-29 修正（用户拍板）：**正文齐全即已采** → 归为 `done`。
    理由：实测 25 条逐层对比证明这批帖**正文本来就采全了、站方也照样公开**
    （站方详情页锚点数 == ajax 正文楼层数 == 本地实存楼层数，pid 逐个对得上）。
    记成「不计入已采详情帖」= **账面上说它们没采到**，而正文明明在库里。

★ 本闸覆盖五件事
  ① 判据层：`_probe_site_floors` 能不能实测出站方真实楼层数；
            **能不能认出站方「提示信息」页（= 站方限制）并复验**
  ② 状态层：实测 <= 实收 → 归为 `done`（**计入**已采详情帖、不写正文标记行）
            实测 >  实收 → 仍走补采（真漏采不能被误标）
            站方提示页   → 归为 `restricted`
  ③ 标记层：「计数核对」标记能复用实测值（列表计数没变时不再打站方）
  ④ 反向验证：把阈值改坏（`<=` 写成 `<`）→ 同一批断言**必须报红**
  ⑤ 库接口：`mark_detail_done_bulk` 的语义（删旧标记行 / 置 done / 队列终态 /
            留痕 / posts_n 保留）

★ **反向验证闸**（本项目纪律）：
  只跑通不报红的闸等于没闸。

用法：
    python _tools/test_count_mismatch.py          # 全量跑
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt import detail as D  # noqa: E402
from msyt.db import DB  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

#: 站方的提示信息页（parse_site_notice 的判据：含提示语 + 无楼层锚点 + title 含「提示信息」）
NOTICE_HTML = (
    "<html><head><title>提示信息 - 民生e点通群众留言板</title></head>"
    "<body><div class=\"alert_error\"><span>"
    "历史帖子仅限作者本人查看"
    "</span></div></body></html>"
)


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name + (f" — {extra}" if extra else ""))
        print(f"  [FAIL] {name} {extra}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 桩
class FakeClient:
    """假 client：按 tid 返回预设的楼层数 / 提示页。

    · `notice_tids`：永远返回「提示信息」页（站方限制）
    · `flaky_tids`：第一次返回提示页、第二次返回正常（模拟站方间歇性提示页）
    """

    def __init__(self, floors_by_tid: dict, page_total: int = 1,
                 notice_tids=(), flaky_tids=()):
        self.floors = floors_by_tid
        self.page_total = page_total
        self.notice_tids = set(notice_tids)
        self.flaky_tids = set(flaky_tids)
        self.calls = 0
        self.hits: dict[int, int] = {}

    def _tid(self, url: str) -> int:
        return int(url.split("tid=")[1].split("&")[0])

    def get(self, url, referer=None, **kw):
        self.calls += 1
        tid = self._tid(url)
        self.hits[tid] = self.hits.get(tid, 0) + 1
        if tid in self.notice_tids:
            return type("R", (), {"text": NOTICE_HTML, "status_code": 200})()
        if tid in self.flaky_tids and self.hits[tid] == 1:
            return type("R", (), {"text": NOTICE_HTML, "status_code": 200})()
        n = self.floors.get(tid, 0)
        pids = "".join(f'<div id="post_{1000 + i}"></div>' for i in range(n))
        # ★ token 必须匹配站方的真实格式（parsers._TOKEN_RE: token:'<16+ 位 hex>'）
        tok = f"{tid:016x}"
        html = (f"<html><title>测试帖</title>{pids}"
                f"<script>var token:'{tok}';</script></html>")
        return type("R", (), {"text": html, "status_code": 200})()

    def get_nowait(self, url, params=None, referer=None, **kw):
        self.calls += 1
        tid = int(params["tid"])
        n = self.floors.get(tid, 0)
        posts = {str(1000 + i): f"正文{i}" for i in range(n)}
        payload = {"code": 1, "message": [
            {"pid": int(k), "number": i + 1, "message": v}
            for i, (k, v) in enumerate(posts.items())]}
        return type("R", (), {"json": lambda self=payload: payload})()


def make_db(tid: int, replies: int, floors: list[int], state: str = "done") -> DB:
    """造一个临时库：threads 有 replies（列表页计数），posts 有若干层。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = DB(path)
    db.conn.execute(
        "INSERT INTO threads (tid, title, replies, detail_state, posts_n, chars) "
        "VALUES (?,?,?,?,?,?)",
        (tid, "测试帖", replies, state, len(floors), sum(len("正文") for _ in floors)))
    for i, fl in enumerate(floors):
        db.conn.execute(
            "INSERT INTO posts (pid,tid,page_no,floor,author,content,chars) "
            "VALUES (?,?,1,?,?,?,?)",
            (1000 + i, tid, fl, "u", "正文", 2))
    db.conn.commit()
    return db, path


# ---------------------------------------------------------------- ① 判据层
def test_probe():
    section("① 判据层：_probe_site_floors 实测站方真实楼层数 + 认站方限制")
    db, path = make_db(9001, replies=3, floors=[1, 2, 3])
    try:
        col = D.DetailCollector(db, FakeClient({9001: 3}), None)
        r = col._probe_site_floors(9001)
        check("实测成功", r["ok"] is True, str(r))
        check("实测楼层数 == 站方给的 3", r["count"] == 3, str(r))

        col2 = D.DetailCollector(db, FakeClient({9001: 5}), None)
        r2 = col2._probe_site_floors(9001)
        check("实测楼层数 == 站方给的 5", r2["count"] == 5, str(r2))

        class BrokenClient(FakeClient):
            def get(self, url, referer=None, **kw):
                raise RuntimeError("站方不可达")

        col3 = D.DetailCollector(db, BrokenClient({}), None)
        r3 = col3._probe_site_floors(9001)
        check("站方不可达 → ok=False（不能下结论）", r3["ok"] is False, str(r3))
        check("站方不可达 → 有失败原因", bool(r3.get("reason")), str(r3))

        # ★ 站方限制：详情页只给「提示信息」页
        col4 = D.DetailCollector(db, FakeClient({9001: 0}, notice_tids=[9001]), None)
        r4 = col4._probe_site_floors(9001)
        check("站方提示页 → restricted=True", r4.get("restricted") is True, str(r4))
        check("站方提示页 → ok=False（不能当成「实测到 0 楼」）",
              r4["ok"] is False, str(r4))
        check("站方提示页 → 带回提示原文", bool(r4.get("notice")), str(r4))

        # ★ 复验：站方会**间歇性**返回提示页 —— 复验通过就**不能**判限制
        col5 = D.DetailCollector(db, FakeClient({9001: 3}, flaky_tids=[9001]), None)
        r5 = col5._probe_site_floors(9001)
        check("提示页但复验正常 → restricted=False（瞬时异常不算限制）",
              r5.get("restricted") is False, str(r5))
        check("提示页但复验正常 → 能拿到楼层数", r5.get("count") == 3, str(r5))
    finally:
        db.conn.close()
        os.unlink(path)


# ---------------------------------------------------------------- ② 状态层
def test_state_machine():
    section("② 状态层：实测 <= 实收 → done（正文齐全即已采）；实测 > 实收 → 仍补采")
    # 情形 A：站方实测 3 == 实收 3，但列表称 4 → 站方计数虚高 → 正文齐全 → done
    db, path = make_db(9002, replies=3, floors=[1, 2, 3])
    try:
        col = D.DetailCollector(db, FakeClient({9002: 3}), None)
        col.integrity(sink=None)
        row = db.conn.execute(
            "SELECT detail_state, detail_warn, posts_n FROM threads "
            "WHERE tid=9002").fetchone()
        check("A: 状态归为 done（不是「不计入已采」的终态）",
              row["detail_state"] == "done", str(dict(row)))
        check("A: 告警写明差额来源（列表计数未随删楼而减）",
              "未随删楼而减" in (row["detail_warn"] or ""), str(dict(row)))
        check("A: 告警写明站方实测楼层数",
              "实测 3 楼" in (row["detail_warn"] or ""), str(dict(row)))
        check("A: 告警含「计数核对」标记（供后续复用实测值）",
              D.COUNT_VER_MARK in (row["detail_warn"] or ""), str(dict(row)))
        check("A: 标记能被 COUNT_VER_RE 解析出实测值与列表值",
              D.COUNT_VER_RE.search(row["detail_warn"] or "") is not None,
              str(row["detail_warn"]))
        check("A: posts_n 保留（正文确实采到了）",
              row["posts_n"] == 3, str(dict(row)))
        q = db.conn.execute(
            "SELECT state FROM detail_queue WHERE tid=9002").fetchone()
        check("A: 队列行已置终态（不再是 pending）",
              q is None or q["state"] != "pending", str(q))
        n_mark = db.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=9002 AND pid<0").fetchone()[0]
        check("A: 不写正文标记行（正文是实的，不需要说明行）",
              n_mark == 0, str(n_mark))
        check("A: ★ 计入已采详情帖（正文齐全就是已采）",
              db.stats()["detail_done"] >= 1, str(db.stats()["detail_done"]))
    finally:
        db.conn.close()
        os.unlink(path)

    # 情形 B：站方实测 5 > 实收 3 → 真漏采 → 必须仍然补采
    db2, path2 = make_db(9003, replies=4, floors=[1, 2, 3])
    try:
        col2 = D.DetailCollector(db2, FakeClient({9003: 5}), None)
        col2.integrity(sink=None)
        row2 = db2.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=9003").fetchone()
        check("B: 真漏采不得被误标为已采",
              row2["detail_state"] != "done", str(dict(row2)))
        check("B: 真漏采仍被标记为待补采（stale）",
              row2["detail_state"] == "stale", str(dict(row2)))
        q2 = db2.conn.execute(
            "SELECT state FROM detail_queue WHERE tid=9003").fetchone()
        check("B: 队列里是 pending（等补采）",
              q2 is not None and q2["state"] == "pending", str(q2))
        w = db2.conn.execute(
            "SELECT detail_warn FROM threads WHERE tid=9003").fetchone()["detail_warn"]
        check("B: 告警含独立的补采次数标记",
              "补采次数：" in (w or ""), str(w))
    finally:
        db2.conn.close()
        os.unlink(path2)

    # 情形 C：站方限制（详情页只给提示信息页）→ restricted
    db3, path3 = make_db(9006, replies=3, floors=[1, 2, 3])
    try:
        col3 = D.DetailCollector(db3, FakeClient({9006: 0}, notice_tids=[9006]), None)
        col3.integrity(sink=None)
        row3 = db3.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=9006").fetchone()
        check("C: 站方限制 → 标 restricted",
              row3["detail_state"] == "restricted", str(dict(row3)))
        check("C: 站方限制 → 不计入已采详情帖",
              db3.stats()["detail_done"] == 0, str(db3.stats()["detail_done"]))
        q3 = db3.conn.execute(
            "SELECT state FROM detail_queue WHERE tid=9006").fetchone()
        check("C: 站方限制 → 队列置终态（不再被捡起来重采）",
              q3 is None or q3["state"] != "pending", str(q3))
    finally:
        db3.conn.close()
        os.unlink(path3)


# ---------------------------------------------------------------- ③ 标记层
def test_count_ver_mark():
    section("③ 标记层：「计数核对」可复用实测值；列表计数变了必须重新实测")
    # A：列表计数没变 → 复用上次的实测值 → 不再打站方
    db, path = make_db(9004, replies=3, floors=[1, 2, 3])
    try:
        cli = FakeClient({9004: 3})
        col = D.DetailCollector(db, cli, None)
        col.integrity(sink=None)
        n1 = cli.calls
        w = db.conn.execute(
            "SELECT detail_warn FROM threads WHERE tid=9004").fetchone()["detail_warn"]
        check("A: 归位后带「计数核对」标记", D.COUNT_VER_MARK in (w or ""), str(w))
        # 第二轮：列表计数没变 → 不该再实测（不打站方）
        col.integrity(sink=None)
        check("A: ★ 列表计数没变 → 复用实测值，不再打站方",
              cli.calls == n1, f"第一轮 {n1} 次，第二轮后 {cli.calls} 次")
        w2 = db.conn.execute(
            "SELECT detail_warn FROM threads WHERE tid=9004").fetchone()["detail_warn"]
        check("A: 复用时状态仍是 done", db.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=9004").fetchone()
            ["detail_state"] == "done", w2)
    finally:
        db.conn.close()
        os.unlink(path)

    # B：列表计数变了（站方可能来了新回复）→ 必须重新实测
    db2, path2 = make_db(9005, replies=3, floors=[1, 2, 3])
    try:
        # 第一轮：站方给的楼层 == 实收 → 归位为 done，并写下「计数核对」标记
        cli2 = FakeClient({9005: 3})
        col2 = D.DetailCollector(db2, cli2, None)
        col2.integrity(sink=None)
        n1 = cli2.calls
        st0 = db2.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=9005").fetchone()["detail_state"]
        check("B: 第一轮正文齐全 → 归位 done", st0 == "done", str(st0))
        # 站方来了新回复 → 列表计数 3 → 4，且站方详情页确实多了楼
        db2.conn.execute("UPDATE threads SET replies=4 WHERE tid=9005")
        db2.conn.commit()
        cli2.floors[9005] = 5
        col2.integrity(sink=None)
        check("B: ★ 列表计数变了 → 必须重新实测（不能沿用旧实测值）",
              cli2.calls > n1, f"第一轮 {n1} 次，第二轮后 {cli2.calls} 次")
        st = db2.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=9005").fetchone()["detail_state"]
        check("B: 重新实测后若站方确实有更多楼 → 判真漏采（stale）",
              st == "stale", str(st))
    finally:
        db2.conn.close()
        os.unlink(path2)


# ---------------------------------------------------------------- ④ 反向验证
def test_reverse():
    section("④ 反向验证闸：把阈值改坏，同一批断言必须报红")
    db, path = make_db(9007, replies=3, floors=[1, 2, 3])
    try:
        # 反向验证 1：让实测**永远失败**（模拟「没有实测能力」的旧行为）
        orig = D.DetailCollector._probe_site_floors
        try:
            D.DetailCollector._probe_site_floors = (
                lambda self, tid, max_pages=12: {
                    "ok": False, "count": 0, "pages": 0, "reason": "被反向验证改坏",
                    "restricted": False, "notice": ""})
            col = D.DetailCollector(db, FakeClient({9007: 3}), None)
            col.integrity(sink=None)
            row = db.conn.execute(
                "SELECT detail_state FROM threads WHERE tid=9007").fetchone()
            check("反向 1：实测失效时**不能**判成已采",
                  row["detail_state"] != "done", str(dict(row)))
            check("反向 1：实测失效时退回旧行为（标 stale 补采）",
                  row["detail_state"] == "stale", str(dict(row)))
        finally:
            D.DetailCollector._probe_site_floors = orig

        # 反向验证 2：把「实测 <= 实收 → 归位」这条分支的阈值改坏
        #   （改成「实测 < 实收」→ 严格小于 → 站方实测 3 == 实收 3 不再进分支）
        #   → 期望：同一批断言**必须报红**。
        src_path = os.path.join(PROJ, "程序", "msyt", "detail.py")
        src = open(src_path, encoding="utf-8").read()
        broken = src.replace('_probe["count"] <= uniq', '_probe["count"] < uniq')
        check("反向 2：能构造出「阈值改坏」的源码变体",
              broken != src and '_probe["count"] < uniq' in broken)

        broken_mod = types.ModuleType("detail_broken")
        broken_mod.__file__ = src_path
        broken_mod.__package__ = "msyt"
        broken_mod.__dict__["__name__"] = "msyt.detail_broken"
        exec(compile(broken, src_path, "exec"), broken_mod.__dict__)
        real_integrity = D.DetailCollector.integrity
        try:
            D.DetailCollector.integrity = broken_mod.DetailCollector.integrity
            db.conn.execute("UPDATE threads SET detail_state='done', "
                            "detail_warn='' WHERE tid=9007")
            db.conn.commit()
            col2 = D.DetailCollector(db, FakeClient({9007: 3}), None)
            col2.integrity(sink=None)
            row2 = db.conn.execute(
                "SELECT detail_state FROM threads WHERE tid=9007").fetchone()
            print(f"     → 阈值改坏后 detail_state = {row2['detail_state']}")
            check("反向 2：阈值改坏后**「归位为 done」的断言必须报红**",
                  row2["detail_state"] != "done",
                  f"改坏后仍判成 {row2['detail_state']} → 闸没咬到点上")
        finally:
            D.DetailCollector.integrity = real_integrity

        # 反向验证 3：提示页若复验通过，**不得**判成 restricted
        db3, path3 = make_db(9008, replies=3, floors=[1, 2, 3])
        try:
            col3 = D.DetailCollector(db3, FakeClient({9008: 3}, flaky_tids=[9008]), None)
            col3.integrity(sink=None)
            st3 = db3.conn.execute(
                "SELECT detail_state FROM threads WHERE tid=9008").fetchone()["detail_state"]
            check("反向 3：提示页复验通过 → 不得判 restricted",
                  st3 != "restricted", str(st3))
        finally:
            db3.conn.close()
            os.unlink(path3)

        # 静态检查：新旧口径不得混用
        #   ★ 只查**代码**（引号里的状态值 / 方法调用），注释里提到历史名词是留痕，允许
        check("源码里已不再调用 mark_count_mismatch",
              "mark_count_mismatch" not in src, "源码里仍有 mark_count_mismatch 调用")
        check("源码里已不再把 count_mismatch 当状态值",
              "'count_mismatch'" not in src and '"count_mismatch"' not in src,
              "源码里仍有 'count_mismatch' 状态值")
        check("源码里补采次数用独立标记（不再用「已补采 N 次」）",
              r'r"补采次数[:：]\s*(\d+)"' in src and
              r'r"已补采\s*(\d+)\s*次"' not in src,
              "仍有旧的「已补采 N 次」正则")
        check("源码里有「计数核对」标记与正则",
              "COUNT_VER_MARK" in src and "COUNT_VER_RE" in src)
    finally:
        db.conn.close()
        os.unlink(path)


# ---------------------------------------------------------------- ⑤ 库内接口
def test_db_api():
    section("⑤ 库内接口：mark_detail_done_bulk 的语义")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = DB(path)
    try:
        db.conn.execute(
            "INSERT INTO threads (tid,title,replies,detail_state,posts_n,chars) "
            "VALUES (1,'t',3,'done',7,100)")
        db.conn.execute(
            "INSERT INTO detail_queue (tid,state) VALUES (1,'pending')")
        # 模拟旧状态留下的正文标记行（pid 为负）
        db.conn.execute(
            "INSERT INTO posts (pid,tid,page_no,floor,author,content,chars) "
            "VALUES (-1,1,0,0,'站方计数','旧标记行',10)")
        db.conn.commit()

        n = db.mark_detail_done_bulk([(1, "站方列表称 4 楼，实测 3 楼；计数核对：已按详情页实测（实测 3 楼，列表称 4 楼）")])
        check("mark_detail_done_bulk 返回条数正确", n == 1, str(n))
        t = db.conn.execute(
            "SELECT detail_state, detail_warn, posts_n FROM threads WHERE tid=1").fetchone()
        q = db.conn.execute(
            "SELECT state FROM detail_queue WHERE tid=1").fetchone()
        check("mark_detail_done_bulk → detail_state = done",
              t["detail_state"] == "done", str(dict(t)))
        check("mark_detail_done_bulk → 队列置终态",
              q["state"] == "done", str(dict(q)))
        check("mark_detail_done_bulk → 告警保留差额说明",
              "站方列表称 4 楼" in (t["detail_warn"] or ""), str(dict(t)))
        check("mark_detail_done_bulk → 保留「计数核对」标记",
              D.COUNT_VER_MARK in (t["detail_warn"] or ""), str(dict(t)))
        n_neg = db.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=1 AND pid<0").fetchone()[0]
        check("mark_detail_done_bulk → 删掉旧的正文标记行", n_neg == 0, str(n_neg))
        check("mark_detail_done_bulk → posts_n 保留（正文确实采到了）",
              t["posts_n"] == 7, str(dict(t)))

        # bulk
        for i in (2, 3, 4):
            db.conn.execute(
                "INSERT INTO threads (tid,title,replies,detail_state) "
                "VALUES (?,?,3,'done')", (i, "t"))
            db.conn.execute(
                "INSERT INTO detail_queue (tid,state) VALUES (?,'pending')", (i,))
        db.conn.commit()
        n2 = db.mark_detail_done_bulk([(2, "x"), (3, "y"), (4, "z")])
        check("bulk 返回条数正确", n2 == 3, str(n2))
        states = [r["detail_state"] for r in db.conn.execute(
            "SELECT detail_state FROM threads WHERE tid IN (2,3,4)")]
        check("bulk 全部归为 done",
              all(s == "done" for s in states), str(states))
        qs = [r["state"] for r in db.conn.execute(
            "SELECT state FROM detail_queue WHERE tid IN (2,3,4)")]
        check("bulk 队列全部置终态",
              all(s == "done" for s in qs), str(qs))

        # ★ 状态枚举已下线：体检只应剩四类
        check("INTEGRITY_KINDS 只剩四类",
              tuple(DB.INTEGRITY_KINDS) == ("problems", "growth", "warnings",
                                            "restricted"),
              str(DB.INTEGRITY_KINDS))
        check("stats 不再有 detail_count_mismatch",
              "detail_count_mismatch" not in db.stats(), str(db.stats().keys()))
    finally:
        db.conn.close()
        os.unlink(path)


def main() -> int:
    print("闸：站方删帖 →「少贴」误判修正（2026-10-29 口径：正文齐全即已采）")
    test_probe()
    test_state_machine()
    test_count_ver_mark()
    test_reverse()
    test_db_api()
    print(f"\n{'=' * 60}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  [FAIL] {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
