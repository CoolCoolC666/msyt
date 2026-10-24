# -*- coding: utf-8 -*-
"""验收：CSS **类名不能撞车**（2026-10-24 用户实测踩到）。

★ 事故经过
  「当前生效的筛选条件」那排 chip 我用了类名 `.chip`，
  而**顶栏的统计胶囊早就叫 `.chip`**（深青底上的半透明白）。
  新规则写在文件后面 → **把顶栏的样式整个盖掉**：
  胶囊背景变成浅色，里面的数字 `<b>` 仍是 `#fff`、单位 `.dim` 仍是
  `rgba(255,255,255,.8)` → **白字压浅底**，整排几乎看不清。

★ 这个测试防的是**一整类**事故：
  同一个类名在样式表里被定义两次 → 后写的默默改掉前一个的样式，
  而页面上「控件有值/有样式」看不出来，只会觉得「颜色怎么不对了」。

★ 判定口径
  · **独立类选择器**（`.foo {`）同一个类名**只能定义一次**
  · 复合选择器（`button.foo` / `.foo b` / `.foo.link`）不算重复 ——
    它们是有意只覆盖某个子形态
"""
from __future__ import annotations

import os
import re
import sys

ROOT = r"【项目根】"
WEB = os.path.join(ROOT, "程序", "msyt", "web")
CSS = os.path.join(WEB, "style.css")
HTML = os.path.join(WEB, "index.html")
JS = os.path.join(WEB, "app.js")

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


css = open(CSS, encoding="utf-8").read()
html = open(HTML, encoding="utf-8").read()
js = open(JS, encoding="utf-8").read()

print("=" * 72)
print("CSS 类名撞车验收")
print("=" * 72)

# ---- ① 独立类选择器不得重复定义 ----
print("\n① 独立类选择器不得定义两次")
# 只取「行首就是 .foo {」或「.foo, …」的写法；
# `.foo:hover` / `.foo::before` 是**伪类变体**，不算重复定义；
# `button.foo {` / `.foo b {` 是复合选择器，也不算。
standalone = re.findall(r"(?m)^\.([A-Za-z][\w-]*)\s*(?=[,{])", css)
dup = {}
for c in standalone:
    dup[c] = dup.get(c, 0) + 1
dups = {k: v for k, v in dup.items() if v > 1}
check("没有类名被重复定义", not dups, str(dups))
check("样式表里定义了足够的类（>60）", len(standalone) > 60, str(len(standalone)))

# ---- ② 顶栏统计胶囊的样式必须还是「深青底上的半透明白」 ----
print("\n② 顶栏统计胶囊（.chip）保持原样")
m = re.search(r"(?m)^\.chip\s*\{(.*?)\}", css, re.S)
check("能找到 .chip 的定义", bool(m))
chip_body = m.group(1) if m else ""
check(".chip 仍是半透明白底（深青顶栏用）",
      "rgba(255,255,255,.12)" in chip_body, chip_body.strip()[:80])
check(".chip 的数字仍是白色", re.search(r"(?m)^\.chip b\s*\{[^}]*color:\s*#fff", css) is not None)
check(".chip 的单位仍是浅色",
      re.search(r"(?m)^\.chip \.dim\s*\{[^}]*rgba\(255,255,255", css) is not None)

# ---- ③ 筛选条件 chip 必须用独立类名 ----
print("\n③ 筛选条件 chip 用独立类名 .fchip")
check("样式表里有 .fchip", re.search(r"(?m)^\.fchip\s*\{", css) is not None)
check(".fchip 用浅底深字（浅色页面上的胶囊）",
      re.search(r"(?m)^\.fchip\s*\{[^}]*background:\s*var\(--accent-soft\)", css) is not None,
      "")
check("JS 生成筛选 chip 时用 class=\"fchip\"", 'class="fchip"' in js)
check("JS 不再用 class=\"chip\" 生成筛选 chip",
      'class="chip"' not in js)
check("JS 取 chip 用 .fchip 选择器", "'#active-filters .fchip'" in js)

# ---- ④ 页面里顶栏用的还是 .chip，且没有被别的东西顶掉 ----
print("\n④ 页面里的用法")
top = re.findall(r'<div class="chip"[^>]*>', html)
check("顶栏统计胶囊仍是 .chip", len(top) >= 4, f"{len(top)} 个")
check("顶栏 .chip 出现在 .topmeta 里",
      re.search(r'(?s)class="topmeta".*?class="chip"', html) is not None)

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
