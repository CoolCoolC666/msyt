# -*- coding: utf-8 -*-
"""验收：「少贴」→ 保留原帖 + 标记 + 进增量补采队列。

★ 用户 2026-10-23 晚上：
  「另外，就是数据缺口的情况
    建议如果有少贴的情况下，保留原贴并且标记
    主要是作增量的补采」

★ 验证：
  ① 少贴的帖 → detail_state='stale' + detail_queue 置 pending
  ② **原帖保留**（posts 一条不少，正文不删）
  ③ 补采次数上限：超限就不再入队（防无限重采）
  ④ 「楼层号缺口」（中间缺，站方删楼）**不**触发补采
  ⑤ 计数对得上的帖**不**触发补采
  ⑥ 判据是**被测代码里的函数**（不是测试自己手写的判断）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = r"【项目根】"
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt.db import DB                          # noqa: E402
from msyt.detail import is_short_and_retryable  # noqa: E402  ← 被测函数

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


print("=" * 72)
print("「少贴 → 保留 + 标记 + 补采」验收")
print("=" * 72)

tmp = tempfile.mkdtemp(prefix="msyt-gap-")
path = os.path.join(tmp, "t.db")
db = DB(path)

# ---------------------------------------------------------------- ① 判据函数
print("\n① 判据函数 is_short_and_retryable()")
check("★ 尾部少（楼层连续、总数少）→ **要补采**",
      is_short_and_retryable(5, 3, [1, 2, 3]) is True)
check("★ 中间缺（站方删楼）→ **不补采**",
      is_short_and_retryable(4, 3, [1, 3, 4]) is False)
check("★ 计数对得上 → 不补采",
      is_short_and_retryable(5, 5, [1, 2, 3, 4, 5]) is False)
check("★ 实收比站方多 → 不补采",
      is_short_and_retryable(3, 5, [1, 2, 3, 4, 5]) is False)
check("★ 没有楼层信息时按数量判断（要补采）",
      is_short_and_retryable(5, 3, []) is True)
check("★ 站方称 0 → 不补采", is_short_and_retryable(0, 0, [1]) is False)

# ---------------------------------------------------------------- ② 标记动作
print("\n② 标记动作：保留原帖 + stale + 入队")
tid = 900001
with db.lock:
    db.conn.execute(
        "INSERT INTO threads(tid,title,replies,detail_state,posts_n,chars) "
        "VALUES(?,'少贴测试帖',4,'done',3,30)", (tid,))
    for i, fl in ((1, 1), (2, 2), (3, 3)):
        db.conn.execute(
            "INSERT INTO posts(tid,pid,page_no,floor,author,content,chars) "
            "VALUES(?,?,1,?,'甲','某楼',10)", (tid, 90000100 + i, fl))
    db.conn.commit()

n = db.mark_detail_stale([tid], reason="少贴：站方称应有 5 楼，实收 3 楼（少 2 楼）")
check("mark_detail_stale 返回处理条数", n == 1, str(n))
st = db.conn.execute("SELECT detail_state FROM threads WHERE tid=?",
                      (tid,)).fetchone()["detail_state"]
check("★ detail_state = 'stale'（界面显示「待更新」）", st == "stale", st)
q = db.conn.execute("SELECT state FROM detail_queue WHERE tid=?",
                    (tid,)).fetchone()
check("★ detail_queue 置回 pending（会被自动捡走重采）",
      q and q["state"] == "pending", str(dict(q)) if q else "无队列行")

print("\n③ ★ 原帖保留（一条都不删）")
np = db.conn.execute("SELECT COUNT(*) FROM posts WHERE tid=?", (tid,)).fetchone()[0]
check("★ 楼层一条不少", np == 3, f"{np} 层")
th = db.conn.execute("SELECT title,posts_n FROM threads WHERE tid=?",
                     (tid,)).fetchone()
check("★ 原帖记录还在", th and th["title"] == "少贴测试帖", str(dict(th)) if th else "")

print("\n④ 补采次数上限（防无限重采）")
db.set_detail_warn(tid, "少贴：…；已补采 2 次")
w = db.get_detail_warn(tid)
check("get_detail_warn 能读回", "已补采 2 次" in w, w)
import re as _re
m = _re.search(r"已补采\s*(\d+)\s*次", w)
check("能解析出补采次数", bool(m) and int(m.group(1)) == 2,
      m.group(1) if m else "解析失败")

print("\n⑤ 代码层面：标记动作在写完 done 之后")
src = open(os.path.join(ROOT, "程序", "msyt", "detail.py"), encoding="utf-8").read()
i_done = src.find('self.db.set_thread_detail(\n            tid, detail_state="done"')
i_mark = src.find("mark_detail_stale([_t_id]")
check("★ mark_detail_stale 在 set_thread_detail(done) **之后**",
      i_done > 0 and i_mark > i_done, f"done@{i_done} mark@{i_mark}")
check("有 GAP_RETRY_MAX 上限", "GAP_RETRY_MAX = 2" in src)
check("判据调用的是那个函数",
      "if is_short_and_retryable(expect_posts, len(unique), floors):" in src)
check("db.py 有 get_detail_warn", "def get_detail_warn" in
      open(os.path.join(ROOT, "程序", "msyt", "db.py"), encoding="utf-8").read())

shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
