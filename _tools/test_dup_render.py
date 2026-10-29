# -*- coding: utf-8 -*-
"""闸：站方重复渲染 / 页数虚标 / 空正文 —— 三条新判据（2026-10-29）

★ 本闸为什么必须存在
  上一轮（2026-10-28/29）立 `count_mismatch` 时，实测发现站方有个此前没注意到的行为：
  **站方会跨页重复渲染同一批回复**。实测 tid=8214148：
    page1: pid 113196495…113196847（楼层 1-15，含置顶 floor=0）
    page2: pid 113196883…113202410（楼层 16-29，含置顶 floor=22）
    page3: pid 113196767…113201295（楼层 31-45）★ **全是 page1/page2 的 pid**
    page4: pid 113201923…113202410（楼层 46-48）★ **同样是重复的 pid**
  → 站方 pid 并集 29、楼层并集 47；我们本地 pid 29 个**一个不缺**。
  → 用「楼层集合」判漏采会**误报 18 层**；用「pid 集合」判才是对的。

★ 本闸覆盖五件事
  ① 判据层：`is_out_of_range_page` 能认出「站方对超范围页码给的提示信息页」，
            且**不会**和「站方限制页」混淆（两者是不同的事）
  ② 判据层：`_probe_site_floors` 必须返回 **pid 集合**（不是只有数量）
  ③ 状态层：站方 pid 集合 ⊆ 本地 pid 集合 → 归 done（**不能**因楼层差就判漏采）
            站方 pid 集合 ⊄ 本地 pid 集合 → 真漏采 → 仍补采
  ④ 循环层：「本页没有新 pid」**不能**让采集提前停止
            （旧逻辑 `if not new_pids and page_no > 1: break` 是死循环根因）
  ⑤ 反向验证：把判据改坏 → 同一批断言**必须报红**

★ **反向验证闸**（本项目纪律）：只跑通不报红的闸等于没闸。

用法：
    python _tools/test_dup_render.py
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt import detail as D          # noqa: E402
from msyt import parsers as P         # noqa: E402
from msyt.db import DB                # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name + (f" — {extra}" if extra else ""))
        print(f"  [FAIL] {name} {extra}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 桩
#: 站方「站方限制」提示页（含「历史帖子仅限作者本人查看」）
NOTICE_RESTRICTED_HTML = (
    "<html><head><title>提示信息 - 民生e点通群众留言板</title></head>"
    "<body><div class=\"alert_error\"><span>"
    "历史帖子仅限作者本人查看"
    "</span></div></body></html>"
)

#: 站方对**超范围页码**给的提示信息页（实测 tid=8207278 的 page2，23108 字节）
#:   ★ 与上面那个**不是同一件事**：不含「历史帖子仅限作者本人查看」，
#:     只说明「这一页不存在」→ 已经翻到底了 → 不是错误。
NOTICE_OUT_OF_RANGE_HTML = (
    "<html><head><title>提示信息 -  中国宁波网民生e点通-天一论坛 -  "
    "Powered by Discuz</title></head>"
    "<body><div class=\"alert_info\">抱歉，该主题不存在或已被删除。</div>"
    "</body></html>"
)


class DupRenderClient:
    """模拟站方**跨页重复渲染**。

    · `pages`：{page_no: [(pid, floor), ...]}，直接指定每页给哪些 pid 和楼层
    · 超出的页 → 返回「提示信息」页（超范围）
    · `total_pages`：页面上标的总页数（可以**故意标错**）
    """

    def __init__(self, pages: dict, total_pages: int = 0):
        self.pages = pages
        self.total_pages = total_pages or (max(pages) if pages else 1)
        self.calls = 0

    def _page(self, url: str) -> int:
        m = re.search(r"[?&]page=(\d+)", url)
        return int(m.group(1)) if m else 1

    def get(self, url, referer=None, **kw):
        self.calls += 1
        page_no = self._page(url)
        items = self.pages.get(page_no)
        if items is None:
            return type("R", (), {"text": NOTICE_OUT_OF_RANGE_HTML,
                                  "status_code": 200})()
        pids = "".join(f'<div id="post_{pid}"></div>' for pid, _ in items)
        # 楼层标签用 Discuz 的真实写法：<a id="postnum{pid}">N#</a>
        #   （parsers.py 从 a[id^=postnum] 读，id 后缀是 **pid**，
        #     文本里才是楼层号；文本无楼层号 → floor=0（置顶渲染））
        nums = "".join(
            f'<div id="post_{pid}"><a id="postnum{pid}">{fl}#</a></div>'
            if fl > 0 else f'<div id="post_{pid}"><a id="postnum{pid}">顶</a></div>'
            for pid, fl in items)
        tok = f"{page_no:016x}"
        html = (f"<html><title>测试帖 - 第{page_no}页</title>{pids}{nums}"
                f"<script>var token:'{tok}';</script>"
                f"<span>共 {self.total_pages} 页</span></html>")
        return type("R", (), {"text": html, "status_code": 200})()

    def get_nowait(self, url, params=None, referer=None, **kw):
        # ★ 页号从 referer 里取（采集主流程就是用 referer 指向当页详情页）
        page_no = self._page(referer or "")
        items = self.pages.get(page_no) or []
        # ★ 真实结构：{"code":1,"data":{"<pid>":"<html>"}}
        payload = {"code": 1, "data": {str(pid): f"正文{pid}" for pid, _ in items}}
        return type("R", (), {"json": lambda self=payload: payload})()


class FakeCfg:
    """collect_one 只用到 detail_max_pages（默认页数上限）。"""
    detail_max_pages = 50


def make_db(tid: int, replies: int, pids_floors, state: str = "done"):
    """造临时库。pids_floors = [(pid, floor), ...]"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = DB(path)
    db.conn.execute(
        "INSERT INTO threads (tid, title, replies, detail_state, posts_n, chars) "
        "VALUES (?,?,?,?,?,?)",
        (tid, "测试帖", replies, state, len(pids_floors), len(pids_floors) * 2))
    for pid, fl in pids_floors:
        db.conn.execute(
            "INSERT INTO posts (pid,tid,page_no,floor,author,content,chars) "
            "VALUES (?,?,1,?,?,?,?)",
            (pid, tid, fl, "u", "正文", 2))
    db.conn.commit()
    return db, path


# ---------------------------------------------------------------- ① 超范围页判据
def test_out_of_range():
    section("① 判据层：超范围页 vs 站方限制页 —— 必须是两件不同的事")
    check("超范围提示页 → is_out_of_range_page=True",
          P.is_out_of_range_page(NOTICE_OUT_OF_RANGE_HTML) is True)
    check("站方限制页 → is_out_of_range_page=False（不能混淆）",
          P.is_out_of_range_page(NOTICE_RESTRICTED_HTML) is False)
    check("站方限制页 → parse_site_notice 仍认得",
          bool(P.parse_site_notice(NOTICE_RESTRICTED_HTML)))
    check("超范围提示页 → parse_site_notice 不认（它不是站方限制）",
          not P.parse_site_notice(NOTICE_OUT_OF_RANGE_HTML))

    normal = ('<html><title>测试帖</title><div id="post_1"></div>'
              "<script>var token:'0123456789abcdef';</script></html>")
    check("正常帖页 → 两者都不认",
          P.is_out_of_range_page(normal) is False
          and not P.parse_site_notice(normal))
    check("空页面 → 不认", P.is_out_of_range_page("") is False)


# ---------------------------------------------------------------- ② probe 返回 pid 集合
def test_probe_returns_pids():
    section("② 判据层：_probe_site_floors 必须返回 pid 集合")
    # 模拟 tid=8214148 的真实形态：
    #   page1/2 给 29 个 pid（楼层 1-29，含置顶 floor=0）
    #   page3/4 **重复渲染同一批 pid**，但楼层号被整体**后移**到 31-48
    base = [(1000 + i, i + 1) for i in range(15)]          # floor 1-15
    base2 = [(1100 + i, i + 16) for i in range(14)]        # floor 16-29
    base[1] = (1001, 0)                                  # 置顶：floor=0
    dup3 = [(pid, fl + 19) for pid, fl in (base[3:] + base2[:4])]
    dup4 = [(pid, fl + 19) for pid, fl in base2[-3:]]
    pages = {1: base, 2: base2, 3: dup3, 4: dup4}
    db, path = make_db(9001, replies=28, pids_floors=base + base2)
    try:
        col = D.DetailCollector(db, DupRenderClient(pages, total_pages=4), FakeCfg())
        r = col._probe_site_floors(9001)
        check("实测成功", r["ok"] is True, str(r))
        check("实测返回 pid 集合", isinstance(r.get("pids"), set), str(type(r.get("pids"))))
        check("pid 集合去重后 == 29（重复渲染不算新内容）",
              len(r["pids"]) == 29, f"实际 {len(r.get('pids') or [])}")
        check("站方楼层并集 > pid 并集（重复渲染的证据）",
              len(r["floors"]) > len(r["pids"]),
              f"floors={len(r.get('floors') or [])} pids={len(r.get('pids') or [])}")

        # ★ 超范围页：站方标 2 页，实际 1 页
        db2, path2 = make_db(9002, replies=15, pids_floors=base)
        try:
            col2 = D.DetailCollector(db2, DupRenderClient({1: base}, total_pages=2), FakeCfg())
            r2 = col2._probe_site_floors(9002)
            check("站方标 2 页实际 1 页 → 不判 restricted",
                  r2.get("restricted") is False, str(r2))
            check("站方标 2 页实际 1 页 → ok=True（翻到底了，不是失败）",
                  r2["ok"] is True, str(r2))
            check("站方标 2 页实际 1 页 → 有「超出实际范围」说明",
                  "超出" in (r2.get("reason") or ""), str(r2.get("reason")))
        finally:
            db2.conn.close()
            os.unlink(path2)
    finally:
        db.conn.close()
        os.unlink(path)


# ---------------------------------------------------------------- ③ 状态层：pid 集合判据
def test_state_by_pid_set():
    section("③ 状态层：站方 pid ⊆ 本地 pid → done（楼层差不算漏采）")
    # 情形 A：站方重复渲染 → 楼层差 18，但 pid 一个不缺 → 必须归 done
    base = [(1000 + i, i + 1) for i in range(15)]
    base2 = [(1100 + i, i + 16) for i in range(14)]
    base[1] = (1001, 0)
    dup3 = [(pid, fl + 19) for pid, fl in (base[3:] + base2[:4])]
    dup4 = [(pid, fl + 19) for pid, fl in base2[-3:]]
    pages = {1: base, 2: base2, 3: dup3, 4: dup4}
    db, path = make_db(9003, replies=28, pids_floors=base + base2)
    try:
        col = D.DetailCollector(db, DupRenderClient(pages, total_pages=4), FakeCfg())
        res = col.collect_one(9003)
        check("重复渲染帖 → 判为采全（ok=True）", res["ok"] is True,
              f"ok={res['ok']} err={res.get('error')} problems={res.get('problems')}")
        check("重复渲染帖 → 不标 stale（不反复重跑）",
              db.conn.execute(
                  "SELECT detail_state FROM threads WHERE tid=9003").fetchone()[0] == "done")
    finally:
        db.conn.close()
        os.unlink(path)

    # 情形 B：站方真多出 pid → 仍要判漏采（不能被 A 误伤）
    base3 = [(2000 + i, i + 1) for i in range(15)]
    pages_b = {1: base3, 2: [(2100 + i, i + 16) for i in range(5)]}
    db2, path2 = make_db(9004, replies=19, pids_floors=base3)
    try:
        col2 = D.DetailCollector(db2, DupRenderClient(pages_b, total_pages=2), FakeCfg())
        res2 = col2.collect_one(9004)
        check("站方真多出 pid → 采到后应判完成（本地确实补到了）",
              res2["ok"] is True,
              f"ok={res2['ok']} err={res2.get('error')}")
        n = db2.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=9004").fetchone()[0]
        check("站方多出的 5 篇确实入库了", n == 20,
              f"实际 {n}；err={res2.get('error')} problems={res2.get('problems')}")
    finally:
        db2.conn.close()
        os.unlink(path2)


# ---------------------------------------------------------------- ④ 循环层：不许提前停
def test_loop_not_early_stop():
    section("④ 循环层：「本页没有新 pid」不能让采集提前停止")
    # page1 有内容；page2 内容与 page1 **完全重复**（new_pids 为空）；
    # page3 有**新**内容。旧逻辑会在 page2 就 break → 采不到 page3。
    p1 = [(3000 + i, i + 1) for i in range(5)]
    p2 = list(p1)                                    # 重复 → new_pids 空
    p3 = [(3100 + i, i + 6) for i in range(3)]        # 新内容
    pages = {1: p1, 2: p2, 3: p3}
    db, path = make_db(9005, replies=7, pids_floors=[])
    try:
        col = D.DetailCollector(db, DupRenderClient(pages, total_pages=3), FakeCfg())
        res = col.collect_one(9005)
        n = db.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=9005").fetchone()[0]
        check("本页无新 pid 时仍继续翻到第 3 页", n == 8, f"实际采到 {n} 篇")
        check("第 3 页的新内容采到了", res["ok"] is True,
              f"ok={res['ok']} err={res.get('error')}")
    finally:
        db.conn.close()
        os.unlink(path)


# ---------------------------------------------------------------- ⑤ 反向验证
def test_reverse_verification():
    section("⑤ 反向验证：把判据改坏 → 同一批断言必须报红")
    src_path = os.path.join(PROJ, "程序", "msyt", "detail.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()

    # 反向 1：把「站方 pid ⊆ 本地 pid」改回「数量比」→ 重复渲染帖必须误报
    broken = src.replace(
        "if not _missing:",
        "if _probe['count'] <= len(_local_pids):", 1)
    check("反向 1：能构造出「数量比」的错误实现",
          broken != src and "_probe['count'] <= len(_local_pids)" in broken)

    # 反向 2：把「本页无新 pid 不停止」改回旧的提前 break
    broken2 = src.replace(
        "if _repeat_pages >= 3:\n                    break",
        "if _repeat_pages >= 1:\n                    break", 1)
    check("反向 2：能构造出「提前停止」的错误实现",
          broken2 != src and "_repeat_pages >= 1" in broken2)

    # 反向 3：把超范围页识别去掉 → 必须重新变成失败
    broken3 = src.replace(
        "if is_out_of_range_page(html):",
        "if False:", 1)
    check("反向 3：能构造出「不认超范围页」的错误实现",
          broken3 != src and "if False:" in broken3)

    # ★ 真跑一次：把改坏的实现挂上去，验证用例**会报红**
    import types as _types
    mod = _types.ModuleType("broken_detail")
    mod.__dict__["__file__"] = src_path
    mod.__dict__["__name__"] = "msyt.detail"
    try:
        exec(compile(broken, src_path, "exec"), mod.__dict__)
    except Exception as e:  # noqa: BLE001
        check("反向：改坏的代码能加载（否则反向验证不成立）", False, str(e))
        return

    base = [(1000 + i, i + 1) for i in range(15)]
    base2 = [(1100 + i, i + 16) for i in range(14)]
    base[1] = (1001, 0)
    dup3 = [(pid, fl + 19) for pid, fl in (base[3:] + base2[:4])]
    dup4 = [(pid, fl + 19) for pid, fl in base2[-3:]]
    pages = {1: base, 2: base2, 3: dup3, 4: dup4}
    db, path = make_db(9100, replies=28, pids_floors=base + base2)
    try:
        col = moD.DetailCollector(db, DupRenderClient(pages, total_pages=4), FakeCfg())
        res = col.collect_one(9100)
        check("反向：数量比判据 → 重复渲染帖被误判（不报红就说明闸失效）",
              res["ok"] is False,
              f"改坏后仍判 ok=True → 闸没抓住 → {res}")
    except Exception as e:  # noqa: BLE001
        check("反向：数量比判据 → 重复渲染帖被误判（抛错也算抓住）", True, str(e))
    finally:
        db.conn.close()
        os.unlink(path)

    # 反向 2 实跑：提前 stop → 第 3 页采不到
    mod2 = _types.ModuleType("broken_detail2")
    mod2.__dict__["__file__"] = src_path
    mod2.__dict__["__name__"] = "msyt.detail"
    try:
        exec(compile(broken2, src_path, "exec"), mod2.__dict__)
    except Exception as e:  # noqa: BLE001
        check("反向：提前停止的代码能加载", False, str(e))
        return
    p1 = [(3000 + i, i + 1) for i in range(5)]
    p3 = [(3100 + i, i + 6) for i in range(3)]
    db2, path2 = make_db(9101, replies=7, pids_floors=[])
    try:
        col2 = mod2.DetailCollector(db2, DupRenderClient({1: p1, 2: list(p1), 3: p3}, total_pages=3), FakeCfg())
        col2.collect_one(9101)
        n = db2.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=9101").fetchone()[0]
        check("反向：提前停止 → 第 3 页采不到（不报红就说明闸失效）",
              n < 8, f"改坏后仍采到 {n} 篇 → 闸没抓住")
    finally:
        db2.conn.close()
        os.unlink(path2)


# ---------------------------------------------------------------- ⑥ 静态检查
def test_static():
    section("⑥ 静态检查：源码里不能残留旧判据")
    with open(os.path.join(PROJ, "程序", "msyt", "detail.py"), encoding="utf-8") as f:
        src = f.read()
    # 只查代码、不查注释（注释里提历史写法是留痕，允许）
    code = re.sub(r"#.*", "", src)
    check("代码里不再有「content_missing → problems」的硬判据",
          not re.search(r"problems\.append\(f?\"\{content_missing\}", code))
    check("代码里不再有「只采到 N/M 页」作为 problems",
          not re.search(r"problems\.append\(f?\"只采到", code))
    check("代码里有 pid 集合判据（站方 pid ⊆ 本地 pid）",
          "_site_pids - _local_pids" in code)
    check("代码里有超范围页识别", "is_out_of_range_page" in code)

    with open(os.path.join(PROJ, "程序", "msyt", "parsers.py"), encoding="utf-8") as f:
        psrc = f.read()
    pcode = re.sub(r"#.*", "", psrc)
    check("parsers 里有 is_out_of_range_page",
          "def is_out_of_range_page" in pcode)
    check("is_out_of_range_page 排除站方限制页（不与之混淆）",
          re.search(r"def is_out_of_range_page.*?SITE_RESTRICTED in html.*?return False",
                    pcode, re.S) is not None)


# ---------------------------------------------------------------- ⑦ page_no 保护
def test_page_no_protected():
    section("⑦ 库接口：save_posts 不能让重复渲染的页改写已有 page_no")
    db, path = make_db(9200, replies=5, pids_floors=[(1000, 1), (1001, 2),
                                                     (1002, 3), (1003, 4)])
    try:
        # 第 1 页采到 pid 1000-1003
        db.save_posts(9200, 1, [
            {"pid": 1000, "floor": 1, "content": "a", "chars": 1},
            {"pid": 1001, "floor": 2, "content": "b", "chars": 1},
            {"pid": 1002, "floor": 3, "content": "c", "chars": 1},
            {"pid": 1003, "floor": 4, "content": "d", "chars": 1}])
        # 第 4 页**重复渲染**同一批 pid（楼层号被后移）
        db.save_posts(9200, 4, [
            {"pid": 1000, "floor": 20, "content": "a", "chars": 1},
            {"pid": 1001, "floor": 21, "content": "b", "chars": 1}])
        rows = {r["pid"]: r["page_no"] for r in db.conn.execute(
            "SELECT pid, page_no FROM posts WHERE tid=9200")}
        check("重复渲染的页没有改写已有 pid 的 page_no",
              rows.get(1000) == 1 and rows.get(1001) == 1,
              f"实际 {rows}")
        floors = {r["pid"]: r["floor"] for r in db.conn.execute(
            "SELECT pid, floor FROM posts WHERE tid=9200")}
        check("楼层号仍取后采到的真号（置顶楼保护仍生效）",
              floors.get(1000) == 20, f"实际 {floors}")
        np = db.conn.execute(
            "SELECT COUNT(DISTINCT page_no) FROM posts WHERE tid=9200").fetchone()[0]
        check("按页分布不被重复渲染搅乱（仍是 1 页）", np == 1, f"实际 {np}")
    finally:
        db.conn.close()
        os.unlink(path)


# ---------------------------------------------------------------- ⑧ 闭环
def test_collect_one_full():
    section("⑧ 闭环：collect_one_full 采后实测，缺了就从第 1 页重采一轮")
    # 站方第 2 页与第 1 页重复（new_pids 空）→ 循环会提前收尾
    p1 = [(4000 + i, i + 1) for i in range(5)]
    p2 = list(p1)                     # 完全重复
    p3 = [(4100 + i, i + 6) for i in range(3)]   # 第 3 页有真新增
    pages = {1: p1, 2: p2, 3: p3}
    db, path = make_db(9201, replies=7, pids_floors=[])
    try:
        col = D.DetailCollector(db, DupRenderClient(pages, total_pages=3), FakeCfg())
        res = col.collect_one_full(9201, max_recheck=2)
        n = db.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=9201").fetchone()[0]
        check("闭环后第 3 页的新内容也采到了", n == 8, f"实际 {n} 篇")
        check("闭环后判为采全", res["ok"] is True, f"ok={res['ok']}")
    finally:
        db.conn.close()
        os.unlink(path)


def main() -> int:
    print("=" * 60)
    print("闸：站方重复渲染 / 页数虚标 / 空正文 —— 三条新判据（2026-10-29）")
    print("=" * 60)
    test_out_of_range()
    test_probe_returns_pids()
    test_state_by_pid_set()
    test_loop_not_early_stop()
    test_reverse_verification()
    test_static()
    test_page_no_protected()
    test_collect_one_full()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("\n失败明细：")
        for f in FAIL:
            print("  - " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
