# -*- coding: utf-8 -*-
"""
页面解析：列表页（帖的元数据 + 多维标签） 与 详情页（楼层 + 受理流程）
================================================================
站点是 Discuz! X3.5，实测确认的三个关键结构：

1) 列表页：服务端渲染，`tbody[id^=normalthread_]` 每页 99 行。
   行内 `th` 的方括号标签混着「帖子类别」和「部门名」两类，
   **必须先摘掉 span.typewrap / typewrapmulti / sortwrap 再统计分布**，
   否则 [海曙区] 之类部门名会被当成帖子类别混进类别维度。

2) 详情页：正文**不在首屏 HTML**，`#postmsg_{pid}` 是空容器，
   正文由 ajax_getpost.php?token=..&tid=..&src=pc 注入。
   站方 JS 写得很直白：
       jQuery('#postmsg_'+pid).replaceWith(html)
   → 所以「正文容器」就是 postmsg_{pid}，ajax 返回的 html 是它的替换体。
   ★ 但**「这一页有哪些楼层」要看外层容器 `#post_{pid}`**，不是 postmsg_：
     站方偶尔不给某层生成 postmsg_ 容器（ajax 照样返回它的正文），
     只认 postmsg_ 会漏掉那一层、把整帖卡在半截。详见 _POSTBOX_RE。

3) 详情页会分页：一帖 N 页，每页有**各自的 token**，
   ajax 也只返回**当页**楼层。实测（tid=8339985，3 页）：
       每页 ajax 的 data 键集合  ==  该页 postmsg_{pid} 锚点集合（精确相等）
   这条等式是防「静默串帖」的唯一依据，见 detail.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

from bs4 import BeautifulSoup, Comment

from .taxonomy import CATEGORIES, STATES

#: 站方「亮灯/时效」图标 → (代码, 站方自己的措辞)
#: 实测 4 种：green=3个工作日内回复 / yellow=5个工作日内回复
#:            grey=5个工作日外回复 / warn=24小时未受理
LIGHT_SVG = {
    "green.svg": ("green", "3个工作日内回复"),
    "yellow.svg": ("yellow", "5个工作日内回复"),
    "grey.svg": ("grey", "5个工作日外回复"),
    "warn.svg": ("warn", "24小时未受理"),
}

_CATEGORY_NAMES = {c["name"] for c in CATEGORIES}
_STATE_NAMES = {s["name"] for s in STATES}

_CN_TZ = timezone(timedelta(hours=8))


def to_ts(s: str) -> Optional[int]:
    """'2026-10-19 18:53' / '2026-10-19 18:53:24' → Unix 秒（北京时间）。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=_CN_TZ).timestamp())
        except ValueError:
            continue
    return None


# ================================================================= 列表页
@dataclass
class ListRow:
    tid: int
    title: str
    url: str
    # ---- 多维标签（站方自带，不自造维度） ----
    category: str = ""          # 类别：投诉/建议/咨询/点赞/市民巡访/追问/求助/其他
    state: str = ""             # 受理状态：已回复/已受理/未受理
    dept_id: str = ""           # 主部门 typeid
    dept_name: str = ""
    extra_dept_ids: List[str] = field(default_factory=list)   # typewrapmulti
    extra_dept_names: List[str] = field(default_factory=list)
    mailbox_id: str = ""        # 留言箱 sortid 23/24
    mailbox_name: str = ""
    light_code: str = ""        # green/yellow/grey/warn
    light_name: str = ""
    # ---- 帖子元数据 ----
    author: str = ""
    author_uid: str = ""
    pub_time: str = ""
    pub_ts: int = 0
    replies: int = 0
    views: int = 0
    has_image: int = 0
    last_reply_by: str = ""
    last_reply_time: str = ""
    last_reply_ts: int = 0

    #: ★ 这一行是不是来自**全局公告区**（`#msbgt`），而不是帖子行？
    #:
    #: 为什么要有这个标记（2026-10-24 踩过）：
    #:   同一个 tid 在站方不同板块列表页里，**站方给的标题不一样**：
    #:     · 主板块的**置顶行**：「必读公告：中国宁波网民生e点通（天一论坛"我要问政"）管理规定」
    #:     · 公告区（每个板块页都有）  ：「中国宁波网民生e点通群众留言板（天一论坛问政频道）管理规定」
    #:   而 upsert 是**覆盖式**的 → 后采的源会把先采的盖掉，
    #:   标题来回变不说，公告行的 author/replies/pub_time **全是空**，
    #:   一并把帖行的真值也盖成空（实测 6177045 的 author、replies 被清成空/0）。
    #:   → 公告行**只允许「填空」，不允许「覆盖」**（见 db.upsert_threads）。
    is_announcement: int = 0

    def as_tuple(self) -> tuple:
        return (self.tid, self.title, self.url, self.category, self.state,
                self.dept_id, self.dept_name, ",".join(self.extra_dept_ids),
                ",".join(self.extra_dept_names), self.mailbox_id,
                self.mailbox_name, self.light_code, self.light_name,
                self.author, self.author_uid, self.pub_time, self.pub_ts,
                self.replies, self.views, self.has_image,
                self.last_reply_by, self.last_reply_time, self.last_reply_ts)


def _direct_texts(el) -> List[str]:
    """元素的**直接**文本子节点（排除 span/注释）。"""
    out = []
    for t in el.find_all(string=True, recursive=False):
        if isinstance(t, Comment):
            continue
        s = (t or "").strip()
        if s:
            out.append(s)
    return out


def _bracket_tags(el) -> List[str]:
    """收集 th 里的方括号标签：font 内的 + 直接文本的。"""
    tags = []
    for f in el.select("font"):
        s = f.get_text(strip=True)
        if s.startswith("[") and s.endswith("]"):
            tags.append(s[1:-1].strip())
    for s in _direct_texts(el):
        if s.startswith("[") and s.endswith("]"):
            tags.append(s[1:-1].strip())
    return tags


def _num(s: str) -> int:
    s = (s or "").strip().replace(",", "")
    m = re.search(r"\d+", s)
    return int(m.group()) if m else 0


#: 站方链接里的 tid 参数
_TID_RE = re.compile(r"[?&]tid=(\d+)")


def _abs_url(href: str, list_url: str = "") -> str:
    """把站方的相对链接补成绝对地址。

    base 从当前列表页地址推（`…/forum.php` 之前那一段），
    推不出来就退回站方默认域名 —— 不写死具体板块路径。
    """
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("http"):
        return href
    base = "https://bbs.cnnb.com.cn"
    if list_url and "/forum.php" in list_url:
        base = list_url.split("/forum.php")[0]
    return f"{base}/{href.lstrip('./')}"


def parse_announcements(soup, list_url: str = "") -> List[ListRow]:
    """解析列表页顶部的「全局公告」区（``#msbgt``）。

    ★ 为什么必须单独识别（2026-10-24，用户反馈「置顶的帖会漏采」）：
      公告区**不是帖子行**。它的真实结构是::

          <div id="msbgt" class="flms"><div class="wrapper1">
            <a href="forum.php?mod=viewthread&tid=6177045">…管理规定</a>
            <a href="forum.php?mod=viewthread&tid=7342697">…删帖流程…</a>
            <a href="forum.php?mod=viewthread&tid=7342780">…总版规…</a>
          </div></div>

      没有 ``tbody[id^=normalthread_]`` 那种结构 → 旧的列表解析器
      （只扫帖子行）**永远看不见它们** → 公告帖一条都没进过库。
      实测库里 tid=7342780 / 7342697 / 6177045 全部为 0 条。

    公告行能给的元数据很少：只有 tid / 标题 / 链接；
    类别、受理状态、部门、作者、时间**都得靠详情页补**。
    """
    out: List[ListRow] = []
    seen = set()
    for a in soup.select("#msbgt a[href*='viewthread']"):
        href = a.get("href") or ""
        m = _TID_RE.search(href)
        if not m:
            continue
        tid = int(m.group(1))
        if tid in seen:
            continue
        title = a.get_text(strip=True)
        if not title:
            continue
        seen.add(tid)
        out.append(ListRow(tid=tid, title=title, url=_abs_url(href, list_url),
                           is_announcement=1))
    return out


def parse_list_page(html: str, list_url: str = "") -> List[ListRow]:
    """解析一页列表。返回去重后的行（同一 tid 可能出现两次）。

    ★ 三种来源都要收（2026-10-24 补，组员反馈「置顶的帖会漏采」）：
      ① `normalthread_XXX` —— 普通帖
      ② `stickthread_XXX`  —— **置顶帖**（站方把重要帖钉在列表顶部）
         ★ 旧实现只取 ① → **置顶帖从来没被采到过**（库里一个都没有）
      ③ `#msbgt` 公告区     —— **全局公告**（每个板块列表页顶部那几个红字链接）
         ★ 它们根本不是帖子行，是独立的链接块 → 旧实现完全没看 → 也漏
    """
    soup = BeautifulSoup(html or "", "html.parser")
    rows: List[ListRow] = []
    seen = set()

    # ---- ① 普通帖 + ② 置顶帖（结构相同，一起解析）----
    #    ★ 顺序要紧：**先收帖行，后收公告**。
    #      同一个 tid 可能既是置顶帖、又出现在公告区（实测 tid=6177045 两者都是）。
    #      帖行的元数据更全（作者/发帖时间/回复数/浏览数），
    #      公告行只有标题和链接 → 先到先得，**让帖行赢**。
    for tb in soup.select("tbody[id^=normalthread_], tbody[id^=stickthread_]"):
        m = re.search(r"(?:normalthread|stickthread)_(\d+)", tb.get("id") or "")
        if not m:
            continue
        tid = int(m.group(1))
        if tid in seen:
            continue
        seen.add(tid)

        th = tb.select_one("th")
        if th is None:
            continue

        row = ListRow(tid=tid, title="", url="")

        # ---- 方括号标签：按「是不是已知维度名」来分派，不靠位置 ----
        #     （站方改版会调顺序，靠位置必崩）
        for tag in _bracket_tags(th):
            if tag in _STATE_NAMES and not row.state:
                row.state = tag
            elif tag in _CATEGORY_NAMES and not row.category:
                row.category = tag

        # ---- 部门（typewrap 是主部门，typewrapmulti 是多标签部门） ----
        tw = th.select_one("span.typewrap em a")
        if tw:
            tm = re.search(r"typeid=(\d+)", tw.get("href") or "")
            row.dept_id = tm.group(1) if tm else ""
            row.dept_name = tw.get_text(strip=True)
        for a in th.select("span.typewrapmulti em a"):
            tm = re.search(r"typeid=(\d+)", a.get("href") or "")
            if tm:
                row.extra_dept_ids.append(tm.group(1))
                row.extra_dept_names.append(a.get_text(strip=True))

        # ---- 留言箱（sortid 23/24） ----
        sw = th.select_one("span.sortwrap em a")
        if sw:
            sm = re.search(r"sortid=(\d+)", sw.get("href") or "")
            row.mailbox_id = sm.group(1) if sm else ""
            row.mailbox_name = sw.get_text(strip=True)

        # ---- 时效灯 ----
        # 实测：**只有已回复的帖才带灯**（绿/黄/灰），未受理超 24h 才有红色预警；
        # 已受理、未受理的帖 th 里根本没有图标（只有两个空 span）。
        # 所以「没有图标」是**正常的未点亮状态**，要显式记成 none，
        # 不能留空 —— 留空分不清「没点亮」和「解析失败」。
        row.light_code, row.light_name = "none", "未点亮（尚未回复或未满24小时）"
        for img in th.select("img[src*=tyxg]"):
            fname = (img.get("src") or "").split("/")[-1]
            if fname in LIGHT_SVG:
                row.light_code, row.light_name = LIGHT_SVG[fname]
                break

        # ---- 标题 ----
        a = th.select_one("a.s.xst")
        if a:
            row.title = a.get_text(strip=True)
            href = a.get("href") or ""
            if href:
                row.url = _abs_url(href, list_url)
        row.has_image = 1 if th.select_one("img[alt=attach_img]") else 0

        # ---- by / num 两组单元格 ----
        bys = tb.select("td.by")
        if bys:
            b0 = bys[0]
            cite = b0.select_one("cite a")
            if cite:
                row.author = cite.get_text(strip=True)
                um = re.search(r"uid=(\d+)", cite.get("href") or "")
                row.author_uid = um.group(1) if um else ""
            tm2 = b0.select_one("em span")
            if tm2:
                row.pub_time = tm2.get_text(strip=True)
                row.pub_ts = to_ts(row.pub_time) or 0
        if len(bys) >= 2:
            b1 = bys[1]
            c1 = b1.select_one("cite a")
            if c1:
                row.last_reply_by = c1.get_text(strip=True)
            l1 = b1.select_one("em a")
            if l1:
                row.last_reply_time = l1.get_text(strip=True)
                row.last_reply_ts = to_ts(row.last_reply_time) or 0

        num = tb.select_one("td.num")
        if num:
            na = num.select_one("a")
            if na:
                row.replies = _num(na.get_text(strip=True))
            ne = num.select_one("em")
            if ne:
                row.views = _num(ne.get_text(strip=True))

        if row.title:
            rows.append(row)

    # ---- ③ 全局公告区（最后收：帖行已占的 tid 自动跳过）----
    for r in parse_announcements(soup, list_url):
        if r.tid in seen:
            continue
        seen.add(r.tid)
        rows.append(r)
    return rows


_TOTALPAGE_RE = re.compile(r'totalpage="(\d+)"')
_TOTALPAGE_CN_RE = re.compile(r"共\s*(\d+)\s*页")
_THREADCOUNT_RE = re.compile(r'(?:共有|主题总数[:：]?)\s*<?[^>]*>?\s*(\d+)')


def parse_list_page_info(html: str) -> Dict:
    """读出列表页的分页信息。

    `totalpage` 是 Discuz 服务端渲染的分页上限 —— Discuz X3.5 的
    forumdisplay 最多给 500 页，再往后就回绕到第 1 页。
    **必须读它**，否则会以为可以一直往后翻，结果把第 1 页当成第 501 页
    重复入库，还把水位线污染掉。
    """
    html = html or ""
    out: Dict = {"total_page": 0, "total_threads": 0}
    m = _TOTALPAGE_RE.search(html)
    if not m:
        m = _TOTALPAGE_CN_RE.search(html)
    if m:
        out["total_page"] = int(m.group(1))
    mt = _THREADCOUNT_RE.search(html)
    if mt:
        out["total_threads"] = int(mt.group(1))
    return out


# ================================================================= 详情页
@dataclass
class FloorMeta:
    pid: int
    floor: int = 0            # 1 = 楼主
    author: str = ""
    author_uid: str = ""
    post_time: str = ""
    post_ts: int = 0
    is_author: int = 0


@dataclass
class FlowStep:
    step: int
    name: str
    time: str = ""
    ts: int = 0


@dataclass
class ThreadPage:
    tid: int = 0
    page_no: int = 1
    token: str = ""
    pids: List[int] = field(default_factory=list)          # 文档顺序
    floors: Dict[int, FloorMeta] = field(default_factory=dict)
    total_pages: int = 1
    title: str = ""
    flow: List[FlowStep] = field(default_factory=list)
    dept_id: str = ""
    dept_name: str = ""

    @property
    def head_pid(self) -> Optional[int]:
        """头 = 楼主那一楼。"""
        for pid in self.pids:
            f = self.floors.get(pid)
            if f and f.is_author:
                return pid
        return self.pids[0] if self.pids else None

    @property
    def tail_pid(self) -> Optional[int]:
        """尾 = 本页最后一楼（跨页时以末页为准）。"""
        return self.pids[-1] if self.pids else None


_TOKEN_RE = re.compile(r"token:'([0-9a-f]{16,})'")

#: 站方对**历史帖子**只给这句提示（实测 2026-10-23，页面原文）：
#:   "历史帖子仅限作者本人查看"
#: 这不是我们程序坏了，是**站方的可见性策略** —— 详情页正文对非作者不公开。
SITE_RESTRICTED = "历史帖子仅限作者本人查看"

#: Discuz 的「提示信息」页常见容器
_NOTICE_PAT = re.compile(
    r'(?:<div[^>]*class="[^"]*(?:alert_error|alert_info|alert_warning)[^"]*"[^>]*>'
    r'|<div[^>]*id="message"[^>]*>)(.*?)(?:</div>)', re.S | re.I)


def parse_site_notice(html: str) -> str:
    """这个页面是不是站方的「提示信息」页？返回提示原文（没有则返回空串）。

    ★ 为什么要单独识别它
      详情页拿不到正文时，可能是**三种**完全不同的事：
        ① 站方限制（历史帖子仅限作者本人查看）→ **谁采都采不到**，
           属站方数据现实，不该记成「失败」，更不该让成员反复白跑；
        ② 帖子被删 / 不存在 → 404；
        ③ 我们的解析退化了 → 页面其实有内容，只是没匹配上。
      把①单独摘出来，是为了不再把它混进②③里乱猜。

    ★ 判据要**严**：必须是「提示信息页」且含有那句提示，
      否则普通帖子里恰好提到这句话会被误判。
    """
    if not html or SITE_RESTRICTED not in html:
        return ""
    if 'id="post_' in html or "token:'" in html:
        return ""                      # 页面里有楼层锚点 → 是正常帖，不是提示页
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    title = m.group(1).strip() if m else ""
    if "提示信息" not in title:
        return ""                      # 不是提示页 → 宁可漏判也不误判
    for mm in _NOTICE_PAT.finditer(html):
        txt = re.sub(r"<[^>]+>", " ", mm.group(1))
        txt = re.sub(r"\s+", " ", txt).strip()
        if SITE_RESTRICTED in txt:
            return txt
    return SITE_RESTRICTED
_PAGECOUNT_RE = re.compile(r"共\s*(\d+)\s*页")
_POSTMSG_RE = re.compile(r'id="postmsg_(\d+)"')
#: 外层楼层容器 `<div id="post_{pid}">` —— **这一页有哪些楼层的完整清单**
#:
#: ★ 为什么以它为准、而不是 postmsg_（2026-10-24 修正）：
#:   实测 tid=6177045 第 4 页：post_ 容器 15 个，postmsg_ 锚点只有 14 个。
#:   缺的那层（pid=108201995，第 51 楼）在页面上**楼层容器是齐的**
#:   （favatar / postnum / authorposton 都有，楼层号 51 也读得到），
#:   只是站方**没给它生成 ajax 注入目标**（没有 postmsg_ 容器），
#:   而 ajax_getpost 照样返回了它的正文。
#:   旧实现只认 postmsg_ → 少一个 pid → 「pid 集合精确相等」校验失败
#:   → **整帖停在第 3 页**，后面的 3 页再也采不到。
#:
#:   已批量验证的不变式（5 个帖共 15 页全过）：
#:     postmsg_ ⊆ post_  且  post_ == ajax 返回集合（精确相等）
#:   → 用 post_ 既不会漏楼层，也不会放宽防串帖的校验。
_POSTBOX_RE = re.compile(r'id="post_(\d+)"')
#: 详情页分页块：<div class="pgt"><div class="pg"><strong>1</strong><a ...&page=2>2</a>
_PG_BLOCK_RE = re.compile(r'<div class="pg">(.{0,3000}?)</div>', re.S)
_PG_HREF_RE = re.compile(r'href="[^"]*[?&]page=(\d+)"')
#: 楼层号标签：楼主 / <em>5</em><sup>#</sup> / 来自 2# / 顶来自 2#
_FLOOR_RE = re.compile(r"(\d+)\s*[#楼]")


def parse_total_pages(html: str) -> int:
    """读出详情页的总页数。

    ⚠ 判据是「有没有分页块」，不是「共几页」：
      · 单页帖**根本没有** `<div class="pg">`（实测 tid=8332684）
      · 多页帖有，形如 `<div class="pg"><strong>1</strong><a ...page=2>2</a>`
      · 单页帖请求 page=2 会**回绕返回第 1 页**（pid 集合完全相同、顺序略变）
    所以「有没有分页块」才是可靠信号；拿不到就当 1 页，
    绝不能盲目往下翻 —— 否则会把第 1 页当成第 2 页重复采一遍。
    """
    html = html or ""
    m = _PG_BLOCK_RE.search(html)
    if m:
        nums = [int(x) for x in _PG_HREF_RE.findall(m.group(1))]
        if nums:
            return max(nums)
    m = _PAGECOUNT_RE.search(html)
    if m:
        return max(1, int(m.group(1)))
    return 1


def parse_thread_page(html: str, tid: int = 0, page_no: int = 1) -> ThreadPage:
    """解析一页详情页。"""
    tp = ThreadPage(tid=tid, page_no=page_no)
    html = html or ""

    mt = _TOKEN_RE.search(html)
    tp.token = mt.group(1) if mt else ""

    # pid 清单：post_ 外层容器是**全集**，postmsg_ 只是「正文由 ajax 注入」的子集。
    # 站方偶尔不给某层生成 postmsg_ 容器（见 _POSTBOX_RE 上方说明），
    # 只认 postmsg_ 会漏掉那一层 → pid 集合校验失败 → 整帖采不下去。
    pids_box = [int(x) for x in _POSTBOX_RE.findall(html)]
    pids_msg = [int(x) for x in _POSTMSG_RE.findall(html)]
    pids = pids_box if pids_box else pids_msg
    # 去重但保持顺序
    seen = set()
    tp.pids = [p for p in pids if not (p in seen or seen.add(p))]

    tp.total_pages = parse_total_pages(html)

    soup = BeautifulSoup(html, "html.parser")

    # 标题
    subj = soup.select_one("#thread_subject")
    if subj:
        tp.title = subj.get_text(strip=True)

    # 楼层：作者 / 楼层号 / 发帖时间 分散在三个 id 前缀里
    for el in soup.select("div[id^=favatar]"):
        pid_s = (el.get("id") or "")[len("favatar"):]
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        a = el.select_one(".authi a")
        fm = tp.floors.setdefault(pid, FloorMeta(pid=pid))
        if a:
            fm.author = a.get_text(strip=True)
            um = re.search(r"uid=(\d+)", a.get("href") or "")
            if um:
                fm.author_uid = um.group(1)

    for el in soup.select("a[id^=postnum]"):
        pid_s = (el.get("id") or "")[len("postnum"):]
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        fm = tp.floors.setdefault(pid, FloorMeta(pid=pid))
        # 楼层标签三种形态：
        #   "楼主"                      → 第 1 楼
        #   "<em>5</em><sup>#</sup>"     → 5#
        #   "顶来自 2#" / "来自 2#"      → 置顶/被顶替的楼层
        label = el.get_text(" ", strip=True)
        if "楼主" in label:
            fm.floor = 1
            fm.is_author = 1
        else:
            fm2 = _FLOOR_RE.search(label)
            fm.floor = int(fm2.group(1)) if fm2 else 0

    for el in soup.select("em[id^=authorposton]"):
        pid_s = (el.get("id") or "")[len("authorposton"):]
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        fm = tp.floors.setdefault(pid, FloorMeta(pid=pid))
        txt = el.get_text(" ", strip=True)
        mt2 = re.search(r"发表于\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}(?::[0-9]{2})?)", txt)
        if mt2:
            fm.post_time = mt2.group(1)
            fm.post_ts = to_ts(fm.post_time) or 0

    # 受理流程时间线（白送的数据：分类/受理/回复时长可直接算）
    for i, li in enumerate(soup.select(".cdProgress li")):
        st = li.select_one("strong")
        pp = li.select_one("p")
        if not st:
            continue
        name = st.get_text(strip=True).replace("\xa0", "").strip()
        tm3 = ""
        if pp:
            tm3 = pp.get_text(" ", strip=True)
        tp.flow.append(FlowStep(step=i, name=name, time=tm3, ts=to_ts(tm3) or 0))

    # 详情页头部的部门
    for a in soup.select('a[href*="typeid="]'):
        nm = a.get_text(strip=True)
        if nm and not tp.dept_name:
            mm = re.search(r"typeid=(\d+)", a.get("href") or "")
            tp.dept_id = mm.group(1) if mm else ""
            tp.dept_name = nm
            break

    return tp


def parse_ajax_posts(payload: dict) -> Dict[int, str]:
    """ajax_getpost.php 返回 {"code":1,"data":{"<pid>":"<html>"}} → {pid: html}。"""
    if not isinstance(payload, dict):
        return {}
    if payload.get("code") != 1:
        return {}
    data = payload.get("data") or {}
    out: Dict[int, str] = {}
    for k, v in data.items():
        try:
            out[int(k)] = v if isinstance(v, str) else str(v)
        except (TypeError, ValueError):
            continue
    return out
