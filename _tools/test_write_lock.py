# -*- coding: utf-8 -*-
"""验收：并发写下登录不再 500（治「读事务升级为写」）。

★ 复现原故障的思路：
  1. 进程 A（模拟服务端）先 SELECT —— 打开一个**读事务**
  2. 进程 B（模拟另一个写者）持有写锁
  3. 进程 A 再 UPDATE —— 「读事务升级为写」→ SQLite 不走 busy handler
     → 立刻抛 `database is locked`（原实现就是这个）
  4. 现在：进写块时先 commit 掉读事务 → 走正常 busy 等待 → 成功
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = r"【项目根】"
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt.db import DB                          # noqa: E402

OK = 0
FAIL = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {label}" + (f"  —— {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  —— {extra}" if extra else ""))


print("=" * 70)
print("并发写下的登录安全（读事务升级为写）")
print("=" * 70)

tmp = tempfile.mkdtemp(prefix="msyt-lock-")
path = os.path.join(tmp, "t.db")
db = DB(path)
db.conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
db.conn.commit()

# ---------------------------------------------------------------- ① 复现原理
print("\n① 原理复现：裸 sqlite3 连接会踩坑")
con = sqlite3.connect(path, timeout=1)      # timeout 故意很小
con.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
con.execute("INSERT OR REPLACE INTO t VALUES (1,'x')")
con.commit()
con.execute("SELECT * FROM t")               # ← 打开读事务，不 commit

# 另一个连接持写锁
other = sqlite3.connect(path, timeout=1)
other.execute("BEGIN IMMEDIATE")
other.execute("INSERT OR REPLACE INTO t VALUES (2,'y')")
try:
    con.execute("INSERT OR REPLACE INTO t VALUES (3,'z')")   # 升级为写
    check("裸连接升级为写会失败（这就是原故障）", False, "居然成功了")
except sqlite3.OperationalError as e:
    check("裸连接升级为写会失败（这就是原故障）", "locked" in str(e).lower(),
          f"{type(e).__name__}: {e}")
other.rollback()
other.close()
con.close()

# ---------------------------------------------------------------- ② 现在的行为
print("\n② 现在的行为：写锁进入时先 commit 读事务")
db2 = DB(path)
# 模拟「先查后写」：先 SELECT（开读事务），再进写块
db2.conn.execute("SELECT * FROM t").fetchall()      # 开读事务，不 commit

# 另一个连接此刻持有写锁
# ★ 连接必须在**用它的那条线程**里创建（sqlite3 的 check_same_thread 限制）


def writer():
    holder = sqlite3.connect(path, timeout=1)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT OR REPLACE INTO t VALUES (9,'holder')")
    time.sleep(1.2)                               # 1.2 秒后释放锁
    holder.rollback()
    holder.close()


t = threading.Thread(target=writer)
t.start()
time.sleep(0.3)          # 等 holder 真的拿到写锁
ok = False
err = ""
t0 = time.time()
try:
    with db2.lock:
        db2.conn.execute("INSERT OR REPLACE INTO t VALUES (4,'writer')")
        db2.conn.commit()
    ok = True
except Exception as e:                            # noqa: BLE001
    err = f"{type(e).__name__}: {e}"
t.join()
dt = time.time() - t0

check("写块成功（没有抛 locked）", ok, err)
# 主线程在 t.start() 后已先睡了 0.3 秒，holder 在 1.2 秒后释放
# → 写块大约要再等 0.9 秒
check("确实等了对方释放锁", dt >= 0.5, f"耗时 {dt:.2f}s")

# ---------------------------------------------------------------- ③ 锁确实是共享的
print("\n③ 共享锁：两个 DB 实例指向同一个文件时互斥")
# ★ 每个线程**各自 new 一个 DB**（连接属于该线程），
#   但它们指向同一个文件 → 拿到的是**同一把进程级共享锁**。
order = []
holder_box = {}


def t3():
    d = DB(path)
    holder_box["a"] = d
    with d.lock:
        order.append("A-in")
        time.sleep(0.4)
        order.append("A-out")


def t4():
    time.sleep(0.1)
    d = DB(path)
    holder_box["b"] = d
    with d.lock:
        order.append("B-in")
        order.append("B-out")


ta = threading.Thread(target=t3)
tb = threading.Thread(target=t4)
ta.start(); tb.start()
ta.join(); tb.join()
check("两个实例的写块不重叠", order == ["A-in", "A-out", "B-in", "B-out"], str(order))
check("两个实例的锁是同一把",
      holder_box.get("a") is not None
      and holder_box["a"].lock._rlock is holder_box["b"].lock._rlock)

# ---------------------------------------------------------------- ④ 进锁时读事务已结束
print("\n④ 进写块时读事务确实已结束")
db5 = DB(path)
db5.conn.execute("SELECT * FROM t").fetchall()
with db5.lock:
    in_tx = db5.conn.in_transaction
check("进锁后不在读事务里", in_tx is False, f"in_transaction={in_tx}")

print()
print("=" * 70)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
