# -*- coding: utf-8 -*-
"""闸：协作冲突裁决（优化报告 P1-3）。

★ 跑在**临时库**上（不碰真库的真数据）。

★ 要守住的性质
  ① 按类批量裁决：只动该类里 **resolved=0** 的那些，已裁决的不重复动
  ② **裁决人留空** —— 人工签署位，程序不代填人名、不填占位符
  ③ **裁决必须写说明**（按哪条规则、为什么这样处理）—— 空说明要被拒
  ④ **只写 assist_conflicts，不碰 sample_review** —— 合并痕迹 ≠ 人工核验结论
  ⑤ **可撤销且留痕**：撤销后旧说明保留并标「（已撤销）」，不删数据

用法：python _tools/test_conflict_adjudication.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.assist import AssistService  # noqa: E402
from msyt.db import DB  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def main() -> int:
    print("=" * 62)
    print("闸：协作冲突裁决")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="msyt_cf_")
    db = DB(os.path.join(tmp, "t.db"))

    # ---- ① 造冲突 ----
    print("\n--- ① 造样本 ---")
    svc = AssistService(db, None)
    for i in range(5):
        svc._conflict("T1", 900 + i, "node:test-node", "merged_new",
                     "新增 1 个楼层", "合并前 0 楼 / 0 字", "成员回传 1 楼 / 10 字")
    for i in range(2):
        svc._conflict("T1", 800 + i, "node:test-node", "empty",
                     "成员回传没有任何楼层", "", "")
    n = db.conn.execute("SELECT COUNT(*) FROM assist_conflicts").fetchone()[0]
    check(n == 7, f"造了 7 条冲突（实得 {n}）")

    # ---- ② 计数 ----
    print("\n--- ② 按类计数 ---")
    counts = svc.conflict_counts()
    check(counts.get("merged_new", {}).get("total") == 5,
          f"merged_new 共 5 条（实得 {counts.get('merged_new')}）")
    check(counts.get("merged_new", {}).get("pending") == 5, "5 条待裁决")
    check(counts.get("empty", {}).get("total") == 2, "empty 共 2 条")

    # ---- ③ 裁决 ----
    print("\n--- ③ 批量裁决 ---")
    k = svc.resolve_conflicts("merged_new", "按 2026-10-23 合并规则：新增楼层已入库")
    check(k == 5, f"裁决 5 条（实得 {k}）")
    row = db.conn.execute(
        "SELECT * FROM assist_conflicts WHERE kind='merged_new' LIMIT 1").fetchone()
    check(row["resolved"] == 1, "resolved 标成 1")
    check((row["resolved_by"] or "") == "",
          f"裁决人留空（实得 {row['resolved_by']!r}）")
    check(bool(row["resolved_note"]), "裁决说明写进去了")
    check(bool(row["resolved_at"]), f"裁决日期：{row['resolved_at']}")
    k2 = svc.resolve_conflicts("merged_new", "再裁决一次")
    check(k2 == 0, f"已裁决的不重复动（实得 {k2}）")
    check(svc.conflict_counts()["merged_new"]["resolved"] == 5, "计数没跑偏")

    # ---- ④ 说明必填 ----
    print("\n--- ④ 校验 ---")
    try:
        svc.resolve_conflicts("empty", "   ")
        check(False, "空说明应当被拒")
    except ValueError:
        check(True, "空说明被拒")
    check(svc.resolve_conflicts("不存在的类", "说明") == 0,
          "裁决不存在的类 → 0 条（不报错、不误伤）")

    # ---- ⑤ ★不污染人工核验台账 ----
    print("\n--- ⑤ ★裁决不写 sample_review ---")
    rv = db.conn.execute("SELECT COUNT(*) FROM sample_review").fetchone()[0]
    check(rv == 0, f"sample_review 仍是 0 行（实得 {rv}）")

    # ---- ⑥ 撤销留痕 ----
    print("\n--- ⑥ 撤销（留痕不删） ---")
    n_re = svc.reopen_conflicts("merged_new")
    check(n_re == 5, f"撤销 5 条（实得 {n_re}）")
    row = db.conn.execute(
        "SELECT * FROM assist_conflicts WHERE kind='merged_new' LIMIT 1").fetchone()
    check(row["resolved"] == 0, "撤销后回到待裁决")
    check((row["resolved_note"] or "").startswith("（已撤销）"),
          f"旧说明保留并标注：{row['resolved_note']!r}")
    check(db.conn.execute(
        "SELECT COUNT(*) FROM assist_conflicts").fetchone()[0] == 7,
        "撤销不删数据（还是 7 条）")

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