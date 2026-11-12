# -*- coding: utf-8 -*-
"""闸：一键增量（分类体系 + 列表全选 + 详情全选，2026-11-12）。

★ 要守住的性质
  ① 增量 = **续采不重采**：列表段 restart 必须为 False（不从头重来）
  ② 列表段必须**先刷分类体系**（taxonomy=True）+ 全选部门 + 全站视图
  ③ 详情段必须**跑队列**（dept_ids 为空）—— 列表采集结束时才把待采详情入队，
     按部门硬挑会漏掉这一轮新入队的帖，也绕过了水位线
  ④ 两段**串行**：先列表后详情（并行会漏）
  ⑤ 端点**管理员专属**（服务器端采集）；与现有采集任务互斥
  ⑥ 前端挂点齐全，且这个卡在采集作业页**第一栏**

★ 必须有**真反向验证**：把实现改坏后，同一批断言必须报红。

用法：python _tools/test_increment_job.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt import webapp  # noqa: E402
from msyt.db import DB  # noqa: E402

FAILED: list = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def expect_red(fn) -> bool:
    """fn 里的断言**必须报红**。返回 False = 断言不够狠。"""
    try:
        fn()
    except Exception:
        return True
    return False


def seed_depts(db) -> None:
    with db.lock:
        for typeid, name in (("495", "市住建局"), ("512", "综合"), ("726", "某部门")):
            db.conn.execute(
                "INSERT INTO depts(typeid,name,group_code,group_name) VALUES(?,?,?,?)",
                (typeid, name, "func", "职能部门"))
        db.conn.commit()


def main() -> int:
    print("=" * 62)
    print("闸：一键增量（分类体系 + 列表全选 + 详情全选）")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="msyt_inc_")
    db = DB(os.path.join(tmp, "t.db"))
    seed_depts(db)

    # ---- ① 参数口径 ----
    print("\n--- ① 一键增量的参数口径 ---")
    ps = webapp.increment_params(db, 300, 50)
    lp = ps["list"]
    dp = ps["detail"]
    check(sorted(lp["dept_ids"]) == [495, 512, 726],
          "列表段：部门**全选**（%s）" % lp["dept_ids"])
    check(lp["use_global"] is True, "列表段：同时采全站视图 global")
    check(lp["taxonomy"] is True, "列表段：先刷分类体系")
    check(lp["restart"] is False, "列表段：restart=False（增量＝续采，不从头重采）")
    check(lp["pages"] == 300, "列表段：每源页数按传入（%s）" % lp["pages"])
    check(dp["dept_ids"] == [] and dp["tids"] == [],
          "详情段：跑队列（不按部门硬挑，也不定点）")
    check(dp["limit"] == 50, "详情段：本次上限按传入（%s）" % dp["limit"])
    check(dp["retry_failed"] is True, "详情段：失败的重置为待采（失败也算「还没采完」）")
    check(ps["dept_count"] == 3, "部门数统计正确（%s）" % ps["dept_count"])

    # ---- ② 端点与权限 ----
    print("\n--- ② 端点：管理员专属 + 互斥 ---")
    src = (Path(PROJ) / "程序" / "msyt" / "webapp.py").read_text(encoding="utf-8")
    i = src.find("def api_job_increment")
    check(i >= 0, "webapp.py 里有 /api/jobs/increment")
    body = src[i:i + 2500] if i >= 0 else ""
    check("_require_admin_jobs" in body, "端点：管理员专属（_require_admin_jobs）")
    for kind in ("list", "detail", "increment"):
        check('_reject_if_running("%s"' % kind in body,
              "端点：与同节点的「%s」任务互斥" % kind)
    check('JOBS.create("increment"' in body, "端点：建的是 increment 任务")
    check("_job_increment(cfg, j)" in body, "端点：跑的是 _job_increment")
    j = src.find("def _job_increment")
    jb = src[j:j + 2000] if j >= 0 else ""
    check(jb.find("_job_list(") >= 0 and jb.find("_job_detail(") >= 0, "任务体：两段都在")
    check(jb.find("_job_list(") < jb.find("_job_detail("),
          "任务体：先列表后详情（串行 —— 并行会漏掉这一轮新入队的帖）")
    check("job.should_stop()" in jb, "任务体：阶段之间看停止指令")

    # ---- ②b 任务体真的按顺序跑（**不联网**：把两段换成假的） ----
    class _FakeJob:
        def __init__(self, params):
            self.params = params
            self.logs: list = []

        def log(self, m):
            self.logs.append(str(m))

        def should_stop(self):
            return False

    calls: list = []
    _orig_list, _orig_detail = webapp._job_list, webapp._job_detail

    def fake_list(cfg, job, p=None):
        calls.append("list")
        return {"ok": "list"}

    def fake_detail(cfg, job, p=None):
        calls.append("detail")
        return {"ok": "detail"}

    webapp._job_list, webapp._job_detail = fake_list, fake_detail
    try:
        fj = _FakeJob({"list": lp, "detail": dp})
        res = webapp._job_increment(None, fj)
        check(calls == ["list", "detail"],
              "任务体：先跑列表再跑详情（%s）" % calls)
        check(bool(res.get("list")) and bool(res.get("detail")),
              "任务体：两段的结果都带回来")
        check(any("阶段" in m for m in fj.logs),
              "任务体：日志里写明阶段（%s）" % (fj.logs[0] if fj.logs else "无"))

        class _StopJob(_FakeJob):
            def should_stop(self):
                return True

        calls.clear()
        res2 = webapp._job_increment(None, _StopJob({"list": lp, "detail": dp}))
        check(calls == ["list"], "被叫停后不再跑详情段（%s）" % calls)
        check("已停止" in str(res2.get("detail")),
              "被叫停时结果里说清「已停止」（%s）" % res2.get("detail"))
    finally:
        webapp._job_list, webapp._job_detail = _orig_list, _orig_detail

    # ---- ③ 前端挂点 ----
    print("\n--- ③ 前端挂点 ---")
    web = Path(PROJ) / "程序" / "msyt" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    app_js = (web / "app.js").read_text(encoding="utf-8")
    for eid in ("inc-card", "btn-increment", "inc-pages", "inc-limit", "inc-estimate"):
        check(eid in html, "index.html 里有 #%s" % eid)
    k = html.find('id="sub-jobs"')
    check(k >= 0 and 0 < html.find('id="inc-card"') - k < 400,
          "「一键增量」卡在采集作业页的第一栏（紧跟 sub-jobs 之后）")
    check("/api/jobs/increment" in app_js, "app.js 会打 /api/jobs/increment")
    check("increment: '一键增量'" in app_js, "app.js 的任务栏认得 increment 这个类型")
    check("$('#inc-card').style.display = isAdmin" in app_js,
          "app.js：这个卡按管理员身份显隐（成员看不到）")
    check("updateIncEstimate" in app_js, "app.js：点之前先看工作量")

    # ---- ④ 真反向验证 ----
    print("\n--- ④ 真反向验证：改坏实现后同一批断言必须报红 ---")

    def assert_increment_shape():
        p = webapp.increment_params(db, 300, 50)
        assert p["list"]["taxonomy"] is True, "必须先刷分类体系"
        assert p["list"]["restart"] is False, "增量＝续采，不能从头重采"
        assert p["detail"]["dept_ids"] == [], "详情段必须跑队列"
        return True

    check(assert_increment_shape(), "参数口径：分类体系 + 续采 + 跑队列")
    _orig = webapp.increment_params

    def broken_taxonomy(d, pages=500, detail_limit=0):
        p = _orig(d, pages, detail_limit)
        p["list"]["taxonomy"] = False
        return p

    webapp.increment_params = broken_taxonomy
    try:
        check(expect_red(assert_increment_shape), "不刷分类体系 → 同一批断言必须报红")
    finally:
        webapp.increment_params = _orig

    def broken_restart(d, pages=500, detail_limit=0):
        p = _orig(d, pages, detail_limit)
        p["list"]["restart"] = True
        return p

    webapp.increment_params = broken_restart
    try:
        check(expect_red(assert_increment_shape), "从头重采 → 同一批断言必须报红")
    finally:
        webapp.increment_params = _orig

    def broken_queue(d, pages=500, detail_limit=0):
        p = _orig(d, pages, detail_limit)
        p["detail"]["dept_ids"] = [495]
        return p

    webapp.increment_params = broken_queue
    try:
        check(expect_red(assert_increment_shape), "详情改成按部门硬挑 → 同一批断言必须报红")
    finally:
        webapp.increment_params = _orig

    def fe_problems(html: str, app_js: str) -> list:
        ps2: list = []
        if "btn-increment" not in html:
            ps2.append("index.html 里没有一键增量按钮")
        if "/api/jobs/increment" not in app_js:
            ps2.append("app.js 没打 /api/jobs/increment")
        if "$('#inc-card').style.display = isAdmin" not in app_js:
            ps2.append("一键增量卡没按管理员显隐")
        return ps2

    def assert_fe_ok(h: str, a: str) -> None:
        """检查器本体：有问题就抛错。"""
        ps2 = fe_problems(h, a)
        assert not ps2, "前端有问题：%s" % ("；".join(ps2))

    ps2 = fe_problems(html, app_js)
    check(not ps2, "前端挂点齐全（问题：%s）" % ("；".join(ps2) or "无"))

    def assert_broken_fe():
        assert_fe_ok(html, app_js.replace("$('#inc-card').style.display = isAdmin", ""))

    check(expect_red(assert_broken_fe), "去掉管理员显隐 → 同一批检查必须报出问题")

    print("\n" + "=" * 62)
    if FAILED:
        print("FAIL  %d 项未通过：" % len(FAILED))
        for f in FAILED:
            print("   -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
