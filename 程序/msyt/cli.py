# -*- coding: utf-8 -*-
"""
命令行入口
================================================================
两条链路是**分开的两个命令**，因为它们的代价、频率、失败模式都不一样：
    msyt list     —— 列表采集（拿元数据，便宜）
    msyt detail   —— 详情采集（拿正文，贵，一帖 N×2 次请求）

常用：
    python -m msyt doctor
    python -m msyt list --sources global --pages 5
    python -m msyt list --sources dept:495,737 --pages 500
    python -m msyt detail --limit 50
    python -m msyt search "违停" --scope all
    python -m msyt stats
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import List

from .config import Config
from .db import DB
from .detail import DetailCollector
from .doctor import format_report, run_doctor
from .http_client import Client
from .listpage import ListCollector
from .sources import describe, expand_sources
from .taxonomy import CATEGORIES, LIGHTS, MAILBOXES, STATES


def _cfg(args) -> Config:
    cfg = Config.load(getattr(args, "config", None))
    if getattr(args, "interval", None) is not None:
        cfg.interval = args.interval
    if getattr(args, "list_interval", None) is not None:
        cfg.list_interval = args.list_interval
    cfg.ensure_dirs()
    return cfg


def _db(cfg) -> DB:
    return DB(cfg.db_path)


# ================================================================== 命令
def cmd_doctor(args) -> int:
    cfg = _cfg(args)
    client = Client(cfg, verbose=True)
    db = _db(cfg) if args.with_db else None
    res = run_doctor(client, db, deep=args.deep)
    print(format_report(res))
    if db:
        db.log("doctor", "-", "run", res["ok"], res["summary"])
    return 0 if res["ok"] else 1


def cmd_taxonomy(args) -> int:
    cfg = _cfg(args)
    client = Client(cfg, verbose=True)
    db = _db(cfg)
    lc = ListCollector(db, client, cfg)
    tax = lc.collect_taxonomy()
    n = db.backfill_dept_meta()
    print(f"[taxonomy] 已按新字典重算 {n} 条帖子的分组/区县市")
    print(json.dumps({
        "depts": len(tax["depts"]),
        "districts": len(tax["districts"]),
        "site_stats": tax["site_stats"].__dict__,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_list(args) -> int:
    cfg = _cfg(args)
    client = Client(cfg, verbose=True)
    db = _db(cfg)
    lc = ListCollector(db, client, cfg)

    depts = [dict(r) for r in db.conn.execute(
        "SELECT typeid,name,group_code,group_name,is_district FROM depts")]
    srcs = expand_sources(args.sources, depts)
    if not srcs:
        print("没有解析出任何采集源。用 --sources 指定，例如 global / dept:495 / group:func")
        return 2
    print("本次采集源：")
    print(describe(srcs))

    if args.taxonomy or not depts:
        lc.collect_taxonomy()

    results = []
    for s in srcs:
        res = lc.collect(s, max_pages=args.pages, resume=not args.restart,
                         stop_after=args.stop_after)
        results.append(res)

    # 全局视图采完 → 定水位线
    cutoff = lc.set_cutoff_from_global()
    if cutoff:
        lc.apply_cutoff(cutoff)

    # 入队详情（受水位线约束）
    tids = [r[0] for r in db.conn.execute(
        "SELECT tid FROM threads WHERE COALESCE(detail_state,'pending')='pending'")]
    q, sk = db.enqueue_details(tids, cutoff)
    print(f"\n[queue] 新入队 {q} 条，水位线跳过 {sk} 条")

    print("\n" + json.dumps(results, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_detail(args) -> int:
    cfg = _cfg(args)
    client = Client(cfg, verbose=True)
    db = _db(cfg)
    if args.retry_failed:
        n = db.conn.execute(
            "UPDATE detail_queue SET state='pending' WHERE state='failed'").rowcount
        db.conn.commit()
        print(f"[detail] 重置 {n} 条失败任务为待采")
    dc = DetailCollector(db, client, cfg)

    if args.tid:
        # 定点采集：绕过队列，直接采指定的 tid（调试/补采用）
        out = []
        for tid in args.tid:
            res = dc.collect_one(int(tid),)
            out.append(res)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if all(r["ok"] for r in out) else 1

    res = dc.run(limit=args.limit, max_attempts=args.max_attempts)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_search(args) -> int:
    cfg = _cfg(args)
    db = _db(cfg)
    res = db.search(
        args.query, scope=args.scope, dept=args.dept, group=args.group,
        district=args.district, category=args.category, state=args.state,
        mailbox=args.mailbox, light=args.light, date_from=args.date_from,
        date_to=args.date_to, limit=args.limit, offset=args.offset,
        sort=args.sort, min_fts_chars=args.min_fts_chars, op=args.op)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"命中 {res['total']} 条 / 模式 {res['mode']} / 耗时 {res['took_ms']}ms "
          f"/ 范围 {res['scope']}")
    if res["mode"] == "like":
        print("提示：查询词短于 FTS 阈值，已整体退回 LIKE 模式（结果可能慢，但不会漏）")
    for it in res["items"]:
        print(f"\n[{it['tid']}] {it['title']}")
        meta = []
        if it.get("dept_name"):
            meta.append(it["dept_name"])
        if it.get("category"):
            meta.append(it["category"])
        if it.get("state"):
            meta.append(it["state"])
        if it.get("pub_time"):
            meta.append(it["pub_time"])
        if it.get("replies") is not None:
            meta.append(f"回复{it['replies']}")
        if it.get("detail_state"):
            meta.append(f"详情:{it['detail_state']}")
        print("   " + " | ".join(meta))
        if it.get("snippet"):
            print("   " + it["snippet"][:160].replace("\n", " "))
    return 0


def cmd_stats(args) -> int:
    cfg = _cfg(args)
    db = _db(cfg)
    st = db.stats()
    st["site_stats"] = db.latest_site_stats()
    print(json.dumps(st, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_integrity(args) -> int:
    cfg = _cfg(args)
    db = _db(cfg)
    client = Client(cfg, verbose=False)
    dc = DetailCollector(db, client, cfg)
    res = dc.integrity()
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0 if not res["problems"] else 1


def cmd_sources(args) -> int:
    cfg = _cfg(args)
    db = _db(cfg)
    depts = [dict(r) for r in db.conn.execute(
        "SELECT typeid,name,group_code,group_name,is_district FROM depts")]
    srcs = expand_sources(args.sources, depts)
    print(describe(srcs) if srcs else "（无）")
    print("\n可用维度：")
    print("  维度名   含义                     例")
    print("  global   全站视图                 global")
    print("  dept     部门/单位（109 个）      dept:495,972")
    print("  group    部门分组                 group:func,district,enterprise,other")
    print("  district 区县市（10 个）          district:737")
    print("  category 类别（8 类）             category:1,2")
    print("  state    受理状态（3 类）         state:1,2,999")
    print("  mailbox  留言箱（2 个）           mailbox:23,24")
    print("  dateline 时间窗（秒）            dateline:2592000")
    print("\n各维度取值：")
    for label, lst in (("category", CATEGORIES), ("state", STATES),
                       ("mailbox", MAILBOXES), ("light", LIGHTS)):
        print(f"  {label:9} " + ", ".join(f"{x['code']}={x['name']}" for x in lst))
    return 0


def cmd_export(args) -> int:
    cfg = _cfg(args)
    db = _db(cfg)
    os.makedirs(cfg.export_dir, exist_ok=True)
    path = os.path.join(cfg.export_dir, args.out)
    rows = db.conn.execute(
        "SELECT t.tid,t.title,t.url,t.pub_time,t.pub_day,t.dept_name,t.dept_group,"
        "t.dept_district,t.category,t.state,t.mailbox_id,t.light_code,t.light_name,"
        "t.author,t.replies,t.views,t.detail_state,t.posts_n,t.chars,"
        "t.first_post_id,t.last_post_id,t.pages_total,"
        "(SELECT group_concat(content,'\\n') FROM posts p WHERE p.tid=t.tid) AS full_text "
        "FROM threads t ORDER BY t.pub_ts ASC").fetchall()
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if rows:
            w.writerow(rows[0].keys())
            for r in rows:
                w.writerow(list(r))
    print(f"已导出 {len(rows)} 条 → {path}")
    return 0


# ================================================================== 入口
def _has_ticket_file() -> bool:
    """当前目录（或包所在目录）里有没有 `agent-ticket.txt`。

    ★ 那是**网站登录后签发的一次性票据**。有它就不用问密码 ——
      agent 会自己读它、换成 JWT、直接开跑。
    """
    from pathlib import Path
    cands = [Path.cwd() / "agent-ticket.txt",
              Path(__file__).resolve().parent.parent / "agent-ticket.txt"]
    for c in cands:
        try:
            if c.exists():
                for line in c.read_text(encoding="utf-8").splitlines():
                    if line.strip() and not line.strip().startswith("#"):
                        return True
        except Exception:                             # noqa: BLE001
            continue
    return False


def _ask_password(prompt: str = "  密码    : ") -> str:
    """问密码，**带 `*` 回显**。

    ★ 为什么要自己写而不用 `getpass.getpass()`：
      Windows 的 getpass 走 msvcrt.getwch()，**一个字符都不回显**，
      光标也不动 —— 看着就像程序卡死了。
      实测有组员以为「密码无法输入」，其实密码早就读进去了（登录是成功的），
      只是屏幕上什么都没有。
      这里改成：每敲一个字符打一个 `*`，让人看得见「我确实在输入」，
      密码本身仍然不落到屏幕上。

    其它平台用 getpass（终端原生支持关回显）；都失败就退回普通 input。
    """
    import sys as _sys
    try:
        if _sys.platform.startswith("win"):
            import msvcrt
            _sys.stdout.write(prompt)
            _sys.stdout.flush()
            buf = []
            while True:
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    break
                if ch in ("\x08", "\x7f"):            # 退格
                    if buf:
                        buf.pop()
                        _sys.stdout.write("\b \b")
                        _sys.stdout.flush()
                    continue
                if ch == "\x03":                    # Ctrl+C
                    raise KeyboardInterrupt
                buf.append(ch)
                _sys.stdout.write("*")
                _sys.stdout.flush()
            _sys.stdout.write("\n")
            _sys.stdout.flush()
            return "".join(buf)
        import getpass
        return getpass.getpass(prompt)
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        return input(prompt)


def cmd_agent(args) -> int:
    """成员端采集代理（Beta）。

    小组成员在自己机器上跑这个，登录后自动领活、本地采集、回传。
    ★ 全程**内存 SQLite**：不写任何磁盘文件，回传完正文立即释放。

    ★ 凭据有三条来路，优先级从高到低：
        ① --token   网页端「一键启动本机 agent」把当前 JWT 递过来
        ② --ticket  下载来的启动包里的一次性票据（运行时换 JWT）
        ③ 都没给 → **交互式问你要邮箱和密码**（下载来的启动包走的就是这条）
      密码只在内存里用，**不写进任何文件**。
    """
    from .agent import Agent, AgentError
    email = (args.email or os.environ.get("MSYT_EMAIL") or "").strip()
    password = args.password or os.environ.get("MSYT_PASSWORD") or ""
    # ★ 连通性诊断：**不需要任何凭据**，所以要在问密码**之前**做
    #   （组员 diagnose 的目的就是「先搞清哪一环不通」，别让他先输密码）
    if args.diagnose:
        ag_d = Agent(server=args.server, verbose=True,
                     server_alt=args.server_alt or "", proxy=args.proxy or "")
        print("\n[agent] ===== 连通性诊断 =====")
        for line in ag_d.diagnose():
            print("[agent] " + line)
        print("[agent] =======================\n")
        return 0
    # ★ 包里有 `agent-ticket.txt`（网站登录后签发的一次性票据）时**不要问密码**：
    #   agent 会自己读它换 JWT。所以这里先探一下。
    has_ticket = bool(args.ticket) or _has_ticket_file()
    if not (email and password) and not args.token and not has_ticket:
        # ★ 成员端最常见的样子：双击 cmd → 装完依赖 → 问一句账号密码
        try:
            print("")
            print("  请登录采集网站（用你在网页上登录的同一个邮箱）")
            email = input("  邮箱    : ").strip()
            password = _ask_password("  密码    : ")
            print("")
        except (EOFError, KeyboardInterrupt):
            print("\n[agent] 已取消")
            return 130
        if not email or not password:
            print("[agent] 邮箱和密码都要填")
            return 2
    ag = Agent(server=args.server, email=email, password=password,
               node=args.node or "", limit=args.limit, interval=args.interval,
               task=args.task or "", verbose=not args.quiet,
               heartbeat=args.heartbeat, fail_limit=args.fail_limit,
               token=args.token or "", ticket=args.ticket or "",
               server_alt=args.server_alt or "", proxy=args.proxy or "",
               net_wait_minutes=int(getattr(args, "net_wait", 20) or 0),
               workers=int(getattr(args, "workers", 1) or 1))
    # ---- 本机自主采集模式（不领协作活，采完就退）----
    _tids = [int(x) for x in (args.tids or "").replace("，", ",").split(",")
             if str(x).strip().isdigit()]
    if args.local or args.sources or _tids:
        try:
            ag.login()
            res = ag.run_local(sources=args.sources or "",
                               pages=max(1, int(args.pages or 20)),
                               tids=_tids)
        except AgentError as e:
            print(f"[agent] 失败：{e}")
            return 1
        except KeyboardInterrupt:
            print("\n[agent] 已中断（已回传的都入库了）")
            return 130
        print(f"[agent] 本机采集结束：列表元数据 {res.get('list_rows', 0)} 帖 / "
              f"{res.get('list_sources', 0)} 个源；正文成功 {res.get('detail_ok', 0)} 帖，"
              f"站方限制 {res.get('detail_restricted', 0)} 帖，"
              f"失败 {res.get('detail_failed', 0)} 帖")
        return 0 if not res.get("detail_failed") else 1
    try:
        res = ag.run(once=args.once, max_rounds=args.rounds)
    except AgentError as e:
        # ★ 包里的启动凭证过期/已用过 → 改成问账号密码再来一次
        if getattr(e, "need_credentials", False):
            print(f"\n[agent] {e}")
            try:
                print("  请登录采集网站（用你在网页上登录的同一个邮箱）")
                ag.email = input("  邮箱    : ").strip()
                ag.password = _ask_password("  密码    : ")
            except (EOFError, KeyboardInterrupt):
                print("\n[agent] 已取消")
                return 130
            if not ag.email or not ag.password:
                print("[agent] 邮箱和密码都要填")
                return 2
            try:
                res = ag.run(once=args.once, max_rounds=args.rounds)
            except AgentError as e2:
                print(f"[agent] 失败：{e2}")
                return 1
        else:
            print(f"[agent] 失败：{e}")
            return 1
    except KeyboardInterrupt:
        print("\n[agent] 已中断（回传过的都已入库，未回传的会退回池子）")
        return 130
    print(f"[agent] 结束：完成 {res['done']} 帖，失败 {res['failed']} 帖"
          + ("（因与控制面失联而自动停止）" if res.get("stopped_by_link") else ""))
    return 0 if res["failed"] == 0 else 1


def cmd_web(args) -> int:
    """启动本地 Web 前端。

    采集动作全部在**后台线程**里跑（见 webapp.py），HTTP 请求不会被长任务阻塞。
    默认只监听 127.0.0.1 —— 这是本地工具，不该直接暴露到公网。
    """
    try:
        import uvicorn
    except ImportError:
        print("缺少 uvicorn，无法启动 Web 前端。pip install uvicorn")
        return 2
    from .webapp import app, ensure_admin
    cfg = _cfg(args)
    try:
        ensure_admin(cfg)
    except Exception as e:                       # noqa: BLE001
        print(f"[web] 初始化管理员账号失败：{e}")
    print(f"[web] 数据库: {cfg.db_path}")
    print(f"[web] 地址: http://{args.host}:{args.port}/")
    print("[web] 采集动作走后台线程，页面可随时关闭（任务会继续，进度已落库）")
    if args.reload:
        uvicorn.run("msyt.webapp:app", host=args.host, port=args.port, reload=True)
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="msyt", description="民生e点通·天一论坛 群众留言板 舆情采集器")
    p.add_argument("--config", help="配置文件路径（默认 程序/config.json）")
    p.add_argument("--interval", type=float, help="帖与帖之间的限速秒数")
    p.add_argument("--list-interval", dest="list_interval", type=float,
                   help="列表页之间的限速秒数")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("doctor", help="API 自检")
    sp.add_argument("--deep", action="store_true", help="把各维度筛选端点也探一遍")
    sp.add_argument("--with-db", action="store_true", help="顺便检查本地 FTS5")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("taxonomy", help="抓分类体系（部门目录/区县市/站方统计）")
    sp.set_defaults(func=cmd_taxonomy)

    sp = sub.add_parser("list", help="列表采集（链路一）")
    sp.add_argument("--sources", default="global",
                    help="采集源，如 global / dept:495 / group:func / category:1")
    sp.add_argument("--pages", type=int, help="每个源最多采几页")
    sp.add_argument("--stop-after", type=int, default=0,
                    help="每个源采到 N 页就停（试采集用）")
    sp.add_argument("--restart", action="store_true", help="忽略断点，从头再采一遍")
    sp.add_argument("--taxonomy", action="store_true", help="采集前先刷分类体系")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("detail", help="详情采集（链路二，先旧后新）")
    sp.add_argument("--limit", type=int, default=0, help="本次最多采几帖（0=不限）")
    sp.add_argument("--max-attempts", dest="max_attempts", type=int, default=3)
    sp.add_argument("--retry-failed", action="store_true", help="把失败任务重置为待采")
    sp.add_argument("--tid", type=int, nargs="*", help="定点采集指定的 tid（调试/补采）")
    sp.set_defaults(func=cmd_detail)

    sp = sub.add_parser("search", help="检索（标题 / 楼层正文 / 两者）")
    sp.add_argument("query")
    sp.add_argument("--scope", choices=["thread", "post", "all"], default="all")
    sp.add_argument("--dept", help="部门 typeid，逗号分隔")
    sp.add_argument("--group", help="部门分组 func/district/enterprise/other")
    sp.add_argument("--district", help="区县市 typeid")
    sp.add_argument("--category", help="类别 code")
    sp.add_argument("--state", help="状态 code")
    sp.add_argument("--mailbox", help="留言箱 code")
    sp.add_argument("--light", help="时效灯 code")
    sp.add_argument("--date-from", dest="date_from")
    sp.add_argument("--date-to", dest="date_to")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--sort", default="time_desc",
                    choices=["time_desc", "time_asc", "replies_desc", "views_desc", "dept"])
    sp.add_argument("--min-fts-chars", dest="min_fts_chars", type=int, default=3)
    sp.add_argument("--op", choices=["AND", "OR"], default="AND")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("stats", help="库内统计")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("integrity", help="详情完整性体检")
    sp.set_defaults(func=cmd_integrity)

    sp = sub.add_parser("sources", help="列出/展开采集源")
    sp.add_argument("--sources", default="global")
    sp.set_defaults(func=cmd_sources)

    sp = sub.add_parser("export", help="导出 CSV")
    sp.add_argument("--out", default="threads.csv")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("web", help="启动本地 Web 前端")
    sp.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--reload", action="store_true", help="改代码自动重启（开发用）")
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("agent", help="成员端采集代理（Beta）")
    sp.add_argument("--server", required=True, help="控制面地址，如 http://127.0.0.1:8765")
    sp.add_argument("--server-alt", default="",
                    help="备用控制面地址（启动包由「本地浏览器+服务器」协同生成，"
                         "带主/备两个；主地址探不通会自动换这个）")
    sp.add_argument("--proxy", default="",
                    help="代理地址，如 http://127.0.0.1:7890"
                         "（留空=自动读 Windows 系统代理；"
                         "Edge 能打开而这里连不上，多半就是它）")
    sp.add_argument("--diagnose", action="store_true",
                    help="只做连通性诊断（DNS/TCP/直连/代理/本机助手），不采集")
    sp.add_argument("--email", default="", help="成员邮箱（与 --token/--ticket 二选一）")
    sp.add_argument("--password", default="", help="成员密码")
    sp.add_argument("--token", default="",
                    help="直接用已有 JWT（网页端「一键启动」走这条）")
    sp.add_argument("--ticket", default="",
                    help="一次性启动票据（下载来的启动包里带的，运行时换 JWT）")
    sp.add_argument("--node", default="", help="节点号（默认登录后由控制面分配）")
    sp.add_argument("--limit", type=int, default=50,
                    help="每次领多少帖（0=用默认批量；不限配额）")
    sp.add_argument("--interval", type=float, default=4.0, help="轮询间隔秒")
    sp.add_argument("--task", default="", help="只做某个协作任务编号（如 A1）")
    sp.add_argument("--once", action="store_true", help="只跑一轮就退出")
    sp.add_argument("--rounds", type=int, default=0, help="最多跑几轮（0=不限）")
    sp.add_argument("--heartbeat", type=float, default=12.0,
                    help="心跳间隔秒（<=0 关闭；连续失败达阈值即判定失联并自动停止）")
    sp.add_argument("--fail-limit", type=int, default=0,
                    help="连续多少次联系不上控制面判定为失联（0=用默认 4；"
                         "失败后至少等 30 秒再试）")
    sp.add_argument("--quiet", action="store_true", help="不打印过程")
    sp.add_argument("--net-wait", type=int, default=20,
                    help="网络不通时等多久再放弃（分钟，默认 20；0=不等直接退出）。"
                         "等待期间程序不退出，网络恢复后自动接上")
    sp.add_argument("--workers", type=int, default=1,
                    help="并发采集的线程数（默认 1=串行）。"
                         "并发单位是「一帖」不是「一个请求」（同帖的详情页→ajax "
                         "必须贴在一起，站方 token 有效期 <30 秒）。"
                         "实测 2026-10-25：1 路 0 异常约 6.2 万帖/天，"
                         "3 路 0 异常约 12.3 万帖/天（最优点），"
                         "5 路出现连接错误、吞吐反降 —— 建议不超过 3")
    # ---- 本机自主采集（2026-10-23 新增）----
    #   成员要在**自己机器**上采指定的部门/帖子，采完回传服务器。
    #   ★ 为什么必须走本机：站方不提供 CORS，浏览器不能直采；
    #     而且在服务器上采 → 站方看到的流量全来自管理员那一个 IP，
    #     「多个分散的普通用户」这个 Beta 核心前提就落空了。
    sp.add_argument("--sources", default="",
                    help="本机采列表页元数据的源（如 dept:495 / group:func / global）")
    sp.add_argument("--pages", type=int, default=20,
                    help="每个列表源最多采多少页（默认 20）")
    sp.add_argument("--tids", default="",
                    help="本机采指定帖的正文，逗号分隔的 tid")
    sp.add_argument("--local", action="store_true",
                    help="只跑「本机自主采集」（采完就退出，不领协作活）")
    sp.set_defaults(func=cmd_agent)

    return p


def main(argv: List[str] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断（进度已落库，下次 `msyt detail` 会从断点继续）")
        return 130


if __name__ == "__main__":
    sys.exit(main())
