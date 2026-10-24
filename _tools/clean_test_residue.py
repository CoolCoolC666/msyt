# -*- coding: utf-8 -*-
"""清理真实库里残留的**测试数据**（tid >= 999999000）。

★ 为什么需要（2026-10-23）：
  test_beta_permissions.py 在真实库上造验收数据，
  它的清理用错了连接（WAL 下的「读事务升级为写」让 DELETE 没生效），
  于是 tid=999999001 的两楼「本地第一楼/本地第二楼」留在了权威库里。
  真实库是**交付物**，里面不能有测试造的数据。

只删 tid >= 999999000 的行 —— 真实数据的 tid 都是 7~8 位数（83xxxxxx），
999999000 以上只可能是测试造的。
"""
import os
import sqlite3
import sys

ROOT = r"【项目根】"
DB = os.path.join(ROOT, "程序", "data", "msyt.db")
THRESHOLD = 999999000

TABLES = ["posts", "thread_pages", "flows", "detail_queue",
          "assist_conflicts", "assist_picks", "threads"]

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
try:
    con.execute("PRAGMA busy_timeout=30000")
    print("清理前：")
    for t in TABLES:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE tid>=?", (THRESHOLD,)).fetchone()[0]
        except sqlite3.Error as e:
            print(f"  {t}: 查不了（{e}）")
            continue
        if n:
            print(f"  {t}: {n} 行")
    print()

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
