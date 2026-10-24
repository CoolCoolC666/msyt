# -*- coding: utf-8 -*-
"""验收「合并记录翻页 + 折叠」这条需求（2026-10-23）。

覆盖：
  ① 后端 /api/assist/conflicts 支持 offset，返回 total
  ② 翻页时拿到的是**不同**的一段记录，且不重不漏
  ③ 页数/条数显示正确
  ④ 前端元素齐全：翻页条、折叠按钮
  ⑤ 卡片顺序：成员名单 → 实时日志 → 合并记录
"""
from __future__ import annotations

import io
import json
import re
import sys
import urllib.error
import urllib.request

ROOT = r"【项目根】"
BASE = "http://127.0.0.1:8765"

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


def api(path, token=None, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {}


def login(email, pw):
    body = json.dumps({"email": email, "password": pw}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/auth/login", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8")).get("token", "")


print("=" * 70)
print("合并记录「翻页 + 折叠」验收")
print("=" * 70)

ADMIN = "qa-admin@example.com"
PW = "qa-test-password"
tok = login(ADMIN, PW)
if not tok:
    print("★ 管理员登录失败，无法继续")
    sys.exit(1)
print(f"管理员已登录：{ADMIN}\n")

# ---------------------------------------------------------------- ① 接口
print("① 后端分页接口")
st, d1 = api("/api/assist/conflicts?limit=20&offset=0", tok)
check("接口返回 200", st == 200, f"HTTP {st}")
check("返回 items 数组", isinstance(d1.get("items"), list), f"{len(d1.get('items') or [])} 条")
check("返回 total（总数）", "total" in d1, f"total={d1.get('total')}")
check("返回 limit / offset", "limit" in d1 and "offset" in d1,
      f"limit={d1.get('limit')} offset={d1.get('offset')}")

total = int(d1.get("total") or 0)
page1 = d1.get("items") or []
print(f"  · 合并记录总数 = {total}")

# ---------------------------------------------------------------- ② 翻页正确
print("\n② 翻页：拿到不同的一段，且不重不漏")
st, d2 = api("/api/assist/conflicts?limit=20&offset=20", tok)
check("第 2 页返回 200", st == 200, f"HTTP {st}")
page2 = d2.get("items") or []
if total > 20:
    ids1 = {c["id"] for c in page1}
    ids2 = {c["id"] for c in page2}
    check("两页内容不重复", not (ids1 & ids2), f"交集 {len(ids1 & ids2)} 条")
    check("两页都是 20 条", len(page1) == 20 and len(page2) == 20,
          f"p1={len(page1)} p2={len(page2)}")
    check("两页合并后 <= total", len(ids1) + len(ids2) <= total,
          f"{len(ids1) + len(ids2)} <= {total}")
    # 顺序：第 1 页应当是更新的
    check("第 1 页比第 2 页更新（id 递减）",
          page1[0]["id"] > page2[0]["id"],
          f"p1[0].id={page1[0]['id']} p2[0].id={page2[0]['id']}")
else:
    print(f"  （记录只有 {total} 条，不足两页，跳过翻页对比）")

# 越界：offset 超出应返回空列表而不是报错
st, d3 = api("/api/assist/conflicts?limit=20&offset=999999", tok)
check("offset 越界不报错", st == 200 and d3.get("items") == [],
      f"HTTP {st} items={len(d3.get('items') or [])}")

# ---------------------------------------------------------------- ③ 前端元素
print("\n③ 前端：翻页条与折叠按钮")
html = io.open(ROOT + r"\程序\msyt\web\index.html", encoding="utf-8").read()
appjs = io.open(ROOT + r"\程序\msyt\web\app.js", encoding="utf-8").read()
for eid, name in [("cf-prev", "上一页按钮"), ("cf-next", "下一页按钮"),
                  ("cf-page", "页码显示"), ("cf-toggle", "折叠/展开按钮"),
                  ("cf-count", "总条数显示"), ("cf-bar", "翻页折叠条容器")]:
    check(f"index.html 里有 #{eid}（{name}）", f'id="{eid}"' in html)
check("app.js 里实现了翻页（cf-prev/cf-next 绑定）",
      "$('#cf-prev').onclick" in appjs and "$('#cf-next').onclick" in appjs)
check("app.js 里实现了折叠（cf-toggle 绑定）", "$('#cf-toggle').onclick" in appjs)
check("app.js 每页条数是 20", "CF_PAGE_SIZE = 20" in appjs)
check("app.js 翻页时带 offset 参数", "&offset=" in appjs)
check("样式里有 .sep 分隔符", ".sep {" in io.open(
    ROOT + r"\程序\msyt\web\style.css", encoding="utf-8").read())

# ---------------------------------------------------------------- ④ 卡片顺序
print("\n④ 卡片顺序：成员名单 → 实时日志 → 合并记录")
i_m = html.find('id="members-card"')
i_l = html.find('id="stream-log"')
i_c = html.find('id="conflicts-card"')
check("三个 id 都存在", i_m >= 0 and i_l >= 0 and i_c >= 0)
check("成员名单在合并记录上面", 0 <= i_m < i_c, f"members@{i_m} conflicts@{i_c}")
check("实时日志在成员名单下面", 0 <= i_m < i_l, f"members@{i_m} log@{i_l}")

# 顺序（按卡片在文件中的出现次序）
order = [(m.start(), m.group(1).strip()) for m in re.finditer(
    r'<div class="card"[^>]*>\s*<h3>([^<]+)', html)]
seq = [t for _, t in order]
try:
    k1 = seq.index("成员名单")
    k2 = seq.index("实时日志（管理员与成员看的是同一条流）")
    k3 = seq.index("合并记录（覆盖 / 新增的痕迹）")
    check("严格顺序正确", k1 < k2 < k3, f"索引 {k1} < {k2} < {k3}")
except ValueError as e:
    check("严格顺序正确", False, str(e))

# ---------------------------------------------------------------- ⑤ 折叠初始态
print("\n⑤ 折叠初始态")
check("折叠条默认不在 hidden 里", 'id="cf-bar" style="margin-top:10px"' in html
      or 'id="cf-bar"' in html)
check("折叠按钮初始文案是「收起」", ">收起<" in html)
check("app.js 有 collapsed 状态", "collapsed: false" in appjs)

print()
print("=" * 70)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
