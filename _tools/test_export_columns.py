# -*- coding: utf-8 -*-
"""闸：导出 CSV 必须**自证可追溯**（优化报告 P0-2 · 课件字段①④⑤）。

★ 跑在**临时库**上，跑的是真代码 `DB.export_csv()`（不是闸自己的副本）。

★ 这次改动的要害
  导出是**用户真正拿到手的交付物**。改之前 23 列里：
    · 没有「发现时间」 → 不知道样本什么时候被发现的
    · 没有「原文摘要」 → 只有全文，人读不出这条诉求是什么
    · 没有「核验结论」 → 不知道这条凭什么纳入/排除
    · 没有「候选池路径」 → 不知道它从哪个通道进的池子
    · 连已有的 `detail_warn` 都没导出 → 系统诊断白留了
  → 改后 28 列（23 + 5）。

用法：python _tools/test_export_columns.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.db import DB  # noqa: E402

FAILED: list[str] = []
OLD_COLS = ["tid", "title", "url", "pub_time", "pub_day", "dept_name",
            "dept_group", "dept_district", "category", "state", "mailbox_id",
            "light_code", "light_name", "author", "replies", "views",
            "detail_state", "posts_n", "chars", "first_post_id", "last_post_id",
            "pages_total", "full_text"]
NEW_COLS = ["发现时间", "原文摘要", "核验结论", "候选池路径", "系统诊断", "词表命中"]


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def main() -> int:
    print("=" * 62)
    print("闸：导出 CSV 列与内容")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="msyt_export_")
    db = DB(os.path.join(tmp, "t.db"))
    now = "2026-11-05 10:00:00"

    # ---- 造 3 条样本 ----
    db.conn.execute(
        "INSERT INTO threads(tid,title,url,dept_name,category,state,pub_time,"
        "pub_ts,replies,views,detail_state,posts_n,chars,detail_warn) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1001, "地铁2号线换乘站无障碍通道关闭", "http://x/1001", "市交通局",
         "投诉", "已受理", "2026-10-01 09:00", 1790000000, 3, 120,
         "done", 3, 120, "楼层号缺口 [7]（站方删楼/置顶）"))
    db.conn.execute(
        "INSERT INTO threads(tid,title,url,dept_name,category,state,pub_time,"
        "pub_ts,replies,views,detail_state,posts_n,chars,detail_warn) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1002, "垃圾清运不及时", "http://x/1002", "市环卫局", "建议", "未受理",
         "2026-10-02 09:00", 1790100000, 1, 50, "done", 1, 30, ""))
    db.conn.execute(
        "INSERT INTO threads(tid,title,url,dept_name,category,state,pub_time,"
        "pub_ts,replies,views,detail_state,posts_n,chars,detail_warn) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1003, "纯图片帖", "http://x/1003", "某部门", "", "", "2026-10-03 09:00",
         1790200000, 0, 10, "done", 1, 0, ""))
    # 楼层：1001 三楼（楼主 + 两楼），1002 一楼，1003 一楼纯图片
    for pid, tid, pg, fl, au, ct, ft in (
            (9001, 1001, 1, 1, "市民", "换乘站无障碍通道长期关闭，希望尽快恢复。", now),
            (9002, 1001, 1, 2, "路人甲", "我也遇到过。", now),
            (9003, 1001, 1, 3, "路人乙", "同问。", now),
            (9004, 1002, 1, 1, "市民乙", "垃圾清运不及时，望处理。", "2026-11-01 08:00:00"),
            (9005, 1003, 1, 1, "市民丙", "[图:http://x/a.jpg]", "2026-11-02 08:00:00")):
        db.conn.execute(
            "INSERT INTO posts(pid,tid,page_no,floor,author,content,chars,"
            "images,fetched_at,post_time) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pid, tid, pg, fl, au, ct, len(ct), "[]", ft, "2026-10-01 09:00"))
    db.conn.execute(
        "INSERT INTO thread_sources(tid,source,first_seen) VALUES(?,?,?)",
        (1001, "dept:512", now))
    db.conn.execute(
        "INSERT INTO thread_list_forms(tid,source,kind,first_seen) VALUES(?,?,?,?)",
        (1001, "dept:512", "row", now))
    db.conn.execute(
        "INSERT INTO sample_review(tid,verdict,reason,reviewed_by,reviewed_at,"
        "method,updated_at) VALUES(?,?,?,?,?,?,?)",
        (1001, "纳入", "诉求明确、已受理", "", "2026-11-05", "人工", now))
    db.conn.commit()

    # ---- 词表（P1-1）：先造词表并扫一次，导出里才有「词表命中」 ----
    db.add_keyword("无障碍通道", "issue", note="具体诉求")
    db.add_keyword("换乘", "issue", note="具体诉求")
    db.add_keyword("垃圾清运", "subject")
    db.scan_keywords()

    # ---- 跑真代码 ----
    out = os.path.join(tmp, "out.csv")
    n = db.export_csv(out)
    check(n == 3, f"导出行数 = 3（实得 {n}）")

    with open(out, "r", encoding="utf-8-sig", newline="") as f:
        rd = list(csv.reader(f))
    head, body = rd[0], rd[1:]

    # ---- ① 表头：旧 23 列一个不少、顺序不变 ----
    print("\n--- ① 旧列一个不少、顺序不变 ---")
    check(head[:len(OLD_COLS)] == OLD_COLS,
          f"前 {len(OLD_COLS)} 列与旧版一致")
    for c in OLD_COLS:
        check(c in head, f"旧列 {c} 还在")

    # ---- ② 新增 6 列 ----
    print("\n--- ② 新增 6 列 ---")
    for c in NEW_COLS:
        check(c in head, f"新列 {c} 在")
    check(head[len(OLD_COLS):] == NEW_COLS, "新列追加在末尾、顺序正确")
    check(len(head) == 29, f"总列数 = 29（实得 {len(head)}）")

    # ---- ③ 每列都有内容 ----
    print("\n--- ③ 内容逐列核对（取 tid=1001 那行）---")
    row = dict(zip(head, next(r for r in body if r[0] == "1001")))
    check(row["发现时间"] == "2026-11-05 10:00:00", f"发现时间 = {row['发现时间']}")
    check("无障碍通道" in row["原文摘要"], f"摘要含正文：{row['原文摘要']}")
    check("市交通局" in row["原文摘要"] and "投诉" in row["原文摘要"]
          and "已受理" in row["原文摘要"], "摘要带锚点")
    check(row["核验结论"] == "纳入｜诉求明确、已受理", f"核验结论 = {row['核验结论']}")
    check("dept:512" in row["候选池路径"] and "row" in row["候选池路径"],
          f"候选池路径 = {row['候选池路径']}")
    check("楼层号缺口" in row["系统诊断"], f"系统诊断 = {row['系统诊断']}")
    # ⚠ `full_text` 的楼层分隔符是**字面两个字符 `\n`**（SQLite 不解析
    #   字符串里的转义）—— 这是**沿用下来的原有行为**，本次不改（改了旧列内容就变了）。
    check(row["full_text"].count("\\n") == 2,
          f"full_text 仍是三篇正文（没被改）：{row['full_text'][:60]!r}")

    # ---- ④ 没核验的帖：核验结论留空，不编 ----
    print("\n--- ④ 未复核的帖不编结论 ---")
    row2 = dict(zip(head, next(r for r in body if r[0] == "1002")))
    check(row2["核验结论"] == "", f"未复核 → 空（实得 {row2['核验结论']!r}）")
    check("垃圾清运" in row2["原文摘要"], "未复核的帖也有摘要")
    row3 = dict(zip(head, next(r for r in body if r[0] == "1003")))
    check("无文字正文" in row3["原文摘要"], f"纯图片帖如实说明：{row3['原文摘要']}")

    # ---- ⑤ 可复现：导出两次内容一致 ----
    print("\n--- ⑤ 可复现：连导两次内容完全一致 ---")
    out2 = os.path.join(tmp, "out2.csv")
    db.export_csv(out2)
    a = open(out, "rb").read()
    b = open(out2, "rb").read()
    check(a == b, "两次导出字节级一致（摘要可复现）")

    # ---- ⑥ ★反向验证 ----
    print("\n--- ⑥ ★反向验证 ---")
    check(len(head) != 23, "确实不是旧版的 23 列了")
    check(row2["核验结论"] == "", "无核验记录时结论为空（不是回退成别的值）")
    check("纯图片帖" not in row3["原文摘要"], "空正文不把标题塞进摘要")

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
