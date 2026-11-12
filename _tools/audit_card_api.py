# -*- coding: utf-8 -*-
"""本机 API 冒烟：舆情卡片（优化报告 P2-6，2026-11-12）。

★ **默认不花钱**：只走「已有卡片且没过时 → 跳过」这条幂等路径，
  一次模型调用都不发生。真要生成卡片请在界面点。

★ 自己签令牌而不是登录（沿用其他接口审计的做法，不动用户密码）。

★ 跑法：python _tools/audit_card_api.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJ, "程序"))

from msyt.config import Config  # noqa: E402
from msyt.db import DB  # noqa: E402
from msyt.webapp import make_token  # noqa: E402

BASE = os.environ.get("MSYT_BASE", "http://127.0.0.1:8765")
FAILED: list = []


def check(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILED.append(msg)


def call(path, method="GET", body=None, tok=None):
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=120).read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_detail": e.read().decode("utf-8", "replace")}


def main() -> int:
    print("=" * 62)
    print("闸：舆情卡片 API（本机冒烟，零额度消耗）")
    print("=" * 62)
    cfg = Config.load()
    db = DB(cfg.db_path)
    sec = db.get_meta("jwt_secret")
    arow = db.conn.execute(
        "SELECT email, role, node_id FROM members WHERE role='admin' LIMIT 1").fetchone()
    mrow = db.conn.execute(
        "SELECT email, role, node_id FROM members WHERE role='member' LIMIT 1").fetchone()
    if not arow or not sec:
        print("没有管理员账号或 jwt_secret，先跑一次 web 启动")
        return 1
    atok = make_token({"email": arow["email"], "role": "admin",
                       "node": arow["node_id"] or "admin"}, sec)
    mtok = (make_token({"email": mrow["email"], "role": "member",
                         "node": mrow["node_id"] or ""}, sec) if mrow else "")

    row = db.conn.execute(
        "SELECT tid FROM thread_opinion_cards ORDER BY tid LIMIT 1").fetchone()
    tid = int(row["tid"]) if row else 0

    # ---- ① 未登录 ----
    print("\n--- ① 未登录 ---")
    check(call("/api/anno/card/stats").get("_http") == 401, "卡片统计：未登录 401")
    check(call("/api/anno/card/generate", "POST", {"tid": tid or 1}).get("_http") == 401,
          "单条生成：未登录 401")
    check(call("/api/jobs/card-batch", "POST", {"tids": [tid or 1]}).get("_http") == 401,
          "批量生成：未登录 401")
    check(call("/api/anno/card/%d/edit" % (tid or 1), "POST",
               {"patch": {"level": "关注"}}).get("_http") == 401, "人工改：未登录 401")

    # ---- ② 管理员只读 ----
    print("\n--- ② 取卡与统计 ---")
    d = call("/api/anno/card/stats", tok=atok)
    check("cards" in d and "coverage" in d and "by_level" in d,
          "统计返回成功（已有卡片 %s 条，覆盖 %s%%）"
          % (d.get("cards"), d.get("coverage")))
    n_cards = db.conn.execute("SELECT COUNT(*) FROM thread_opinion_cards").fetchone()[0]
    check(d.get("cards") == n_cards, "统计里的卡片数与库里一致（%s）" % n_cards)
    if tid:
        d = call("/api/threads/%d/card" % tid, tok=atok)
        c = d.get("card") or {}
        check(d.get("exists") is True and c.get("facts"), "取到卡片 %d" % tid)
        check((c.get("judge") or {}).get("level") in ("提示", "关注", "预警", "紧急"),
              "卡片有风险等级（%s）" % (c.get("judge") or {}).get("level"))
        check(bool(c.get("fingerprint")), "卡片带数据快照指纹")
        check(bool(c.get("signal_labels") is not None), "卡片带信号中文标签")
        check("facts" in c and "signals" in c and "ai" in c and "judge" in c,
              "四层字段各归各层")

    # ---- ③ 幂等：已有卡片 → 跳过（不烧额度） ----
    print("\n--- ③ 幂等：已有卡片不重复生成 ---")
    if tid:
        d = call("/api/anno/card/generate", "POST", {"tid": tid}, tok=atok)
        check(d.get("skipped") is True, "单条生成：已有卡片 → 跳过（%s）"
              % (d.get("reason") or "已存在"))
        j = call("/api/jobs/card-batch", "POST", {"tids": [tid]}, tok=atok)
        jid = (j.get("job") or {}).get("id")
        check(bool(jid), "批量生成：拿到任务号")
        if jid:
            res = {}
            for _ in range(40):
                time.sleep(1.5)
                st = call("/api/jobs/%s?tail=100" % jid, tok=atok)
                if st.get("state") != "running":
                    res = st.get("result") or {}
                    logs = " ".join(str(x.get("m") or "") for x in (st.get("logs") or []))
                    break
            check(res.get("skipped", 0) >= 1,
                  "批量生成：已有的那条被跳过（成功 %s / 跳过 %s / 失败 %s）"
                  % (res.get("ok"), res.get("skipped"), res.get("failed")))
            check("跳过" in logs, "批量生成：日志里写明「跳过」（逐条有话说）")

    # ---- ④ 组员可用 + 权限反向验证 ----
    print("\n--- ④ 组员可用 & 权限 ---")
    if mtok:
        d = call("/api/anno/card/stats", tok=mtok)
        check("cards" in d, "组员能看卡片统计")
        if tid:
            d = call("/api/threads/%d/card" % tid, tok=mtok)
            check(d.get("exists") is True, "组员能取卡片")
    d = call("/api/anno/card/999999999/edit", "POST", {"patch": {"level": "关注"}}, tok=atok)
    check(d.get("_http") == 400, "改一张没有卡片的 → 400（不静默建卡）")
    if tid:
        d = call("/api/anno/card/%d/edit" % tid, "POST",
                 {"patch": {"level": "最高级"}}, tok=atok)
        check(d.get("_http") == 400, "非法风险等级 → 400")

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
