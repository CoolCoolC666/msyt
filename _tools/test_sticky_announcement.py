# -*- coding: utf-8 -*-
"""验收：置顶帖与全局公告帖**必须能被采到**（2026-10-24 用户反馈「置顶的帖会漏采」）。

★ 用户原话
  「https://bbs.cnnb.com.cn/forum.php?mod=viewthread&tid=6177045&extra=page%3D1
    类似于置顶，会有落采的情况，需要补采。注意，这个界面内部有6页……
    另外包括 …tid=7342780，…tid=7342697 需要收录」

★ 真因（实测坐实，不是猜测）
  ① 列表解析器只扫 `tbody[id^=normalthread_]` → **置顶帖**（`tbody[id^=stickthread_]`）从来没被看过
  ② 全局公告在 `#msbgt` 里，是**裸链接**，压根不是帖子行 → 旧解析器连看都没看
     → 实测库里 tid=6177045 / 7342780 / 7342697 / 8333204 / 8333106 / 8228673 **全部为 0 条**
  ③ 详情页 pid 清单只认 `postmsg_`，而站方**偶尔不给某层生成 postmsg_ 容器**
     → 少一个 pid → 「pid 集合精确相等」校验失败 → 整帖卡在半截（6177045 卡在第 3 页）
  ④ 一帖页数上限 50 页，盖楼活动置顶帖有 81 页 → 卡在一半，**且断点续传也接不上**
     （续传从 pages_done+1 起算，一样落在 50 之外）

★ 顺带排除的一个伪线索
  用户提到的 URL 形态 `…&extra=page%3D1&page=X`：**不是病根**。
  实测不带 extra 也能正确翻满 6 页、第 7 页才回绕 —— 分页 URL 形态无罪。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = r"【项目根】"
PKG = os.path.join(ROOT, "程序")
sys.path.insert(0, PKG)

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


from msyt.parsers import (parse_announcements, parse_list_page,   # noqa: E402
                          parse_thread_page)

# ======================================================================
# ① 列表页样本
#    结构照抄站方真实页面（2026-10-24 实测）：
#      · 普通帖   tbody[id^=normalthread_]
#      · 置顶帖   tbody[id^=stickthread_]        ← 旧解析器漏掉的
#      · 全局公告 #msbgt 里的裸链接              ← 旧解析器漏掉的
#      · 6177045 **既**是置顶帖 **又**出现在公告区
LIST_HTML = """
<html><body>
<div id="msbgt" class="flms"><div class="wrapper1">
<a href="forum.php?mod=viewthread&amp;tid=6177045" class="">中国宁波网民生e点通群众留言板（天一论坛问政频道）管理规定</a>
<a href="forum.php?mod=viewthread&amp;tid=7342697" class="">民生e点通和天一论坛申请删帖流程以及操作办法</a>
<a href="forum.php?mod=viewthread&amp;tid=7342780" class="">天一论坛总版规(含各级用户的编辑帖子时限)</a>
</div></div>
<tbody id="normalthread_8344484"><tr><td class="icn"></td>
<th class="new"><font color="#000">[未受理]</font> [投诉]
  <span class="sortwrap"><em><a href="forum.php?mod=forumdisplay&amp;fid=1176&amp;sortid=23">我要问政</a></em></span>
  <span class="typewrap"><em><a href="forum.php?mod=forumdisplay&amp;fid=1176&amp;filter=typeid&amp;typeid=502">市住建局</a></em></span>
  <a class="s xst" href="forum.php?mod=viewthread&amp;tid=8344484&amp;extra=page%3D1">某小区停车难</a>
</th>
<td class="by"><cite><a href="home.php?mod=space&amp;uid=123">网友A</a></cite><em><span>2026-10-24 09:00</span></em></td>
<td class="num"><a class="xi2" href="#">3</a><em>120</em></td>
</tr></tbody>
<tbody id="normalthread_8344485"><tr><td class="icn"></td>
<th class="common"><a class="s xst" href="forum.php?mod=viewthread&amp;tid=8344485&amp;extra=page%3D1">咨询公交线路</a></th>
<td class="by"><cite><a href="home.php?mod=space&amp;uid=456">网友B</a></cite><em><span>2026-10-23 20:11</span></em></td>
<td class="num"><a class="xi2" href="#">1</a><em>88</em></td>
</tr></tbody>
<tbody id="stickthread_8333204"><tr><td class="icn"></td>
<th class="common"><span class="sortwrap"></span><span class="typewrap"></span>
  <a class="s xst" href="forum.php?mod=viewthread&amp;tid=8333204&amp;extra=page%3D1">25周年盖楼活动开启</a>
</th>
<td class="by"><cite><a href="home.php?mod=space&amp;uid=4867">天一论坛</a></cite><em><span>2026-9-2 14:49</span></em></td>
<td class="num"><a class="xi2" href="#">1206</a><em>6310308</em></td>
</tr></tbody>
<tbody id="stickthread_6177045"><tr><td class="icn"></td>
<th class="common"><span class="sortwrap"></span>
  <span class="typewrap"><em><a href="forum.php?mod=forumdisplay&amp;fid=1176&amp;filter=typeid&amp;typeid=972">宁波日报报业集团</a></em></span>
  <a class="s xst" href="forum.php?mod=viewthread&amp;tid=6177045&amp;extra=page%3D1">必读公告：民生e点通管理规定</a>
</th>
<td class="by"><cite><a href="home.php?mod=space&amp;uid=4867">宁波民生e点通</a></cite><em><span>2013-8-30 11:54</span></em></td>
<td class="num"><a class="xi2" href="#">78</a><em>6048241</em></td>
</tr></tbody>
</body></html>
"""

print("### ① 列表页：置顶帖 + 全局公告")
rows = parse_list_page(LIST_HTML, "https://bbs.cnnb.com.cn/forum.php?mod=forumdisplay&fid=1176")
by = {r.tid: r for r in rows}
check("普通帖被解析", 8344484 in by and 8344485 in by, f"共 {len(rows)} 行")
check("置顶帖 8333204 被解析（旧实现漏）", 8333204 in by)
check("置顶帖 6177045 被解析", 6177045 in by)
check("公告帖 7342697 被解析（旧实现漏）", 7342697 in by)
check("公告帖 7342780 被解析", 7342780 in by)
check("公告帖没有类别/部门是正常的（站方事实）",
      by[7342780].category == "" and by[7342780].dept_id == "",
      f"category={by[7342780].category!r} dept={by[7342780].dept_id!r}")
check("公告帖有标题和绝对地址",
      by[7342780].title.startswith("天一论坛总版规")
      and by[7342780].url == "https://bbs.cnnb.com.cn/forum.php?mod=viewthread&tid=7342780",
      by[7342780].url)
check("同一 tid 既置顶又公告时**只出现一次**",
      len([r for r in rows if r.tid == 6177045]) == 1)
check("重合时取**帖行**的元数据（作者/回复数不能丢）",
      by[6177045].author == "宁波民生e点通" and by[6177045].replies == 78
      and by[6177045].dept_name == "宁波日报报业集团",
      f"author={by[6177045].author!r} replies={by[6177045].replies} "
      f"dept={by[6177045].dept_name!r}")
check("置顶帖的作者/回复数/发帖时间被读到",
      by[8333204].author == "天一论坛" and by[8333204].replies == 1206
      and by[8333204].pub_time == "2026-9-2 14:49",
      f"{by[8333204].author!r} {by[8333204].replies} {by[8333204].pub_time!r}")
check("列表页没有 #msbgt 时不报错",
      len(parse_list_page("<html><body><div>x</div></body></html>")) == 0)

print()
print("### ② 详情页：pid 清单以 post_ 为准（站方偶尔不给某层生成 postmsg_）")
# 照抄实测：第 2 层（pid=108201995）**有完整的楼层容器**，但没有 postmsg_ 容器
THREAD_HTML = """
<html><body>
<div id="post_107222794"><table id="pid107222794"><tr>
<td class="pls"><div id="favatar107222794" class="pls cl favatar">
  <div class="authi"><a href="home.php?mod=space&amp;uid=1">楼主</a></div></div></td>
<td class="plc"><div class="pi"><strong><a id="postnum107222794" href="#"><em>1</em><sup>#</sup></a></strong>
  <em id="authorposton107222794">发表于 2016-10-11 10:00</em></div>
  <div id="postmsg_107222794"></div></td></tr></table></div>
<div id="post_108201995"><table id="pid108201995"><tr>
<td class="pls"><div id="favatar108201995" class="pls cl favatar">
  <div class="authi"><a href="home.php?mod=space&amp;uid=514069">宁波雄鹰9</a></div></div></td>
<td class="plc"><div class="pi"><strong><a id="postnum108201995" href="#"><em>51</em><sup>#</sup></a></strong>
  <em id="authorposton108201995">发表于 2016-10-12 12:16</em></div>
  <!-- ★ 这里**没有** postmsg_ 容器：站方没给它生成 ajax 注入目标 -->
  </td></tr></table></div>
</body></html>
"""
tp = parse_thread_page(THREAD_HTML, tid=6177045, page_no=4)
check("pid 集合以 post_ 为准，缺 postmsg_ 的那层**也在**",
      set(tp.pids) == {107222794, 108201995}, f"pids={tp.pids}")
check("该层的楼层号被读到（51）",
      tp.floors.get(108201995) and tp.floors[108201995].floor == 51,
      f"floor={tp.floors.get(108201995).floor if tp.floors.get(108201995) else None}")
check("该层的作者被读到",
      tp.floors.get(108201995) and tp.floors[108201995].author == "宁波雄鹰9")
check("完全没有 post_ 时退回 postmsg_（不能整个解析失败）",
      set(parse_thread_page(
          '<div id="postmsg_123"></div>').pids) == {123})

print()
print("### ③ 长帖页数上限：站方标了多少页就采多少页")
detail_src = open(os.path.join(PKG, "msyt", "detail.py"), encoding="utf-8").read()
parsers_src = open(os.path.join(PKG, "msyt", "parsers.py"), encoding="utf-8").read()
listpage_src = open(os.path.join(PKG, "msyt", "listpage.py"), encoding="utf-8").read()
check("detail.py 有硬天花板常量",
      "DETAIL_PAGE_HARD_CAP" in detail_src)
check("预算按站方标的总页数放开",
      "min(site_total_pages, DETAIL_PAGE_HARD_CAP)" in detail_src)
check("续传时预算至少能进得去循环（pages_done+1）",
      "max(self.cfg.detail_max_pages, pages_done + 1)" in detail_src)
check("续传时总页数在每一页都更新（不只第 1 页）",
      re.search(r"elif\s+tp\.total_pages\s+and\s+tp\.total_pages\s+>\s*site_total_pages",
                detail_src) is not None)
check("parsers.py 有 _POSTBOX_RE",
      "_POSTBOX_RE" in parsers_src)
check("parse_thread_page 用了 _POSTBOX_RE",
      "_POSTBOX_RE.findall" in parsers_src)
check("列表选择器含 stickthread_",
      "stickthread_" in parsers_src)
check("列表解析调用了 parse_announcements",
      "parse_announcements(soup, list_url)" in parsers_src)
check("listpage.py 把当页 URL 传进解析器（用于拼绝对地址）",
      "parse_list_page(r.text, url)" in listpage_src)

print()
print("### ④ parse_announcements 单独可用")
from bs4 import BeautifulSoup                              # noqa: E402
ann = parse_announcements(BeautifulSoup(LIST_HTML, "html.parser"),
                          "https://bbs.cnnb.com.cn/forum.php?mod=forumdisplay&fid=1176")
check("公告区解析出 3 条", len(ann) == 3, f"tids={[a.tid for a in ann]}")
check("公告条目只有标题与链接（类别/部门留空）",
      all(a.category == "" and a.dept_id == "" for a in ann))

print()
print("### ⑤ 公告行**不得覆盖**帖行已有的真值（2026-10-24 踩过）")
check("公告行带 is_announcement=1 标记", all(a.is_announcement == 1 for a in ann))
check("普通帖行 is_announcement=0",
      all(r.is_announcement == 0 for r in rows if r.tid in (8344484, 8344485, 8333204)))
check("置顶帖行 is_announcement=0", by[6177045].is_announcement == 0)

# 真库上验一遍：公告行写入后，标题/作者/回复数必须原样不动
import shutil                                        # noqa: E402
import tempfile                                      # noqa: E402

from msyt.db import DB                                # noqa: E402

_tmp = tempfile.mkdtemp(prefix="msyt-ann-test-")
try:
    db = DB(os.path.join(_tmp, "t.db"))
    # 先写一条「帖行」版本（有完整元数据）
    db.upsert_threads([by[6177045]])
    r0 = db.conn.execute(
        "SELECT title,author,replies FROM threads WHERE tid=?", (6177045,)).fetchone()
    # 再写同一 tid 的「公告行」版本（只有标题，其余全空）
    ann_row = [a for a in ann if a.tid == 6177045][0]
    ann_row.title = "中国宁波网民生e点通群众留言板（天一论坛问政频道）管理规定"
    db.upsert_threads([ann_row])
    r1 = db.conn.execute(
        "SELECT title,author,replies FROM threads WHERE tid=?", (6177045,)).fetchone()
    check("公告行写入后标题不变（不能换成公告区简称）",
          r0["title"] == r1["title"],
          f"{r0['title'][:24]!r} → {r1['title'][:24]!r}")
    check("公告行写入后作者不变（不能被清空）",
          r0["author"] == r1["author"] and r1["author"] != "",
          f"{r0['author']!r} → {r1['author']!r}")
    check("公告行写入后回复数不变（不能被清成 0）",
          r0["replies"] == r1["replies"] and r1["replies"] != 0,
          f"{r0['replies']} → {r1['replies']}")
    # 反向：帖行仍然要能覆盖公告行的简称
    db.upsert_threads([by[6177045]])
    r2 = db.conn.execute(
        "SELECT title FROM threads WHERE tid=?", (6177045,)).fetchone()
    check("帖行仍能覆盖公告区的简称标题",
          r2["title"] == by[6177045].title, r2["title"][:30])
    db.conn.close()
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print()
print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
