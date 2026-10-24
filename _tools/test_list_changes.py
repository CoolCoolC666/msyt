# -*- coding: utf-8 -*-
"""
test_list_changes.py —— 列表阶段增量与留痕的验收
================================================================================
两个用户提出来的要求，钉在这里：

① **增量采集要处理「受理状况」**
   旧实现只比列表页给的「回复数」→ 「未受理 → 已受理」这个动作
   **回复数不变**，于是完全检测不到。
   而受理状态一变，站方的**受理流程**（详情页字段）就变了 → 详情必须重采。

② **列表采集阶段要留痕**
   列表采集是「upsert 覆盖」→ 旧值一被盖掉就**永远查不到了**。
   新增 `list_changes` 表，把「覆盖前那一瞬间」存下来。

用法：python _tools/test_list_changes.py
"""
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
sys.path.insert(0, PROG)

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


class Row:
    """模拟 ListRow（带属性的对象）—— 字段与 ListRow 对齐。"""

    DEFAULTS = dict(
        tid=0, title="", url="", category="", state="", dept_id="",
        dept_name="", extra_dept_ids=(), extra_dept_names=(), mailbox_id="",
        mailbox_name="", light_code="", light_name="", author="", author_uid="",
        pub_time="", pub_ts=0, replies=0, views=0, has_image=0,
        last_reply_by="", last_reply_time="", last_reply_ts=0,
    )

    def __init__(self, **kw):
        for k, v in self.DEFAULTS.items():
            setattr(self, k, v)
        for k, v in kw.items():
            setattr(self, k, v)


def main() -> int:
    from msyt.db import DB

    tmp = tempfile.mkdtemp(prefix="msyt_lchg_")
    try:
        db = DB(os.path.join(tmp, "t.db"))
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        # ① 一帖已采完、状态「已受理」
        with db.lock:
            db.conn.execute(
                "INSERT INTO threads(tid,title,state,replies,detail_state,posts_n) "
                "VALUES(90000001,'甲帖','已受理',2,'done',3)")
            db.conn.execute(
                "INSERT INTO threads(tid,title,state,replies,detail_state,posts_n) "
                "VALUES(90000002,'乙帖','已回复',5,'done',6)")
            db.conn.commit()

        print("=== ① 受理状态变了 → 算增量（回复数不变也要能检出） ===")
        rows = [
            # 甲帖：受理状态 未受理→已受理？这里用 已受理→已回复，且**回复数不变**
            Row(tid=90000001, title="甲帖", state="已回复", replies=2),
            # 乙帖：回复数变大
            Row(tid=90000002, title="乙帖", state="已回复", replies=9),
            # 丙帖：新帖
            Row(tid=90000003, title="丙帖", state="未受理", replies=0),
            # 丁帖：标题被改
            Row(tid=90000001, title="甲帖（站方改过）", state="已回复", replies=2),
        ]
        # 上面 90000001 出现两次，模拟两行；先分开测
        growth = db.detect_reply_growth([rows[0]])
        check("★ 受理状态变了（回复数没变）→ 判为增量",
              len(growth) == 1 and growth[0]["kind"] == "state",
              str(growth))
        if growth:
            check("增量里带上了新旧状态",
                  growth[0]["old_state"] == "已受理"
                  and growth[0]["new_state"] == "已回复",
                  f"{growth[0].get('old_state')}→{growth[0].get('new_state')}")
        g2 = db.detect_reply_growth([rows[1]])
        check("回复数变大 → 判为增量（replies）",
              len(g2) == 1 and g2[0]["kind"] == "replies", str(g2))
        g3 = db.detect_reply_growth([rows[2]])
        check("★ 新帖（库里没有）→ 不判为增量", not g3, str(g3))
        g4 = db.detect_reply_growth([Row(tid=90000001, title="x", state="", replies=2)])
        check("★ 受理状态变空 → **不**判为增量（可能是渲染差异）",
              not g4, str(g4))
        g5 = db.detect_reply_growth([Row(tid=90000001, title="x",
                                        state="已受理", replies=2)])
        check("受理状态没变 → 不判为增量", not g5, str(g5))

        print("\n=== ② 列表阶段留痕 ===")
        rows2 = [
            Row(tid=90000001, title="甲帖（站方改过）", state="已回复", replies=2),
            Row(tid=90000002, title="乙帖", state="已回复", replies=9),
            Row(tid=90000003, title="丙帖", state="未受理", replies=0),
        ]
        ch = db.record_list_changes("dept:495", rows2)
        check("留痕记到了新帖", len(ch.get("new") or []) == 1, str(ch.get("new")))
        check("留痕记到了受理状态变化",
              len(ch.get("state") or []) == 1, str(ch.get("state")))
        check("留痕记到了回复数变化",
              len(ch.get("replies") or []) == 1, str(ch.get("replies")))
        check("留痕记到了标题修改",
              len(ch.get("title") or []) == 1, str(ch.get("title")))
        items = db.list_changes()
        check("表里确实写进去了", len(items) == 4, f"{len(items)} 条")
        kinds = {x["kind"] for x in items}
        check("四类变化都在",
              kinds == {"new", "state", "replies", "title"}, str(kinds))
        one = [x for x in items if x["kind"] == "state"][0]
        check("留痕带上了源与新旧值",
              one["source"] == "dept:495" and one["old_val"] == "已受理"
              and one["new_val"] == "已回复", str(dict(one)))

        print("\n=== ③ 覆盖之后旧值仍在留痕里（这才有意义） ===")
        # 真实流程是「先留痕 → 再 upsert 覆盖」，这里照做一遍
        db.upsert_threads(rows2)
        row = db.conn.execute(
            "SELECT state,replies,title FROM threads WHERE tid=90000001").fetchone()
        check("库里已被覆盖成新值", row["state"] == "已回复"
              and row["title"] == "甲帖（站方改过）", str(dict(row)))
        old = [x for x in db.list_changes() if x["kind"] == "state"]
        check("★ 但**旧值还能从留痕里查到**", bool(old)
              and old[0]["old_val"] == "已受理", str(old[:1]))

        print("\n=== ④ 重复留痕不会重复计数（幂等） ===")
        n0 = len(db.list_changes())
        db.record_list_changes("dept:495", rows2)
        n1 = len(db.list_changes())
        check("★ 同样的行再采一遍 → **不再产生新留痕**（值没变）",
              n1 == n0, f"{n0} → {n1}")

        print("\n=== ⑤ 列表采集那条路：受理状态变了 → 标 stale 待重采 ===")
        from msyt.listpage import ListCollector
        from msyt.sources import Source

        class FakeClient:
            def log(self, m):
                pass

        # 一帖已采完（详情 done）、状态「已受理」；列表页现在显示「已回复」
        with db.lock:
            db.conn.execute(
                "INSERT OR REPLACE INTO threads(tid,title,state,replies,"
                "detail_state,posts_n) VALUES(90000010,'丙','已受理',3,'done',4)")
            db.conn.commit()
        src = Source(key="dept:495", dim="dept", code="495", name="市公安局",
                     url="https://x/fid/495")
        lc = ListCollector(db, FakeClient(), None)
        lc._upsert_with_growth(
            [Row(tid=90000010, title="丙", state="已回复", replies=3)], src)
        st = db.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=90000010").fetchone()
        check("★ 受理状态变了 → 详情被标成 stale（待重采）",
              st["detail_state"] == "stale", str(st["detail_state"]))
        q = db.conn.execute(
            "SELECT state,last_error FROM detail_queue WHERE tid=90000010").fetchone()
        check("★ 并被丢回队列", bool(q) and q["state"] == "pending",
              str(dict(q)) if q else "(不在队列)")
        check("★ 队列里写明了原因（受理状态变化）",
              bool(q) and "受理状态" in (q["last_error"] or ""),
              str(q["last_error"])[:60] if q else "")
        lg = db.conn.execute(
            "SELECT msg FROM fetch_log WHERE phase='list' AND kind='detail_growth' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        check("★ fetch_log 里也留了痕（列表阶段也有日志）",
              bool(lg) and "受理状态" in (lg["msg"] or ""),
              str(lg["msg"])[:70] if lg else "(没有)")

        print("\n=== ⑥ 详情还没采过的新帖：不标 stale（采了就是最新的） ===")
        with db.lock:
            db.conn.execute(
                "INSERT OR REPLACE INTO threads(tid,title,state,replies,"
                "detail_state,posts_n) VALUES(90000011,'丁','未受理',0,'pending',0)")
            db.conn.commit()
        lc._upsert_with_growth(
            [Row(tid=90000011, title="丁", state="已受理", replies=0)], src)
        st2 = db.conn.execute(
            "SELECT detail_state FROM threads WHERE tid=90000011").fetchone()
        check("★ 新帖不标 stale", st2["detail_state"] == "pending",
              st2["detail_state"])
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    ok = sum(1 for _, c, _ in results if c)
    bad = [r for r in results if not r[1]]
    print(f"共 {len(results)} 项 · 通过 {ok} · 失败 {len(bad)}")
    for nm, _, e in bad:
        print(f"  FAIL: {nm}  {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
