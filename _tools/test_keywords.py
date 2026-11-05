# -*- coding: utf-8 -*-
"""闸：词表（优化报告 P1-1 · 课件「建立『词表』，关键词设计」）。

★ 跑在**临时库**上。

★ 要守住的性质
  ① 五类词齐全（核心/主体/事件问题/扩展/排除）
  ② 扫描是**一次全表扫描 + 多词判定**（不是 N 次查询）
  ③ **排除词是「减」的**：命中排除词的帖，从其他词的命中里扣掉
     （课件举例「非本地同名地名」——「宁波」同名地点遍布全国）
  ④ 停用的词不参与扫描，但词表里还在

用法：python _tools/test_keywords.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.db import DB  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def main() -> int:
    print("=" * 62)
    print("闸：词表")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="msyt_kw_")
    db = DB(os.path.join(tmp, "t.db"))

    # ---- ① 表与字段 ----
    print("\n--- ① 表与字段 ---")
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(keywords)")]
    for c in ("id", "word", "kind", "parent", "enabled", "note", "created_at"):
        check(c in cols, f"keywords 有字段 {c}")
    hcols = [r[1] for r in db.conn.execute("PRAGMA table_info(keyword_hits)")]
    for c in ("keyword_id", "tid", "post_count", "first_hit_at"):
        check(c in hcols, f"keyword_hits 有字段 {c}")

    # ---- ② 五类词 ----
    print("\n--- ② 五类词 ---")
    check(len(db.KEYWORD_KINDS) == 5, f"五类词：{db.KEYWORD_KINDS}")
    for k in ("core", "subject", "issue", "variant", "exclude"):
        check(k in db.KEYWORD_KINDS, f"词类 {k} 在")

    # ---- ③ 加词：同词不重复、非法词类被拒 ----
    print("\n--- ③ 加词与校验 ---")
    k1 = db.add_keyword("宁波地铁", "core", note="核心对象")
    check(k1["word"] == "宁波地铁", "加核心词成功")
    db.add_keyword("宁波地铁", "core", note="重复加")
    n = db.conn.execute(
        "SELECT COUNT(*) FROM keywords WHERE word='宁波地铁'").fetchone()[0]
    check(n == 1, f"同一词只留一条（实得 {n}）")
    try:
        db.add_keyword("", "core")
        check(False, "空词应当被拒")
    except ValueError:
        check(True, "空词被拒")
    try:
        db.add_keyword("某词", "不存在的类")
        check(False, "非法词类应当被拒")
    except ValueError:
        check(True, "非法词类被拒")

    # ---- ④ 造样本：3 帖 4 楼 ----
    # ★ 先插 threads 再插 posts —— posts.tid 有外键指向 threads.tid
    print("\n--- ④ 造样本 ---")
    for tid, title, n, ch in ((2001, "地铁换乘", 2, 30), (2002, "公交晚点", 1, 15),
                              (2003, "外地新闻", 2, 30)):
        db.conn.execute(
            "INSERT INTO threads(tid,title,url,pub_time,pub_ts,detail_state,posts_n,chars)"
            " VALUES(?,?,?,'2026-11-01',?,'done',?,?)",
            (tid, title, "u%d" % tid, 1790000000 + tid, n, ch))
    for pid, tid, ct in (
            (7001, 2001, "宁波地铁2号线换乘站无障碍通道关闭"),
            (7002, 2001, "晚点了很久"),
            (7003, 2002, "宁波公交晚点严重"),
            (7004, 2003, "外地新闻：宁波的同名城市也有一条地铁新闻"),
            (7005, 2003, "与地铁无关的内容")):
        db.conn.execute(
            "INSERT INTO posts(pid,tid,page_no,floor,author,content,chars,"
            "images,fetched_at) VALUES(?,?,1,1,'x',?,?,'[]','2026-11-05 10:00:00')",
            (pid, tid, ct, len(ct)))
    db.conn.commit()

    # ---- ⑤ 扫描：一次多词判定 ----
    print("\n--- ⑤ 扫描 ---")
    db.add_keyword("晚点", "issue", parent="宁波地铁")
    db.add_keyword("换乘", "issue", parent="宁波地铁")
    db.add_keyword("宁波公交", "subject")
    db.add_keyword("宁波", "variant", parent="宁波地铁")
    res = db.scan_keywords()
    check(res.get("ok") is True, f"扫描成功：{res}")
    check(res.get("scanned") == 5, f"扫了 5 层（实得 {res.get('scanned')}）")

    kws = {k["word"]: k for k in db.list_keywords()}
    check(kws["宁波地铁"]["hit_tids"] == 1,
          f"「宁波地铁」命中 1 帖（实得 {kws['宁波地铁']['hit_tids']}）")
    check(kws["晚点"]["hit_tids"] == 2,
          f"「晚点」命中 2 帖（实得 {kws['晚点']['hit_tids']}）")
    check(kws["换乘"]["hit_tids"] == 1,
          f"「换乘」命中 1 帖（实得 {kws['换乘']['hit_tids']}）")
    check(kws["宁波公交"]["hit_tids"] == 1,
          f"「宁波公交」命中 1 帖（实得 {kws['宁波公交']['hit_tids']}）")

    # ---- ⑥ ★排除词是「减」的 ----
    print("\n--- ⑥ ★排除词：命中的帖从其他词里扣掉 ---")
    db.add_keyword("外地", "exclude", note="非本地的同名内容")
    db.scan_keywords()
    kws = {k["word"]: k for k in db.list_keywords()}
    check(kws["宁波"]["hit_tids"] == 2,
          f"加排除词后「宁波」从 3 帖降到 2 帖（实得 {kws['宁波']['hit_tids']}）")
    check(kws["宁波"]["hit_tids"] != 3, "排除词确实扣掉了命中")

    # ---- ⑦ 停用的词不参与扫描，但还在词表里 ----
    print("\n--- ⑦ 停用的词 ---")
    kid = kws["晚点"]["id"]
    db.set_keyword_enabled(kid, False)
    db.scan_keywords()
    kws2 = {k["word"]: k for k in db.list_keywords()}
    check("晚点" in kws2, "停用的词仍在词表里")
    check(kws2["晚点"]["hit_tids"] == 0,
          f"停用后不参与扫描（实得 {kws2['晚点']['hit_tids']}）")

    # ---- ⑧ 命中列表 ----
    print("\n--- ⑧ 命中列表 ---")
    kd = {k["word"]: k["id"] for k in db.list_keywords()}
    h = db.keyword_hits(kd["宁波公交"], limit=10)
    check(h["total"] == 1, f"「宁波公交」命中帖数 = 1（实得 {h['total']}）")
    check(h["items"] and h["items"][0]["tid"] == 2002,
          f"命中的是 tid=2002（实得 {h['items'][0]['tid'] if h['items'] else None}）")

    # ---- ⑨ 删词 ----
    print("\n--- ⑨ 删词 ---")
    n = db.delete_keyword(kd["换乘"])
    check(n == 1, "删词返回 1")
    left = db.conn.execute(
        "SELECT COUNT(*) FROM keyword_hits WHERE keyword_id=?", (kd["换乘"],)).fetchone()[0]
    check(left == 0, "删词后它的命中记录也清了")

    # ---- ⑩ ★反向验证 ----
    print("\n--- ⑩ ★反向验证 ---")
    # 排除词逻辑若去掉 → ⑥ 的断言必须报红：这里直接证明「被排除的帖」
    #   确实本来会命中普通词（否则排除逻辑就是空转）
    db.conn.execute("DELETE FROM keywords")
    db.conn.execute("DELETE FROM keyword_hits")
    db.add_keyword("宁波", "variant")
    db.add_keyword("外地", "exclude")
    db.scan_keywords()
    kws3 = {k["word"]: k for k in db.list_keywords()}
    check(kws3["宁波"]["hit_tids"] == 2,
          "排除后剩 2 帖（另 1 帖被排除词扣掉）")
    # 把 exclude 词改成普通词 → 命中数必须变回 3 → 证明扣减逻辑真的在起作用
    db.conn.execute("UPDATE keywords SET kind='variant' WHERE word='外地'")
    db.scan_keywords()
    kws4 = {k["word"]: k for k in db.list_keywords()}
    check(kws4["宁波"]["hit_tids"] == 3,
          f"排除词改成普通词后命中变 2（实得 {kws4['宁波']['hit_tids']}）"
          "→ 证明第 ⑥ 步的扣减不是空转")

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
