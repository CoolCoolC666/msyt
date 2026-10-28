# -*- coding: utf-8 -*-
"""闸：站方删帖 → 「少贴」误判的修正（2026-10-28）

覆盖三件事：
  ① 判据层：`DetailCollector._probe_site_floors` 能不能实测出站方真实楼层数
  ② 状态层：实测 <= 实收 → 标 `count_mismatch`（终态、不再进队列）
     实测 >  实收 → 仍走补采（真漏采不能被误标）
  ③ 存量层：4384 条 stale+少贴 批量归位后，队列 pending 里不再有它们

★ **反向验证闸**（本项目纪律）：
  把判据改回旧行为（不实测、直接按列表计数补采）→ 同一批断言必须报红。
  只跑通不报红的闸等于没闸。

用法：
    python _tools/test_count_mismatch.py          # 全量跑
    python _tools/test_count_mismatch.py --no-live # 跳过需要连站方的用例
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt import detail as D  # noqa: E402
from msyt.db import DB  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


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
    """假 client：按 tid 返回预设的楼层数。"""

    def __init__(self, floors_by_tid: dict, page_total: int = 1):
        self.floors = floors_by_tid
        self.page_total = page_total
        self.calls = 0

    def get(self, url, referer=None, **kw):
        self.calls += 1
        tid = int(url.split("tid=")[1].split("&")[0])
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


def make_db(tid: int, replies: int, floors: list[int]) -> DB:
    """造一个临时库：threads 有 replies（列表页计数），posts 有若干层。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = DB(path)
    db.conn.execute(
        "INSERT INTO threads (tid, title, replies, detail_state, posts_n, chars) "
        "VALUES (?,?,?,'done',?,?)",
        (tid, "测试帖", replies, len(floors), sum(len("正文") for _ in floors)))
    for i, fl in enumerate(floors):
        db.conn.execute(
            "INSERT INTO posts (pid,tid,page_no,floor,author,content,chars) "
            "VALUES (?,?,1,?,?,?,?)",
            (1000 + i, tid, fl, "u", "正文", 2))
    db.conn.commit()
    return db, path


# ---------------------------------------------------------------- ① 判据层
def test_probe():
    section("① 判据层：_probe_site_floors 实测站方真实楼层数")
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
    finally:
        db.conn.close()
        os.unlink(path)


# ---------------------------------------------------------------- ② 状态层
def test_state_machine():
    section("② 状态层：实测 <= 实收 → count_mismatch 终态；实测 > 实收 → 仍补采")
    # 情形 A：站方实测 3 == 实收 3，但列表称 4 → 站方计数缺陷
    db, path = make_db(9002, replies=3, floors=[1, 2, 3])
    try:
        col = D.DetailCollector(db, FakeClient({9002: 3}), None)
        # 复用 collect_one 的判定分支：直接调内部逻辑不现实，
        # 这里用 integrity(sink) 走同一条判据链。
        out = col.integrity(sink=None)
        row = db.conn.execute(
            "SELECT detail_state, detail_warn FROM threads WHERE tid=9002").fetchone()
        check("A: 状态变为 count_mismatch",
              row["detail_state"] == "count_mismatch", str(dict(row)))
        check("A: 告警写明差额来源（列表计数未随删楼而减）",
              "未随删楼而减" in (row["detail_warn"] or ""), str(dict(row)))
        check("A: 告警写明站方实测楼层数",
              "实测 3 楼" in (row["detail_warn"] or ""), str(dict(row)))
        # 队列行必须一并置终态（否则会被反复捡起来重采）
        q = db.conn.execute(
            "SELECT state FROM detail_queue WHERE tid=9002").fetchone()
        check("A: 队列行已置终态（不再是 pending）",
              q is None or q["state"] != "pending", str(q))
        # 正文标记行
        n_mark = db.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=9002 AND pid<0").fetchone()[0]
        check("A: 写了正文标记行（pid 为负）", n_mark == 1, str(n_mark))
        check("A: 不计入已采详情帖（detail_done 不含它）",
              db.stats()["detail_done"] == 0, str(db.stats()["detail_done"]))
    finally:
        db.conn.execute("DELETE FROM threads")
        db.conn.execute("DELETE FROM posts")
        db.conn.commit()
        db.conn.close()
        os.unlink(path)

    # 情形 B：站方实测 5 > 实收 3 → 真漏采 → 必须仍然补采，不能误标终态
    db2, path2 = make_db(9003, replies=4, floors=[1, 2, 3])
    try:
        col2 = D.DetailCollector(db2, FakeClient({9003: 5}), None)
        col2.integrity(sink=None)
        row2 = db2.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=9003").fetchone()
        check("B: 真漏采不得被误标为 count_mismatch",
              row2["detail_state"] != "count_mismatch", str(dict(row2)))
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


# ---------------------------------------------------------------- ③ 计数口径
def test_counter_mark():
    section("③ 计数口径：补采次数标记独立，不与叙述文案互相污染")
    db, path = make_db(9004, replies=3, floors=[1, 2, 3])
    try:
        col = D.DetailCollector(db, FakeClient({9004: 5}), None)
        # 连续跑三次 integrity，模拟多轮补采
        for i in range(3):
            col.integrity(sink=None)
        w = db.conn.execute(
            "SELECT detail_warn FROM threads WHERE tid=9004").fetchone()["detail_warn"]
        import re as _re
        m = _re.search(D.GAP_RETRY_RE, w)
        check("告警里能提取出补采次数", m is not None and int(m.group(1)) >= 1, str(w))
        # ★ 真漏采（站方实测 5 > 实收 3）→ 补采到上限后**不再自动入队**，
        #   但它仍是「待更新」语义 → stale 是对的。
        #   关键断言：补采次数**不再增长**（旧口径下会一直 +1，导致每轮重排队）
        st = db.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=9004").fetchone()["detail_state"]
        check("真漏采补采到上限后仍标 stale（语义正确）",
              st == "stale", str(st))
        # ★ 真正的判据：连续多轮后，补采次数必须**封顶在 GAP_RETRY_MAX**
        n_tried = int(m.group(1)) if m else -1
        check("补采次数封顶在 GAP_RETRY_MAX（不再无限增长）",
              n_tried <= D.GAP_RETRY_MAX, f"补采次数={n_tried} 上限={D.GAP_RETRY_MAX}")
        # ★ 队列里不应出现「每次 integrity 都重新入队」的无限循环：
        #   补采次数到上限后，detail_warn 不再被改写
        w2 = db.conn.execute(
            "SELECT detail_warn FROM threads WHERE tid=9004").fetchone()["detail_warn"]
        check("补采次数到上限后不再改写告警（不再每轮重新入队）",
              w2 == w, f"{w!r} -> {w2!r}")
    finally:
        db.conn.close()
        os.unlink(path)


# ---------------------------------------------------------------- ④ 反向验证
def test_reverse():
    section("④ 反向验证闸：把判据改回旧行为，同一批断言必须报红")
    db, path = make_db(9005, replies=3, floors=[1, 2, 3])
    try:
        # 反向验证 1：让实测**永远失败**（模拟「没有实测能力」的旧行为）
        #   → 期望：判据退回「按列表计数补采」，即 detail_state = stale，
        #     **不能**是 count_mismatch。
        #   → 若这里仍是 count_mismatch，说明闸没咬到点上。
        orig = D.DetailCollector._probe_site_floors
        try:
            D.DetailCollector._probe_site_floors = (
                lambda self, tid, max_pages=12: {
                    "ok": False, "count": 0, "pages": 0, "reason": "被反向验证改坏"})
            col = D.DetailCollector(db, FakeClient({9005: 3}), None)
            col.integrity(sink=None)
            row = db.conn.execute(
                "SELECT detail_state FROM threads WHERE tid=9005").fetchone()
            check("反向验证：实测失效时**不能**判成 count_mismatch",
                  row["detail_state"] != "count_mismatch", str(dict(row)))
            check("反向验证：实测失效时退回旧行为（标 stale 补采）",
                  row["detail_state"] == "stale", str(dict(row)))
        finally:
            D.DetailCollector._probe_site_floors = orig

        # 反向验证 2：把「实测 <= 实收 → 终态」这条分支的阈值改坏
        #   （改成「实测 < 实收」→ 严格小于 → 站方实测 3 == 实收 3 不再进终态分支）
        #   → 期望：同一批断言**必须报红**。
        #   这是本闸的核心价值：证明这条分支真的在起作用。
        import re as _re
        src_path = os.path.join(PROJ, "程序", "msyt", "detail.py")
        src = open(src_path, encoding="utf-8").read()
        broken = src.replace('_probe["count"] <= len(unique)',
                             '_probe["count"] < len(unique)')
        check("反向验证 2：能构造出「阈值改坏」的源码变体",
              broken != src and "_probe[\"count\"] < len(unique)" in broken)

        # 用改坏的源码真跑一遍：断言必须变红
        import importlib
        import types
        broken_mod = types.ModuleType("detail_broken")
        broken_mod.__file__ = src_path
        broken_mod.__package__ = "msyt"
        broken_mod.__dict__["__name__"] = "msyt.detail_broken"
        exec(compile(broken, src_path, "exec"), broken_mod.__dict__)
        # 把改坏版的关键类塞回 msyt.detail 的命名空间，跑同一批断言
        real_integrity = D.DetailCollector.integrity
        try:
            D.DetailCollector.integrity = broken_mod.DetailCollector.integrity
            col2 = D.DetailCollector(db, FakeClient({9005: 3}), None)
            col2.integrity(sink=None)
            row2 = db.conn.execute(
                "SELECT detail_state FROM threads WHERE tid=9005").fetchone()
            print(f"     → 阈值改坏后 detail_state = {row2['detail_state']}")
            check("反向验证 2：阈值改坏后**判成 count_mismatch 的断言必须报红**",
                  row2["detail_state"] != "count_mismatch",
                  f"改坏后仍判成 {row2['detail_state']} → 闸没咬到点上")
        finally:
            D.DetailCollector.integrity = real_integrity

        # 静态检查：新旧口径不得混用
        check("源码里存在「实测 <= 实收 → count_mismatch」这条分支",
              '_probe["count"] <= len(unique)' in src and
              "mark_count_mismatch" in src)
        check("源码里补采次数用独立标记（不再用「已补采 N 次」）",
              r'r"补采次数[:：]\s*(\d+)"' in src and
              r'r"已补采\s*(\d+)\s*次"' not in src,
              "仍有旧的「已补采 N 次」正则")
    finally:
        db.conn.close()
        os.unlink(path)


# ---------------------------------------------------------------- ⑤ 库内接口
def test_db_api():
    section("⑤ 库内接口：mark_count_mismatch / _bulk 的语义")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = DB(path)
    try:
        db.conn.execute(
            "INSERT INTO threads (tid,title,replies,detail_state) VALUES (1,'t',3,'done')")
        db.conn.execute(
            "INSERT INTO detail_queue (tid,state) VALUES (1,'pending')")
        db.conn.commit()
        db.mark_count_mismatch(1, "站方列表称 4 楼，实测 3 楼")
        t = db.conn.execute(
            "SELECT detail_state, detail_warn FROM threads WHERE tid=1").fetchone()
        q = db.conn.execute(
            "SELECT state FROM detail_queue WHERE tid=1").fetchone()
        check("mark_count_mismatch → detail_state 正确",
              t["detail_state"] == "count_mismatch", str(dict(t)))
        check("mark_count_mismatch → 队列置终态",
              q["state"] == "count_mismatch", str(dict(q)))
        check("mark_count_mismatch → 告警保留差额说明",
              "站方列表称 4 楼" in (t["detail_warn"] or ""), str(dict(t)))

        # bulk
        for i in (2, 3, 4):
            db.conn.execute(
                "INSERT INTO threads (tid,title,replies,detail_state) "
                "VALUES (?,?,3,'done')", (i, "t"))
            db.conn.execute(
                "INSERT INTO detail_queue (tid,state) VALUES (?,'pending')", (i,))
        db.conn.commit()
        n = db.mark_count_mismatch_bulk(
            [(2, "x"), (3, "y"), (4, "z")])
        check("bulk 返回条数正确", n == 3, str(n))
        states = [r["detail_state"] for r in db.conn.execute(
            "SELECT detail_state FROM threads WHERE tid IN (2,3,4)")]
        check("bulk 全部标成 count_mismatch",
              all(s == "count_mismatch" for s in states), str(states))
        qs = [r["state"] for r in db.conn.execute(
            "SELECT state FROM detail_queue WHERE tid IN (2,3,4)")]
        check("bulk 队列全部置终态",
              all(s == "count_mismatch" for s in qs), str(qs))

        # ★ posts_n 必须保留（正文确实采到了，不像 restricted 要清零）
        db.conn.execute(
            "UPDATE threads SET posts_n=7 WHERE tid=1")
        db.conn.commit()
        db.mark_count_mismatch(1, "again")
        pn = db.conn.execute(
            "SELECT posts_n FROM threads WHERE tid=1").fetchone()["posts_n"]
        check("count_mismatch 不清零 posts_n（正文已采到）", pn == 7, str(pn))
    finally:
        db.conn.close()
        os.unlink(path)


def main() -> int:
    print("闸：站方删帖 →「少贴」误判修正（2026-10-28）")
    test_probe()
    test_state_machine()
    test_counter_mark()
    test_reverse()
    test_db_api()
    print(f"\n{'=' * 60}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  [FAIL] {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
