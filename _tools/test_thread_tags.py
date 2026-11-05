# -*- coding: utf-8 -*-
"""闸：三维标签（优化报告 P2-2 · 课件「关系/性质过程/影响」三角度）。

★ 跑在**临时库**上。

★ 要守住的性质
  ① 关系：站方部门分组打底（官民/属地/企民/其它）+ 医患/师生细分（**多值**）
  ② 过程：受理流程的**最后阶段** + 处理时长分档
  ③ 影响：回复数分档（热度）+ 标题**归一化后**跨部门出现
  ④ ★标题归一化：`#wm#.公共秩序-乱停放乱停车` 与 `公共秩序-乱停放乱停车`
     是同一件事（站方的 `#wm#.` 只是渲染前缀）→ 必须算出跨部门重复

用法：python _tools/test_thread_tags.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.db import DB  # noqa: E402
from msyt.tags import duration_bucket, heat_bucket, norm_title  # noqa: E402

FAILED: list[str] = []

T0 = 1700000000          # 假起点
DAY = 86400


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def main() -> int:
    print("=" * 62)
    print("闸：三维标签")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="msyt_tags_")
    db = DB(os.path.join(tmp, "t.db"))

    # ---- ① 造样本 ----
    print("\n--- ① 造样本 ---")
    threads = [
        # tid, title, dept_name, dept_group, replies
        (101, "地铁2号线换乘站无障碍通道关闭", "市交通局", "func", 3),
        (102, "医院挂号难请尽快解决", "市卫生健康委", "func", 1),
        (103, "#wm#.公共秩序-乱停放乱停车", "市公安局", "func", 0),
        (104, "公共秩序-乱停放乱停车", "市城管局", "func", 25),
        (105, "某企业排放废水影响周边", "某某化工有限公司", "enterprise", 10),
        (106, "短标题", "市教育局", "func", 0),
        (107, "跨区同题反映到多个部门", "市住建局", "func,district", 6),
    ]
    for tid, title, dept, grp, rep in threads:
        db.conn.execute(
            "INSERT INTO threads(tid,title,url,dept_name,dept_group,replies,"
            "detail_state,posts_n,chars) VALUES(?,?,'u',?,?,?,'done',1,10)",
            (tid, title, dept, grp, rep))
    # 流程时间线
    for tid, name, ts in (
            (101, "已发帖", T0), (101, "已受理", T0 + 2 * DAY),
            (105, "已发帖", T0), (105, "已回复", T0 + 10 * DAY),
            (103, "已发帖", T0), (103, "已受理", T0 + 0 * DAY),
    ):
        db.conn.execute(
            "INSERT INTO flows(tid,step,name,time,ts) VALUES(?,?,?,?,?)",
            (tid, 0 if name == "已发帖" else 1, name, "2026-01-01 00:00:00", ts))
    db.conn.commit()

    # ---- ② 纯函数 ----
    print("\n--- ② 分档规则 ---")
    check(norm_title("#wm#.公共秩序") == "公共秩序", "标题归一化剥掉 #wm#. 前缀")
    check(heat_bucket(0) == "无互动", "热度：0 回复 → 无互动")
    check(heat_bucket(3) == "中", "热度：3 回复 → 中")
    check(heat_bucket(25) == "很高", "热度：25 回复 → 很高")
    check(duration_bucket(2) == "1-3天", "时长：2 天 → 1-3天")
    check(duration_bucket(40) == "30天以上", "时长：40 天 → 30天以上")

    # ---- ③ 跑计算 ----
    print("\n--- ③ 全库计算 ---")
    res = db.compute_tags()
    check(res.get("ok") is True, f"计算成功：{res}")
    check(res.get("posts") == 7, f"算了 7 个帖（实得 {res.get('posts')}）")
    tags = db.tag_map()

    def has(tid, tag):
        return tag in tags.get(tid, [])

    # ---- ④ 关系 ----
    print("\n--- ④ 关系维度 ---")
    check(has(101, "官民"), "职能机关 → 官民")
    check(has(102, "医患"), "卫生部门 → 医患")
    check(has(102, "官民"), "医患帖同时也是官民（多值）")
    check(has(105, "企民"), "企业部门 → 企民")
    check(has(106, "师生"), "教育部门 → 师生")
    check(has(107, "官民") and has(107, "属地"), "混合分组 → 两个关系标签")

    # ---- ⑤ 过程 ----
    print("\n--- ⑤ 过程维度 ---")
    check(has(101, "阶段：已受理"), "阶段取流程的最后一步")
    check(has(101, "时长：1-3天"), "时长分档：1-3天")
    check(has(105, "阶段：已回复"), "阶段：已回复")
    check(has(105, "时长：7-30天"), "时长分档：7-30天")
    check(has(103, "时长：1天内"), "时长分档：1天内")
    check(not any(t.startswith("阶段") for t in tags.get(102, [])),
          "没有流程的帖 → 不打阶段标签（不编）")

    # ---- ⑥ 影响 ----
    print("\n--- ⑥ 影响维度 ---")
    check(has(104, "热度：很高"), "热度：很高")
    check(has(101, "热度：中"), "热度：中")
    check(has(101, "单部门"), "只出现在一个部门 → 单部门")
    check(has(103, "跨部门重复") and has(104, "跨部门重复"),
          "★归一化后同名且跨部门 → 跨部门重复")
    check(not has(106, "跨部门重复"),
          "短标题（<8 字）不算跨部门重复（站方很多帖标题就是分类词）")

    # ---- ⑦ ★反向验证 ----
    print("\n--- ⑦ ★反向验证 ---")
    # 把归一化去掉 → 103 与 104 不再同名 → 「跨部门重复」必须消失
    db.conn.execute("DELETE FROM thread_tags")
    db.conn.execute("UPDATE threads SET title=REPLACE(title,'#wm#.','')")
    db.conn.commit()
    db.compute_tags()
    t2 = db.tag_map()
    dup_after = "跨部门重复" in t2.get(103, [])
    check(dup_after, "★去掉渲染前缀后仍应判为同名（归一化真的生效了）")
    # 反过来：把标题改得完全不同 → 跨部门重复必须消失
    db.conn.execute("UPDATE threads SET title='完全不同的另一个标题甲' WHERE tid=103")
    db.conn.execute("UPDATE threads SET title='完全不同的另一个标题乙' WHERE tid=104")
    db.conn.commit()
    db.compute_tags()
    t3 = db.tag_map()
    check("跨部门重复" not in t3.get(103, []),
          "标题不同 → 不该判跨部门重复（排除逻辑不是空转）")

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