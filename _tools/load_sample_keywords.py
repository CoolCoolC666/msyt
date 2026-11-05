# -*- coding: utf-8 -*-
"""把「样例词表.csv」灌进真库的词表。

★ 为什么要有这个脚本
  样例词表有 40 多个词，手工在界面上一个个加太慢；
  这个脚本读 CSV → 逐个 `add_keyword`（同一个词重复加 = 更新，不新增）
  → 可选跑一次扫描。

★ CSV 是**唯一事实源**：说明文档（`_报告/样例词表.md`）只解释口径，
  不重复抄词表 —— 两处抄就会迟早对不上。

用法（在项目「程序」目录下跑，路径里有空格要加引号）：
    python _tools/load_sample_keywords.py           # 只灌词表
    python _tools/load_sample_keywords.py --scan    # 灌完顺便扫描全库
    python _tools/load_sample_keywords.py --dry     # 只看要灌什么，不写库
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.config import Config  # noqa: E402
from msyt.db import DB  # noqa: E402

CSV_PATH = os.path.join(PROJ, "_报告", "样例词表.csv")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="灌完顺便扫描全库")
    ap.add_argument("--dry", action="store_true", help="只打印，不写库")
    args = ap.parse_args()

    if not os.path.exists(CSV_PATH):
        print("找不到词表 CSV：%s" % CSV_PATH)
        return 1

    with open(CSV_PATH, "r", encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f)
                if (r.get("word") or "").strip()]
    print("CSV：%s（%d 个词）" % (CSV_PATH, len(rows)))
    by_kind: dict = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r["word"])
    for k, ws in by_kind.items():
        print("  %-8s %2d 个：%s" % (k, len(ws), "、".join(ws)))
    if args.dry:
        return 0

    cfg = Config.load()
    db = DB(cfg.db_path)
    n = 0
    for r in rows:
        db.add_keyword(r["word"].strip(), (r["kind"] or "").strip(),
                       parent=(r.get("parent") or "").strip(),
                       note=(r.get("note") or "").strip())
        n += 1
    print("\n已写入 %d 个词 → %s" % (n, cfg.db_path))
    for k in DB.KEYWORD_KINDS:
        got = [x for x in db.list_keywords() if x["kind"] == k]
        print("  %-8s %2d 个" % (k, len(got)))
    if args.scan:
        print("\n扫描全库…")
        res = db.scan_keywords()
        print("  %s" % res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
