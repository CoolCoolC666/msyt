# -*- coding: utf-8 -*-
"""闸：舆情卡片（优化报告 P2-6，2026-11-12）。

★ 跑在**临时库**上（不碰真库）；模型用**本地假服务**顶替，不发外网请求。

★ 要守住的性质
  ① 三层字段各归各层：事实/信号由程序算，AI 只填它那 6 项 —— 改不了事实层与等级
  ② 风险等级**规则算**：同样输入两次结果一致；信号变了等级就变；AI 不能直接给等级
  ③ 候选校验：AI 给的 subject/emotion/nature/advice 只能在候选清单里，越界丢弃并记下
  ④ urgent 无依据 → 降级并记疑点；缺概述/性质/建议 → 记疑点（不阻塞显示）
  ⑤ 时效：指纹随源数据变；站方一动 → 卡片标「已过时」；过时的会被重新生成
  ⑥ 幂等：已有卡片且没过时 → 跳过，不重复烧额度
  ⑦ 人工改过的卡片不被自动重新生成覆盖（要 force 才覆盖），人工痕迹保留
  ⑧ 前端挂点齐全；批量走后台任务，成员自己的 key 不进 job.params

★ 必须有**真反向验证**：把实现改坏后，同一批断言必须报红。

用法：python _tools/test_opinion_card.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt import ai, card  # noqa: E402
from msyt.db import DB  # noqa: E402

FAILED: list = []
CALLS: list = []          # 假模型每次收到的请求（数它 = 看有没有烧额度）


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def expect_red(fn) -> bool:
    """fn 里的断言**必须报红**。返回 False = 断言不够狠。

    ★ 捕 Exception 而不是只捕 AssertionError：实现坏掉时可能抛
      IndexError/KeyError，那也是「报红」，漏判成通过等于闸是空的。
    """
    try:
        fn()
    except Exception:
        return True
    return False


# ------------------------------------------------------------------ 假模型
GOOD_CARD = {
    "summary": "市民反映无障碍通道被占用，要求尽快恢复通行",
    "focus": "恢复无障碍通道的通行",
    "subject": "个人",
    "emotion": "中",
    "nature": "民生诉求",
    "stability": "无",
    "advice": "转办",
    "advice_reason": "属地住建部门职责",
    "urgent": False,
    "urgent_reason": "",
    "evidence": "无障碍通道被占用，轮椅无法通过",
    # ★ 故意塞两个越权字段：事实层与等级不许被 AI 改（下面要验）
    "state": "已受理",
    "level": "紧急",
}

BAD_CARD = {
    "summary": "占道经营影响通行",
    "focus": "清理占道经营",
    "subject": "一个小区",     # 候选外
    "emotion": "爆",           # 候选外
    "nature": "外星人",        # 候选外
    "stability": "无",
    "advice": "随便",          # 候选外
    "advice_reason": "看着办",
    "urgent": True,
    "urgent_reason": "",       # 没依据
    "evidence": "店门口占道经营",
}


class _FakeAI(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8")
        body = json.loads(raw)
        text = json.dumps(body, ensure_ascii=False)
        CALLS.append(text)
        # 楼层原文里有「占道经营」→ 返回坏卡片（越界取值 + 无依据的紧急）
        obj = BAD_CARD if "占道经营" in text else GOOD_CARD
        content = json.dumps(obj, ensure_ascii=False)
        out = {"choices": [{"message": {"content": content}}]}
        data = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ------------------------------------------------------------------ 造数据
THREAD_COLS = ("tid,title,url,category,state,dept_name,dept_group,dept_district,"
               "light_code,light_name,mailbox_name,author,author_uid,pub_time,"
               "pub_ts,replies,views,detail_state,posts_n,chars,last_reply_time,"
               "last_reply_ts,first_post_id,last_post_id,extra_dept_names")

THREAD_ROWS = [
    # 2001：干净的一条，只命中「楼主追问 ≥2 轮」→ 1 个信号
    (2001, "无障碍通道被占用", "https://x/1", "投诉", "已回复", "市住建局", "func", "",
     "green", "绿", "23", "市民", "u1", "2026-11-01 09:00:00", 1761958800,
     2, 10, "done", 3, 80, "2026-11-04 09:00:00", 1762218000, 11, 13, ""),
    # 2002：状态说已回复但回复数是 0（矛盾）+ 灰灯（超期）→ 2 个信号
    (2002, "占道经营影响通行", "https://x/2", "建议", "已回复", "综合", "other", "",
     "grey", "灰", "24", "市民", "u2", "2026-10-20 09:00:00", 1760931600,
     0, 3, "done", 2, 40, "", 0, 21, 22, ""),
    # 2003：未受理 + 长期没人管 → 2 个信号
    (2003, "小区噪音扰民长期无人管", "https://x/3", "投诉", "未受理", "城管局", "func", "",
     "none", "未点亮", "23", "市民", "u3", "2026-10-18 09:00:00", 1760758800,
     0, 5, "done", 1, 20, "", 0, 31, 31, ""),
    # 2004：干干净净 → 0 个信号
    (2004, "咨询办理进度", "https://x/4", "咨询", "已回复", "市市场监管局", "func", "",
     "green", "绿", "23", "市民", "u4", "2026-11-10 09:00:00", 1762736400,
     1, 8, "done", 2, 25, "2026-11-10 15:00:00", 1762758000, 41, 42, ""),
]

POST_ROWS = [
    (11, 2001, 1, "市民", "u1", "2026-11-01 09:00:00", 1761958800,
     "无障碍通道被长期占用，轮椅无法通过"),
    (12, 2001, 2, "市民", "u1", "2026-11-02 09:00:00", 1762045200, "还是没人管"),
    (13, 2001, 3, "市民", "u1", "2026-11-03 09:00:00", 1762131600, "已经第三次反映了"),
    (21, 2002, 1, "市民", "u2", "2026-10-20 09:00:00", 1760931600, "店门口占道经营，早高峰很堵"),
    (22, 2002, 2, "市民", "u2", "2026-10-21 09:00:00", 1761018000, "没人管"),
    (31, 2003, 1, "市民", "u3", "2026-10-18 09:00:00", 1760758800, "晚上噪音太大"),
    (41, 2004, 1, "市民", "u4", "2026-11-10 09:00:00", 1762736400, "我想问一下进度"),
    (42, 2004, 2, "市监局", "u9", "2026-11-10 15:00:00", 1762758000, "已办结"),
]


def seed(db) -> None:
    with db.lock:
        for row in THREAD_ROWS:
            db.conn.execute(
                "INSERT INTO threads(%s) VALUES(%s)"
                % (THREAD_COLS, ",".join("?" * len(row))), row)
        for pid, tid, floor, author, uid, pt, pts, content in POST_ROWS:
            db.conn.execute(
                "INSERT INTO posts(pid,tid,floor,author,author_uid,post_time,post_ts,"
                "content,chars) VALUES(?,?,?,?,?,?,?,?,?)",
                (pid, tid, floor, author, uid, pt, pts, content, len(content)))
        db.conn.execute(
            "INSERT INTO flows(tid,step,name,time,ts) VALUES(2001,1,'已受理',"
            "'2026-11-02 10:00:00',1762044000)")
        db.conn.execute(
            "INSERT INTO thread_anno_tags(tid,dim,tag,annotator,annotated_at) "
            "VALUES(2001,'relation','官民','测试员','2026-11-12')")
        db.conn.commit()


def main() -> int:
    print("=" * 62)
    print("闸：舆情卡片（事实/信号/AI 三层 + 规则等级 + 指纹时效）")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="msyt_card_")
    ai.AI_CONFIG_PATH = Path(tmp) / "ai_config.json"      # ★ 不碰服务器上的真配置
    db = DB(os.path.join(tmp, "t.db"))
    seed(db)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%s/v1" % srv.server_address[1]
    cfg = {"protocol": "openai", "base_url": base, "model": "test-model",
           "api_key": "sk-test", "timeout": 30}
    svc = card.CardService(db, cfg)

    # ================================================================= ① 事实层
    print("\n--- ① 事实层：程序算的，AI 改不了 ---")
    f1 = card.build_facts(db, 2001)
    f2 = card.build_facts(db, 2002)
    f3 = card.build_facts(db, 2003)
    f4 = card.build_facts(db, 2004)
    check(f1["state"] == "已回复", "受理状态来自库（%s）" % f1["state"])
    check(f1["replies"] == 2 and f1["posts_n"] == 3,
          "回复数/楼层数来自库（%s/%s）" % (f1["replies"], f1["posts_n"]))
    check(f1["ask_rounds"] == 2,
          "楼主追问轮次算得出来（%s 轮）" % f1["ask_rounds"])
    check(f1["human_tags"].get("relation") == ["官民"],
          "人工标签带进来了（%s）" % f1["human_tags"])
    check(f1["elapsed_days"] is not None and f1["elapsed_days"] > 7,
          "已历时算得出来（%s 天）" % f1["elapsed_days"])
    check(f1["last_flow"].startswith("已受理"), "受理流程最后阶段带进来了")

    # ================================================================= ② 信号层
    print("\n--- ② 研判信号：程序算的异常 ---")
    s1 = card.build_signals(db, 2001, f1)
    s2 = card.build_signals(db, 2002, f2)
    s3 = card.build_signals(db, 2003, f3)
    s4 = card.build_signals(db, 2004, f4)
    n1 = sum(1 for k in card.GENERAL_SIGNALS if s1[k])
    check(s1["ask_rounds_ge2"] is True, "楼主追问 ≥2 轮被抓到")
    check(n1 == 1, "2001 只命中 1 个信号（实际 %d）" % n1)
    check(s2["state_conflict"] is True, "状态矛盾（站方说已回复、回复数却是 0）被抓到")
    check(s2["overdue"] is True, "超期（灰灯）被抓到")
    check(s3["not_accepted"] is True and s3["long_unanswered"] is True,
          "未受理 + 长期无人管被抓到")
    check(not any(s4[k] for k in card.GENERAL_SIGNALS), "干净的帖一个信号都不命中")
    # 站方有更新 → changed
    with db.lock:
        db.conn.execute(
            "INSERT INTO list_changes(ts,source,tid,kind,old_val,new_val,note) "
            "VALUES('2026-11-12','global',2004,'state','未受理','已受理','测试')")
        db.conn.commit()
    s4b = card.build_signals(db, 2004, f4)
    check(s4b["changed"] is True, "站方有更新（状态变了）被抓到")
    with db.lock:
        db.conn.execute("DELETE FROM list_changes WHERE tid=2004")
        db.conn.commit()

    # ================================================================= ③ 等级
    print("\n--- ③ 风险等级：规则算的纯函数 ---")
    lv1 = card.level_of(f1, s1, {})
    lv1b = card.level_of(f1, s1, {})
    check(lv1["level"] == "关注" and lv1b["level"] == lv1["level"],
          "命中 1 个信号 → 关注（%s）" % lv1["level"])
    check(card.level_of(f2, s2, {})["level"] == "预警", "命中 2 个信号 → 预警")
    check(card.level_of(f3, s3, {})["level"] == "预警", "未受理+长期无人管 → 预警")
    check(card.level_of(f4, s4, {})["level"] == "提示", "没命中信号 → 提示")
    check(card.level_of(f4, s4, {"urgent": True, "urgent_reason": "涉人身安全"})["level"]
          == "紧急", "AI 判紧急且有依据 → 紧急")
    check(card.level_of(f1, s1, {"level": "紧急"})["level"] == "关注",
          "AI 不能直接给等级（塞了「紧急」仍是 %s）"
          % card.level_of(f1, s1, {"level": "紧急"})["level"])
    s1b = dict(s1)
    s1b["overdue"] = True
    check(card.level_of(f1, s1b, {})["level"] == "预警", "信号变了 → 等级跟着变")
    why3 = card.level_of(f3, s3, {})["why"]
    check("not_accepted" not in why3 and "long_unanswered" not in why3,
          "等级理由用中文说法，不露英文键名（%s）" % why3)
    check("未受理" in why3, "等级理由里说的是人话（%s）" % why3)

    # ================================================================= ④ 解析
    print("\n--- ④ AI 输出：候选校验 + 疑点 ---")
    ok = card.parse_card(json.dumps(GOOD_CARD, ensure_ascii=False))
    check(ok["subject"] == "个人" and ok["nature"] == "民生诉求"
          and ok["advice"] == "转办", "候选内的取值被保留")
    check(ok["needs_review"] is False, "好卡片无疑点")
    bad = card.parse_card(json.dumps(BAD_CARD, ensure_ascii=False))
    check(bad["subject"] == "" and bad["emotion"] == "" and bad["nature"] == ""
          and bad["advice"] == "", "候选外的取值全被丢弃")
    check(len(bad["dropped"]) >= 4, "丢弃的都记下来了（%d 条）" % len(bad["dropped"]))
    check(bad["urgent"] is False, "没依据的「紧急」被降级")
    check(bool(bad["doubts"]) and bad["needs_review"],
          "疑点被标出来（%s）" % "；".join(bad["doubts"]))
    empty = card.parse_card(json.dumps(
        {"summary": "", "focus": "", "nature": "", "advice": "", "advice_reason": ""},
        ensure_ascii=False))
    check(empty["needs_review"] and len(empty["doubts"]) >= 3,
          "缺概述/性质/建议 → 记疑点（%s）" % "；".join(empty["doubts"]))

    # ================================================================= ⑤ 单条生成
    print("\n--- ⑤ 单条生成：落库 + 分层隔离 + 幂等 ---")
    CALLS.clear()
    c1 = svc.generate(2001)
    check(len(CALLS) == 1, "调了一次模型")
    check(c1["facts"]["state"] == "已回复",
          "事实层没被 AI 改（状态仍是 %s）" % c1["facts"]["state"])
    check(c1["judge"]["level"] == "关注",
          "等级还是规则算的（%s）" % c1["judge"]["level"])
    check((c1["ai"] or {}).get("summary") == GOOD_CARD["summary"], "AI 的概述落在 AI 层")
    row = db.get_card(2001)
    check(row["prompt_version"] == card.PROMPT_VERSION, "落库：记了提示词版本")
    check(row["model"] == "test-model", "落库：记了用的模型")
    check(row["fingerprint"] == card.fingerprint(f1), "落库：记了数据快照指纹")
    check(row["edited_by"] == "", "落库：还没人改过")
    CALLS.clear()
    c1b = svc.generate(2001)
    check(c1b.get("skipped") is True, "幂等：已有卡片且没过时 → 跳过")
    check(len(CALLS) == 0, "幂等：没再调模型（没烧额度）")
    # 坏卡片那条（端到端）
    c2b = svc.generate(2002)
    check(c2b["ai"]["subject"] == "" and bool(c2b["ai"]["dropped"]),
          "端到端：候选外的取值在真实调用里也被丢掉")
    check(c2b["needs_review"] is True, "端到端：坏卡片被标出疑点")
    check(c2b["judge"]["level"] == "预警",
          "端到端：等级还是规则算的（%s）" % c2b["judge"]["level"])

    # ================================================================= ⑥ 时效
    print("\n--- ⑥ 时效：指纹 + 过时判定 ---")
    check(svc.get(2001).get("stale") is False, "源数据没动 → 不过时")
    with db.lock:
        db.conn.execute("UPDATE threads SET replies=5 WHERE tid=2001")
        db.conn.commit()
    c_stale = svc.get(2001)
    check(c_stale.get("stale") is True, "站方有动静 → 卡片过时")
    check(bool(c_stale.get("stale_reason")),
          "过时要说理由（%s）" % c_stale.get("stale_reason"))
    check(card.fingerprint(card.build_facts(db, 2001)) != card.fingerprint(f1),
          "指纹跟着源数据变了")
    CALLS.clear()
    c2 = svc.generate(2001)
    check(len(CALLS) == 1 and not c2.get("skipped"),
          "过时的卡片会重新生成（时效性要求）")

    # ================================================================= ⑦ 人工改
    print("\n--- ⑦ 人工改：留痕 + 不被自动覆盖 ---")
    svc.edit(2001, {"level": "紧急", "advice": "催办", "advice_reason": "催一下",
                    "summary": "人工改过的概述", "note": "人工备注"},
             "tester@example.com")
    row = db.get_card(2001)
    check(row["edited_by"] == "tester@example.com",
          "留了痕（edited_by=%s）" % row["edited_by"])
    check(row["judge"]["level"] == "紧急", "等级改了")
    check((row["ai"] or {}).get("human_note") == "人工备注", "人工备注存下了")
    CALLS.clear()
    c3 = svc.generate(2001)
    check(c3.get("skipped") is True, "自动重新生成不覆盖人工改过的卡片")
    check("人工改过" in (c3.get("reason") or ""), "说清为什么不覆盖")
    check(len(CALLS) == 0, "没调模型（没烧额度）")
    check(db.get_card(2001)["judge"]["level"] == "紧急", "人工改的内容原样还在")
    CALLS.clear()
    svc.generate(2001, force=True)
    check(len(CALLS) == 1, "force 才真的重生成")
    f_now = card.build_facts(db, 2001)
    rule_lv = card.level_of(f_now, card.build_signals(db, 2001, f_now), {})["level"]
    check(db.get_card(2001)["judge"]["level"] == rule_lv
          and rule_lv != "紧急",
          "force 后内容回到规则算的（%s，人工改的「紧急」被覆盖了）"
          % db.get_card(2001)["judge"]["level"])
    check(db.get_card(2001)["edited_by"] == "tester@example.com",
          "force 之后人工痕迹仍然保留（之后自动批量仍会跳过它）")
    try:
        svc.edit(2001, {"level": "最高级"}, "tester@example.com")
        check(False, "非法等级应报错")
    except Exception:
        check(True, "非法等级报错")
    try:
        svc.edit(2999, {"level": "关注"}, "tester@example.com")
        check(False, "没有卡片时编辑应报错")
    except Exception:
        check(True, "没有卡片时编辑报错")

    # ================================================================= ⑧ 静态检查
    print("\n--- ⑧ 接口与前端挂点 ---")
    app_src = (Path(PROJ) / "程序" / "msyt" / "webapp.py").read_text(encoding="utf-8")

    def api_problems(src: str) -> list:
        ps: list = []
        for route in ("/api/threads/{tid}/card", "/api/anno/card/generate",
                      "/api/jobs/card-batch", "/api/anno/card/{tid}/edit",
                      "/api/anno/card/stats"):
            if route not in src:
                ps.append("webapp.py 里少了接口 %s" % route)
        i = src.find("def api_job_card_batch")
        if i < 0:
            ps.append("webapp.py 里找不到批量卡片接口")
        else:
            body = src[i:i + 5000]
            j = body.find("JOBS.create(")
            if j < 0:
                ps.append("批量卡片没走后台任务 → 同步跑，页面看着像死了")
            else:
                seg = body[j:body.find(")", j) + 1]
                for bad in ("api_key", "cfg_override", '"ai"'):
                    if bad in seg:
                        ps.append("job.params 里带了 %s（/api/jobs 会把 params 摊开给人看）"
                                  % bad)
            if '"job"' not in body:
                ps.append("批量卡片没返回 job → 前端拿不到任务号")
        return ps

    def assert_api_ok(src: str) -> None:
        """检查器本体：有问题就抛错。"""
        ps = api_problems(src)
        assert not ps, "批量卡片接口有问题：%s" % ("；".join(ps))

    ps = api_problems(app_src)
    check(not ps, "接口齐全、批量走后台任务、key 不进 job.params（问题：%s）"
          % ("；".join(ps) or "无"))

    def assert_broken_api():
        # ★ 替换要命中「卡片批量」那一段（同一个字面量 AI 批量那边也有，
        #   只替换第一次会改到 AI 批量上 → 检查器反而不会报错）
        bad = app_src.replace(
            'JOBS.create("card_batch", {"tids": tids, "limit": limit}',
            'JOBS.create("card_batch", {"tids": tids, "limit": limit, '
            '"ai": cfg_override}', 1)
        assert "card_batch" in bad and '"ai": cfg_override' in bad, "替换没生效"
        assert_api_ok(bad)       # 坏输入必须让检查器抛错

    check(expect_red(assert_broken_api),
          "把成员自己的 key 塞进 job.params → 同一批检查必须报出问题")

    web = Path(PROJ) / "程序" / "msyt" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    app_js = (web / "app.js").read_text(encoding="utf-8")
    card_js = (web / "card.js").read_text(encoding="utf-8")
    css = (web / "style.css").read_text(encoding="utf-8")

    def fe_problems(html: str, app_js: str, card_js: str, css: str) -> list:
        ps: list = []
        if "dw-card" not in app_js:
            ps.append("app.js 里没有 #dw-card → 详情页没地方放卡片")
        if "renderCard(tid)" not in app_js:
            ps.append("app.js 的 openThread 里没调 renderCard → 详情页看不到卡片")
        for i in ("btn-card-batch", "card-log", "anno-card-batch", "anno-card-log",
                  "anno-card-stop"):
            if i not in html:
                ps.append("index.html 里没有 #%s → 批量入口/进度没地方显示" % i)
        if "pollJob" not in card_js:
            ps.append("card.js 没复用 pollJob → 又写了一份轮询")
        if "localAiPayload" not in card_js:
            ps.append("card.js 没带成员自己的 AI 配置")
        if "ocard-lv" not in css:
            ps.append("style.css 里没有卡片等级样式")
        return ps

    def assert_fe_ok(h: str, a: str, c: str, s: str) -> None:
        """检查器本体：有问题就抛错。"""
        ps = fe_problems(h, a, c, s)
        assert not ps, "前端有问题：%s" % ("；".join(ps))

    ps = fe_problems(html, app_js, card_js, css)
    check(not ps, "前端挂点齐全（问题：%s）" % ("；".join(ps) or "无"))

    def assert_broken_fe():
        assert_fe_ok(html, app_js.replace("renderCard(tid);", ""), card_js, css)

    check(expect_red(assert_broken_fe),
          "去掉详情页的卡片渲染 → 同一批检查必须报出问题")

    # ================================================================= ⑨ 反向验证
    print("\n--- ⑨ 真反向验证：改坏实现后同一批断言必须报红 ---")

    def assert_level_rules():
        lv = card.level_of(f1, s1, {"level": "紧急"})
        assert lv["level"] == "关注", "AI 不能直接给等级（实际 %s）" % lv["level"]
        return True

    check(assert_level_rules(), "等级：AI 给的等级被忽略")
    _orig_level = card.level_of

    def broken_level(facts, signals, ai_out=None):
        return {"level": str((ai_out or {}).get("level") or "提示"),
                "why": "坏实现", "signals": []}

    card.level_of = broken_level
    try:
        check(expect_red(assert_level_rules), "把等级改成 AI 直给 → 同一批断言必须报红")
    finally:
        card.level_of = _orig_level

    def assert_fp_sensitive():
        a = card.fingerprint(f1)
        b = card.fingerprint(dict(f1, state="已受理"))
        assert a != b, "状态变了指纹必须变"
        return True

    check(assert_fp_sensitive(), "指纹：状态一变就变")
    _orig_fp = card.fingerprint
    card.fingerprint = lambda facts: "const-fp"
    try:
        check(expect_red(assert_fp_sensitive), "把指纹写死 → 同一批断言必须报红")
    finally:
        card.fingerprint = _orig_fp

    def assert_candidate_guard():
        o = card.parse_card(json.dumps(BAD_CARD, ensure_ascii=False))
        assert o["subject"] == "", "候选外的 subject 必须丢弃（实际 %s）" % o["subject"]
        return True

    check(assert_candidate_guard(), "候选校验：越界取值被丢弃")
    _orig_subject = card.SUBJECT
    card.SUBJECT = list(_orig_subject) + ["一个小区"]
    try:
        check(expect_red(assert_candidate_guard), "把越界值塞进候选清单 → 同一批断言必须报红")
    finally:
        card.SUBJECT = _orig_subject

    def assert_human_protected():
        c = svc.generate(2001)
        assert c.get("skipped") is True, "人工改过的不该被自动覆盖"
        return True

    check(assert_human_protected(), "人工改过：自动重新生成跳过它")
    _orig_gen = card.CardService.generate

    def broken_generate(self, tid, force=False):
        return _orig_gen(self, tid, force=True)   # ★ 坏实现：无视人工改过

    card.CardService.generate = broken_generate
    try:
        check(expect_red(assert_human_protected), "无视人工改过 → 同一批断言必须报红")
    finally:
        card.CardService.generate = _orig_gen

    srv.shutdown()

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
