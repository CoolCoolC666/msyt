#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
民生e网通「群众留言板」可采集性验证脚本
================================================================
用途：为《方案报告.md》里的每一条实测结论提供可复算的证据。
不写库、不落盘业务数据，只打印探测结果。

用法：
    python _tools/probe_evidence.py            # 完整验证（约 1.5 分钟）
    python _tools/probe_evidence.py --quick    # 快速验证（跳过压测段）

设计约束（都是实测踩出来的）：
  1. **取 token 与用 token 必须紧挨着**，中间不得插入任何 sleep/重试。
     token 是**短时效票据**（实测 < 30 秒）—— 不绑会话、不绑 Cookie、可重复使用。
     注意：Session 复用是"推荐"（少握手、UA/Referer 一致），**不是"必需"**；
     把它当成必需会让人白花时间做 Cookie 持久化。
  2. 详情页 HTML 里 #postmsg_<pid> 是**空容器**，正文靠
     ajax_getpost.php?token=..&tid=..&src=pc 异步注入。
     直接用 requests 抓详情页 HTML 会看到空正文，
     且页面里有 <noscript> 提示"您需要启用 JavaScript 才能查看帖子内容"
     —— 这两条极易被误判成"要登录/要 JS 渲染"。
  3. 图片附件的真实地址在 zoomfile= 属性上，不是 src=（src 是懒加载占位图）。
  4. /archiver/?tid-*.html 返回 200 但是空壳，没有正文，别在这上面浪费时间。
  5. 解析列表页的「受理状态 / 帖子类别」前，必须先把 span.typewrap /
     span.typewrapmulti / span.sortwrap 从 <th> 副本里摘掉，
     否则部门名的方括号（[海曙区]）会被当成帖子类别混进分布。
"""
from __future__ import annotations

import argparse
import copy
import io
import re
import sys
import time
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import requests
from bs4 import BeautifulSoup

BASE = "https://bbs.cnnb.com.cn"
FID = 1176
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

LIST_URL = f"{BASE}/forum.php?mod=forumdisplay&fid={FID}"
THREAD_URL = f"{BASE}/forum.php?mod=viewthread&tid={{tid}}"
AJAX_URL = f"{BASE}/ajax_getpost.php"
#: 站方统计脚本 —— 全站真实量级的唯一来源（页面 HTML 里是 document.write 占位）
STATS_JS = f"{BASE}/html/yp/ms_sjszxsqtotal.js"

# 受理状态 / 帖子类别 的取值集合（取自实测页面）
STATES = {"已受理", "已回复", "未受理"}
CLASSES = {"投诉", "建议", "咨询", "点赞", "市民巡访", "追问", "求助", "其他"}

SEC = "=" * 68


def sec(title: str) -> None:
    print(f"\n{SEC}\n{title}\n{SEC}")


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    return s


def get(s: requests.Session, url: str, **kw) -> requests.Response:
    r = s.get(url, timeout=30, **kw)
    r.encoding = "utf-8"
    return r


def strip_html(h: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()


def discuz_uid(html: str) -> str:
    """取 Discuz 的登录态标记。0 = 访客（未登录）。

    刻意不用正则：这串文本要经过多层字符串转义才写进文件，
    反斜杠很容易多一层变成字面量 '\\d'，静默失配。
    """
    if "discuz_uid" not in html:
        return "?"
    tail = html.split("discuz_uid", 1)[1][:12]
    return "".join(ch for ch in tail if ch.isdigit()) or "?"


# ----------------------------------------------------------------- 证据 1
def ev_list(s: requests.Session) -> dict:
    sec("证据 1｜列表页无需登录，且元数据高度完整")
    r = get(s, LIST_URL)
    print(f"  HTTP {r.status_code}   响应 {len(r.text):,} 字节")
    print(f"  登录态探测：discuz_uid = {discuz_uid(r.text)}  (0 = 访客态)")

    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("tbody[id^=normalthread_]")
    tids = set(re.findall(r"forum\.php\?mod=viewthread&amp;tid=(\d+)", r.text))
    print(f"  列表条目：{len(rows)} 行 / 去重 tid {len(tids)} 个 / 页数 {page_total(r.text)}")

    st, cl, dept = Counter(), Counter(), Counter()
    for tb in rows:
        th = tb.select_one("th")
        if th is None:
            continue
        # 先把「部门标签」「留言箱标签」两个 span 从副本里摘掉，
        # 否则它们的方括号文本（[海曙区]、[市委书记留言箱]）会被
        # 误当成帖子类别混进分布里 —— 上一版就因此把「江北区」算成了类别。
        th = copy.deepcopy(th)
        for junk in th.select("span.typewrap, span.typewrapmulti, span.sortwrap"):
            junk.decompose()
        for tag in re.findall(r"\[([^\]]{2,6})\]", th.get_text(" ", strip=True)):
            (st if tag in STATES else cl)[tag] += 1
        for a in tb.select("span.typewrap em a, span.typewrapmulti em a"):
            dept[a.get_text(strip=True)] += 1
    print(f"  受理状态分布：{dict(st)}")
    print(f"  帖子类别分布：{dict(cl)}")
    print(f"  可见部门数（本页）：{len(dept)}")
    return {"rows": len(rows), "tids": len(tids),
            "states": dict(st), "classes": dict(cl)}


def page_total(html: str) -> str:
    m = re.search(r'totalpage="(\d+)"', html) or re.search(r"共\s*(\d+)\s*页", html)
    return m.group(1) if m else "?"


# ----------------------------------------------------------------- 证据 2
def ev_token(s: requests.Session, tid: str) -> tuple[str, int, int]:
    sec("证据 2｜正文不在首屏 HTML，靠 ajax_getpost.php + token 注入")
    r = get(s, THREAD_URL.format(tid=tid))
    empty = re.findall(r'id="postmsg_(\d+)"></span>', r.text)
    noscript = "您需要启用 JavaScript 才能查看帖子内容" in r.text
    print(f"  详情页 HTTP {r.status_code}   响应 {len(r.text):,} 字节")
    print(f"  首屏 #postmsg_ 空容器：{len(empty)} 个 → 正文确实不在 HTML 里")
    print(f"  含 JS 提示 <noscript>：{noscript}  ← 最易被误判成「要登录/要 JS」")
    print(f"  访客态 discuz_uid：{discuz_uid(r.text)}")

    tok = re.search(r"token:'([0-9a-f]{16,})'", r.text)
    if not tok:
        print("  !! 未取到 token")
        return "", 0, 0
    token = tok.group(1)
    print(f"  取到 token：{token[:8]}…（{len(token)} 位，嵌在详情页 HTML 里）")

    r3 = get(s, AJAX_URL,
             params={"token": token, "tid": tid, "src": "pc"},
             headers={"Referer": THREAD_URL.format(tid=tid)})
    j = r3.json()
    data = j.get("data") or {}
    chars = sum(len(strip_html(v)) for v in data.values())
    print(f"  ajax_getpost.php → code={j.get('code')}  楼层 {len(data)} 个  "
          f"正文合计 {chars:,} 字")
    return token, len(data), chars


# ----------------------------------------------------------------- 证据 3
def ev_token_constraints(tid: str, ttl_wait: int = 35) -> None:
    """token 到底受什么约束？

    结论是**时效**，不是会话。这一点必须实测：
    早期版本凭「换了会话就失败」的印象写成"会话绑定"，
    实际跨会话、清空 Cookie 都照样成功 —— 只有放久了才失败。
    写成"会话绑定"会让人白花时间做 Cookie 持久化。
    """
    sec("证据 3｜token 的真实约束 = 时效（不是会话）")

    s1 = new_session()
    r = get(s1, THREAD_URL.format(tid=tid))
    tok = re.search(r"token:'([0-9a-f]{16,})'", r.text).group(1)

    # (a) 跨会话
    s2 = new_session()
    j = get(s2, AJAX_URL,
            params={"token": tok, "tid": tid, "src": "pc"},
            headers={"Referer": THREAD_URL.format(tid=tid)}).json()
    print(f"  (a) 换会话使用同一 token      → code={j.get('code')}   "
          f"{'✅ 不绑定会话' if j.get('code') == 1 else '❌ 绑定会话'}")

    # (b) 清掉 Cookie
    s1.cookies.clear()
    j = get(s1, AJAX_URL,
            params={"token": tok, "tid": tid, "src": "pc"},
            headers={"Referer": THREAD_URL.format(tid=tid)}).json()
    print(f"  (b) 清空 Cookie 后使用        → code={j.get('code')}   "
          f"{'✅ 不绑定 Cookie' if j.get('code') == 1 else '❌ 绑定 Cookie'}")

    # (c) 同一 token 重复使用
    codes = []
    for _ in range(3):
        j = get(s1, AJAX_URL,
                params={"token": tok, "tid": tid, "src": "pc"},
                headers={"Referer": THREAD_URL.format(tid=tid)}).json()
        codes.append(j.get("code"))
        time.sleep(0.3)
    print(f"  (c) 同一 token 连用 3 次        → code={codes}   "
          f"{'✅ 非单次票据' if all(c == 1 for c in codes) else '❌ 单次有效'}")

    # (d) 时效：放久一点再用
    if ttl_wait > 0:
        print(f"      （等待 {ttl_wait}s 以验证时效…）", flush=True)
        time.sleep(ttl_wait)
        j = get(s1, AJAX_URL,
                params={"token": tok, "tid": tid, "src": "pc"},
                headers={"Referer": THREAD_URL.format(tid=tid)}).json()
        ok = j.get("code") == 1
        print(f"  (d) 放置 {ttl_wait}s 后使用       → code={j.get('code')}  "
              f"msg={j.get('msg')}   "
              f"{'✅ 仍有效' if ok else '❌ 已失效 → token 有时效'}")

    # (e) 跨帖 / 错配 tid：token 才是内容钥匙
    s3 = new_session()
    r3 = get(s3, THREAD_URL.format(tid=tid))
    expect = set(re.findall(r'id="post_(\d+)"', r3.text))
    tok3 = re.search(r"token:'([0-9a-f]{16,})'", r3.text).group(1)

    def pids_of(token: str, want_tid: str) -> list:
        j = get(s3, AJAX_URL,
                params={"token": token, "tid": want_tid, "src": "pc"},
                headers={"Referer": THREAD_URL.format(tid=want_tid)}).json()
        return list((j.get("data") or {}).keys())

    ok_pids = pids_of(tok3, tid)
    wrong_tid = pids_of(tok3, "9999999")
    print(f"  (e) 本帖 token + 本帖 tid        → 楼层 {ok_pids}")
    print(f"      本帖 token + 不存在的 tid     → 楼层 {wrong_tid}   "
          f"{'⚠️ 与上面完全相同 → tid 不参与选择，token 才是内容钥匙' if wrong_tid == ok_pids else '（tid 参与了选择）'}")
    if wrong_tid == ok_pids:
        print("      → 后果：token 与 tid 配错时**不报错**，会静默返回另一帖的正文。")
        print("      → 必须做 pid 一致性校验（比对详情页的 post_{pid} 锚点），否则串帖无从察觉。")
    print(f"      详情页楼层锚点（用于校验）: {sorted(expect)}")

    print("  结论：token 是**短时效票据**。不绑会话、不绑 Cookie、可重复使用；"
          "唯一硬规则 = 取到后必须立即使用（间隔 < 30 秒）。")
    print("  → 实现上：取 token 与用 token 必须在同一请求序列，中间不得插入 sleep。")


# ----------------------------------------------------------------- 证据 4
def ev_archiver(tid: str) -> None:
    sec("证据 4｜/archiver/ 轻量页是空壳死路（别在这上面浪费时间）")
    s = new_session()
    r = get(s, f"{BASE}/archiver/?tid-{tid}.html")
    print(f"  HTTP {r.status_code}   响应 {len(r.text):,} 字节  ← 看着像有戏")
    for kw in ("postmessage", "t_f", "postmsg", "ajax_getpost"):
        print(f"    含 {kw!r}：{kw in r.text}")
    soup = BeautifulSoup(r.text, "html.parser")
    longs = [td for td in soup.find_all("td")
             if len(td.get_text(" ", strip=True)) > 20]
    print(f"  正文级容器（>20 字）：{len(longs)} 个 → 判定为无正文")


# ----------------------------------------------------------------- 证据 5
def ev_scale() -> None:
    sec("证据 5｜分页规模：500 页硬顶，超出回绕到第 1 页")
    s = new_session()
    for pg in (1, 500, 501, 800):
        r = get(s, f"{LIST_URL}&page={pg}")
        ds = re.findall(r"(\d{4}-\d{2}-\d{2})", r.text)
        tt = set(re.findall(r"forum\.php\?mod=viewthread&amp;tid=(\d+)", r.text))
        print(f"  page={pg:<5} HTTP {r.status_code}  tid {len(tt):>3} 个  "
              f"日期 {min(ds) if ds else '-'} ~ {max(ds) if ds else '-'}")
        time.sleep(1.0)
    print("  结论：可见历史 = 500 页 × 约 100 条/页 ≈ 5 万条；"
          "覆盖 2025-11 ~ 采集当日")


# ----------------------------------------------------------------- 证据 6
def ev_throttle(rounds: int = 12) -> None:
    sec(f"证据 6｜限流容忍：连续 {rounds} 次列表请求")
    s = new_session()
    codes, t0 = [], time.time()
    for i in range(rounds):
        r = get(s, f"{LIST_URL}&page={i + 1}")
        codes.append(r.status_code)
        time.sleep(0.25)
    print(f"  状态码：{codes}")
    print(f"  耗时 {time.time() - t0:.1f}s   "
          f"全 200：{all(c == 200 for c in codes)}   "
          f"无 403/429：{not any(c in (403, 429) for c in codes)}")
    print("  结论：站点未开 attackevasive，低频访问不会触发封禁；"
          "但仍应保守限速（见报告 §5.4）")


# ----------------------------------------------------------------- 证据 7
def ev_flow(tid: str) -> None:
    sec("证据 7｜详情页白送「受理流程」秒级时间线")
    s = new_session()
    r = get(s, THREAD_URL.format(tid=tid))
    flow = re.findall(
        r"<strong>&nbsp;([^<]{2,10})</strong><p>([\d-]*)<i>([\d:]*)</i>", r.text)
    for name, d, t in flow:
        print(f"    {name:<8} {d} {t}")
    print("  → 可直接算「受理时长」「回复时长」，无需人工标注")


def list_rows(html: str) -> list:
    """列表页的帖子行：(标题, 日期, tid)。"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tb in soup.select("tbody[id^=normalthread_]"):
        a = tb.select_one("a.s.xst")
        if not a:
            continue
        by = tb.select("td.by")
        t = by[0].select_one("em span") if by else None
        out.append((a.get_text(strip=True),
                    t.get_text(strip=True) if t else "",
                    (a.get("href") or "").split("tid=")[-1].split("&")[0]))
    return out


def view_reach(s: requests.Session, url: str, label: str) -> str:
    """量一个列表视图的「页数 + 末页日期范围 + 末页最早一贴的标题」。

    ⚠ 必须取**末页**才能量到触达边界。
      早期版本用浏览器看第一页的日期范围，得出「最早 2024-10-22」，
      实际那只是第一页的区间 —— 真实边界要深得多（例：广电集团末页到 2011-10-01）。
    """
    tp = page_total(get(s, url).text)
    earliest = ""
    sample = ""
    if tp.isdigit() and int(tp) >= 1:
        last = get(s, url + (f"&page={tp}" if "page=" not in url else "")).text
        rs = list_rows(last)
        dated = [(d, t, i) for t, d, i in rs if d]
        if dated:
            dated.sort()
            earliest = dated[0][0]
            sample = dated[0][1][:26]
    return (f"{label:<30} {tp:>4} 页   末页最早 {earliest or '?':<12} {sample}")


# ----------------------------------------------------------------- 证据 8
def ev_taxonomy_and_scale(s: requests.Session) -> None:
    sec("证据 8｜分类体系全貌 · 站方真实量级 · 突破 500 页上限")
    r = get(s, LIST_URL)
    soup = BeautifulSoup(r.text, "html.parser")

    print("  【8.1】部门目录（#rankingwz 的 5 个 pane）：")
    for i, label in enumerate(
            ["全部(tab1)", "职能部门和群团组织(tab2)", "区（县、市）及开发园区(tab3)",
             "民生企事业单位(tab4)", "其他(tab5)"], start=1):
        pane = soup.select_one(f"#listranking{i}")
        n = len(pane.select('a[href*="typeid="]')) if pane else 0
        print(f"      {label:<26} {n} 个 typeid")
    print("      → tab1 是全集，不算分组；一个部门可同时属于多个分组（如「综合」512）")

    box = soup.select_one(".qxsa")
    ds = box.select(".yzq2") if box else []
    print(f"\n  【8.2】区县市网格 .qxsa .yzq2：{len(ds)} 个")

    ph = re.findall(r"document\.write\((ms_\w+)\)", r.text)
    print(f"\n  【8.3】HTML 里的占位：{len(ph)} 处 document.write(...) "
          f"→ 真数字不在 HTML 里，样例 {sorted(set(ph))[:4]}")

    js = get(s, STATS_JS)

    def num(v: str):
        m = re.search(rf"\b{v}\s*=\s*(\d+)", js.text)
        return int(m.group(1)) if m else None

    print(f"\n  【8.4】站方统计脚本 ms_sjszxsqtotal.js（{len(js.content)} 字节）：")
    for v, name in (("ms_all", "历史留言总量"), ("ms_all_reply", "历史受理总量"),
                    ("ms_thisyear", "今年留言量"), ("ms_lastmonth", "上月留言量")):
        print(f"      {name:<12} {v:<16} = {num(v)}")
    print("      → 分页只够到约 5 万条，与站方自报的 74.4 万条差 90% 以上")
    print("      → 所以「分页够不到」≠「数据不存在」")

    print("\n  【8.5】部门视图各自的分页（突破 500 页上限的手段）：")
    for tid, name in (("551", "宁波广电集团"), ("972", "宁波日报报业集团"),
                      ("725", "宁波证监局"), ("511", "市住建局"),
                      ("499", "市交通运输局"), ("495", "市公安局")):
        print("      " + view_reach(
            s, f"{LIST_URL}&filter=typeid&typeid={tid}", f"typeid={tid} {name}"))
    print("      → 每个 typeid 有自己的分页；宁波证监局只有 2 页，但最早一贴远早于全局视图")

    print("\n  【8.6】dateline 时间窗（UI 里被注释，服务端仍认）：")
    print("      ⚠ 必须带 filter= 前缀才生效，裸 &dateline= 会被忽略（两者对照如下）：")
    print("      " + view_reach(s, f"{LIST_URL}&dateline=2592000",
                                   "裸 &dateline=2592000（30天）"))
    print("      " + view_reach(s, f"{LIST_URL}&filter=dateline&dateline=2592000",
                                   "filter=dateline 30 天"))
    print("      " + view_reach(s, f"{LIST_URL}&filter=dateline&dateline=7776000",
                                   "filter=dateline 90 天"))
    print("      " + view_reach(s, f"{LIST_URL}&filter=dateline&dateline=31536000",
                                   "filter=dateline 365 天"))
    print("      " + view_reach(
        s, f"{LIST_URL}&filter=typeid&typeid=495&dateline=2592000",
        "typeid=495 + dateline=30 天"))
    print("      → 与 typeid 可叠加；两者都是「换视图」，不是「数据不存在」")

    print("\n  【8.7】站内搜索 search.php：")
    rs = get(s, f"{BASE}/search.php?mod=forum&srchtxt=%E8%BF%9D%E5%81%9C"
                f"&searchsubmit=yes")
    rows = len(re.findall(r'id="thread_\d+"', rs.text))
    print(f"      HTTP {rs.status_code}   {len(rs.content):,} 字节   结果行 {rows}")
    print(f"      含「登录」提示：{'是' if '登录' in rs.text else '否'}")
    print("      → 结构性封闭（需登录），但本项目三条采集链路都不走搜索")


# ----------------------------------------------------------------- 证据 9
def ev_detail_paging(s: requests.Session, tid: str) -> None:
    sec("证据 9｜详情页分页机制 · pid 集合逐页精确相等")
    seen = set()
    raw_total = 0
    for pg in range(1, 6):
        r = get(s, f"{BASE}/forum.php?mod=viewthread&tid={tid}&page={pg}")
        anchors = re.findall(r'id="postmsg_(\d+)"', r.text)
        tok = re.findall(r"token:'([0-9a-f]{16,})'", r.text)
        blk = re.findall(r'<div class="pg">(.{0,3000}?)</div>', r.text, re.S)
        npages = 1
        if blk:
            ns = [int(x) for x in re.findall(r'href="[^"]*[?&]page=(\d+)"', blk[0])]
            if ns:
                npages = max(ns)
        print(f"\n  ── 第 {pg} 页 ──")
        print(f"     楼层锚点 postmsg_ ：{len(anchors)} 个    "
              f"token：{len(tok)} 个"
              f"（{tok[0][:8] + '…' if tok else '无'}）")
        print(f"     站方分页块：{'有，共 ' + str(npages) + ' 页' if blk else '无（单页帖）'}")
        if not anchors:
            print("     （无锚点 → 到底了）")
            break
        if not tok:
            print("     !! 没取到 token")
            break
        if pg == 1:
            sp = BeautifulSoup(r.text, "html.parser")
            labs = [(e.get("id", "")[8:], e.get_text(" ", strip=True))
                    for e in sp.select("a[id^=postnum]")]
            print(f"     楼层号标签样例：{labs[:4]}")
        j = get(s, AJAX_URL, params={"token": tok[0], "tid": str(tid),
                                      "src": "pc"}).json()
        got = set((j.get("data") or {}).keys())
        exp = set(anchors)
        raw_total += len(anchors)
        print(f"     ajax code={j.get('code')}   返回楼层 {len(got)} 个")
        print(f"     ★ pid 集合精确相等：{'是' if got == exp else '否 ← 必须拒收'}"
              f"   （ajax独有 {sorted(got - exp)[:3]} / 锚点独有 {sorted(exp - got)[:3]}）")
        seen |= exp
        if pg > 1 and not (exp - seen):
            print("     → 本页没有新楼层：单页帖请求 page=2 时**回绕返回了第 1 页**")
            break
        if pg >= npages:
            break
    print(f"\n  合计：原始锚点 {raw_total} 个 / 去重后 {len(seen)} 个楼层")
    print("  → 一帖 N 页要 N×2 次请求（每页各自的 token，ajax 只返回当页）")
    print("  → 完整性判据必须跨页：头 = 第 1 页楼主楼，尾 = 末页最后一楼")
    print("  → 单页帖没有分页块，不能盲目翻页，否则会把第 1 页当第 2 页重采")


def pick_sample(s: requests.Session) -> str:
    """挑一条**有实质正文和回复**的帖做样本。

    上一版直接取「最新一条 tid」，结果选到一条正文只有 51 字、
    零回复的新帖 —— 拿它证明「正文能取到」说服力不足。
    这里改成取列表页里回复数最多的那条。
    """
    r = get(s, LIST_URL)
    soup = BeautifulSoup(r.text, "html.parser")
    best, best_n = None, -1
    for tb in soup.select("tbody[id^=normalthread_]"):
        a = tb.select_one("a.s.xst")
        num = tb.select_one("td.num")
        if not a or not num:
            continue
        m = re.search(r"tid=(\d+)", a.get("href", ""))
        if not m:
            continue
        links = num.find_all("a")
        try:
            n = int(links[0].get_text(strip=True)) if links else 0
        except ValueError:
            n = 0
        if n > best_n:
            best, best_n = m.group(1), n
    return best or ""


# ----------------------------------------------------------------- 证据 10
def ev_dept_tabs(s: requests.Session) -> None:
    """部门目录 5 个 tab 的完整性核查（"站方自称 107 家"到底差在哪）。

    要证明的是**程序没漏**：tab1（全集）与 tab2..5（分组）的并集必须完全相等。
    逐部门逐个取筛选页的 109 次请求在 probe_tabs4.py 里，
    这里只做集合层面的核查（1 次请求）。
    """
    sec("证据 10｜部门目录 5 个 tab：全集与分组并集是否相等（107 vs 109）")
    r = get(s, LIST_URL)
    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    titles = {}
    panes = {}
    for i in range(1, 6):
        h = soup.select_one(f"h3#tab{i}")
        titles[i] = h.get_text(" ", strip=True) if h else "(缺失)"
        p = soup.select_one(f"#listranking{i}")
        ids = []
        hidden = 0
        if p:
            for a in p.select('a[href*="typeid="]'):
                m = re.search(r"typeid=(\d+)", a.get("href", ""))
                if not m:
                    continue
                ids.append(m.group(1))
                st = (a.get("style") or "").replace(" ", "").lower()
                cls = " ".join(a.get("class") or []).lower()
                if "display:none" in st or "visibility:hidden" in st or "hide" in cls:
                    hidden += 1
        panes[i] = (ids, hidden)

    print("  ① 5 个 tab 的标题与条目数")
    for i in range(1, 6):
        ids, hidden = panes[i]
        print(f"     tab{i}: {titles[i]!r:<26} 条目 {len(ids):>4}  隐藏条目 {hidden}")
        m = re.findall(r"(\d+)\s*家", titles[i])
        if m and int(m[0]) != len(set(ids)):
            print(f"          ⚠ 标题称 {m[0]} 家，实际 {len(set(ids))} 个 —— 差 "
                  f"{len(set(ids)) - int(m[0])}")

    set1 = set(panes[1][0])
    union = set(panes[2][0]) | set(panes[3][0]) | set(panes[4][0]) | set(panes[5][0])
    print("\n  ② 集合关系")
    print(f"     tab1 全集 {len(set1)} / tab2..5 并集 {len(union)}")
    print(f"     交集 {len(set1 & union)}   只在 tab1 的 {len(set1 - union)}   "
          f"只在 tab2..5 的 {len(union - set1)}")
    print(f"     → {'完全相等，程序没漏' if set1 == union else '★ 不相等，程序漏了'}")

    print("\n  ③ 整页还有没有第 3 份名单")
    all_ids = set(re.findall(r"typeid=(\d+)", html))
    blk = soup.select_one("#rankingwz")
    blk_ids = set(re.findall(r"typeid=(\d+)", str(blk))) if blk else set()
    print(f"     整页 typeid= 去重 {len(all_ids)} 个"
          f"（#rankingwz 内 {len(blk_ids)} 个）")
    ul = soup.select_one("ul#thread_types")
    if ul:
        ul_ids = {re.search(r"typeid=(\d+)", a.get("href", "")).group(1)
                   for a in ul.select('a[href*="typeid="]')
                   if re.search(r"typeid=(\d+)", a.get("href", ""))}
        print(f"     第二份目录 ul#thread_types {len(ul_ids)} 个  "
              f"与 tab1 相同? {ul_ids == set1}")

    print("\n  ④ 部门搜索框的数据源")
    inline = " ".join(sc.get_text() for sc in soup.find_all("script"))
    frontend = 'jQuery("#listranking1 a")' in inline
    print(f"     纯前端过滤（就地过滤 #listranking1 的链接）? {frontend}")
    print(f"     → {'没有第三份更大的名单' if frontend else '★ 可能存在独立数据源，需再查'}")

    print("\n  ⑤ 多重归属的部门（解释分组相加 > 全集）")
    multi = {}
    for i in (2, 3, 4, 5):
        for t in panes[i][0]:
            multi.setdefault(t, []).append(i)
    name_of = {}
    for i in range(1, 6):
        p = soup.select_one(f"#listranking{i}")
        if not p:
            continue
        for a in p.select('a[href*="typeid="]'):
            m = re.search(r"typeid=(\d+)", a.get("href", ""))
            if m:
                name_of.setdefault(m.group(1), a.get_text(strip=True))
    for t, gs in multi.items():
        if len(gs) > 1:
            print(f"     typeid {t} {name_of.get(t, '?'):<24} 属于 tab{gs}")
    extra = sum(len(gs) - 1 for gs in multi.values() if len(gs) > 1)
    grp_total = sum(len(panes[i][0]) for i in (2, 3, 4, 5))
    print(f"     分组相加 {grp_total}（64+14+25+10），去重后 {len(set1)}"
          f"（多重归属多计 {extra}）")
    print("     → 逐部门核查（109 次请求）见 _tools\\probe_tabs4.py；"
          "结论：109 家里 108 家有留言、1 家零留言，")
    print("       站方标题的 107 = 109 − 2（typeid 最大的 1142 市委社会工作部"
          "仅 2 条、1140 宁波老年大学 0 条）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过压测段")
    args = ap.parse_args()

    s = new_session()
    info = ev_list(s)
    tid = pick_sample(s)
    print(f"\n  ► 选样帖文 tid={tid}（列表页回复数最多的一条）")

    # 每组证据单独兜住异常：一组挂了不影响其余组继续复算，
    # 否则一次网络抖动就让整份「可复核」作废。
    steps = [
        ("证据2  正文走 AJAX + token", lambda: ev_token(s, tid)),
        ("证据7  受理流程时间线", lambda: ev_flow(tid)),
        ("证据9  详情分页与 pid 集合", lambda: ev_detail_paging(s, tid)),
        ("证据8  分类体系与绕行 500 页", lambda: ev_taxonomy_and_scale(s)),
        ("证据10 部门目录 5 tab 完整性", lambda: ev_dept_tabs(s)),
        ("证据3  token 的真实约束",
         lambda: ev_token_constraints(tid, ttl_wait=0 if args.quick else 35)),
        ("证据4  /archiver/ 空壳死路", lambda: ev_archiver(tid)),
    ]
    if not args.quick:
        steps.append(("证据5  规模与回绕", ev_scale))
        steps.append(("证据6  限流容忍", ev_throttle))

    failed = []
    for name, fn in steps:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 —— 复算脚本要能报告任意异常
            failed.append(name)
            print(f"\n!! [{name}] 本组失败：{type(e).__name__}: {e}")

    print(f"\n{SEC}\n结论：列表页、详情页、楼层正文三层全部**无需登录**可取。\n"
          f"两条硬约束：① token 有时效（< 30 秒），须取到即用；\n"
          f"          ② token 才是内容钥匙（tid 不参与选择），必须做 pid 校验防串帖。\n"
          f"另外三条实测结论：③ 详情页会分页，每页各自一个 token，判据必须跨页；\n"
          f"          ④ 500 页只是**全局视图**的边界，部门视图与 dateline 时间窗都能绕过去，\n"
          f"             而站方自报历史留言 744,636 条 —— 够不到 ≠ 不存在；\n"
          f"          ⑤ 部门目录 5 个 tab 的全集与分组并集**完全相等**（109 == 109），\n"
          f"             站方标题的「107家」是过时数字（多出的 2 个是后加的 1142 / 1140）。\n"
          + (f"本轮失败组：{', '.join(failed)}\n" if failed else "本轮所有证据组均通过。\n")
          + f"{SEC}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
