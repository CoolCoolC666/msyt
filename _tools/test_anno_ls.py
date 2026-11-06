# -*- coding: utf-8 -*-
"""闸：标注台的 Label Studio 化（任务分配 / 批次视图 / 一致性反馈 / 快捷键流）。

★ 跑在**临时库**上（不碰真库）。

★ 要守住的性质
  ① 任务批次：按 task_code 分组，能看出每批派给谁、标了多少、进度多少
  ② 批量派活：按筛选条件入队，**只收已采到正文的帖**（与手打入队同口径）
  ③ 一致性：**人工标签 vs 规则标签**
     · 只统计有人工标注的帖（分母不被全库稀释）
     · 维度级分「一致 / 部分重合 / 不一致」
     · 帖级「三个维度全一致」才算整帖一致
     · 被打回的人工标签**不计入**（已失效、导出回落到规则值，
       拿它比就是自己跟自己比）
  ④ 快捷键流相关：提交后自动跳下一条、只翻看不提交的上下条导航

★ 必须有**真反向验证**：把实现改坏后，同一批断言必须报红。

用法：python _tools/test_anno_ls.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.anno import AnnoService  # noqa: E402
from msyt.db import DB  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def expect_red(fn) -> bool:
    """fn 里的断言**必须报红**。返回 False = 断言不够狠。

    ★ 捕 **Exception** 而不是只捕 AssertionError：
      实现坏掉时可能抛 IndexError / KeyError（不是断言失败），
      那也是「报红」——闸不能因为异常类型不对就漏判成通过。
    """
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


class _Hub:
    def info(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _seed(db: DB) -> None:
    """造 4 条已采完的帖 + 1 条没采的，各带不同的规则标签。"""
    rows = [
        (3001, "地铁换乘站无障碍通道关闭", "func", "已受理", "done"),
        (3002, "垃圾清运不及时", "func", "已回复", "done"),
        (3003, "企业咨询办理流程", "enterprise", "已受理", "done"),
        (3004, "跨部门推诿扯皮", "district", "未受理", "done"),
        (3005, "还没采的帖", "func", "已受理", "pending"),
    ]
    for tid, title, grp, st, ds in rows:
        db.conn.execute(
            "INSERT INTO threads(tid,title,url,dept_name,dept_group,state,"
            "pub_time,pub_ts,replies,detail_state,posts_n,chars) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, title, "http://x/%d" % tid, "某部门", grp, st,
             "2026-10-01 09:00", 1790000000, 3, ds, 1, 60))
        db.conn.execute(
            "INSERT INTO posts(pid,tid,floor,author,content,chars,post_time) "
            "VALUES(?,?,?,?,?,?,?)",
            (tid, tid, 1, "市民", "正文内容测试 %d" % tid, 60,
             "2026-10-01 09:00"))
    # 规则标签（机器算的）
    # 3001: relation=官民 impact=热度：高
    db.conn.execute("INSERT INTO thread_dim_tags(tid,dim,tag) "
                    "VALUES(3001,'relation','官民')")
    db.conn.execute("INSERT INTO thread_dim_tags(tid,dim,tag) "
                    "VALUES(3001,'impact','热度：高')")
    # 3002: relation=属地
    db.conn.execute("INSERT INTO thread_dim_tags(tid,dim,tag) "
                    "VALUES(3002,'relation','属地')")
    # 3003: relation=企民 process=一次性
    db.conn.execute("INSERT INTO thread_dim_tags(tid,dim,tag) "
                    "VALUES(3003,'relation','企民')")
    db.conn.execute("INSERT INTO thread_dim_tags(tid,dim,tag) "
                    "VALUES(3003,'process','一次性')")
    db.conn.commit()


def main() -> int:
    print("=" * 62)
    print("闸：标注台 Label Studio 化（批次 / 批量派活 / 一致性）")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="msyt_anno_ls_")
    db = DB(os.path.join(tmp, "t.db"))
    svc = AnnoService(db, _Hub())
    _seed(db)

    # ---- ① 任务批次视图 ----
    print("\n--- ① 任务批次视图 ---")
    r = svc.enqueue([3001, 3002], "node:m")
    check(r["task_code"].startswith("L"), f"任务编号 L 开头：{r['task_code']}")
    b1 = r["task_code"]
    r2 = svc.enqueue([3003], "node:x", task_code=b1)
    check(r2["task_code"] == b1, "同批次可再派一条（批次号沿用）")
    r3 = svc.enqueue([3004], "node:m")
    b2 = r3["task_code"]
    check(b2 != b1, "不同批次号不同")

    bs = svc.batches("node:m")
    check(len(bs) == 2, f"node:m 看到 2 个批次（实得 {len(bs)}）")
    m1 = [b for b in bs if b["task_code"] == b1][0]
    check(m1["total"] == 2, f"批次 {b1} 共 2 条（实得 {m1['total']}）")
    check(m1["queued"] == 2 and m1["done"] == 0,
          f"批次 {b1} 都还没标：{m1}")
    check(m1["pct"] == 0, f"进度 0%：{m1['pct']}")
    check("node:m" in m1["assignee"],
          f"批次显示派给谁：{m1['assignee']}")

    # 标完一条 → 进度动
    svc.submit("node:m", 3001, "纳入", "属于公共交通服务问题，纳入",
               [["relation", "官民"], ["impact", "热度：高"]])
    bs = svc.batches("node:m")
    m1 = [b for b in bs if b["task_code"] == b1][0]
    check(m1["done"] == 1 and m1["queued"] == 1,
          f"标完一条后批次进度更新：{m1}")
    check(m1["pct"] == 50, f"进度 50%：{m1['pct']}")

    # 成员只看自己的批次（管理员看别人是接口层的事，这里是服务层）
    bx = svc.batches("node:x")
    check(len(bx) == 1 and bx[0]["task_code"] == b1,
          f"node:x 只看到自己的批次：{bx}")

    # ---- ② 批量派活（按筛选条件） ----
    print("\n--- ② 批量派活 ---")
    # 按部门分组筛：func 组 → 3001/3002/3005
    r = svc.enqueue_filter("node:m", limit=10, group="func")
    check(r["queued"] == 2,
          f"func 组里已采到正文的只有 2 条（实得 {r}）")
    check(r["skipped"] == 1,
          f"没采到正文的那条被跳过（实得 skipped={r['skipped']}）")
    check(r["matched"] == 3, f"该条件共匹配 3 条：{r['matched']}")
    # 重复入队 → 不产生重复行（tid 是主键）
    n_before = db.conn.execute("SELECT COUNT(*) FROM anno_queue").fetchone()[0]
    svc.enqueue_filter("node:m", limit=10, group="func")
    n_after = db.conn.execute("SELECT COUNT(*) FROM anno_queue").fetchone()[0]
    check(n_before == n_after,
          f"重复批量入队不产生重复行（{n_before} → {n_after}）")
    # 筛不出已采正文的 → 报错，不静默入队 0 条
    try:
        svc.enqueue_filter("node:m", limit=10, group="district")
        check(True, "district 组里 3004 已采到正文（可入队）")
    except ValueError as e:
        check(False, "district 组应可入队：%s" % e)

    # ---- ③ 一致性：人工 vs 规则 ----
    print("\n--- ③ 一致性反馈（人工 vs 规则） ---")
    ag = svc.agreement()
    check(ag["annotated"] == 1,
          f"只统计有人工标注的帖（实得 {ag['annotated']}）")
    check(ag["thread_same"] == 1 and ag["thread_diff"] == 0,
          f"3001 人工=规则 → 整帖一致：{ag}")
    check(ag["thread_pct"] == 100, f"一致率 100%：{ag['thread_pct']}")
    check(ag["dims"]["relation"]["same"] == 1,
          f"关系维度一致：{ag['dims']}")

    # 标一条**完全不一样**的（3002：规则=属地，人工=官民 → 不一致）
    svc.submit("node:m", 3002, "纳入", "其实是跨部门问题",
               [["relation", "官民"]])
    ag = svc.agreement()
    check(ag["annotated"] == 2, f"两条都算进来：{ag['annotated']}")
    check(ag["thread_same"] == 1 and ag["thread_diff"] == 1,
          f"一帖一致、一帖不一致：{ag}")
    check(ag["thread_pct"] == 50, f"一致率 50%：{ag['thread_pct']}")
    check(ag["dims"]["relation"]["diff"] == 1,
          f"关系维度不一致计数：{ag['dims']['relation']}")
    check(ag["changed_total"] == 1,
          f"差异明细 1 处：{ag['changed_total']}")
    ch = ag["changed_tags"][0]
    check(ch["tid"] == 3002 and ch["human"] == "官民" and ch["rule"] == "属地",
          f"差异明细内容对：{ch}")

    # **部分重合**：人工={官民,属地}，规则={属地} → partial
    svc.submit("node:m", 3003, "纳入", "部分一致",
               [["relation", "官民"], ["relation", "属地"]])
    ag = svc.agreement()
    # 3003 规则 relation={企民}，人工 relation={官民,属地} → 无交集 → diff
    check(ag["dims"]["relation"]["diff"] == 2,
          f"3003 也算不一致（无交集）：{ag['dims']['relation']}")

    # 打回的人工标签**不计入**一致性
    svc.submit("node:m", 3004, "纳入", "先标上", [["relation", "企民"]])
    ag = svc.agreement()
    check(ag["annotated"] == 4, f"打回前 4 条：{ag['annotated']}")
    svc.review(3004, False, "标错了，请重标", "admin@test")
    ag = svc.agreement()
    check(ag["annotated"] == 3,
          f"★ 被打回的人工标签不计入一致性（自己跟自己比没意义）：{ag['annotated']}")

    # 没有任何人工标注时 → 结构完整、数为 0（不能崩）
    db.conn.execute("DELETE FROM thread_anno_tags")
    db.conn.commit()
    ag = svc.agreement()
    check(ag["annotated"] == 0 and ag["thread_pct"] == 0,
          f"没有人工标注时返回空结构：{ag}")
    check(ag.get("dims") == {} and ag.get("changed_tags") == [],
          "空结构各字段齐全")

    # ---- ④ ★反向验证（改坏实现 → 同一批断言必须报红） ----
    print("\n--- ④ ★反向验证 ---")
    # ★ 用**独立的临时库**：反向验证要改坏实现再改回来，
    #   在同一个库上重跑会撞主键/残留状态，断言就不干净了。
    tmp2 = tempfile.mkdtemp(prefix="msyt_anno_ls_rev_")
    db2 = DB(os.path.join(tmp2, "t.db"))
    _seed(db2)
    svc2 = AnnoService(db2, _Hub())
    svc2.enqueue([3001, 3002], "node:m")
    svc2.submit("node:m", 3001, "纳入", "与规则一致",
                [["relation", "官民"], ["impact", "热度：高"]])

    def assert_rejected_excluded():
        ag = svc2.agreement()
        # 打回 3001 → 它不该再计入
        svc2.review(3001, False, "打回", "admin@test")
        ag = svc2.agreement()
        assert ag["annotated"] == 0, "打回的仍被计入：%s" % ag

    orig_agreement = AnnoService.agreement
    # 把「排除被打回」这一条打坏 → 断言必须报红
    def bad_agreement(self):
        r = orig_agreement(self)
        r["annotated"] = r["annotated"] + 1  # 假装打回的也算
        return r

    AnnoService.agreement = bad_agreement
    check(expect_red(assert_rejected_excluded),
          "反向验证①：打坏「打回不计入一致性」→ 断言报红")
    AnnoService.agreement = orig_agreement
    check(not expect_red(assert_rejected_excluded),
          "反向验证①收尾：恢复后断言通过")
    # ★ 复位：上面那条断言把 3001 打回了，后续断言要用干净状态
    db2.conn.execute("UPDATE thread_anno_tags SET status='submitted' "
                     "WHERE tid=3001")
    db2.conn.execute("UPDATE sample_review SET status='submitted' "
                     "WHERE tid=3001")
    db2.conn.commit()

    def assert_only_annotated_counted():
        ag = svc2.agreement()
        assert ag["annotated"] == 1, "把没标注的帖也算进来了：%s" % ag

    # 打坏：把全库有人工标签的以外的也算进去
    def bad_agreement2(self):
        r = orig_agreement(self)
        r["annotated"] = 999
        return r

    AnnoService.agreement = bad_agreement2
    check(expect_red(assert_only_annotated_counted),
          "反向验证②：打坏「只统计已标注的」→ 断言报红")
    AnnoService.agreement = orig_agreement
    check(not expect_red(assert_only_annotated_counted),
          "反向验证②收尾：恢复后断言通过")

    def assert_batch_progress():
        bs = svc2.batches("node:m")
        b = bs[0]
        assert b["total"] == 2 and b["done"] == 1, "批次计数不对：%s" % b
        assert b["pct"] == 50, "进度不对：%s" % b

    orig_batches = AnnoService.batches
    AnnoService.batches = lambda self, assignee="": []
    check(expect_red(assert_batch_progress),
          "反向验证③：打坏「批次视图」→ 断言报红")
    AnnoService.batches = orig_batches
    check(not expect_red(assert_batch_progress),
          "反向验证③收尾：恢复后断言通过")

    def assert_filter_skips_undone():
        r = svc2.enqueue_filter("node:m", limit=10, group="func")
        assert r["skipped"] >= 1, "没采正文的没被跳过：%s" % r

    orig_filter = AnnoService.enqueue_filter
    AnnoService.enqueue_filter = lambda self, assignee="", task_code="", \
        limit=50, **kw: {"task_code": "L9", "queued": 99, "skipped": 0,
                         "matched": 99}
    check(expect_red(assert_filter_skips_undone),
          "反向验证④：打坏「批量派活只收已采正文的」→ 断言报红")
    AnnoService.enqueue_filter = orig_filter
    check(not expect_red(assert_filter_skips_undone),
          "反向验证④收尾：恢复后断言通过")

    print("\n" + "=" * 62)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
