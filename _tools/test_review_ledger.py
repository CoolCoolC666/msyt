# -*- coding: utf-8 -*-
"""闸：核验台账（优化报告 P0-4 · 课件字段⑤⑥ 的载体）。

★ 跑在**临时库**上（不碰真库）。

★ 为什么要有这个闸
  核验台账是「结论可追溯」的落点，两条纪律最容易破：
    · `reviewed_by`（复核人）是**人工签署位** —— 服务端一旦代填，
      整份台账就变成「AI 冒充人签字」，比没有台账更危险。
    · 有结论没理由 = 等于没核验。

用法：python _tools/test_review_ledger.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.db import DB  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def main() -> int:
    print("=" * 62)
    print("闸：核验台账 sample_review")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="msyt_review_")
    db = DB(os.path.join(tmp, "t.db"))

    # ---- ① 表存在 ----
    print("\n--- ① 表与字段 ---")
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(sample_review)")]
    for c in ("tid", "verdict", "reason", "reviewed_by", "reviewed_at",
              "method", "ai_label", "ai_model", "updated_at"):
        check(c in cols, f"字段 {c} 存在")

    # ---- ② 三态都能写能读 ----
    print("\n--- ② 三态（纳入 / 排除 / 待核验）---")
    for i, v in enumerate(db.VERDICTS):
        r = db.save_review(1000 + i, v, f"理由{i}", reviewed_at="2026-11-05")
        check(r.get("verdict") == v, f"{v} 写入成功")
        check(db.get_review(1000 + i).get("reason") == f"理由{i}",
              f"{v} 读回一致")

    # ---- ③ 有结论必须写理由 ----
    print("\n--- ③ 校验：有结论没理由要报错 ---")
    try:
        db.save_review(2001, "纳入", "")
        check(False, "空理由应当抛错")
    except ValueError as e:
        check(True, f"空理由被拒：{e}")

    # ---- ④ verdict 取值受控 ----
    print("\n--- ④ verdict 只能是三个取值 ---")
    try:
        db.save_review(2002, "随便写的", "有理由")
        check(False, "非法 verdict 应当抛错")
    except ValueError as e:
        check(True, f"非法 verdict 被拒：{e}")

    # ---- ⑤ ★复核人必须留空（人工签署位，AI 不代填）----
    print("\n--- ⑤ ★复核人留空：不传就是真空 ---")
    r = db.save_review(3001, "纳入", "正文齐全", reviewed_at="2026-11-05")
    check(r.get("reviewed_by") == "", f"reviewed_by 为空（实得 {r.get('reviewed_by')!r}）")
    check(r.get("reviewed_at") == "2026-11-05", "reviewed_at 取传入的核对日期")
    # 服务器**不代填**：保存后仍然真空
    check(db.get_review(3001).get("reviewed_by") == "", "落库后复核人仍是真空")
    # 显式传入时照存（那是人自己签的）
    r2 = db.save_review(3002, "纳入", "正文齐全", reviewed_by="张三",
                        reviewed_at="2026-11-05")
    check(r2.get("reviewed_by") == "张三", "显式传入的复核人照存")

    # ---- ⑥ 统计 ----
    # ★ 此时的构成：1000=纳入、1001=排除、1002=待核验、3001=纳入、3002=纳入
    #   （1000 改判成「排除」发生在下面第 ⑦ 步，所以这里还是 3）
    print("\n--- ⑥ 统计 ---")
    st = db.review_stats()
    check(st.get("纳入") == 3, f"纳入计数 = 3（实得 {st.get('纳入')}）")
    check(st.get("排除") == 1, f"排除计数 = 1（实得 {st.get('排除')}）")
    check(st.get("待核验") == 1, f"待核验计数 = 1（实得 {st.get('待核验')}）")
    check("未复核" in st, f"含「未复核」分类（{st}）")

    # ---- ⑦ 重复写同一 tid → 覆盖不新增行 ----
    print("\n--- ⑦ 同一 tid 重复写是覆盖 ---")
    db.save_review(1000, "排除", "改判了", reviewed_at="2026-11-05")
    check(db.get_review(1000).get("verdict") == "排除", "结论已被覆盖")
    n = db.conn.execute(
        "SELECT COUNT(*) FROM sample_review WHERE tid=1000").fetchone()[0]
    check(n == 1, f"仍然只有 1 行（实得 {n}）")

    # ---- ⑧ AI 初筛结果能记 ----
    print("\n--- ⑧ AI 初筛结果与模型（可追溯）---")
    db.save_review(4001, "待核验", "等人工看", method="AI初筛",
                   ai_label="疑似诉求", ai_model="某模型-v1",
                   reviewed_at="2026-11-05")
    r = db.get_review(4001)
    check(r.get("ai_label") == "疑似诉求", "AI 初筛结论已记")
    check(r.get("ai_model") == "某模型-v1", "AI 模型已记（可追溯）")
    check(r.get("reviewed_by") == "", "AI 初筛也没代填复核人")

    # ---- ⑨ ★反向验证：把校验改坏，同一批断言必须报红 ----
    print("\n--- ⑨ ★反向验证：绕过校验后必须能写进非法数据 ---")
    # 直接绕过 save_review 的校验，用 SQL 塞一条非法结论 → 证明校验是有效的防线
    db.conn.execute(
        "INSERT INTO sample_review(tid,verdict,reason,updated_at) "
        "VALUES(9999,'乱写的','x','2026-11-05')")
    db.conn.commit()
    bad = db.get_review(9999).get("verdict")
    check(bad == "乱写的",
          "绕过接口直接写库能落库（说明校验确实在接口层，"
          "所以接口层的 ValueError 断言有意义）")

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
