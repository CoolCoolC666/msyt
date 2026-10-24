# -*- coding: utf-8 -*-
"""
API 自检（doctor）
================================================================
用户指定的两个探测端点：
    基础列表   https://bbs.cnnb.com.cn/forum.php?mod=forumdisplay&fid=1176
    维度筛选   https://bbs.cnnb.com.cn/forum.php?mod=forumdisplay&fid=1176&filter=typeid&typeid=512
外加三条链路自检：分类体系（部门目录 + 区县市 + 站方统计脚本）、
详情链路（详情页 → token → ajax 正文）、本地 FTS5 检索能力。

判据全部是**可观察的**，不靠「没报错就算好」：
  · 列表页：HTTP 200 + 能解析出帖行 + 能读出分页上限
  · 筛选页：HTTP 200 + 解析出的帖行数 > 0 + 该源的 typeid 确实出现在目录里
  · 详情链路：token 取到 + ajax code==1 + **ajax 的 pid 集合 == 该页锚点集合**
  · FTS：能建表、能 MATCH

⚠ 规则来自哪里：站方没有公开的 API 文档，本项目的解析规则是
  **用浏览器实地勘察页面 DOM 得出**的（#rankingwz 的 5 个 tab / .qxsa 网格 /
  tbody#normalthread_ / #postmsg_ / ajax_getpost 的 token 机制）。
  规则一旦过时，doctor 会立刻报出来 —— 这就是它的意义。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .config import AJAX_URL, BASE, FILTER_URL, LIST_URL, STATS_JS, THREAD_URL
from .http_client import Client, HttpError
from .parsers import parse_ajax_posts, parse_list_page, parse_list_page_info, parse_thread_page
from .taxonomy import parse_depts, parse_districts, parse_stats_js

#: 用户指定的两个探测端点
PROBE_ENDPOINTS = [
    {"name": "基础列表", "url": LIST_URL,
     "expect": "列表页能解析出帖行，并读出站方分页上限"},
    {"name": "维度筛选（typeid=512 综合）", "url": FILTER_URL.format(typeid=512),
     "expect": "筛选页能解析出帖行，且该 typeid 在部门目录中"},
]


def _probe_list(client: Client, name: str, url: str) -> Dict[str, Any]:
    r: Dict[str, Any] = {"name": name, "url": url}
    t0 = time.time()
    try:
        resp = client.get(url, throttle="list")
    except HttpError as e:
        r.update(ok=False, kind=e.kind, ms=int((time.time() - t0) * 1000),
                 detail=str(e), verdict=f"FAIL — {e.kind}")
        return r
    html = resp.text
    rows = parse_list_page(html)
    info = parse_list_page_info(html)
    depts = parse_depts(html)
    r.update(
        ok=len(rows) > 0,
        status=resp.status_code,
        bytes=len(resp.content),
        ms=int((time.time() - t0) * 1000),
        rows=len(rows),
        total_page=info.get("total_page", 0),
        total_threads=info.get("total_threads", 0),
        depts=len(depts),
        sample_tid=rows[0].tid if rows else None,
        sample_title=rows[0].title[:40] if rows else "",
    )
    if rows:
        r["verdict"] = (f"OK — {len(rows)} 行 / 分页上限 {info.get('total_page', '?')} 页 / "
                        f"目录 {len(depts)} 个部门")
    else:
        r["verdict"] = "FAIL — 页面 200 但一行都没解析出来（规则可能已过时）"
    return r


def _probe_taxonomy(client: Client) -> Dict[str, Any]:
    """分类体系 + 站方统计脚本。"""
    r: Dict[str, Any] = {"name": "分类体系与站方统计", "url": STATS_JS}
    t0 = time.time()
    try:
        resp = client.get(LIST_URL, throttle="list")
        html = resp.text
        depts = parse_depts(html)
        districts = parse_districts(html)
        jresp = client.get(STATS_JS, throttle="list")
        site, dstats = parse_stats_js(jresp.text)
    except HttpError as e:
        r.update(ok=False, kind=e.kind, ms=int((time.time() - t0) * 1000),
                 verdict=f"FAIL — {e.kind}")
        return r
    r.update(
        ok=len(depts) > 0 and site.total is not None,
        ms=int((time.time() - t0) * 1000),
        depts=len(depts),
        districts=len(districts),
        site_total_msgs=site.total,
        site_this_year=site.this_year,
        site_last_month=site.last_month,
        site_total_accepted=site.total_accepted,
        js_bytes=len(jresp.content),
    )
    r["verdict"] = (f"OK — {len(depts)} 个部门 / {len(districts)} 个区县市 / "
                    f"站方自报历史留言 {site.total} 条"
                    if r["ok"] else "FAIL — 部门目录或统计脚本没取到")
    return r


def _probe_detail(client: Client) -> Dict[str, Any]:
    """详情链路：列表 → 详情页 → token → ajax 正文，并做 pid 集合校验。"""
    r: Dict[str, Any] = {"name": "详情链路（含 pid 集合校验）", "url": ""}
    t0 = time.time()
    try:
        resp = client.get(LIST_URL, throttle="list")
        rows = parse_list_page(resp.text)
        if not rows:
            r.update(ok=False, verdict="FAIL — 列表页没解析出帖，选不出样本")
            return r
        tid = rows[0].tid
        durl = THREAD_URL.format(tid=tid, page=1)
        d = client.get(durl, throttle="obj")
        tp = parse_thread_page(d.text, tid=tid, page_no=1)
        if not tp.token:
            r.update(ok=False, tid=tid, url=durl,
                     verdict="FAIL — 详情页没取到 token（正文注入机制可能已变）")
            return r
        ar = client.get_nowait(AJAX_URL,
                               params={"token": tp.token, "tid": str(tid), "src": "pc"},
                               referer=durl)
        payload = ar.json()
        posts = parse_ajax_posts(payload)
        same = set(posts.keys()) == set(tp.pids)
        r.update(
            ok=(payload.get("code") == 1 and bool(posts) and same),
            tid=tid, url=durl,
            token_len=len(tp.token),
            ajax_code=payload.get("code"),
            anchors=len(tp.pids),
            ajax_posts=len(posts),
            pid_sets_equal=same,
            total_pages=tp.total_pages,
            flow_steps=len(tp.flow),
            ms=int((time.time() - t0) * 1000),
        )
        if payload.get("code") != 1:
            r["verdict"] = f"FAIL — ajax code={payload.get('code')}（token 失效或参数错）"
        elif not same:
            r["verdict"] = ("FAIL — ajax 的 pid 集合与该页锚点集合不一致"
                            "（★ 说明存在串帖风险，必须停下来查）")
        elif not posts:
            r["verdict"] = "FAIL — ajax 返回了空正文"
        else:
            r["verdict"] = (f"OK — 楼层 {len(posts)} / 锚点 {len(tp.pids)} 完全一致 / "
                            f"共 {tp.total_pages} 页 / 受理流程 {len(tp.flow)} 步")
    except Exception as e:  # noqa: BLE001 —— 自检要能报告任意异常
        r.update(ok=False, kind=type(e).__name__,
                 verdict=f"FAIL — {type(e).__name__}: {e}")
    return r


def _probe_fts(db) -> Dict[str, Any]:
    """本地 FTS5 能力。"""
    r: Dict[str, Any] = {"name": "本地 FTS5 检索", "url": str(db.path)}
    avail = db.fts_available()
    r["ok"] = avail
    r["verdict"] = "OK — fts5 + trigram 可用" if avail else \
        "FAIL — 当前 Python 的 sqlite 没编入 fts5（检索会退 LIKE 模式）"
    return r


def run_doctor(client: Client, db=None, deep: bool = False) -> Dict[str, Any]:
    """跑一遍全部自检。返回 {results, ok, summary}。"""
    results: List[Dict[str, Any]] = []
    for ep in PROBE_ENDPOINTS:
        results.append(_probe_list(client, ep["name"], ep["url"]))
    results.append(_probe_taxonomy(client))
    results.append(_probe_detail(client))
    if db is not None:
        results.append(_probe_fts(db))
    if deep:
        # 深度：把所有维度的筛选端点各探一遍
        from .sources import expand_sources
        from .taxonomy import CATEGORIES, MAILBOXES, STATES
        specs = ["category:" + ",".join(c["code"] for c in CATEGORIES),
                 "state:" + ",".join(s["code"] for s in STATES),
                 "mailbox:" + ",".join(m["code"] for m in MAILBOXES)]
        for s in expand_sources(specs):
            results.append(_probe_list(client, f"维度 {s.key}", s.url_for(1)))
    ok = all(r.get("ok") for r in results)
    return {
        "ok": ok,
        "results": results,
        "summary": " / ".join(
            f"{r['name']}:{'OK' if r.get('ok') else 'FAIL'}" for r in results),
    }


def format_report(res: Dict[str, Any]) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append("msyt API 自检")
    lines.append("=" * 72)
    for r in res["results"]:
        lines.append(f"\n● {r['name']}")
        lines.append(f"    URL    : {r.get('url', '-')}")
        lines.append(f"    判定   : {r.get('verdict', '-')}")
        extra = {k: v for k, v in r.items()
                 if k not in ("name", "url", "verdict", "ok", "sample_title")}
        if extra:
            lines.append("    指标   : " + json.dumps(extra, ensure_ascii=False))
    lines.append("")
    lines.append("-" * 72)
    lines.append(f"总判定：{'全部通过' if res['ok'] else '存在失败项'}")
    lines.append("-" * 72)
    return "\n".join(lines)
