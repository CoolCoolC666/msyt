# -*- coding: utf-8 -*-
"""闸：人工标注台（人工标注功能）。

★ 跑在**临时库**上（不碰真库）。

★ 要守住的性质
  ① 队列**只收已采到正文的帖** —— 没正文标不了
  ② 核验结论**理由必填**；结论只能是三个取值；维度只能是三个
  ③ 提交一条 = 核验结论 + 人工标签 + 队列完成，**三件事一起**
  ④ **人工标签优先覆盖规则标签**，规则原值留一列（可复核）
  ⑤ **打回** → 人工标签失效、导出回落规则值；打回**意见必填**
  ⑥ 管理员复核才写复核人；组员标注**不碰**复核人（人工签署位不代签）
  ⑦ 老数据（没标注过的帖）导出**不受影响**

★ 必须有**真反向验证**：把实现改坏后，同一批断言必须报红。

用法：python _tools/test_anno.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt import anno  # noqa: E402
from msyt.anno import AnnoService  # noqa: E402
from msyt.db import DB  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def expect_red(fn) -> bool:
    """fn 里的断言**必须报红**。返回 False = 断言不够狠。"""
    try:
        fn()
    except AssertionError:
        return True
    return False


class _Hub:
    def info(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def main() -> int:
    print("=" * 62)
    print("闸：人工标注台")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="msyt_anno_")
    db = DB(os.path.join(tmp, "t.db"))
    svc = AnnoService(db, _Hub())

    # ---- 造 2 条已采完 + 1 条没采 ----
    for tid, title, st in ((2001, "地铁换乘站无障碍通道关闭", "done"),
                           (2002, "垃圾清运不及时", "done"),
                           (2003, "还没采的帖", "pending")):
        db.conn.execute(
            "INSERT INTO threads(tid,title,url,dept_name,dept_group,state,"
            "pub_time,pub_ts,replies,detail_state,posts_n,chars) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, title, "http://x/%d" % tid, "市交通局", "func", "已受理",
             "2026-10-01 09:00", 1790000000, 3, st, 1, 60))
        db.conn.execute(
            "INSERT INTO posts(pid,tid,floor,author,content,chars,post_time) "
            "VALUES(?,?,?,?,?,?,?)",
            (tid, tid, 1, "市民", "正文内容测试 %d" % tid, 60, "2026-10-01 09:00"))
    # 规则标签（机器算的）
    db.conn.execute("INSERT INTO thread_dim_tags(tid,dim,tag) VALUES(2001,'relation','官民')")
    db.conn.execute("INSERT INTO thread_dim_tags(tid,dim,tag) VALUES(2001,'impact','热度：高')")
    db.conn.execute("INSERT INTO thread_dim_tags(tid,dim,tag) VALUES(2002,'relation','属地')")
    db.conn.commit()

    # ---- ① 队列 ----
    print("\n--- ① 标注队列 ---")
    try:
        svc.enqueue([2003], "node:m")
        check(False, "没采正文的帖应当被拒")
    except ValueError:
        check(True, "没采到正文的帖被拒（不浪费标注员时间）")
    try:
        svc.enqueue([], "node:m")
        check(False, "空队列应当被拒")
    except ValueError:
        check(True, "没挑帖子被拒")
    r = svc.enqueue([2001, 2002, 2003], "node:m")
    check(r["queued"] == 2 and r["skipped"] == 1,
          f"只收 2 条已采正文的（实得 {r}）")
    check(r["task_code"].startswith("L"), f"标注任务编号 L 开头：{r['task_code']}")
    q = svc.queue("node:m")
    check(len(q) == 2, f"队列里有 2 条（实得 {len(q)}）")
    check(q[0]["state"] == "queued", "状态是待标")

    # ---- ② 校验 ----
    print("\n--- ② 校验（信任边界） ---")
    for bad, why in ((dict(verdict="乱写", reason="x"), "非法结论"),
                      (dict(verdict="纳入", reason="  "), "有结论没理由")):
        try:
            svc.submit("node:m", 2001, bad.get("verdict"), bad.get("reason"), [])
            check(False, why + " 应当被拒")
        except ValueError:
            check(True, why + " 被拒")
    try:
        svc.submit("node:m", 2001, "", "", [["瞎写的维度", "某值"]])
        check(False, "非法维度应当被拒")
    except ValueError:
        check(True, "非法维度被拒")

    # ---- ③ 提交一条标注 ----
    print("\n--- ③ 提交标注（三件事一起） ---")
    r = svc.submit("node:m", 2001, "纳入", "属于公共交通服务问题，纳入",
                   [["relation", "企民"], ["impact", "热度：高"]])
    check(r["ok"] and r["verdict"] == "纳入", f"提交成功：{r}")
    rv = db.get_review(2001)
    check(rv["verdict"] == "纳入" and rv["reason"] == "属于公共交通服务问题，纳入",
          f"核验结论写进 sample_review：{rv}")
    check(rv["annotator"] == "node:m", f"标注人留痕：{rv['annotator']}")
    check((rv["reviewed_by"] or "") == "",
          f"★ 组员标注**不碰复核人**（人工签署位不代签）：{rv['reviewed_by']!r}")
    check(rv["status"] == "submitted", f"状态=待复核：{rv['status']}")
    tags = db.conn.execute(
        "SELECT dim,tag,status FROM thread_anno_tags WHERE tid=2001").fetchall()
    check(len(tags) == 2, f"人工标签写了 2 条（实得 {len(tags)}）")
    check(all(t["status"] == "submitted" for t in tags), "人工标签状态=待复核")
    q = svc.queue("node:m")
    done1 = [x for x in q if x["tid"] == 2001]
    check(bool(done1) and done1[0]["state"] == "done", "队列里这条置完成")

    # ---- ④ 人工优先覆盖规则 ----
    print("\n--- ④ 人工标签优先覆盖规则标签 ---")
    rule = {2001: "热度：高 / 官民", 2002: "属地"}
    man = anno.anno_tag_map_text(db.conn)
    # ★ 键是**整数** tid；顺序按 (dim, tag) 排 → impact 在 relation 前
    check(man.get(2001) == "热度：高 / 企民",
          f"人工标签取到：{man.get(2001)}")
    final = anno.merge_tags(rule, man)
    check(final[2001] == "热度：高 / 企民", "人工值覆盖规则值")
    check(final[2002] == "属地", "没有人工标注的帖子仍用规则值")
    check(final[2002] == rule[2002], "老数据不受影响")

    # ---- ⑤ 导出：两列，人工优先 + 规则留痕 ----
    print("\n--- ⑤ 导出（31 列，人工优先 + 规则值留痕） ---")
    out = os.path.join(tmp, "e.csv")
    n = db.export_csv(out)
    with open(out, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    body = {r[0]: r for r in rows[1:]}
    check(n == 3, f"导出 3 行（实得 {n}）")
    check("三维标签" in header and "规则三维标签" in header,
          "两列都在：三维标签 / 规则三维标签")
    check(header.index("规则三维标签") == header.index("三维标签") + 1,
          "规则值紧跟在人工值后面")
    r1 = body["2001"]
    i = header.index("三维标签")
    check(r1[i] == "热度：高 / 企民", f"「三维标签」= 人工值：{r1[i]}")
    check(r1[i + 1] == "热度：高 / 官民", f"「规则三维标签」= 规则原值：{r1[i + 1]}")
    r2 = body["2002"]
    check(r2[i] == "属地" and r2[i + 1] == "属地",
          f"没人标的帖两列一致：{r2[i]} / {r2[i + 1]}")

    # ---- ⑥ 复核：打回 / 通过 ----
    print("\n--- ⑥ 管理员复核 ---")
    try:
        svc.review(2001, False, "   ", "admin@test")
        check(False, "打回没写意见应当被拒")
    except ValueError:
        check(True, "打回没意见被拒")
    svc.review(2001, False, "标签与正文对不上，请重标", "admin@test")
    rv = db.get_review(2001)
    check(rv["status"] == "rejected", f"打回后状态：{rv['status']}")
    check(rv["reviewed_by"] == "admin@test", f"复核人=本人点击的账号：{rv['reviewed_by']}")
    man2 = anno.anno_tag_map_text(db.conn)
    check(2001 not in man2, f"打回后人工标签失效（实得 {man2}）")
    final2 = anno.merge_tags(rule, man2)
    check(final2[2001] == "热度：高 / 官民", "打回后导出回落到规则标签")
    svc.submit("node:m", 2001, "排除", "其实是企业咨询，不是投诉",
               [["relation", "企民"]])
    svc.review(2001, True, "", "admin@test")
    rv = db.get_review(2001)
    check(rv["status"] == "approved", f"通过后状态：{rv['status']}")
    check(rv["verdict"] == "排除" and rv["reason"] == "其实是企业咨询，不是投诉",
          "通过后结论与理由保留")
    man3 = anno.anno_tag_map_text(db.conn)
    check(man3.get(2001) == "企民", f"通过后人工标签生效：{man3.get(2001)}")

    # ---- ⑦ 统计与候选标签 ----
    print("\n--- ⑦ 统计与候选标签 ---")
    st = svc.stats()
    check(st["queue"].get("done") == 1 and st["queue"].get("queued") == 1,
          f"队列数：{st['queue']}")
    check(st["pending_review"] == 0, f"待复核数：{st['pending_review']}")
    check(st["review"].get("排除") == 1, f"核验结论统计：{st['review']}")
    vc = svc.vocab()
    check("relation" in vc["candidates"], "候选标签里有关系维度")
    check(any(c["tag"] == "官民" for c in vc["candidates"]["relation"]),
          "候选标签含规则已产生的取值")

    # ---- ⑧ ★反向验证（改坏实现 → 同一批断言必须报红） ----
    print("\n--- ⑧ ★反向验证 ---")

    def assert_merge_priority():
        assert anno.merge_tags({"1": "规则值"}, {"1": "人工值"})["1"] == "人工值"

    orig_merge = anno.merge_tags
    anno.merge_tags = lambda rule, man: dict(rule)
    check(expect_red(assert_merge_priority),
          "反向验证①：打坏 merge_tags → 「人工优先」断言报红")
    anno.merge_tags = orig_merge
    check(not expect_red(assert_merge_priority), "反向验证①收尾：恢复后断言通过")

    def assert_anno_map_excludes_rejected():
        assert "2001" not in anno.anno_tag_map_text(db.conn)

    svc.review(2001, False, "再打回一次", "admin@test")
    orig_map = anno.anno_tag_map_text
    anno.anno_tag_map_text = lambda conn: {"2001": "不该出现的值"}
    check(expect_red(assert_anno_map_excludes_rejected),
          "反向验证②：打坏 anno_tag_map_text → 「打回即失效」断言报红")
    anno.anno_tag_map_text = orig_map
    check(not expect_red(assert_anno_map_excludes_rejected),
          "反向验证②收尾：恢复后断言通过")

    def assert_reject_needs_note():
        try:
            svc.review(2001, False, "", "admin@test")
        except ValueError:
            return
        raise AssertionError("打回没写意见竟然没被拒")

    # 反向验证③：把 review 的校验**打坏**（不再要求打回写意见）→ 断言必须报红
    orig_review = AnnoService.review
    AnnoService.review = lambda self, tid, approved, note="", reviewer="": {"ok": True}
    check(expect_red(assert_reject_needs_note),
          "反向验证③：打坏「打回必填意见」→ 断言报红")
    AnnoService.review = orig_review
    check(not expect_red(assert_reject_needs_note),
          "反向验证③收尾：恢复后断言通过")

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
