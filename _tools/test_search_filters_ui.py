# -*- coding: utf-8 -*-
"""验收：检索页的**筛选器必须真的生效**（2026-10-24 用户实测发现的 bug）。

★ 事故经过
  界面上下拉框选得好好的，检索结果**还是全量**（命中 607,028 条）。
  原因：前端 `filters()` 里写的是 `g('category')` → 找 `#category`，
  而真实 DOM id 是 `#f-category` → 永远取到 null →
  **所有下拉筛选被静默丢掉**（只有关键词和日期发得出去）。
  后端其实一直是好的。

★ 这个测试盯三件事
  ① 前端取筛选值时**必须带 f- 前缀**
  ② index.html 里**每个筛选器的 id 都要存在**（且前端引用得到）
  ③ 页面上必须有那几组筛选器（类别/状态/留言箱/分组/区县市/部门/时效灯）
"""
from __future__ import annotations

import os
import re
import sys

ROOT = r"【项目根】"
WEB = os.path.join(ROOT, "程序", "msyt", "web")
APP = os.path.join(WEB, "app.js")
HTML = os.path.join(WEB, "index.html")

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


app = open(APP, encoding="utf-8").read()
html = open(HTML, encoding="utf-8").read()

# ★ 扫描**代码**时必须先去掉注释 ——
#   否则注释里举例写的 g('category') 会被当成真代码（自己写注释把自己判失败）。
app_code = re.sub(r"/\*.*?\*/", "", app, flags=re.S)
app_code = re.sub(r"(?m)^\s*//.*$", "", app_code)
app_code = re.sub(r"(?m)\s+//.*$", "", app_code)

print("=" * 72)
print("检索筛选器验收")
print("=" * 72)

# ---- ① filters() 必须用 'f-' + 键 ----
print("\n① 前端取筛选值的写法")
m = re.search(r"function filters\(\)\s*\{(.*?)\n\}", app_code, re.S)
check("能找到 filters()", bool(m))
body = m.group(1) if m else ""

# 找所有 g('xxx') 的调用（排除 g('f-xxx')）
bare = re.findall(r"g\('([a-z_]+)'\)", body)
bare = [b for b in bare if not b.startswith("f-")]
check("filters() 里没有「漏掉 f- 前缀」的取值", not bare,
      f"发现 {bare}" if bare else "")

check("filters() 按 'f-' + 键 取值", "'f-' + k" in body or "g('f-' + k)" in body)
for key in ("category", "state", "mailbox", "group", "district", "dept", "light"):
    check(f"filters() 会把 {key} 发出去", f"'{key}'" in body, "")

# ---- ② 页面上的筛选器 id 必须齐 ----
print("\n② 页面上的筛选器")
ids = set(re.findall(r'id="(f-[a-z0-9\-]+)"', html))
for key in ("category", "state", "mailbox", "group", "district", "dept", "light"):
    check(f"index.html 有 #f-{key}", f"f-{key}" in ids, str(sorted(ids))[:0])
check("index.html 有 #f-q（关键词）", "f-q" in ids)
check("index.html 有 #f-from / #f-to（日期）", "f-from" in ids and "f-to" in ids)
check("index.html 有 #btn-search / #btn-reset",
      'id="btn-search"' in html and 'id="btn-reset"' in html)

# ---- ③ 前端引用的 f- id 必须都在页面里 ----
print("\n③ 前端引用 vs 页面实际")
used = set(re.findall(r"g\('(f-[a-z0-9\-]+)'\)", app))
used |= set(re.findall(r"\$\('#(f-[a-z0-9\-]+)'\)", app))
missing = sorted(u for u in used if u not in ids)
check("前端引用的筛选器 id 页面里都有", not missing, str(missing))

# ---- ④ 后端支持这些参数 ----
print("\n④ 后端 /api/threads 支持的参数")
webapp = open(os.path.join(ROOT, "程序", "msyt", "webapp.py"), encoding="utf-8").read()
m2 = re.search(r"@app\.get\(\"/api/threads\"\).*?return db\.search\((.*?)\)\n", webapp, re.S)
check("能找到 /api/threads", bool(m2))
args = m2.group(1) if m2 else ""
for key in ("category", "state", "mailbox", "group", "district", "dept", "light"):
    check(f"后端接受 {key}", f"{key}=" in args)

# ---- ⑤ 新增的交互 ----
print("\n⑤ 新补的功能")
check("有 applyTagFilter（点标签即筛）", "function applyTagFilter" in app)
check("有 renderActiveFilters（显示生效条件）", "function renderActiveFilters" in app)
check("表格里的类别可点", 'data-tag="category"' in app)
check("表格里的状态可点", 'data-tag="state"' in app)
check("表格里的时效灯可点", 'data-tag="light"' in app)
check("表格里的部门可点", 'data-tag="dept"' in app)
check("点标签不会误开详情（stopPropagation）", "stopPropagation" in app)
check("下拉改动自动检索", "el.onchange" in app)
check("有 chip 样式", ".chip" in open(os.path.join(WEB, "style.css"), encoding="utf-8").read())

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
