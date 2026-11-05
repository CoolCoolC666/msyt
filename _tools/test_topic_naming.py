# -*- coding: utf-8 -*-
"""闸：选题/任务命名规范（优化报告 P2-3）。

★ 跑在**临时库**上（不碰真库）。

★ 要守住的性质
  ① 规范名 = 监测范围 + 关键词 + 服务对象 + 报告规格（课件口径）
  ② **空要素不写占位** —— 不出现「关键词：—」这种看着完整、实则没信息的写法
  ③ 关键词**认全分隔符**（逗号/顿号/斜杠/空格），去重且保序
  ④ 四要素**入库**、规范名**现算**（不另存一列，免得名与实不符）
  ⑤ 历史任务**可检索**：按编号 / 自定义标题 / 四要素 / 规范名 都能搜到
  ⑥ 老任务（没有四要素的老行）**不受影响**：标题照旧显示，不多出一堆「｜」

★ 必须有**真反向验证**：把实现改坏后，同一批断言必须报红。

用法：python _tools/test_topic_naming.py
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt import topic  # noqa: E402
from msyt.assist import AssistService  # noqa: E402
from msyt.db import DB  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def expect_red(fn) -> bool:
    """fn 里的断言**必须报红**。返回 False = 断言不够狠（改坏实现它还绿）。"""
    try:
        fn()
    except AssertionError:
        return True
    return False


class _Hub:
    """Hub 的最小替身：create() 只会 info() 一句。"""

    def info(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def main() -> int:
    print("=" * 62)
    print("闸：选题/任务命名规范")
    print("=" * 62)

    # ---- ① 规范名 ----
    print("\n--- ① 规范名生成 ---")
    t = topic.build_title("宁波公交·换乘", "晚点,拥挤,换乘", "市交通运输局", "周报·3000字")
    check(t == "监测范围：宁波公交·换乘｜关键词：晚点/拥挤/换乘｜服务对象：市交通运输局｜报告规格：周报·3000字",
          f"四要素齐全 → {t}")
    check(topic.build_title("", "晚点", "", "") == "关键词：晚点",
          "只有关键词 → 只写关键词那一段")
    check(topic.build_title() == "", "全空 → 空字符串（不编名字）")
    check("—" not in topic.build_title("", "", "", ""), "空要素不写占位")
    check(topic.build_title("范围", "", "对象", "") == "监测范围：范围｜服务对象：对象",
          "中间空要素跳过（不留空段）")

    # ---- ② 关键词拆分 ----
    print("\n--- ② 关键词拆分 ---")
    check(topic.split_keywords("晚点，拥挤、换乘") == ["晚点", "拥挤", "换乘"],
          f"逗号/顿号都认：{topic.split_keywords('晚点，拥挤、换乘')}")
    check(topic.split_keywords("晚点 拥挤") == ["晚点", "拥挤"], "空格也认")
    check(topic.split_keywords("晚点 晚点 拥挤") == ["晚点", "拥挤"], "去重且保序")
    check(topic.split_keywords("") == [], "空串 → 空表")
    check(len(topic.split_keywords("a b c d e f g h")) == 8, "8 个词都拆出来（不截断入库）")
    check(topic.build_title("", "a b c d e f g h", "", "").count("/") == 5,
          "规范名里最多列 6 个词（多的截断显示，但入库不丢）")
    check(topic.norm_keywords("晚点，拥挤、换乘") == "晚点/拥挤/换乘",
          f"入库串规范化：{topic.norm_keywords('晚点，拥挤、换乘')}")

    # ---- ③ 建任务：四要素入库 ----
    print("\n--- ③ 建任务（四要素入库） ---")
    tmp = tempfile.mkdtemp(prefix="msyt_topic_")
    db = DB(os.path.join(tmp, "t.db"))
    db.conn.execute("INSERT INTO threads(tid,title,dept_id,dept_name,detail_state) "
                    "VALUES(9001,'测试帖','726','测试部门','pending')")
    db.conn.execute("INSERT INTO threads(tid,title,dept_id,dept_name,detail_state) "
                    "VALUES(9002,'测试帖2','726','测试部门','pending')")
    db.conn.commit()
    svc = AssistService(db, _Hub())
    task = svc.create("本周加急", ["726"], 10, "admin",
                      scope="宁波公交·换乘", keywords="晚点,拥挤,换乘",
                      audience="市交通运输局", spec="周报·3000字")
    row = db.conn.execute("SELECT * FROM assist_tasks WHERE code=?",
                          (task["code"],)).fetchone()
    check(row["scope"] == "宁波公交·换乘", f"监测范围入库：{row['scope']}")
    check(row["keywords"] == "晚点/拥挤/换乘", f"关键词入库（规范化）：{row['keywords']}")
    check(row["audience"] == "市交通运输局", f"服务对象入库：{row['audience']}")
    check(row["spec"] == "周报·3000字", f"报告规格入库：{row['spec']}")
    check(row["title"] == "本周加急", "自定义标题照旧存 title")
    expect = ("本周加急｜监测范围：宁波公交·换乘｜关键词：晚点/拥挤/换乘"
              "｜服务对象：市交通运输局｜报告规格：周报·3000字")
    check(task["topic_title"] == expect, f"规范名（自定义 + 四要素）：{task['topic_title']}")

    # ---- ④ 检索 ----
    print("\n--- ④ 历史任务可检索 ---")
    for q, why in [("晚点", "按关键词"), ("拥挤", "按关键词"),
                   ("市交通", "按服务对象"), ("换乘", "按监测范围"),
                   ("周报", "按报告规格"), ("加急", "按自定义标题"),
                   (task["code"], "按编号")]:
        got = svc.tasks(q)
        check(len(got) == 1, f"{why} 搜「{q}」→ 命中 {len(got)} 条")
    check(len(svc.tasks("")) == 1, "空查询 → 全部")
    check(len(svc.tasks("查无此物")) == 0, "搜不到 → 0 条")
    check(len(svc.tasks("晚点")) == 1 and svc.tasks("晚点")[0]["code"] == task["code"],
          "命中的是对的那条")

    # ---- ⑤ 老任务不受影响 ----
    print("\n--- ⑤ 老任务（无四要素） ---")
    db.conn.execute("INSERT INTO assist_tasks(code,title,dept_ids,total,done,state) "
                    "VALUES('A99','老任务标题','726',3,0,'open')")
    db.conn.commit()
    old = svc.get("A99")
    check(old["topic_title"] == "老任务标题",
          f"老任务标题照旧显示：{old['topic_title']!r}")
    check("｜" not in old["topic_title"], "老任务不多出一堆分隔符")
    check(len(svc.tasks("老任务")) == 1, "老任务也能搜到")

    # ---- ⑥ ★反向验证（改坏实现 → 同一批断言必须报红） ----
    print("\n--- ⑥ ★反向验证 ---")

    def assert_scope_in_title():
        assert "监测范围" in topic.display_title("t", "宁波公交", "晚点", "市局", "周报")

    orig_build = topic.build_title
    topic.build_title = lambda *a, **k: ""
    check(expect_red(assert_scope_in_title),
          "反向验证①：打坏 build_title → 「含监测范围」断言报红")
    topic.build_title = orig_build
    check(not expect_red(assert_scope_in_title), "反向验证①收尾：恢复后断言通过")

    def assert_search_hit():
        assert len(svc.tasks("晚点")) == 1

    orig_hay = topic.search_haystack
    topic.search_haystack = lambda t: ""
    check(expect_red(assert_search_hit),
          "反向验证②：打坏 search_haystack → 「按关键词搜得到」断言报红")
    topic.search_haystack = orig_hay
    check(not expect_red(assert_search_hit), "反向验证②收尾：恢复后断言通过")

    def assert_keywords_stored():
        r = db.conn.execute("SELECT keywords FROM assist_tasks WHERE code=?",
                            (task["code"],)).fetchone()
        assert r["keywords"] == "晚点/拥挤/换乘"

    orig_norm = topic.norm_keywords
    topic.norm_keywords = lambda raw: ""
    db.conn.execute("UPDATE assist_tasks SET keywords='' WHERE code=?",
                    (task["code"],))
    check(expect_red(assert_keywords_stored),
          "反向验证③：打坏 norm_keywords → 「关键词入库」断言报红")
    topic.norm_keywords = orig_norm
    db.conn.execute("UPDATE assist_tasks SET keywords='晚点/拥挤/换乘' WHERE code=?",
                    (task["code"],))
    check(not expect_red(assert_keywords_stored), "反向验证③收尾：恢复后断言通过")

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
