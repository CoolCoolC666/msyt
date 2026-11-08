# -*- coding: utf-8 -*-
"""闸：AI 初注（AI 初筛）+ 批量通过 + 领活范围筛选（优化报告 P2-5，2026-11-08）。

★ 跑在**临时库**上（不碰真库）；模型用一个**本地假服务**顶替，不发外网请求。

★ 要守住的性质
  ① 上下文**必须**含程序算的派生量（发帖 → 首个回复的间隔天数），缺就写「无」
  ② 提示词随源数据变化：候选标签清单、维度定义、楼层原文、词表命中、受理流程
  ③ 模型给的标签**只能在候选清单里** —— 不在就丢掉并记下来（不许自由发挥）
  ④ 疑点（结论待核验 / 标签被丢弃 / 结论缺失）→ needs_review=True
  ⑤ **有疑点的不能批量采纳**（用户 2026-11-08：疑点必须人工确认）
  ⑥ 采纳走**正常提交路径**：核验结论 + 人工标签 + 队列完成，三件事一起
  ⑦ 密钥**只存服务器**：对外视图不回显；空值不改已有密钥
  ⑧ 两种协议的请求格式分别对（OpenAI 兼容 / Anthropic）
  ⑨ 领活可按**检索页同一套口径**筛选
  ⑩ 批量通过：打回必须写意见

★ 必须有**真反向验证**：把实现改坏后，同一批断言必须报红。

用法：python _tools/test_ai_anno.py
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

from msyt import ai  # noqa: E402
from msyt.anno import AnnoService  # noqa: E402
from msyt.db import DB  # noqa: E402

FAILED: list[str] = []


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


class _Hub:
    def info(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


# ------------------------------------------------------------------ 假模型服务
SEEN: list[dict] = []


class _FakeAI(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8")
        body = json.loads(raw)
        text = json.dumps(body, ensure_ascii=False)
        SEEN.append({"path": self.path, "headers": dict(self.headers),
                     "body": body})
        # TID 1002 → 有疑点的建议（结论待核验 + 一个候选外的标签）
        if "1002" in text:
            content = json.dumps({
                "verdict": "待核验",
                "reason": "材料不足，需要再核实",
                "tags": [{"dim": "relation", "tag": "官民"},
                         {"dim": "process", "tag": "外星人"}],
            }, ensure_ascii=False)
        else:
            content = json.dumps({
                "verdict": "纳入",
                "reason": "诉求具体，指向本地民生事项",
                "tags": [{"dim": "relation", "tag": "官民"},
                         {"dim": "process", "tag": "阶段：已受理"}],
            }, ensure_ascii=False)
        if self.path.endswith("/v1/messages"):
            out = {"content": [{"type": "text", "text": content}]}
        else:
            out = {"choices": [{"message": {"content": content}}]}
        data = json.dumps(out, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def seed(db) -> None:
    """两条帖：1001 有回复（可算间隔天数）、1002 没有回复。"""
    with db.lock:
        db.conn.execute(
            "INSERT INTO threads(tid,title,url,category,state,dept_name,dept_group,"
            "dept_district,light_name,mailbox_name,author,pub_time,pub_ts,replies,"
            "views,detail_state,posts_n,chars) VALUES"
            "(1001,'无障碍通道被占用','https://x/1','投诉','已受理','市住建局','func',"
            "'','黄','23','市民','2026-11-01 09:00:00',1761958800,2,10,'done',2,60),"
            "(1002,'占道经营影响通行','https://x/2','建议','待核验','综合','other',"
            "'','绿','24','市民','2026-11-02 09:00:00',1762045200,0,3,'done',1,30)")
        db.conn.execute(
            "INSERT INTO posts(pid,tid,floor,author,post_time,post_ts,content,chars) VALUES"
            "(1,1001,1,'市民','2026-11-01 09:00:00',1761958800,'无障碍通道被长期占用，轮椅无法通过',24),"
            "(2,1001,2,'市住建局','2026-11-04 09:00:00',1762218000,'已现场查看，要求整改',16),"
            "(3,1002,1,'市民','2026-11-02 09:00:00',1762045200,'店门口占道经营，早高峰很堵',20)")
        db.conn.execute(
            "INSERT INTO flows(tid,step,name,time,ts) VALUES"
            "(1001,1,'已受理','2026-11-02 10:00:00',1762044000)")
        db.conn.executemany(
            "INSERT INTO thread_dim_tags(tid,dim,tag) VALUES(?,?,?)",
            [(1001, "relation", "官民"), (1001, "process", "阶段：已受理"),
             (1001, "impact", "热度：低"), (1002, "relation", "其它")])
        db.conn.execute(
            "INSERT INTO keywords(word,kind,enabled,created_at) VALUES('无障碍','core',1,'')")
        db.conn.execute(
            "INSERT INTO keyword_hits(keyword_id,tid,post_count) VALUES(1,1001,1)")
        db.conn.commit()


def make_svc(db):
    return AnnoService(db, _Hub())


def main() -> int:
    print("=" * 62)
    print("闸：AI 初注（AI 初筛）+ 批量通过 + 领活范围筛选")
    print("=" * 62)

    tmp = tempfile.mkdtemp(prefix="msyt_ai_")
    ai.AI_CONFIG_PATH = Path(tmp) / "ai_config.json"      # ★ 不碰服务器上的真配置
    db = DB(os.path.join(tmp, "t.db"))
    seed(db)
    svc = make_svc(db)
    ai_svc = ai.AIService(db, svc)

    # ---- 起假模型服务 ----
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%s/v1" % srv.server_address[1]

    # ================================================================= ① 上下文
    print("\n--- ① 上下文：派生量由程序算 ---")
    ctx = ai.build_context(db, 1001)
    check(ctx["reply_gap_days"] == 3.0,
          "发帖→首个回复的间隔 = %.2f 天（实得 %s）"
          % (3.0, ctx["reply_gap_days"]))
    check(ctx["first_reply_time"].startswith("2026-11-04"),
          "首个回复时间 = %s" % ctx["first_reply_time"])
    ctx2 = ai.build_context(db, 1002)
    check(ctx2["reply_gap_days"] is None,
          "没有回复 → 间隔为 None（不是 0）")
    check(ctx["keywords"] and ctx["keywords"][0]["word"] == "无障碍",
          "词表命中带进来了：%s" % ctx["keywords"])
    check(ctx["last_flow"].startswith("已受理"),
          "受理流程最后阶段 = %s" % ctx["last_flow"])
    check(ctx["rule_tags"].get("relation") == ["官民"],
          "规则三维标签带进来了：%s" % ctx["rule_tags"])

    # ================================================================= ② 提示词
    print("\n--- ② 提示词：随源数据变化 ---")
    cands = ai_svc.candidates()
    system, user = ai.build_prompt(ctx, cands)
    check("发帖到首个回复的间隔：3.00 天" in user,
          "提示词里写了间隔天数（3.00 天）")
    check("官民" in system and "阶段：已受理" in system,
          "候选标签清单进了系统提示词")
    check("relation" in system and "process" in system,
          "三个维度的定义进了系统提示词")
    check("无障碍通道被长期占用" in user, "楼层原文进了提示词")
    check("无障碍" in user, "词表命中进了提示词")
    check("已受理" in user, "受理流程进了提示词")
    sys2, user2 = ai.build_prompt(ctx2, cands)
    check("尚未收到回复" in user2,
          "没有回复时提示词写「无」而不是猜一个数")
    check("只需要输出一个 JSON" in system or "只输出一个 JSON" in system,
          "提示词规定了输出格式（JSON）")

    # ================================================================= ③ 解析校验
    print("\n--- ③ 解析：标签只能在候选清单里 ---")
    good = json.dumps({"verdict": "纳入", "reason": "ok",
                       "tags": [{"dim": "relation", "tag": "官民"}]},
                      ensure_ascii=False)
    r = ai.parse_ai(good, cands)
    check(r["verdict"] == "纳入", "合法结论被接受")
    check(len(r["tags"]) == 1 and r["tags"][0]["tag"] == "官民",
          "候选内的标签被保留")
    check(not r["needs_review"], "无疑点时 needs_review=False")

    bad = json.dumps({"verdict": "纳入", "reason": "ok",
                      "tags": [{"dim": "relation", "tag": "外星人"}]},
                     ensure_ascii=False)
    r2 = ai.parse_ai(bad, cands)
    check(r2["tags"] == [], "候选外的标签被丢掉（不许自由发挥）")
    check(r2["dropped"] and r2["dropped"][0]["tag"] == "外星人",
          "被丢的标签记下来了（人能看见）")
    check(r2["needs_review"], "有丢弃标签 → needs_review=True")

    r3 = ai.parse_ai(json.dumps({"verdict": "待核验", "reason": "x"}), cands)
    check(r3["needs_review"], "结论是待核验 → needs_review=True")
    r4 = ai.parse_ai(json.dumps({"verdict": "胡说", "reason": "x"}), cands)
    check(r4["verdict"] == "" and r4["needs_review"],
          "非法结论被清空并算作疑点")

    # ================================================================= ④ 配置
    print("\n--- ④ 配置：密钥只存服务器、不回显 ---")
    cfg = ai.save_config({"protocol": "openai", "base_url": base,
                          "model": "test-model", "api_key": "sk-secret",
                          "timeout": 30, "enabled": True})
    check("api_key" not in cfg, "对外视图**没有** api_key 字段")
    check(cfg["has_key"] is True, "只回「密钥已配置」")
    check(ai.AI_CONFIG_PATH.exists(), "配置文件写在服务器本地")
    saved = json.loads(ai.AI_CONFIG_PATH.read_text(encoding="utf-8"))
    check(saved["api_key"] == "sk-secret", "密钥确实落在这台机器上")
    ai.save_config({"model": "model-2"})          # 空 api_key = 不改
    check(json.loads(ai.AI_CONFIG_PATH.read_text(encoding="utf-8"))["api_key"]
          == "sk-secret", "api_key 传空 = 不覆盖已有密钥")
    try:
        ai.save_config({"protocol": "乱写"})
        check(False, "非法协议应报错")
    except ValueError:
        check(True, "非法协议报错")
    check(":" not in cfg["config_path"] and "\\" not in cfg["config_path"],
          "对外视图不带本机绝对路径：%s" % cfg["config_path"])
    _orig_cfg_path = ai.AI_CONFIG_PATH
    ai.AI_CONFIG_PATH = Path(tmp) / "not_exist.json"
    try:
        check(ai.load_config()["enabled"] is False,
              "没配过 → 默认停用（不是「已启用」却调不通）")
    finally:
        ai.AI_CONFIG_PATH = _orig_cfg_path

    # ================================================================= ⑤ 两种协议
    print("\n--- ⑤ 两种协议的请求格式 ---")
    SEEN.clear()
    ai.call_model(ai.load_config(), "SYS", "USER")
    req = SEEN[-1]
    check(req["path"] == "/v1/chat/completions",
          "OpenAI 兼容打的是 %s" % req["path"])
    check(req["headers"].get("Authorization") == "Bearer sk-secret",
          "OpenAI 兼容用 Authorization 头")
    check(req["body"]["messages"][0]["role"] == "system",
          "OpenAI 兼容把规则放在 system 消息")
    ai.save_config({"protocol": "anthropic", "base_url": base})
    SEEN.clear()
    ai.call_model(ai.load_config(), "SYS", "USER")
    req = SEEN[-1]
    check(req["path"] == "/v1/messages", "Anthropic 打的是 %s" % req["path"])
    check(req["headers"].get("x-api-key") == "sk-secret",
          "Anthropic 用 x-api-key 头")
    check(req["body"].get("system") == "SYS",
          "Anthropic 把规则放在 system 字段")
    check(req["body"]["messages"][0]["role"] == "user",
          "Anthropic 的 messages 只有 user")
    ai.save_config({"protocol": "openai"})

    # ================================================================= ⑥ 单条初注
    print("\n--- ⑥ 单条 AI 初注：落库 + 疑点标记 ---")
    ai.save_config({"model": "test-model"})
    out = ai_svc.suggest(1001)
    check(out["verdict"] == "纳入", "单条初注拿到结论：%s" % out["verdict"])
    check(out["needs_review"] is False, "无疑点")
    row = db.get_ai_anno(1001)
    check(row.get("verdict") == "纳入", "建议落进 thread_ai_anno")
    check(row.get("model") == "test-model", "记了用的模型（可追溯）")
    check(row.get("prompt_version") == ai.PROMPT_VERSION, "记了提示词版本")
    it = svc.item(1001)
    check((it.get("ai") or {}).get("verdict") == "纳入",
          "标注台条目里能拿到这条建议")
    out2 = ai_svc.suggest(1002)
    check(out2["needs_review"] is True,
          "有疑点的建议被标出来（待核验 + 候选外标签）")
    check(db.get_ai_anno(1002).get("needs_review") is True,
          "疑点标记也落在库里（读出来仍能看到）")

    # ================================================================= ⑦ 批量采纳
    print("\n--- ⑦ 批量采纳：疑点必须人工确认 ---")
    svc.enqueue([1001, 1002], "tester")
    res = ai_svc.adopt_batch([1001, 1002], "tester")
    check(res["adopted"] == 1, "只采纳了 1 条（无疑点的那条）")
    check(res["skipped_n"] == 1, "有疑点的被跳过")
    check(any("疑点" in s["why"] for s in res["skipped"]),
          "跳过原因写明是疑点：%s" % res["skipped"])
    rv = db.conn.execute(
        "SELECT verdict,reason FROM sample_review WHERE tid=1001").fetchone()
    check(rv and rv["verdict"] == "纳入", "采纳后核验结论写进人工台账")
    check(rv and rv["reason"], "采纳后理由也写进人工台账")
    tg = db.conn.execute(
        "SELECT COUNT(*) c FROM thread_anno_tags WHERE tid=1001").fetchone()
    check(tg["c"] >= 1, "采纳后人工标签写进 thread_anno_tags")
    q = db.conn.execute(
        "SELECT state FROM anno_queue WHERE tid=1001").fetchone()
    check(q and q["state"] == "done", "采纳后队列里这条置 done")

    # ================================================================= ⑧ 领活筛选
    print("\n--- ⑧ 领活可按检索页同一套口径筛选 ---")
    db2 = DB(os.path.join(tmp, "t2.db"))
    seed(db2)
    svc2 = make_svc(db2)
    r_all = svc2.claim("tester", 20)
    check(r_all["claimed"] == 2, "不筛选时领到 2 条（实得 %s）" % r_all["claimed"])
    db3 = DB(os.path.join(tmp, "t3.db"))
    seed(db3)
    svc3 = make_svc(db3)
    r_one = svc3.claim("tester", 20, category="投诉")
    check(r_one["claimed"] == 1,
          "按类别=投诉 只领到 1 条（实得 %s）" % r_one["claimed"])
    tids = [int(x["tid"]) for x in svc3.queue("tester")]
    check(tids == [1001], "领到的是 1001：%s" % tids)

    # ================================================================= ⑨ 批量通过
    print("\n--- ⑨ 批量通过 / 批量打回 ---")
    svc.review(1001, True, "", "")
    st = db.conn.execute(
        "SELECT status FROM sample_review WHERE tid=1001").fetchone()
    check(st and st["status"] == "approved", "批量通过把状态置 approved")
    try:
        svc.review_batch([1002], False, "", "")
        check(False, "批量打回不写意见应报错")
    except ValueError:
        check(True, "批量打回不写意见报错")

    # ================================================================= ⑩ 反向验证
    print("\n--- ⑩ ★真反向验证 ---")

    def assert_candidate_filter():
        out = ai.parse_ai(bad, cands)
        assert out["tags"] == [], "候选外的标签必须被丢掉"

    def assert_skip_doubts():
        db4 = DB(os.path.join(tmp, "t4.db"))
        seed(db4)
        s4 = make_svc(db4)
        a4 = ai.AIService(db4, s4)
        a4.suggest(1001)
        a4.suggest(1002)
        s4.enqueue([1001, 1002], "tester")
        r = a4.adopt_batch([1001, 1002], "tester")
        assert r["adopted"] == 1, "有疑点的必须被跳过"

    orig_parse = ai.parse_ai

    def bad_parse(text, candidates):
        obj = ai._extract_json(text)
        return {"verdict": str(obj.get("verdict") or ""),
                "reason": str(obj.get("reason") or ""),
                "tags": [{"dim": t.get("dim"), "tag": t.get("tag")}
                         for t in (obj.get("tags") or [])
                         if isinstance(t, dict)],
                "dropped": [], "doubts": [], "needs_review": False}

    ai.parse_ai = bad_parse
    try:
        check(expect_red(assert_candidate_filter),
              "改坏候选校验（放行一切标签）→ 同一批断言必须报红")
    finally:
        ai.parse_ai = orig_parse

    orig_batch = ai.AIService.adopt_batch

    def bad_adopt_batch(self, tids, node):
        adopted = []
        for tid in tids:
            row = self.get(int(tid))
            if not row or not row.get("verdict"):
                continue
            self.adopt(int(tid), node)
            adopted.append(int(tid))
        return {"adopted": len(adopted), "tids": adopted,
                "skipped": [], "skipped_n": 0}

    ai.AIService.adopt_batch = bad_adopt_batch
    try:
        check(expect_red(assert_skip_doubts),
              "改坏「跳过疑点」→ 同一批断言必须报红")
    finally:
        ai.AIService.adopt_batch = orig_batch

    # ============================================== ⑪ 前端：AI 设置卡必须真能打开
    print("\n--- ⑪ 前端静态检查：AI 设置卡 ---")

    WEB = Path(PROJ) / "程序" / "msyt" / "web"

    def frontend_problems(app_js: str, ai_js: str) -> list[str]:
        """返回前端 AI 设置卡的问题清单（空 = 没问题）。"""
        # ★ 先剥掉行注释：注释里提到「藏起来」之类的话不算真问题
        def _strip(src: str) -> str:
            keep = []
            for ln in src.splitlines():
                i = ln.find("//")
                keep.append(ln[:i] if i >= 0 else ln)
            return "\n".join(keep)

        app_js = _strip(app_js)
        ai_js = _strip(ai_js)
        ps: list[str] = []
        me_lines = [ln for ln in app_js.splitlines() if "loadAiConfig" in ln]
        if not me_lines:
            ps.append("app.js 从不调用 loadAiConfig()，AI 设置卡永远不显示")
        elif not any("renderMe()" in ln for ln in me_lines):
            ps.append("loadAiConfig() 不在「我的账号」子页调用")
        if "loadAiConfig" not in ai_js:
            ps.append("ai.js 里没有 loadAiConfig")
        else:
            body = ai_js.split("async function loadAiConfig", 1)[1]
            body = body.split("async function", 1)[0]
            after = body.split("catch", 1)
            if len(after) == 2 and "hidden" in after[1]:
                ps.append("loadAiConfig 的 catch 里又把卡藏起来了（读配置失败就看不见）")
        return ps

    def assert_frontend_ok(app_js: str, ai_js: str) -> None:
        """检查器本体：有问题就抛错。"""
        ps = frontend_problems(app_js, ai_js)
        assert not ps, "前端 AI 设置卡有问题：%s" % ("；".join(ps))

    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    ai_js = (WEB / "ai.js").read_text(encoding="utf-8")
    ps = frontend_problems(app_js, ai_js)
    check(not ps, "前端 AI 设置卡可达（问题：%s）" % ("；".join(ps) or "无"))

    def assert_broken_frontend():
        bad_app = app_js.replace("renderMe(); loadAiConfig();", "renderMe();")
        bad_ai = ai_js.replace(
            "if (st) st.textContent = '读取配置失败：' + (e.message || e);",
            "card.classList.add('hidden');", 1)
        assert_frontend_ok(bad_app, bad_ai)   # 坏输入必须让检查器抛错

    check(expect_red(assert_broken_frontend),
          "改坏前端（不调用/又藏卡）→ 同一批检查必须报出问题")

    # ================================= ⑫ 组员用自己的 API（存他浏览器）+ 进度回显
    print("\n--- ⑫ 组员权限：个人配置 / 不落盘 / 进度回显 ---")
    ov = {"protocol": "anthropic", "base_url": base + "/anthropic",
          "model": "member-model", "api_key": "member-key", "timeout": 45}
    rc = ai.resolve_config(ov)
    check(rc["api_key"] == "member-key", "个人配置覆盖服务器默认")
    check(rc["model"] == "member-model", "模型名也用自己的")
    check(rc["timeout"] == 45, "超时用自己的")
    check(rc["enabled"] is True,
          "自己填了 key → 就算启用（不必依赖服务器那个总开关）")
    check(json.loads(ai.AI_CONFIG_PATH.read_text(encoding="utf-8"))["api_key"]
          == "sk-secret",
          "★ 服务器上的配置文件没被个人配置改动（不落盘）")
    check(ai.resolve_config(None)["api_key"] == "sk-secret",
          "不带个人配置 → 用服务器那份")

    db5 = DB(os.path.join(tmp, "t5.db"))
    seed(db5)
    s5 = make_svc(db5)
    a5 = ai.AIService(db5, s5, ov)
    SEEN.clear()
    a5.suggest(1001)
    hdr = SEEN[-1]["headers"]
    check(hdr.get("x-api-key") == "member-key",
          "模型请求用的是组员自己的 key（%s）" % hdr.get("x-api-key"))
    check(SEEN[-1]["body"].get("model") == "member-model",
          "用的是组员自己填的模型名")

    # ---- 静态检查：webapp.py ----
    app_src = (Path(PROJ) / "程序" / "msyt" / "webapp.py").read_text(encoding="utf-8")

    def batch_problems(src: str) -> list[str]:
        ps: list[str] = []
        i = src.find("def api_anno_ai_batch")
        if i < 0:
            return ["webapp.py 里找不到批量初注接口"]
        body = src[i:i + 5000]
        j = body.find("JOBS.create(")
        if j < 0:
            ps.append("批量初注没走后台任务 → 同步跑，页面看着像死了")
        else:
            seg = body[j:body.find(")", j) + 1]
            for bad in ("api_key", "cfg_override", '"ai"'):
                if bad in seg:
                    ps.append("job.params 里带了 %s（/api/jobs 会把 params 摊开给人看）" % bad)
        if '"job"' not in body:
            ps.append("批量初注没返回 job → 前端拿不到任务号，没法回显")
        k = src.find("def api_anno_ai_suggest")
        if k >= 0 and "require_admin" in src[k:k + 900]:
            ps.append("单条初注是管理员专属 → 组员用不了")
        return ps

    def assert_batch_ok(src: str) -> None:
        """检查器本体：有问题就抛错。"""
        ps = batch_problems(src)
        assert not ps, "批量初注接口有问题：%s" % ("；".join(ps))

    ps = batch_problems(app_src)
    check(not ps, "批量初注走后台任务、key 不进 job.params、组员可用（问题：%s）"
          % ("；".join(ps) or "无"))

    def assert_broken_batch():
        bad = app_src.replace('{"tids": tids, "limit": limit}',
                              '{"tids": tids, "limit": limit, "ai": cfg_override}', 1)
        assert_batch_ok(bad)      # 坏输入必须让检查器抛错

    check(expect_red(assert_broken_batch),
          "把 key 塞进 job.params → 同一批检查必须报出问题")

    # ---- 静态检查：前端（进度回显 + 领活筛选）----
    web = Path(PROJ) / "程序" / "msyt" / "web"
    app_js = (web / "app.js").read_text(encoding="utf-8")
    ai_js = (web / "ai.js").read_text(encoding="utf-8")
    html = (web / "index.html").read_text(encoding="utf-8")

    def fe_problems(app_js: str, ai_js: str, html: str) -> list[str]:
        ps: list[str] = []
        if "ensureAnnoFilters" not in app_js:
            ps.append("app.js 没调 ensureAnnoFilters → 进标注台不补选项")
        if "cloneFilterOptions" not in ai_js or "loadMeta" not in ai_js:
            ps.append("ai.js 的 ensureAnnoFilters 没先拉字典再克隆")
        if "pollAiJob" not in ai_js or "/api/jobs/" not in ai_js:
            ps.append("ai.js 没轮询任务 → 批量初注没有逐条回显")
        if "anno-ai-log" not in html:
            ps.append("index.html 里没有 #anno-ai-log → 没地方显示进度")
        if "ai-local-card" not in html:
            ps.append("index.html 里没有 #ai-local-card → 组员没法配自己的 API")
        return ps

    def assert_fe_ok(a_js: str, i_js: str, htm: str) -> None:
        """检查器本体：有问题就抛错。"""
        ps = fe_problems(a_js, i_js, htm)
        assert not ps, "前端有问题：%s" % ("；".join(ps))

    ps = fe_problems(app_js, ai_js, html)
    check(not ps, "前端：组员能配自己的 API + 有进度回显 + 领活筛选有选项（问题：%s）"
          % ("；".join(ps) or "无"))

    def assert_broken_fe():
        bad_app = app_js.replace("ensureAnnoFilters();", "")
        bad_ai = ai_js.replace("pollAiJob", "gonePollAiJob")
        assert_fe_ok(bad_app, bad_ai, html)   # 坏输入必须让检查器抛错

    check(expect_red(assert_broken_fe),
          "改坏前端（不补选项/无回显）→ 同一批检查必须报出问题")

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
