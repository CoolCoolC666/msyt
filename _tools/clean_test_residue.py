# -*- coding: utf-8 -*-
"""清理真实库里残留的**测试数据**（tid >= 900000000，即 9 号段）。

★ 为什么需要（2026-10-23 首建）：
  test_beta_permissions.py 在真实库上造验收数据，
  它的清理用错了连接（WAL 下的「读事务升级为写」让 DELETE 没生效），
  于是 tid=999999001 的两楼「本地第一楼/本地第二楼」留在了权威库里。
  真实库是**交付物**，里面不能有测试造的数据。

★ 阈值 2026-10-25 放宽：999999000 → 900000000
  原来只抓 `>= 999999000`，但 `test_gap_retry_e2e.py` 用的是 **tid=999888001**
  → 落在阈值**以下**，工具根本抓不到它 → 那条「【验收用】少贴测试帖」
  留在交付库里，而且 `pub_ts=NULL` → 排序时 `COALESCE(pub_ts,0)=0`
  → **永远排队首** → 第一个 worker 领到它白跑一趟，还会混进导出 CSV。
  → 现在抓**整个 9 号段**：站方真实 tid 是 7~8 位数（83xxxxxx），
    9 开头只可能是测试造的。

★ 现在**默认只报告、不写库**（这个工具删的是交付库），
  确认无误再加 --apply 才真删。

用法：
    python _tools\\clean_test_residue.py            # dry-run：只列出来
    python _tools\\clean_test_residue.py --apply    # 真删
"""
import argparse
import os
import sqlite3
import sys

ROOT = r"【项目根】"
DB = os.path.join(ROOT, "程序", "data", "msyt.db")
THRESHOLD = 900000000

TABLES = ["posts", "thread_pages", "flows", "detail_queue",
          "assist_conflicts", "assist_picks", "threads"]

_ap = argparse.ArgumentParser(description="清理真实库里的测试残留数据")
_ap.add_argument("--apply", action="store_true",
                  help="真删（默认只报告，不写库）")
_args = _ap.parse_args()

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
try:
    con.execute("PRAGMA busy_timeout=30000")

    # 先看看要删的是些什么（标题一眼就能认出是测试数据）
    print("将要清理的帖（tid >= %d）：" % THRESHOLD)
    rows = con.execute(
        "SELECT tid, title, detail_state FROM threads WHERE tid>=? ORDER BY tid",
        (THRESHOLD,)).fetchall()
    if not rows:
        print("  （没有）")
    for r in rows:
        print(f"  tid={r['tid']}  标题={r['title']!r}  状态={r['detail_state']}")

    print("\n清理前：")
    for t in TABLES:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE tid>=?", (THRESHOLD,)).fetchone()[0]
        except sqlite3.Error as e:
            print(f"  {t}: 查不了（{e}）")
            continue
        if n:
            print(f"  {t}: {n} 行")
    print()

    if not _args.apply:
        print("[dry-run] 未写库。确认无误后加 --apply 执行。")
        sys.exit(0)

    total = 0
    for t in TABLES:
        try:
            cur = con.execute(f"DELETE FROM {t} WHERE tid>=?", (THRESHOLD,))
            if cur.rowcount and cur.rowcount > 0:
                print(f"  已删 {t}: {cur.rowcount} 行")
                total += cur.rowcount
        except sqlite3.Error as e:
            print(f"  [WARN] 删 {t} 失败：{e}")
    con.commit()

    print(f"\n合计删除 {total} 行")
    print("\n清理后：")
    for t in TABLES:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE tid>=?", (THRESHOLD,)).fetchone()[0]
        except sqlite3.Error:
            continue
        print(f"  {t}: {n} 行")
finally:
    con.close()
