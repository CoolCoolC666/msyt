# -*- coding: utf-8 -*-
"""舆情卡片（优化报告 P2-6，2026-11-12）—— 详情页上的研判卡。

★ 分层是这个模块的规矩（详见《舆情卡片分类标准-v1》）：
  一 确定性字段  程序算，**AI 不得改写**
  二 研判信号    程序算的异常（超期/矛盾/跨部门/…）
  三 AI 生成     概述 / 诉求焦点 / 诉求主体 / 情绪对抗性 / 涉稳要素 / 处置建议
  四 人工        可改并留痕（人工改过的不被自动覆盖）

★ **风险等级是规则算的**：level_of() 是纯函数 —— 同样的事实 + 同样的信号 + 同样的
  AI 三个标记，必得同样的等级（可复算、可解释）。AI 只提供三个 flag 当输入。

★ **时效性**：生成时记 based_on 指纹；取卡时现算指纹比对 → 不一致就标 stale。
  批量生成靠这个做到幂等（指纹没变就跳过，不重复烧额度）。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional

from . import ai

PROMPT_VERSION = "opinion-card-v1"

#: 事件性质候选（AI 只能在候选里选，选不出来就空着）
NATURE = ["民生诉求", "政策咨询", "处置争议", "服务态度", "质量安全", "涉稳风险"]
#: 情绪对抗性候选
EMOTION = ["低", "中", "高"]
#: 诉求主体候选（个人 / 一家人 / 一片区 / 某单位）
SUBJECT = ["个人", "一家人", "一片区", "某单位"]
#: 处置建议候选
ADVICE = ["转办", "催办", "现场核实", "口径回应", "归档"]
#: 风险等级四档
LEVELS = ["提示", "关注", "预警", "紧急"]

#: 进模型的正文上限（卡片只要结论，不吃长文）
MAX_CONTENT_CHARS = 1200
MAX_FLOORS = 12

#: 研判信号里属于「一般信号」的那些（参与等级判定）
GENERAL_SIGNALS = ("state_conflict", "overdue", "not_accepted", "long_unanswered",
                    "multi_dept", "changed", "high_interaction", "ask_rounds_ge2")

#: 信号的中文说法 —— **单一出处**：界面上显示的、等级理由里写的，都用这份。
#:   （前端另存一份的话，两边迟早跑偏 —— 真出过「理由里露英文键名」的事）
SIGNAL_CN = {
    "state_conflict": "状态矛盾（站方说已回复，回复数却是 0）",
    "overdue": "超期未回复",
    "not_accepted": "未受理",
    "long_unanswered": "长期无人管（过 7 天仍未点亮）",
    "multi_dept": "跨部门协办",
    "changed": "站方有更新（状态/回复/标题变过）",
    "high_interaction": "高互动（回复 ≥ 5）",
    "ask_rounds_ge2": "楼主多次追问（≥ 2 轮）",
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 第一层：事实
def build_facts(db, tid: int) -> Dict[str, Any]:
    """确定性字段。★ 复用 ai.build_context（同一套派生量口径），再补卡片要的几项。

    这里算出来的东西**不许被 AI 改写** —— 卡片的可信度就在这一层。
    """
    ctx = ai.build_context(db, int(tid))
    row = db.conn.execute(
        "SELECT author_uid, extra_dept_names, last_reply_time, last_reply_ts,"
        " detail_state, detail_warn, light_code, first_post_id, last_post_id, pub_ts"
        " FROM threads WHERE tid=?", (int(tid),)).fetchone()
    d = dict(row) if row else {}
    posts = ctx.get("posts") or []
    uid = str(d.get("author_uid") or "")
    # 追问轮次 = 楼主在**自己这帖里**又追加了几楼（匿名帖没有 uid，算不了就记 0）
    # ★ 这里直接查楼层：ai.build_context 返回的楼层**没有 author_uid**，
    #   拿 ctx 里的楼层算会永远是 0（真踩过）。
    ask_rounds = 0
    if uid:
        ask_rounds = int(db.conn.execute(
            "SELECT COUNT(*) n FROM posts WHERE tid=? AND pid>0 "
            "AND COALESCE(author_uid,'')=? AND floor>1",
            (int(tid), uid)).fetchone()["n"])
    human: Dict[str, List[str]] = {}
    for dim, tag in db.conn.execute(
            "SELECT dim,tag FROM thread_anno_tags WHERE tid=? ORDER BY dim,tag",
            (int(tid),)):
        human.setdefault(str(dim), []).append(str(tag))
    # ★ 已历时：从 threads.pub_ts 算（ai.build_context **不返回** pub_ts，
    #   拿 ctx 算会永远是 None → 「长期无人管」这个信号也就永远不触发）
    elapsed = None
    if d.get("pub_ts"):
        elapsed = round((time.time() - int(d["pub_ts"])) / 86400.0, 1)
    state = ctx.get("state") or ""
    return {
        "tid": int(tid),
        "title": ctx.get("title") or "",
        "category": ctx.get("category") or "",
        "state": state,
        "closed": state == "已回复",
        "dept_name": ctx.get("dept_name") or "",
        "extra_dept_names": d.get("extra_dept_names") or "",
        "mailbox_name": ctx.get("mailbox_name") or "",
        "light_code": d.get("light_code") or "",
        "light_name": ctx.get("light_name") or "",
        "author": ctx.get("author") or "",
        "pub_time": ctx.get("pub_time") or "",
        "first_reply_time": ctx.get("first_reply_time") or "",
        "reply_gap_days": ctx.get("reply_gap_days"),
        "last_reply_time": d.get("last_reply_time") or "",
        "last_reply_ts": int(d.get("last_reply_ts") or 0),
        "elapsed_days": elapsed,
        "replies": int(ctx.get("replies") or 0),
        "posts_n": int(ctx.get("posts_n") or 0),
        "chars": int(ctx.get("chars") or 0),
        "ask_rounds": ask_rounds,
        "rule_tags": ctx.get("rule_tags") or {},
        "human_tags": human,
        "keywords": ctx.get("keywords") or [],
        "last_flow": ctx.get("last_flow") or "",
        "detail_state": d.get("detail_state") or "",
        "detail_warn": d.get("detail_warn") or "",
        "first_post_id": int(d.get("first_post_id") or 0),
        "last_post_id": int(d.get("last_post_id") or 0),
        # 楼层原文：提示词要用（不进确定性字段的展示层，只喂模型）
        "posts": posts,
    }


# ------------------------------------------------------------------ 第二层：信号
def build_signals(db, tid: int, facts: Dict[str, Any]) -> Dict[str, Any]:
    """研判信号：程序算出来的异常。★ 全部可复算，不含任何模型输出。

    判据里的时效灯口径来自站方（parsers.py 实测）：
      green=3个工作日内回复 / yellow=5个工作日内 / grey=5个工作日外
      warn=24小时未受理 / none=未点亮（尚未回复或未满24小时）
    """
    light = facts.get("light_code") or ""
    state = facts.get("state") or ""
    # 状态自相矛盾：站方说「已回复」，但回复数是 0，而楼层里确实有内容
    conflict = (state == "已回复" and int(facts.get("replies") or 0) == 0
                and int(facts.get("posts_n") or 0) >= 2)
    elapsed = facts.get("elapsed_days")
    changed = False
    try:
        row = db.conn.execute(
            "SELECT COUNT(*) n FROM list_changes WHERE tid=? AND kind IN "
            "('state','replies','title')", (int(tid),)).fetchone()
        changed = bool(row and int(row["n"]) > 0)
    except Exception:                                   # noqa: BLE001
        changed = False
    return {
        "state_conflict": conflict,
        "overdue": light in ("grey", "warn"),
        "not_accepted": state in ("", "未受理"),
        # 一直没点亮（none）且已经过了 7 天 → 长期无人管
        "long_unanswered": bool(light == "none" and elapsed is not None
                                and elapsed >= 7),
        "multi_dept": bool((facts.get("extra_dept_names") or "").strip()),
        "changed": changed,
        "high_interaction": int(facts.get("replies") or 0) >= 5,
        "ask_rounds_ge2": int(facts.get("ask_rounds") or 0) >= 2,
    }


# ------------------------------------------------------------------ 风险等级
def level_of(facts: Dict[str, Any], signals: Dict[str, Any],
             ai_out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """风险等级：**规则算**的纯函数（同样输入必得同样结果）。

    ★ 为什么不让 AI 直接给等级：等级是**分发依据**，必须可复算、可解释；
      AI 只能提供 flag 当输入（事件性质 / 是否紧急），人可以改。
    """
    ai_out = ai_out or {}
    hits = [k for k in GENERAL_SIGNALS if signals.get(k)]
    nature = str(ai_out.get("nature") or "")
    closed = bool(facts.get("closed"))
    if ai_out.get("urgent"):
        return {"level": "紧急",
                "why": "AI 判定涉人身安全 / 越级 / 群体聚集",
                "signals": hits}
    if nature in ("质量安全", "涉稳风险") and not closed:
        return {"level": "预警",
                "why": "事件性质是「%s」且尚未闭环" % nature,
                "signals": hits}
    if len(hits) >= 2:
        return {"level": "预警",
                "why": "命中 %d 个研判信号：%s"
                       % (len(hits), "、".join(SIGNAL_CN.get(k, k) for k in hits)),
                "signals": hits}
    if len(hits) == 1:
        return {"level": "关注",
                "why": "命中 1 个研判信号：%s" % SIGNAL_CN.get(hits[0], hits[0]),
                "signals": hits}
    return {"level": "提示", "why": "没有命中研判信号", "signals": hits}


# ------------------------------------------------------------------ 指纹
def fingerprint(facts: Dict[str, Any]) -> str:
    """卡片绑定的**数据快照指纹**。站方一动，指纹就变 → 卡片变过时。"""
    raw = "|".join(str(x) for x in (
        facts.get("last_post_id"), facts.get("last_reply_ts"), facts.get("state"),
        facts.get("replies"), facts.get("posts_n"), facts.get("light_code"),
        facts.get("detail_state"), facts.get("last_flow")))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ 提示词
SYS_CARD = (
    "你是民生舆情研判助手。依据下面这条留言的**原文**与系统给出的**事实**，"
    "输出一份舆情卡片的判断。只输出 JSON，不要任何解释文字。格式：",
    '{"summary":"不超过80字，一句话说清谁对谁提了什么要求",',
    ' "focus":"诉求焦点：诉求指向的具体事项",',
    ' "subject":"个人|一家人|一片区|某单位",',
    ' "emotion":"低|中|高",',
    ' "nature":"民生诉求|政策咨询|处置争议|服务态度|质量安全|涉稳风险",',
    ' "stability":"涉稳要素：群体性/串联/越级/极端表述；没有就写「无」",',
    ' "advice":"转办|催办|现场核实|口径回应|归档",',
    ' "advice_reason":"一句话理由",',
    ' "urgent":false,',
    ' "urgent_reason":"只有确实涉人身安全、明确越级或群体聚集时才写依据句，否则留空",',
    ' "evidence":"原文里能支撑上面判断的一句话（照抄，别改写）"}',
    "规则：subject / emotion / nature / advice 只能从上面的候选里选，选不出来就写空字符串；",
    "urgent=true 必须能在 evidence 里找到依据，否则给 false；",
    "不要编造原文里没有的部门、时间、人名、数字。",
)


def build_prompt(facts: Dict[str, Any], signals: Dict[str, Any]) -> tuple:
    """拼提示词。★ 随这条帖的源数据变化 —— 缺的写「无」，不猜。"""
    lines = [
        "标题：%s" % (facts.get("title") or "无"),
        "站方类别：%s" % (facts.get("category") or "无"),
        "受理状态：%s" % (facts.get("state") or "（空）"),
        "办理部门：%s" % (facts.get("dept_name") or "无"),
        "协办部门：%s" % (facts.get("extra_dept_names") or "无"),
        "留言箱：%s" % (facts.get("mailbox_name") or "无"),
        "发帖时间：%s" % (facts.get("pub_time") or "无"),
        "首个回复时间：%s" % (facts.get("first_reply_time") or "无"),
        "发帖→首个回复间隔：%s" % (
            ("%.2f 天" % facts["reply_gap_days"])
            if facts.get("reply_gap_days") is not None else "无"),
        "最后回复时间：%s" % (facts.get("last_reply_time") or "无"),
        "已历时：%s" % (("%.1f 天" % facts["elapsed_days"])
                       if facts.get("elapsed_days") is not None else "无"),
        "时效灯：%s" % (facts.get("light_name") or "无"),
        "楼层数：%s ｜ 回复数：%s ｜ 字数：%s" % (facts.get("posts_n"),
                                          facts.get("replies"), facts.get("chars")),
        "楼主追问轮次：%s" % facts.get("ask_rounds"),
        "受理流程最后阶段：%s" % (facts.get("last_flow") or "无"),
        "研判信号（程序算的）：%s" % ("、".join(k for k in GENERAL_SIGNALS
                                   if signals.get(k)) or "无"),
        "规则三维标签：%s" % json.dumps(facts.get("rule_tags") or {},
                                    ensure_ascii=False),
        "人工标签：%s" % json.dumps(facts.get("human_tags") or {},
                                 ensure_ascii=False),
        "词表命中：%s" % json.dumps(facts.get("keywords") or [],
                                 ensure_ascii=False),
        "楼层原文：",
    ]
    for p in (facts.get("posts") or [])[:MAX_FLOORS]:
        body = str(p.get("content") or "")[:MAX_CONTENT_CHARS]
        lines.append("[%s楼 %s %s] %s" % (p.get("floor"), p.get("author") or "",
                                         p.get("post_time") or "", body))
    # ★ 返回 (system, user) —— 与 ai.build_prompt 同一形状（调用方按两段用）
    return "\n".join(SYS_CARD), "\n".join(lines)


# ------------------------------------------------------------------ 解析
def parse_card(text: str) -> Dict[str, Any]:
    """解析模型输出。★ 候选外的值一律丢弃并记下来（人可见），不许自由发挥。"""
    obj = ai._extract_json(text)                       # 同一套 JSON 抽取，复用
    out: Dict[str, Any] = {}
    dropped: List[str] = []

    def pick(key: str, candidates: List[str]) -> str:
        v = str(obj.get(key) or "").strip()
        if not v:
            return ""
        if v not in candidates:
            dropped.append("%s：%s（不在候选清单里）" % (key, v))
            return ""
        return v

    out["summary"] = str(obj.get("summary") or "").strip()[:200]
    out["focus"] = str(obj.get("focus") or "").strip()[:200]
    out["subject"] = pick("subject", SUBJECT)
    out["emotion"] = pick("emotion", EMOTION)
    out["nature"] = pick("nature", NATURE)
    out["advice"] = pick("advice", ADVICE)
    out["stability"] = str(obj.get("stability") or "").strip()[:200]
    out["advice_reason"] = str(obj.get("advice_reason") or "").strip()[:200]
    out["evidence"] = str(obj.get("evidence") or "").strip()[:300]
    urgent_reason = str(obj.get("urgent_reason") or "").strip()[:200]
    urgent = bool(obj.get("urgent"))
    if urgent and not urgent_reason:
        # ★ 说紧急却不给依据 → 不采信，降级为非紧急并记疑点
        urgent = False
        dropped.append("urgent：标了紧急但没给依据")
    out["urgent"] = urgent
    out["urgent_reason"] = urgent_reason
    out["dropped"] = dropped
    doubts: List[str] = []
    if not out["summary"]:
        doubts.append("没有概述")
    if not out["nature"]:
        doubts.append("没有事件性质")
    if not out["advice"] and not out["advice_reason"]:
        doubts.append("没有处置建议")
    if dropped:
        doubts.append("有 %d 处取值不在候选清单里" % len(dropped))
    if out["stability"] and out["stability"] != "无" and out["nature"] != "涉稳风险":
        doubts.append("写了涉稳要素，但事件性质不是「涉稳风险」")
    out["doubts"] = doubts
    out["needs_review"] = bool(doubts)
    return out


# ------------------------------------------------------------------ 服务
class CardService:
    """舆情卡片服务：生成 → 落库 → 取（带时效判定）→ 人工改。"""

    def __init__(self, db, cfg: Optional[Dict[str, Any]] = None):
        self.db = db
        # ★ 个人配置（成员自己填的，存他浏览器里）：只在这次调用里用，不落盘
        self.cfg = cfg if isinstance(cfg, dict) else {}

    def _cfg(self) -> Dict[str, Any]:
        cfg = ai.resolve_config(self.cfg)
        missing = [k for k in ("base_url", "model", "api_key") if not cfg.get(k)]
        if missing:
            raise ai.AIError("还没配 %s —— 管理员可在「我的账号 → AI 初注设置」配"
                             "服务器默认，成员也可以在同一页填自己的"
                             % "、".join(missing))
        return cfg

    def generate(self, tid: int, force: bool = False) -> Dict[str, Any]:
        """生成一张卡并落库。

        ★ **幂等**：已有卡片且指纹没变 → 直接返回，**不重复烧额度**。
        ★ **人工改过的卡片不覆盖**（除非 force）—— 覆盖要人点头。
        ★ 已有卡片但**已过时**（站方有动静）→ 重新生成（时效性要求）。
        """
        tid = int(tid)
        old = self.db.get_card(tid)
        if old and not force:
            cur = self.get(tid)
            if old.get("edited_by"):
                # ★ 人工改过 → 不覆盖。**不算失败**（批量里记「跳过」）——
                #   否则人改过的帖每跑一次批量就报一条错，看着像坏了。
                cur["skipped"] = True
                cur["reason"] = ("这条卡片人工改过（%s），不覆盖；"
                                 "要重生成请用「强制重生成」" % old.get("edited_by"))
                return cur
            if cur and not cur.get("stale"):
                cur["skipped"] = True
                return cur
        facts = build_facts(self.db, tid)
        signals = build_signals(self.db, tid, facts)
        cfg = self._cfg()
        system, user = build_prompt(facts, signals)
        text = ai.call_model(cfg, system, user)
        out = parse_card(text)
        level = level_of(facts, signals, out)
        fp = fingerprint(facts)
        # ★ force 重生成：内容换成 AI 的，但**人工痕迹保留** ——
        #   这条被人动过，之后的自动批量仍要跳过它（不许悄悄换掉人看过的结论）。
        keep = (old.get("edited_by") or "") if (force and old) else ""
        self.db.save_card(tid, {
            "facts": facts,
            "signals": signals,
            "ai": out,
            "judge": level,
            "advice": {"advice": out.get("advice") or "",
                       "advice_reason": out.get("advice_reason") or ""},
            "fingerprint": fp,
            "model": cfg.get("model") or "",
            "protocol": cfg.get("protocol") or "",
            "prompt_version": PROMPT_VERSION,
        }, edited_by=keep)
        return self.get(tid)

    def get(self, tid: int) -> Dict[str, Any]:
        """取卡 + **时效判定**（指纹比对）。"""
        tid = int(tid)
        row = self.db.get_card(tid)
        if not row:
            return {}
        now_fp = ""
        try:
            now_fp = fingerprint(build_facts(self.db, tid))
        except Exception:                               # noqa: BLE001
            now_fp = ""
        stale = bool(now_fp and now_fp != (row.get("fingerprint") or ""))
        row["stale"] = stale
        row["current_fingerprint"] = now_fp
        ai_out = row.get("ai") or {}
        signals = row.get("signals") or {}
        row["signal_labels"] = {k: SIGNAL_CN.get(k, k) for k in GENERAL_SIGNALS
                                if signals.get(k)}
        row["doubts"] = ai_out.get("doubts") or []
        row["needs_review"] = bool(row["doubts"])
        if (row.get("facts") or {}).get("detail_state") == "stale":
            # 站方又回复了 → 正文本身也过时，卡片不可用
            row["stale_reason"] = "站方有更新（正文也过时）"
        elif stale:
            row["stale_reason"] = "站方在这条帖上又有动静，卡片对应的是旧快照"
        else:
            row["stale_reason"] = ""
        return row

    def edit(self, tid: int, patch: Dict[str, Any], editor: str) -> Dict[str, Any]:
        """人工改卡片（留痕）。★ 改完的卡片不被自动重新生成覆盖。"""
        tid = int(tid)
        row = self.db.get_card(tid)
        if not row:
            raise ai.AIError("这条还没有卡片")
        judge = dict(row.get("judge") or {})
        advice = dict(row.get("advice") or {})
        ai_out = dict(row.get("ai") or {})
        if "level" in patch:
            lv = str(patch.get("level") or "")
            if lv not in LEVELS:
                raise ai.AIError("风险等级只能是 %s" % "、".join(LEVELS))
            judge["level"] = lv
            judge["why"] = str(patch.get("why") or judge.get("why") or "人工改判")
        if "advice" in patch:
            advice["advice"] = str(patch.get("advice") or "")
            advice["advice_reason"] = str(patch.get("advice_reason") or "")
        if "summary" in patch:
            ai_out["summary"] = str(patch.get("summary") or "")
        if "note" in patch:
            ai_out["human_note"] = str(patch.get("note") or "")[:500]
        self.db.save_card(tid, {
            "facts": row.get("facts") or {},
            "signals": row.get("signals") or {},
            "ai": ai_out,
            "judge": judge,
            "advice": advice,
            "fingerprint": row.get("fingerprint") or "",
            "model": row.get("model") or "",
            "protocol": row.get("protocol") or "",
            "prompt_version": row.get("prompt_version") or PROMPT_VERSION,
        }, edited_by=editor or "")
        return self.get(tid)

