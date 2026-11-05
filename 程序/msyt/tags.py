# -*- coding: utf-8 -*-
"""三维标签（优化报告 P2-2 · 课件「关系 / 性质·过程 / 影响」三个分类角度）。

★ 为什么单独成模块：标签的**判定规则**要能被逐条复核（课件：人工核验与留痕）——
  规则集中在一个文件里，好核对、好写闸；也不必把 db.py 撑得更胖。
★ 全部用**库里现成的信号**，不猜语义、不用模型：
  · 关系 = 站方自己的部门分组（func/district/enterprise/other）
          + 部门名细分的「医患」「师生」
  · 过程 = 受理流程时间线（flows）的**最后阶段** + 从发帖到该阶段的**时长分档**
  · 影响 = 回复数分档（热度）+ 标题归一化后**跨多个部门**出现（跨部门重复）
★ 标题归一化：站方会给某些标题加 `#wm#.` 渲染前缀
  （`#wm#.公共秩序` 与 `公共秩序` 是同一件事）→ 先剥前缀再比。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

#: 站方的部门分组 → 诉求方与办理方的关系
RELATION_BY_GROUP = {
    "func": "官民",
    "district": "属地",
    "enterprise": "企民",
    "other": "其它",
}

#: 三个维度
TAG_DIMS = ("relation", "process", "impact")


def norm_title(title: str) -> str:
    """标题归一化：剥掉站方的 `#wm#.` 渲染前缀。"""
    return (title or "").replace("#wm#.", "")


def heat_bucket(replies: int) -> str:
    """回复数 → 热度档（实测分布见报告；浏览数是站方自报，不做热度依据）。"""
    r = int(replies or 0)
    if r <= 0:
        return "无互动"
    if r <= 2:
        return "低"
    if r <= 5:
        return "中"
    if r <= 20:
        return "高"
    return "很高"


def duration_bucket(days: int) -> str:
    """处理时长（发帖 → 最后一个流程阶段）分档。"""
    if days <= 1:
        return "1天内"
    if days <= 3:
        return "1-3天"
    if days <= 7:
        return "3-7天"
    if days <= 30:
        return "7-30天"
    return "30天以上"


def compute_tags(conn) -> Dict[str, Any]:
    """给全库打三维标签 → 写 `thread_dim_tags`（**全量重算**）。"""
    t0 = time.time()
    # ---- ① 流程时间线：每帖的最早时间 / 最后阶段 ----
    flow: Dict[int, Tuple[int, str]] = {}
    for tid, ts, name in conn.execute(
            "SELECT tid, ts, name FROM flows ORDER BY tid, step"):
        tid = int(tid)
        ts = int(ts or 0)
        row = flow.get(tid)
        if row is None:
            flow[tid] = (ts, name or "")
        else:
            # 同一帖按 step 升序读 → 后来的就是更靠后的阶段
            flow[tid] = (row[0], name or row[1])
    # ---- ② 跨部门重复的标题（归一化后，长度≥8）----
    dup_titles = {r[0] for r in conn.execute(
        "SELECT title FROM (SELECT REPLACE(title,'#wm#.','') AS title, "
        "dept_name FROM threads WHERE title<>'' AND LENGTH(title)>=8) "
        "GROUP BY title HAVING COUNT(DISTINCT dept_name)>1")}
    # ---- ③ 逐帖打标签 ----
    rows: List[Tuple[int, str, str]] = []
    n_posts = 0
    for tid, title, dept_name, dept_group, replies in conn.execute(
            "SELECT tid, title, dept_name, dept_group, replies FROM threads"):
        tid = int(tid)
        n_posts += 1
        # —— 关系 ——
        for g in (dept_group or "").split(","):
            g = g.strip()
            if not g:
                continue
            name = RELATION_BY_GROUP.get(g)
            if name:
                rows.append((tid, "relation", name))
        dn = dept_name or ""
        if "卫生" in dn or "医院" in dn or "医" in dn:
            rows.append((tid, "relation", "医患"))
        if "教育" in dn or "学校" in dn or "学院" in dn:
            rows.append((tid, "relation", "师生"))
        # —— 过程 ——
        f = flow.get(tid)
        if f:
            t_first, stage = f
            rows.append((tid, "process", "阶段：" + (stage or "未知")))
            # 时长要「最早 ts → 最晚 ts」，这里先只记最早 ts 与阶段名，
            #   最晚 ts 统一在下面用 MAX(ts) 一次取。
        # —— 影响 ——
        rows.append((tid, "impact", "热度：" + heat_bucket(int(replies or 0))))
        rows.append((tid, "impact",
                     "跨部门重复" if norm_title(title) in dup_titles else "单部门"))
    # ---- ④ 时长分档（需要每帖的最晚 ts）----
    last_ts = {int(r[0]): int(r[1] or 0) for r in conn.execute(
        "SELECT tid, MAX(ts) FROM flows GROUP BY tid")}
    for tid, (t_first, _stage) in list(flow.items()):
        t_last = last_ts.get(tid, 0)
        if t_first and t_last >= t_first:
            days = (t_last - t_first) // 86400
            rows.append((tid, "process", "时长：" + duration_bucket(days)))
    # ---- ⑤ 落库 ----
    conn.execute("DELETE FROM thread_dim_tags")
    conn.executemany(
        "INSERT OR REPLACE INTO thread_dim_tags(tid,dim,tag) VALUES(?,?,?)", rows)
    conn.commit()
    by_dim: Dict[str, int] = {}
    for _tid, dim, _tag in rows:
        by_dim[dim] = by_dim.get(dim, 0) + 1
    return {"ok": True, "posts": n_posts, "tags": len(rows),
            "by_dim": by_dim, "elapsed": round(time.time() - t0, 1)}


def tag_map(conn) -> Dict[int, List[str]]:
    """{tid: [标签]} —— 导出与列表用。"""
    out: Dict[int, List[str]] = {}
    for tid, dim, tag in conn.execute(
            "SELECT tid, dim, tag FROM thread_dim_tags ORDER BY tid, dim, tag"):
        out.setdefault(int(tid), []).append(tag)
    return out


def tag_map_text(conn) -> Dict[int, str]:
    # {tid: 标签1 / 标签2} —— 导出用（拼好的字符串，省内存）
    out: Dict[int, str] = {}
    for tid, tag in conn.execute(
            'SELECT tid, tag FROM thread_dim_tags ORDER BY tid, dim, tag'):
        tid = int(tid)
        if tid in out:
            out[tid] = out[tid] + ' / ' + tag
        else:
            out[tid] = tag
    return out


def tag_stats(conn) -> Dict[str, Dict[str, int]]:
    """每个维度下各标签的帖数（界面做筛选与分布用）。"""
    out: Dict[str, Dict[str, int]] = {}
    for dim, tag, n in conn.execute(
            "SELECT dim, tag, COUNT(DISTINCT tid) FROM thread_dim_tags GROUP BY dim, tag"):
        out.setdefault(str(dim), {})[str(tag)] = int(n)
    return out