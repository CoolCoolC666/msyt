# -*- coding: utf-8 -*-
"""
test_integrity_paging.py —— 体检结果「分页 + 按需加载 + 跨页勾选」的验收
================================================================================
背景（2026-10-28）
--------------------
体检页原来把整份结果（60 万帖量级）一次性 innerHTML 出去：
  · 服务端：明细整份挂在 job.result 上 → 几百 MB JSON
  · 浏览器：JSON.parse 一份 + 几十万行 <tr> 的 DOM 一份
→ 标签页直接被撑爆。

本轮改造：
  · 后端：job.result 只留**摘要**；明细落库；新增
          GET /api/integrity/page（按类分页）
          GET /api/integrity/tids（只回 tid 数组，给「全选」用）
          GET /api/integrity/summary（上次体检摘要）
  · 前端：勾选态从 DOM 搬到 Set（int_state.js）→ 翻页不丢勾选；
          四类各自独立分页、展开才加载。

验收要点（按本项目既有方法学）
--------------------------------
  ① 分页真的分页：总数/页数/每页条数/不重不漏/越界不报错
  ② 全选语义：拿到的是**该类全部** tid，不是当前页那几十条
  ③ 跨页勾选保持：第 1 页勾 3 条 → 第 2 页勾 2 条 → 回第 1 页仍勾着，共 5 条
  ④ 权限两道都在：成员调体检接口 → 403
  ⑤ **反向验证闸**：把分页实现改成「无视 page 返回全量」，
     同一批断言**必须报红** —— 否则说明这条闸是空的
  ⑥ 前端状态模块用 Node 直接单测（纯逻辑、无 DOM）
  ⑦ 静态检查：app.js 不再依赖 DOM 里的勾选态；int_state.js 在 app.js 之前加载

用法：python _tools/test_integrity_paging.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = os.path.join(ROOT, "程序")
WEB = os.path.join(PROG, "msyt", "web")
sys.path.insert(0, PROG)

BASE = "http://127.0.0.1:8765"
ADMIN = ("qa-admin@example.com", "qa-test-password")
MEMBER = ("test@example.com", os.environ.get("MSYT_TEST_PW", "test-password"))
RUN_ID = "TESTPAGING"
#: 合成数据各类的条数（不依赖真扫全库，测试可秒级完成）
N_PROBLEMS, N_GROWTH, N_WARNINGS, N_RESTRICTED = 250, 30, 12, 7
#: ★ 2026-10-29 下线第五类 count_mismatch（站方计数缺陷）：
#:   那批帖的正文**本来就采全了**（实测 25 条逐层对比），
#:   正确处置是**归为 done（正文齐全即已采）**，不再单列一栏。

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  —— {extra}" if extra else ""))


def call(path, method="GET", body=None, token="", timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:                                  # noqa: BLE001
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:                                      # noqa: BLE001
            return e.code, raw
    except Exception as e:                                    # noqa: BLE001
        return 0, str(e)


def login(cred):
    st, d = call("/api/auth/login", "POST",
                 {"email": cred[0], "password": cred[1]})
    if st != 200:
        print(f"  登录失败 {cred[0]}: {st} {d}")
        return ""
    return d["token"]


# ---------------------------------------------------------------- 合成数据
def seed():
    """在库里写一次**合成的**体检结果（不碰真实数据）。"""
    from msyt.config import Config
    from msyt.db import DB
    db = DB(str(Config.load().db_path))
    with db.lock:
        db.conn.execute("DELETE FROM integrity_items WHERE run_id=?", (RUN_ID,))
        db.conn.execute("DELETE FROM integrity_runs WHERE run_id=?", (RUN_ID,))
        db.conn.execute(
            "INSERT INTO integrity_runs(run_id,created_at,checked,ok,counts)"
            " VALUES(?,?,?,?,?)",
            (RUN_ID, "2999-01-01 00:00:00", 999, 700,
             json.dumps({"problems": N_PROBLEMS, "growth": N_GROWTH,
                         "warnings": N_WARNINGS, "restricted": N_RESTRICTED})))
        for kind, n in (("problems", N_PROBLEMS), ("growth", N_GROWTH),
                        ("warnings", N_WARNINGS), ("restricted", N_RESTRICTED)):
            rows = [(i, 90000000 + i, json.dumps({"tid": 90000000 + i,
                                                   "posts": i, "pages": 1,
                                                   "problems": ["测试项"]},
                                                  ensure_ascii=False))
                    for i in range(1, n + 1)]
            db.integrity_add_items(RUN_ID, kind, rows)
        db.conn.commit()
    return db


def unseed(db):
    with db.lock:
        db.conn.execute("DELETE FROM integrity_items WHERE run_id=?", (RUN_ID,))
        db.conn.execute("DELETE FROM integrity_runs WHERE run_id=?", (RUN_ID,))
        db.conn.commit()
    db.close()


# ---------------------------------------------------- ① 分页断言（可复用）
def assert_paging(fetch, tag):
    """fetch(kind, page, page_size) -> dict。对**正常实现**必须全过。"""
    d = fetch("problems", 1, 50)
    check(f"[{tag}] 首页：总数 {N_PROBLEMS} / 页数 5",
          d.get("total") == N_PROBLEMS and d.get("pages") == 5,
          f"total={d.get('total')} pages={d.get('pages')}")
    check(f"[{tag}] 首页条数 = 50", len(d.get("items") or []) == 50,
          f"{len(d.get('items') or [])} 条")
    d2 = fetch("problems", 2, 50)
    first2 = (d2.get("items") or [{}])[0].get("tid")
    check(f"[{tag}] 第 2 页不重不漏（首条 = 第 51 条）",
          first2 == 90000051, f"first tid={first2}")
    d5 = fetch("problems", 5, 50)
    check(f"[{tag}] 末页条数 = 50", len(d5.get("items") or []) == 50,
          f"{len(d5.get('items') or [])} 条")
    d6 = fetch("problems", 6, 50)
    check(f"[{tag}] 越界页 → 空列表而不是报错",
          d6.get("items") == [] and d6.get("total") == N_PROBLEMS,
          f"items={len(d6.get('items') or [])}")
    d20 = fetch("problems", 1, 20)
    check(f"[{tag}] 每页 20 条 → 20 条",
          len(d20.get("items") or []) == 20,
          f"{len(d20.get('items') or [])} 条")
    d200 = fetch("problems", 1, 200)
    check(f"[{tag}] 每页 200 条 → 200 条",
          len(d200.get("items") or []) == 200,
          f"{len(d200.get('items') or [])} 条")
    d1 = fetch("problems", 1, 1)
    check(f"[{tag}] 每页 1 条 → 1 条（分页参数真的生效）",
          len(d1.get("items") or []) == 1,
          f"{len(d1.get('items') or [])} 条")


def paging_fetch(atok):
    def f(kind, page, page_size):
        st, d = call(f"/api/integrity/page?kind={kind}&page={page}"
                     f"&page_size={page_size}&run_id={RUN_ID}", "GET", None, atok)
        return d if st == 200 else {"items": [], "total": 0, "pages": 0}
    return f


def main() -> int:
    atok = login(ADMIN)
    mtok = login(MEMBER)
    if not atok:
        print("管理员登录失败，先起 web 服务")
        return 1
    print(f"管理员登录{'成功' if atok else '失败'}；"
          f"成员登录{'成功' if mtok else '失败'}")

    db = seed()
    try:
        print("\n=== ① 摘要接口 ===")
        st, d = call(f"/api/integrity/summary?run_id={RUN_ID}", "GET", None, atok)
        # summary 取「最近一次」，合成 run 的 created_at 故意设成最大 → 它就是最近一次
        check("summary → 200", st == 200, f"HTTP {st}")
        if st == 200:
            run = d.get("run") or {}
            check("summary 带出 run_id", bool(run.get("run_id")), str(run.get("run_id")))
            check("summary 带出各类条数",
                  (run.get("counts") or {}).get("problems") == N_PROBLEMS,
                  str(run.get("counts")))

        print("\n=== ② 分页接口 ===")
        st, d = call(f"/api/integrity/page?kind=problems&page=1&page_size=50"
                     f"&run_id={RUN_ID}", "GET", None, atok)
        check("page → 200", st == 200, f"HTTP {st}")
        assert_paging(paging_fetch(atok), "线上")

        print("\n=== ③ tids 接口（「全选」靠它）===")
        st, d = call(f"/api/integrity/tids?kind=problems&run_id={RUN_ID}",
                     "GET", None, atok)
        check("tids → 200", st == 200, f"HTTP {st}")
        tids = d.get("tids") or []
        check(f"★ tids 拿到的是**全部** {N_PROBLEMS} 条（不是当前页 50 条）",
              len(tids) == N_PROBLEMS, f"{len(tids)} 条")
        check("tids 是纯数字数组",
              all(isinstance(t, int) for t in tids), str(tids[:3]))
        st, d = call(f"/api/integrity/tids?kind=restricted&run_id={RUN_ID}",
                     "GET", None, atok)
        check("restricted 的 tids 也能取到（前端不提供勾选，但数据在）",
              st == 200 and len(d.get("tids") or []) == N_RESTRICTED,
              f"HTTP {st} {len(d.get('tids') or [])} 条")

        print("\n=== ④ 参数与权限 ===")
        st, d = call("/api/integrity/page?kind=evil&run_id=" + RUN_ID,
                     "GET", None, atok)
        check("★ 非法 kind → 400（白名单，不拼 SQL）", st == 400, f"HTTP {st}")
        st, d = call("/api/integrity/page?kind=problems&page_size=99999"
                     "&run_id=" + RUN_ID, "GET", None, atok)
        check("page_size 越界 → 4xx", 400 <= st < 500, f"HTTP {st}")
        st, d = call(f"/api/integrity/page?kind=problems&run_id={RUN_ID}",
                     "GET", None, mtok)
        check("★ 成员调体检接口 → 403（接口层那道）", st == 403, f"HTTP {st}")
        st, d = call(f"/api/integrity/tids?kind=problems&run_id={RUN_ID}",
                     "GET", None, mtok)
        check("★ 成员调 tids → 403", st == 403, f"HTTP {st}")
        st, d = call("/api/integrity/summary", "GET", None, mtok)
        check("★ 成员调 summary → 403", st == 403, f"HTTP {st}")

        print("\n=== ⑤ retry / purge 的入参契约没变 ===")
        st, d = call("/api/integrity/retry", "POST", {"tids": [99999999]},
                     atok)
        check("retry 接受 {tids:[...]} → 200", st == 200, f"HTTP {st} {str(d)[:60]}")
        st, d = call("/api/integrity/purge", "POST", {"tids": [99999999]},
                     atok)
        check("purge 接受 {tids:[...]} → 200", st == 200, f"HTTP {st} {str(d)[:60]}")

        print("\n=== ⑥ ★反向验证闸：把分页改坏，断言必须报红 ===")
        rev_ok = reverse_paging_gate()
        check("★ 反向验证：分页失效时断言确实报红（闸不是空的）",
              rev_ok, "" if rev_ok else "改坏分页后断言仍然通过 → 这条闸是空的！")

        print("\n=== ⑦ 前端状态模块（Node 单测）===")
        node_ok = run_node_test()
        check("★ Node 跑 int_state.js 状态单测通过", node_ok, "")

        print("\n=== ⑧ 静态检查：前端不再把勾选态住在 DOM 里 ===")
        appjs = open(os.path.join(WEB, "app.js"), encoding="utf-8").read()
        idx = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
        check("app.js 不再引用 INT_PICKS（DOM 勾选态的旧载体）",
              "INT_PICKS" not in appjs, "")
        check("app.js 不再从 DOM 读勾选（input[data-tid]:checked）",
              "input[data-tid]:checked" not in appjs, "")
        check("app.js 引入了 int_state.js 的状态对象",
              "IntState.createState()" in appjs, "")
        check("index.html 在 app.js **之前**加载 int_state.js",
              idx.index("int_state.js") < idx.index("/static/app.js"), "")
        check("index.html 给 int_state.js 单独的版本号（防缓存陷阱）",
              "__ISV__" in idx, "")

        print("\n=== ⑧' 静态检查：勾选列必须按 SELECTABLE 判断（2026-10-28）===")
        # ★ 新增类别后，若前端还硬编码 `kind === 'restricted'`
        #   来决定画不画勾选框 → 新类别会被误画成可勾选 →
        #   勾了只会让成员白跑（它重采多少次都不会变）。
        check("app.js 的勾选列按 IntState.SELECTABLE 判断，不硬编码类别名",
              "kind === 'restricted'" not in _strip_js_comments(appjs),
              "仍有 `kind === 'restricted'` 硬编码")
        check("app.js 的表头列数也按 SELECTABLE 判断",
              "kind === 'restricted' ? 0 : 1" not in _strip_js_comments(appjs),
              "仍有硬编码的列数计算")
        check("int_state.js 的 SELECTABLE 不含 count_mismatch",
              "count_mismatch" not in
              open(os.path.join(WEB, "int_state.js"), encoding="utf-8").read()
              .split("var SELECTABLE")[1].split(";")[0],
              "count_mismatch 被误列入可勾选")
        from msyt.db import DB as _DB
        check("后端 INTEGRITY_KINDS 只剩四类（count_mismatch 已下线）",
              "count_mismatch" not in _DB.INTEGRITY_KINDS, str(_DB.INTEGRITY_KINDS))
    finally:
        unseed(db)

    print("\n" + "=" * 70)
    ok = sum(1 for _, c, _ in results if c)
    bad = [r for r in results if not r[1]]
    print(f"共 {len(results)} 项 · 通过 {ok} · 失败 {len(bad)}")
    for nm, _, e in bad:
        print(f"  FAIL: {nm}  {e}")
    return 1 if bad else 0


def reverse_paging_gate() -> bool:
    """把 DB.integrity_page 换成「无视 page/page_size、返回全量」，
    跑同一批断言 —— **必须失败**。返回 True 表示闸有效。

    ★ 为什么这么做：一条只会「通过」的断言等于没有断言。
      必须证明它**抓得住**那个具体的坏实现。
    """
    from fastapi.testclient import TestClient
    from msyt.db import DB
    import msyt.webapp as webapp

    orig = DB.integrity_page

    def broken(self, run_id, kind, page=1, page_size=50):
        """坏实现：无视分页参数，一口气返回全部。"""
        out = orig(self, run_id, kind, 1, 200)
        if kind == "problems":
            allrows = self.conn.execute(
                "SELECT payload FROM integrity_items WHERE run_id=? AND kind=?"
                " ORDER BY ord", (run_id, kind)).fetchall()
            out["items"] = [json.loads(r["payload"]) for r in allrows]
            out["total"] = len(out["items"])
            out["pages"] = 1
        return out

    DB.integrity_page = broken
    try:
        client = TestClient(webapp.app)
        # ★ 令牌按 webapp 的签发方式造（payload + secret 都要对）
        tok = webapp.make_token({"email": ADMIN[0], "role": "admin",
                                 "node": "admin"}, webapp._secret(webapp._cfg()))
        headers = {"Authorization": "Bearer " + tok}

        def f(kind, page, page_size):
            r = client.get(f"/api/integrity/page?kind={kind}&page={page}"
                           f"&page_size={page_size}&run_id={RUN_ID}",
                           headers=headers)
            return r.json() if r.status_code == 200 else {"items": [], "total": 0,
                                                          "pages": 0}

        caught = []
        for kind, page, size, want in (("problems", 1, 50, 50),
                                       ("problems", 2, 50, 50),
                                       ("problems", 1, 20, 20),
                                       ("problems", 1, 1, 1)):
            d = f(kind, page, size)
            got = len(d.get("items") or [])
            if got != want:
                caught.append((kind, page, size, want, got))
        DB.integrity_page = orig
        if caught:
            print(f"     反向验证命中 {len(caught)} 处：{caught[:3]}")
            return True
        print("     反向验证：改坏分页后断言仍然通过 → 闸是空的！")
        return False
    except Exception as e:                                    # noqa: BLE001
        DB.integrity_page = orig
        print(f"     反向验证**异常**（不是「闸是空的」）：{type(e).__name__}: {e}")
        return False


def _strip_js_comments(src: str) -> str:
    """去掉 JS 里的行注释与块注释。

    ★ 为什么需要
      静态检查要断言「代码里没有硬编码 `kind === 'restricted'`」，
      但 app.js 的**注释里**会引用这个旧写法来说明「为什么要改」——
      那些注释不是代码，不该让检查报红。
    """
    import re
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def run_node_test() -> bool:
    """跑 Node 的前端状态单测（int_state.js 纯逻辑、无 DOM）。"""
    node = shutil.which("node")
    if not node:
        print("     node 不可用 → 跳过（前端状态单测需要 Node）")
        return True
    js = os.path.join(ROOT, "_tools", "test_int_state.js")
    if not os.path.exists(js):
        print(f"     找不到 {js}")
        return False
    r = subprocess.run([node, js], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines()[-12:]:
        print("     " + line)
    return r.returncode == 0


if __name__ == "__main__":
    raise SystemExit(main())
