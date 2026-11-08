# -*- coding: utf-8 -*-
"""AI 初注（AI 初筛）：把一条帖 + 程序算好的上下文交给模型，回收结论/理由/标签建议。

口径（2026-11-08 用户拍板）：
  · 产出 = 结论建议 + 理由 + 三维标签建议（全套）
  · 触发 = 单条手动 + 批量
  · 采纳 = **必须人工二次确认**；有疑点的建议**不允许批量采纳**
  · 密钥 = **只存服务器**（程序/data/ai_config.json）：不进库、不进发布包、不回显
  · AI 产出只是**建议**，作数的是人工标注（课件字段⑥：AI 初筛结果与人工复核结果并列）

★ 为什么上下文必须由程序组装，不能让模型自己找：
  「回复间隔天数」这类量**库里没有现成字段**（发帖时间 → 首个回复时间），
  模型看不到数据库 → 只能程序现算好喂进去；
  提示词随这条帖的**源数据**变化：有的字段没有就写「无」，不猜、不编。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from .anno import DIM_CN
from .config import ROOT

#: 配置文件位置：**运行时目录**（发布脚本整个 程序/data/ 都不进仓库）
AI_CONFIG_PATH = ROOT / "data" / "ai_config.json"

#: 支持的协议：openai = OpenAI 兼容（/chat/completions）；anthropic = /v1/messages
PROTOCOLS = ("openai", "anthropic")

#: 结论口径（与 sample_review 的三个取值一致）
VERDICTS = ("纳入", "排除", "待核验")

#: 提示词版本 —— 改了提示词就换号，导出里能看出这条建议是哪一版提示词给的
PROMPT_VERSION = "ai-anno-v1"

#: 每层正文最多给模型多少字（超了截断）
MAX_CONTENT_CHARS = 1200

#: 最多给几层（帖子动辄几十楼，全给会爆 token 也看不清重点）
MAX_FLOORS = 12


class AIError(Exception):
    """AI 调用 / 解析失败。

    fatal=True 表示**别再继续跑**（鉴权、额度、网络）——批量时遇它就停手，
    不然一次失败会把后面的钱全烧掉。fatal=False 是这一条的问题（如没正文），跳过即可。
    """

    def __init__(self, msg: str, fatal: bool = True):
        super().__init__(msg)
        self.fatal = fatal


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 配置
def load_config() -> Dict[str, Any]:
    """读服务器上的 AI 配置。**只在本机读**，不随导出、不进数据库。"""
    d: Dict[str, Any] = {}
    if AI_CONFIG_PATH.exists():
        try:
            d = json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            d = {}
    if not isinstance(d, dict):
        d = {}
    return {
        "protocol": d.get("protocol") or "openai",
        "base_url": str(d.get("base_url") or "").strip(),
        "model": str(d.get("model") or "").strip(),
        "api_key": str(d.get("api_key") or "").strip(),
        "timeout": int(d.get("timeout") or 60),
        # ★ 默认**停用**：没配密钥却说「已启用」是误导（界面上看着像能用，一调就报错）
        "enabled": bool(d.get("enabled", False)),
    }


def resolve_config(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """服务器默认配置 + **本次请求带来的个人配置**（成员自己填的那份）。

    ★ 为什么有个人配置（2026-11-08 用户提）：
      管理员配的是**服务器默认**（全组共用一份，费用走管理员）；
      成员可以填**自己的** key —— 存**他自己浏览器**里，费用各归各人。
    ★ 个人配置只在**这一次请求**里用：
      不落盘、不入库、不写日志、不进任务参数（/api/jobs 会把 params 摊开给人看）。
    ★ 个人配置里带了 key → 就算「已启用」，不必依赖服务器那个总开关。
    """
    cfg = load_config()
    ov = override if isinstance(override, dict) else {}
    for k in ("protocol", "base_url", "model", "api_key"):
        v = str(ov.get(k) or "").strip()
        if v:
            cfg[k] = v
    try:
        t = int(ov.get("timeout") or 0)
    except (TypeError, ValueError):
        t = 0
    if 5 <= t <= 300:
        cfg["timeout"] = t
    if str(ov.get("api_key") or "").strip():
        cfg["enabled"] = True
    return cfg


def save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """保存配置。★ api_key 传空字符串 = **不改**（前端不回显密钥，只能重填）。"""
    cfg = load_config()
    for k in ("protocol", "base_url", "model", "api_key", "timeout", "enabled"):
        v = data.get(k)
        if v is None:
            continue
        if k == "api_key" and str(v).strip() == "":
            continue
        cfg[k] = v
    if cfg["protocol"] not in PROTOCOLS:
        raise ValueError("协议只能是 %s" % ("、".join(PROTOCOLS),))
    cfg["timeout"] = int(cfg["timeout"])
    if not (5 <= cfg["timeout"] <= 300):
        raise ValueError("超时要在 5~300 秒之间")
    if cfg["enabled"] and not (cfg["base_url"] and cfg["model"] and cfg["api_key"]):
        raise ValueError("启用前要填 base_url、model 和 api_key")
    AI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AI_CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return public_config(cfg)


def public_config(cfg: Dict[str, Any] = None) -> Dict[str, Any]:
    """对外视图：**不回显密钥**，只回「有没有配」。"""
    cfg = cfg or load_config()
    return {
        "protocol": cfg["protocol"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "timeout": cfg["timeout"],
        "enabled": cfg["enabled"],
        "has_key": bool(cfg["api_key"]),
        # ★ 只给相对路径：绝对路径是本机信息，不往界面/API 里送
        "config_path": "程序/data/ai_config.json",
    }


# ------------------------------------------------------------------ 上下文
def _gap_days(pub_ts: Optional[int], reply_ts: Optional[int]) -> Optional[float]:
    if not pub_ts or not reply_ts or reply_ts < pub_ts:
        return None
    return round((reply_ts - pub_ts) / 86400.0, 2)


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…（截断）"


def build_context(db, tid: int) -> Dict[str, Any]:
    """把一条帖 + 程序算好的派生量组装成给模型的上下文。

    ★ 派生量（回复间隔天数等）**库里没有现成字段**，是这里现算的。
    ★ 缺什么写「无」，不猜 —— 猜出来的数字会被模型当事实用。
    """
    t = db.conn.execute("SELECT * FROM threads WHERE tid=?", (int(tid),)).fetchone()
    if not t:
        raise AIError("帖 %s 不在库里" % tid, fatal=False)
    d = dict(t)
    posts = [dict(r) for r in db.conn.execute(
        "SELECT pid,floor,author,post_time,post_ts,content,chars FROM posts "
        "WHERE tid=? AND pid>0 ORDER BY pid", (int(tid),))]
    if not posts:
        raise AIError("这条还没采到正文（没正文就没法初注）", fatal=False)
    flows = [dict(r) for r in db.conn.execute(
        "SELECT step,name,time FROM flows WHERE tid=? ORDER BY step", (int(tid),))]
    rule_tags: Dict[str, List[str]] = {}
    for dim, tag in db.conn.execute(
            "SELECT dim,tag FROM thread_dim_tags WHERE tid=? ORDER BY dim,tag",
            (int(tid),)):
        rule_tags.setdefault(str(dim), []).append(str(tag))
    kws = [{"word": r["word"], "kind": r["kind"], "n": int(r["post_count"])}
           for r in db.conn.execute(
               "SELECT k.word,k.kind,h.post_count FROM keyword_hits h "
               "JOIN keywords k ON k.id=h.keyword_id WHERE h.tid=? ORDER BY k.word",
               (int(tid),))]
    # 发帖 → 首个回复 的间隔（楼主那一楼不算回复）
    reply_ts = min([int(p["post_ts"]) for p in posts[1:] if p.get("post_ts")],
                   default=None)
    gap = _gap_days(d.get("pub_ts"), reply_ts)
    last_flow = flows[-1] if flows else {}
    return {
        "tid": int(tid),
        "title": d.get("title") or "",
        "url": d.get("url") or "",
        "dept_name": d.get("dept_name") or "",
        "dept_group": d.get("dept_group") or "",
        "dept_district": d.get("dept_district") or "",
        "category": d.get("category") or "",
        "state": d.get("state") or "",
        "light_name": d.get("light_name") or "",
        "mailbox_name": d.get("mailbox_name") or "",
        "author": d.get("author") or "",
        "pub_time": d.get("pub_time") or "",
        "replies": int(d.get("replies") or 0),
        "views": int(d.get("views") or 0),
        "posts_n": len(posts),
        "chars": int(d.get("chars") or 0),
        "reply_gap_days": gap,
        "first_reply_time": (
            next((str(p.get("post_time") or "") for p in posts[1:]
                  if p.get("post_ts") == reply_ts), "") if reply_ts else ""),
        "last_flow": (str(last_flow.get("name") or "")
                      + (("（" + str(last_flow.get("time") or "") + "）")
                         if last_flow.get("time") else "")),
        "rule_tags": rule_tags,
        "keywords": kws,
        "flows": flows,
        "posts": posts,
    }


# ------------------------------------------------------------------ 提示词
def build_prompt(ctx: Dict[str, Any],
                 candidates: Dict[str, List[str]]) -> Tuple[str, str]:
    """拼提示词。★ 随这条帖的源数据变化 —— 有的字段没有就写「无」。"""
    sys_lines = [
        "你是民生舆情标注助手。依据下面这条留言的**原文**与系统给出的**事实**，给出一份初筛建议。",
        "",
        "## 一、核验结论（三选一）",
        "- 纳入：诉求具体、指向明确、属于本地民生事项，可作正式样本。",
        "- 排除：与本地民生无关、信息不足无法核验、纯广告灌水、或与既有样本重复无增量。",
        "- 待核验：材料不够或存在疑点，需要人工再核实才能下判断。",
        "",
        "## 二、三个标签维度（每个维度可给 0~多个标签）",
    ]
    for dim, cn in DIM_CN.items():
        vals = [str(x) for x in (candidates.get(dim) or [])]
        sys_lines.append("- %s（%s）：候选值 %s"
                         % (dim, cn, "、".join(vals) if vals else "（暂无）"))
    sys_lines += [
        "",
        "★ 标签**只能从上面候选值里选**，不要自造词；没把握的维度就别给。",
        "★ 事实以系统给出的为准；原文之外的推断要写进 reason。",
        "★ 回复间隔天数等派生量由系统算出，直接采信，不要自己重算。",
        "★ 有疑点就用「待核验」，并在 reason 里写清疑点在哪。",
        "",
        "## 三、输出（只输出一个 JSON 对象，不要解释）",
        '{"verdict":"纳入|排除|待核验","reason":"一句话理由",'
        '"tags":[{"dim":"relation|process|impact","tag":"候选值"}]}',
    ]

    gap = ctx.get("reply_gap_days")
    gap_txt = ("无（尚未收到回复，或时间未回填）" if gap is None
               else "%.2f 天" % float(gap))
    usr_lines = [
        "## 这条留言",
        "- TID：%s" % ctx.get("tid"),
        "- 标题：%s" % (ctx.get("title") or "无"),
        "- 链接：%s" % (ctx.get("url") or "无"),
        "- 站方分类：%s ｜ 受理状态：%s ｜ 时效灯：%s ｜ 留言箱：%s"
        % (ctx.get("category") or "无", ctx.get("state") or "无",
           ctx.get("light_name") or "无", ctx.get("mailbox_name") or "无"),
        "- 办理部门：%s（分组 %s）%s"
        % (ctx.get("dept_name") or "无", ctx.get("dept_group") or "无",
           ("，区县市 " + str(ctx["dept_district"])) if ctx.get("dept_district") else ""),
        "- 发帖人：%s ｜ 发帖时间：%s"
        % (ctx.get("author") or "无", ctx.get("pub_time") or "无"),
        "- 楼层数：%s ｜ 回复数：%s ｜ 浏览数：%s"
        % (ctx.get("posts_n"), ctx.get("replies"), ctx.get("views")),
        "- **发帖到首个回复的间隔：%s**" % gap_txt,
        "- 首个回复时间：%s" % (ctx.get("first_reply_time") or "无"),
        "- 受理流程最后阶段：%s" % (ctx.get("last_flow") or "无"),
        "",
        "## 系统已算出的三维标签（规则判定，可参考也可推翻）",
    ]
    rt = ctx.get("rule_tags") or {}
    for dim in DIM_CN:
        usr_lines.append("- %s：%s" % (dim, "、".join(rt.get(dim) or []) or "无"))
    usr_lines.append("")
    kws = ctx.get("keywords") or []
    usr_lines.append("## 词表命中")
    if kws:
        for k in kws:
            usr_lines.append("- %s（%s，命中 %s 层）"
                             % (k["word"], k["kind"], k["n"]))
    else:
        usr_lines.append("- 无")
    usr_lines += ["", "## 楼层原文"]
    posts = ctx.get("posts") or []
    for i, p in enumerate(posts[:MAX_FLOORS], 1):
        usr_lines.append("")
        usr_lines.append("### 第 %s 层 · %s · %s"
                         % (p.get("floor") or i, p.get("author") or "匿名",
                            p.get("post_time") or "时间未回填"))
        usr_lines.append(_clip(p.get("content") or "", MAX_CONTENT_CHARS))
    if len(posts) > MAX_FLOORS:
        usr_lines.append("")
        usr_lines.append("（还有 %s 层未给出）" % (len(posts) - MAX_FLOORS))
    return "\n".join(sys_lines), "\n".join(usr_lines)


# ------------------------------------------------------------------ 调用
def call_model(cfg: Dict[str, Any], system: str, user: str) -> str:
    """按协议发一次请求，返回模型文本。两种协议的报文格式不一样，这里分开写。"""
    if cfg["protocol"] == "anthropic":
        b = cfg["base_url"].rstrip("/")
        # base_url 填到 /v1 为止或填到域名为止都认（两种习惯都常见）
        url = b + ("/messages" if b.endswith("/v1") else "/v1/messages")
        headers = {"content-type": "application/json",
                   "x-api-key": cfg["api_key"],
                   "anthropic-version": "2023-06-01"}
        body = {"model": cfg["model"], "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user}]}
    else:
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {"content-type": "application/json",
                   "Authorization": "Bearer " + cfg["api_key"]}
        body = {"model": cfg["model"], "temperature": 0,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
    try:
        r = requests.post(url, headers=headers, json=body,
                          timeout=int(cfg["timeout"]))
    except requests.RequestException as e:
        raise AIError("请求失败：%s" % e)
    if r.status_code >= 400:
        raise AIError("HTTP %s：%s" % (r.status_code, (r.text or "")[:200]))
    try:
        data = r.json()
    except ValueError:
        raise AIError("返回不是 JSON：%s" % (r.text or "")[:200])
    return _extract_text(data, cfg["protocol"])


def _extract_text(data: Dict[str, Any], protocol: str) -> str:
    if protocol == "anthropic":
        parts = data.get("content") or []
        txt = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
    else:
        choices = data.get("choices") or []
        txt = ""
        if choices:
            txt = str((choices[0].get("message") or {}).get("content") or "")
    if not txt.strip():
        raise AIError("模型没给内容：%s"
                      % json.dumps(data, ensure_ascii=False)[:200])
    return txt


# ------------------------------------------------------------------ 解析
_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str) -> Dict[str, Any]:
    m = _JSON_RE.search(text or "")
    if not m:
        raise AIError("模型没给 JSON：%s" % (text or "")[:200])
    try:
        obj = json.loads(m.group(0))
    except ValueError as e:
        raise AIError("JSON 解析失败：%s" % e)
    if not isinstance(obj, dict):
        raise AIError("JSON 不是对象")
    return obj


def parse_ai(text: str, candidates: Dict[str, List[str]]) -> Dict[str, Any]:
    """把模型返回抽成结构化建议，并**按候选标签空间校验**。

    ★ 标签不在候选清单里 → **丢掉并记下来**（与 P2-4「标签空间固定」纪律一致：
      一旦让模型自由造词，统计就没法看了）。人可以在界面上看到被丢的是什么。
    ★ 「疑点」= 结论是待核验，或有标签被丢弃 → 这类建议**必须人工确认**，
      批量采纳时会被跳过。
    """
    obj = _extract_json(text)
    verdict = str(obj.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        verdict = ""
    reason = str(obj.get("reason") or "").strip()
    tags: List[Dict[str, str]] = []
    dropped: List[Dict[str, str]] = []
    for t in (obj.get("tags") or []):
        if not isinstance(t, dict):
            continue
        dim = str(t.get("dim") or "").strip()
        tag = str(t.get("tag") or "").strip()
        if dim not in DIM_CN:
            dropped.append({"dim": dim, "tag": tag, "why": "维度不合法"})
            continue
        allowed = [str(x) for x in (candidates.get(dim) or [])]
        if tag not in allowed:
            dropped.append({"dim": dim, "tag": tag, "why": "不在候选清单里"})
            continue
        if not any(x["dim"] == dim and x["tag"] == tag for x in tags):
            tags.append({"dim": dim, "tag": tag})
    doubts: List[str] = []
    if verdict == "待核验":
        doubts.append("结论是「待核验」")
    if dropped:
        doubts.append("有 %d 个标签不在候选清单里" % len(dropped))
    if not verdict:
        doubts.append("模型没给出合法结论")
    return {"verdict": verdict, "reason": reason, "tags": tags,
            "dropped": dropped, "doubts": doubts,
            "needs_review": bool(doubts)}


# ------------------------------------------------------------------ 服务
class AIService:
    """AI 初注的服务层：调模型 → 校验 → 存建议 → 采纳（走正常提交路径）。"""

    def __init__(self, db, anno, cfg: Optional[Dict[str, Any]] = None):
        self.db = db
        self.anno = anno
        # ★ 个人配置（成员自己填的）：只在这次调用里生效，不落盘
        self.override = cfg if isinstance(cfg, dict) else {}

    def candidates(self) -> Dict[str, List[str]]:
        """候选标签：维度 → 标签**字符串**列表。

        ★ 注意：anno.vocab() 给的是 [{"tag":..,"n":..}]（带命中数），
          这里要的是纯标签值 —— 直接透传会导致所有标签都被判成
          「不在候选清单里」而丢掉（2026-11-08 闸抓到的真 bug）。
        """
        raw = self.anno.vocab().get("candidates") or {}
        out: Dict[str, List[str]] = {}
        for dim, items in raw.items():
            vals = []
            for it in items or []:
                v = it.get("tag") if isinstance(it, dict) else it
                if v is not None:
                    vals.append(str(v))
            out[str(dim)] = vals
        return out

    def _require_ready(self) -> Dict[str, Any]:
        cfg = resolve_config(self.override)
        missing = [k for k in ("base_url", "model", "api_key") if not cfg[k]]
        if missing:
            cn = {"base_url": "base_url", "model": "模型名", "api_key": "api_key"}
            raise AIError(
                "还没配 %s —— 管理员可在「我的账号 → AI 初注设置」配服务器默认，"
                "成员也可以在同一页填自己的（只存本人浏览器）"
                % "、".join(cn.get(k, k) for k in missing))
        return cfg

    def suggest(self, tid: int) -> Dict[str, Any]:
        """对一条帖生成 AI 初注建议并落库。"""
        cfg = self._require_ready()
        ctx = build_context(self.db, int(tid))
        system, user = build_prompt(ctx, self.candidates())
        text = call_model(cfg, system, user)
        out = parse_ai(text, self.candidates())
        out["tid"] = int(tid)
        out["model"] = cfg["model"]
        out["protocol"] = cfg["protocol"]
        out["prompt_version"] = PROMPT_VERSION
        self._save(out)
        return out

    def suggest_batch(self, tids: List[int], limit: int = 20,
                      gap: float = 1.0) -> Dict[str, Any]:
        """批量初注。★ 遇**致命错误**（鉴权/额度/网络）立刻停手，不继续烧钱。"""
        items: List[Dict[str, Any]] = []
        stopped = ""
        for tid in list(tids)[:max(1, int(limit))]:
            try:
                r = self.suggest(int(tid))
                items.append({"tid": int(tid), "ok": True,
                              "verdict": r["verdict"],
                              "needs_review": r["needs_review"]})
            except AIError as e:
                items.append({"tid": int(tid), "ok": False, "error": str(e)[:200]})
                if e.fatal:
                    stopped = str(e)[:200]
                    break
                continue
            if gap:
                time.sleep(gap)
        ok = sum(1 for x in items if x["ok"])
        return {"items": items, "ok": ok, "failed": len(items) - ok,
                "stopped": stopped}

    def _save(self, out: Dict[str, Any]) -> None:
        now = _now()
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO thread_ai_anno(tid,verdict,reason,tags,dropped,model,"
                "protocol,prompt_version,created_at,updated_at,status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'suggested') "
                "ON CONFLICT(tid) DO UPDATE SET verdict=excluded.verdict, "
                "reason=excluded.reason, tags=excluded.tags, dropped=excluded.dropped, "
                "model=excluded.model, protocol=excluded.protocol, "
                "prompt_version=excluded.prompt_version, updated_at=excluded.updated_at, "
                "status='suggested'",
                (int(out["tid"]), out.get("verdict") or "", out.get("reason") or "",
                 json.dumps(out.get("tags") or [], ensure_ascii=False),
                 json.dumps(out.get("dropped") or [], ensure_ascii=False),
                 out.get("model") or "", out.get("protocol") or "",
                 out.get("prompt_version") or PROMPT_VERSION, now, now))
            self.db.conn.commit()

    def get(self, tid: int) -> Dict[str, Any]:
        return self.db.get_ai_anno(int(tid))

    def adopt(self, tid: int, node: str) -> Dict[str, Any]:
        """把 AI 建议采纳为人工标注 —— **走正常提交路径**（与手标同一条路）。"""
        row = self.get(int(tid))
        if not row or not (row.get("verdict") or "").strip():
            raise AIError("这条没有可采纳的 AI 建议")
        tags = [[t["dim"], t["tag"]] for t in (row.get("tags") or [])]
        res = self.anno.submit(node, int(tid), verdict=row.get("verdict") or "",
                                reason=row.get("reason") or "", tags=tags)
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE thread_ai_anno SET status='adopted', updated_at=? WHERE tid=?",
                (_now(), int(tid)))
            self.db.conn.commit()
        return res

    def adopt_batch(self, tids: List[int], node: str) -> Dict[str, Any]:
        """批量采纳。

        ★ **有疑点的建议一律跳过**（结论待核验 / 标签被丢弃 / 结论缺失）：
          用户 2026-11-08 明确「AI 标注注意对应有疑点的标注要求人工确认」→
          批量只处理「机器有把握」的那部分，剩下的留给人。
        """
        adopted: List[int] = []
        skipped: List[Dict[str, Any]] = []
        for tid in list(tids):
            row = self.get(int(tid))
            if not row or not (row.get("verdict") or "").strip():
                skipped.append({"tid": int(tid), "why": "没有 AI 建议"})
                continue
            if row.get("needs_review"):
                skipped.append({"tid": int(tid),
                                "why": "有疑点，需人工确认："
                                       + "；".join(row.get("doubts") or [])})
                continue
            try:
                self.adopt(int(tid), node)
                adopted.append(int(tid))
            except Exception as e:
                skipped.append({"tid": int(tid), "why": str(e)[:120]})
        return {"adopted": len(adopted), "tids": adopted,
                "skipped": skipped, "skipped_n": len(skipped)}
