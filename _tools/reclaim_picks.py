# -*- coding: utf-8 -*-
"""把「已领走但没回传」的活退回池子（测试脚本误领 + 回收机制自检）。

用法：python _tools/reclaim_picks.py [--all]
  --all  全部退回（不管多久没回传）；默认只回收超过 30 分钟的
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "程序"))

from msyt.assist import AssistService      # noqa: E402
from msyt.config import Config              # noqa: E402
from msyt.db import DB                      # noqa: E402
from msyt.hub import Hub                    # noqa: E402

cfg = Config.load()
db = DB(cfg.db_path)
svc = AssistService(db, Hub())

all_mode = "--all" in sys.argv
n = svc.reclaim_stale(0 if all_mode else 1800)
print(f"回收了 {n} 条超时未回传的活（{'全部' if all_mode else '超过 30 分钟'}）")

print("\n当前各任务池子状态：")
for t in svc.tasks():
    st = t.get("by_state", {})
    print(f"  {t['code']:<4} {t['title'][:10]:<10} 总={t['total']:<6} "
          f"完成={t['done']:<5} 进行中={t.get('inflight', 0):<5} "
          f"还剩={t.get('pending', 0):<5} 失败={st.get('failed', 0)}")
db.close()
