# -*- coding: utf-8 -*-
"""审计：候选词在**真库**里的命中量（给「样例词表」选词用）。

★ 为什么要有这个脚本
  样例词表不能是拍脑袋写的 —— 得先知道哪些词在真实语料里真的出现、
  出现得够多（否则词表命中一两条，等于没筛）。
  这个脚本把候选词逐个跑一次「楼层正文命中帖数 / 命中楼层数」，
  输出表格；选词只从「有量」的词里挑。

用法（必须在项目「程序」目录下跑，路径里有空格要加引号）：
    python _tools/audit_word_counts.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.config import Config  # noqa: E402
from msyt.db import DB  # noqa: E402

#: 候选词（按拟定的五类词分组）
CANDIDATES = [
    # 核心词：研究对象本身
    "民生e点通", "民生e网通", "12345", "12345政务服务便民热线",
    # 主体词：涉及的公共对象/设施
    "地铁", "公交", "垃圾", "道路", "供水", "供电", "燃气", "停车",
    "菜场", "学校", "医院", "施工", "路灯", "井盖", "河道",
    # 事件/问题词
    "晚点", "换乘", "漏水", "停电", "堵车", "噪音", "污染", "收费",
    "占道", "积水", "异味", "异味扰民", "无法通行", "安全隐患",
    # 扩展词：同一对象的别称/写法变体
    "轨道交通", "公交车", "垃圾箱", "道路破损", "断电", "停水",
    # 排除词：命中即把该帖从其它词的命中里扣掉
    "外地", "外埠", "转载", "招聘", "广告",
]


def main() -> int:
    cfg = Config.load()
    db = DB(cfg.db_path)
    print("库：%s" % cfg.db_path)
    t0 = time.time()
    rows = []
    for w in CANDIDATES:
        t1 = time.time()
        with db.lock:
            n_posts = db.conn.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT tid FROM posts "
                "WHERE pid>0 AND content LIKE ?)", ("%" + w + "%",)).fetchone()[0]
            n_rows = db.conn.execute(
                "SELECT COUNT(*) FROM posts WHERE pid>0 AND content LIKE ?",
                ("%" + w + "%",)).fetchone()[0]
        rows.append((w, n_posts, n_rows, time.time() - t1))
        print("  %-22s 帖 %7d  楼层 %8d  (%.2fs)" % (w, n_posts, n_rows,
                                                     time.time() - t1))
    print("\n合计 %d 个词，用时 %.1fs" % (len(rows), time.time() - t0))
    print("（命中量是**活口径**：库在采、词表在变，每次跑都会不一样）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
