# -*- coding: utf-8 -*-
"""验收：检索页的**筛选器**（2026-10-24 用户实测反馈后补）。

★ 事故经过
  ① 「部门」下拉选了就**检索失败**（页面上是「检索失败：」，连错误信息都是空的）。
     病根在 `db.py` 的 dept 分支：`IN (?,?)` 只给 1 个参数 → 占位符与参数个数对不上
     → sqlite 抛错 → 500。
  ② 109 个部门用原生下拉框根本没法找；站方那个「107家部门与企事业单位在线」的
     搜索框支持 **「输入部门名称、拼音或首字母」** → 我们要同款，且**其他标签同理**。

★ 这个测试盯住：
  · dept 分支的**占位符个数 == 参数个数**（用真实接口验，不靠肉眼）
  · 七个筛选器都能**输入文字**（combobox），并支持名称/拼音/首字母
  · /api/meta 带上 py（拼音首字母）与 group（分组）
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

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


BASE = "http://127.0.0.1:8765"
CANDS = [("test@example.com", "test-password"), ("qa-member@example.com", "qa-test-password")]


def login():
    for email, pw in CANDS:
        body = json.dumps({"email": email, "password": pw}).encode()
        req = urllib.request.Request(BASE + "/api/auth/login", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode())
            if d.get("token"):
                return d["token"]
        except Exception:                             # noqa: BLE001
            pass
    return ""


def q(tok, path, **kw):
    url = BASE + path
    if kw:
        url += "?" + urllib.parse.urlencode(kw)
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"_raw": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:                          # noqa: BLE001
        return -1, {"_raw": str(e)}


print("=" * 72)
print("检索筛选器验收")
print("=" * 72)

tok = login()
check("能登录", bool(tok))
if not tok:
    sys.exit(1)

print("\n① 部门筛选不再 500（这正是原来「检索失败：」的真因）")
st, d = q(tok, "/api/threads", dept="556")
check("dept=556 → 200", st == 200, f"HTTP {st} {d.get('_raw','')}")
check("dept=556 → 有命中", d.get("total", 0) > 0, str(d.get("total")))
st, d = q(tok, "/api/threads", dept="737")
check("dept=737 → 200", st == 200, f"HTTP {st}")
st, d = q(tok, "/api/threads", dept="556,737")
check("dept 多值 → 200", st == 200, f"HTTP {st}")
st, d = q(tok, "/api/threads", dept="99999")
check("dept 不存在的值 → 200 且 0 条", st == 200 and d.get("total") == 0,
      f"HTTP {st} total={d.get('total')}")
st, d = q(tok, "/api/threads", district="737", dept="737")
check("区县市 + 部门 同时给 → 200", st == 200, f"HTTP {st}")

print("\n② /api/meta 带拼音首字母与分组")
st, m = q(tok, "/api/meta")
check("/api/meta → 200", st == 200)
check("部门 109 条", len(m.get("depts", [])) == 109, str(len(m.get("depts", []))))
check("区县市 10 条", len(m.get("districts", [])) == 10, str(len(m.get("districts", []))))
d0 = next((x for x in m.get("depts", []) if x.get("name") == "宁波税务"), None)
check("部门带 py（宁波税务 → NBSW）", bool(d0) and d0.get("py") == "NBSW",
      str(d0))
check("部门带 group（分组代码）", bool(d0) and bool(d0.get("group")), str(d0))
check("类别带 py（投诉 → TS）",
      any(x.get("name") == "投诉" and x.get("py") == "TS"
          for x in m.get("categories", [])))

print("\n③ 七个筛选器都能输入文字（combobox）")
app = open(os.path.join(PKG, "msyt", "web", "app.js"), encoding="utf-8").read()
code = re.sub(r"(?m)^\s*//.*$", "", app)
check("有 makeSearchable 组件", "function makeSearchable" in code)
for sel in ("f-category", "f-state", "f-mailbox", "f-light", "f-group",
            "f-district", "f-dept"):
    check(f"{sel} 挂上了可输入组件", f"'{sel}'" in code and "makeSearchable(" in code)
check("option 带 data-py（前端靠它匹配拼音）", "data-py=" in code)
check("匹配逻辑含名称子串", ".includes(s)" in code)
check("匹配逻辑含拼音首字母", "it.py" in code)
check("列表按分组给表头（站方那 5 列的分组）", "sdrop-g" in code)

print("\n④ 后端 SQL：占位符个数必须等于参数个数")
db = open(os.path.join(PKG, "msyt", "db.py"), encoding="utf-8").read()
m2 = re.search(r"if dept:(.*?)\n\s*if group:", db, re.S)
check("能找到 dept 分支", bool(m2))
body = m2.group(1) if m2 else ""
# ★ 数占位符前先**剥掉注释** —— 注释里举例写的 "IN (?,?)" 会被误数进去
code_only = re.sub(r"(?m)^\s*#.*$", "", body)
n_ph = code_only.count("?")
check("dept 分支每个 id 恰好 2 个占位符（主部门 + 副部门）", n_ph == 2,
      f"占位符 {n_ph} 个")
check("dept 分支按 id 逐个绑定参数", "for i in ids:" in code_only
      and "args += [i," in code_only)
check("不再有「把多 id 绑成一个值」的写法", 'args += [",".join(ids)]' not in db)

print("\n⑤ 拼音模块（可选依赖，没装也不该崩）")
from msyt import pinyin                          # noqa: E402
check("固定词典的首字母（投诉 → TS）", pinyin.initials("投诉") == "TS",
      pinyin.initials("投诉"))
check("固定词典的首字母（已受理 → YSL）", pinyin.initials("已受理") == "YSL")
check("部门名能算出首字母（宁波税务）", pinyin.initials("宁波税务") == "NBSW",
      pinyin.initials("宁波税务"))
check("dict_items 补上 py", pinyin.dict_items([{"name": "投诉", "code": "1"}])[0].get("py") == "TS")

print()
print("=" * 72)
print(f"共 {OK + FAIL} 项 · 通过 {OK} · 失败 {FAIL}")
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
