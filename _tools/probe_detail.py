# -*- coding: utf-8 -*-
"""probe_detail.py —— 直接打一帖详情页，看站方到底给了什么。

为什么需要它
------------
协作池里出现「成员回传为空」时，有两种完全不同的可能：
  ① **站方数据现实**：老帖的详情页已经变成 Discuz「提示信息」页 / 404，
     帖子在站方那边已经打不开了 —— 这种情况**谁采都采不到**，
     不是成员的锅，也不该记成「失败」。
  ② **我们的解析坏了**：页面有内容，但选择器没匹配上。

这两种必须分清，否则：
  · 池子里全是死链 → 成员采了一堆空 → done 永远是 0 → 界面像坏了
  · 解析坏了 → 以为站方没数据，其实是代码退化

用法：
    python _tools\\probe_detail.py 6269949 6820041
"""
from __future__ import annotations

import html as H
import re
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "程序"
sys.path.insert(0, str(PKG))

from msyt.config import THREAD_URL           # noqa: E402
from msyt.http_client import Client          # noqa: E402

KEYWORDS = ("提示信息", "thread closed", "不存在", "已关闭", "无权",
            "抱歉", "访问权限", "指定的主题不存在", "已删除", "帖子不存在")


def probe(tid: int) -> None:
    cfg = __import__("msyt.config", fromlist=["Config"]).Config.load()
    client = Client(cfg, verbose=False)
    url = THREAD_URL.replace("{tid}", str(tid)).replace("{page}", "1")
    print(f"\n=== tid={tid} ===")
    print(f"URL: {url}")
    try:
        r = client.get(url)
    except Exception as e:  # noqa: BLE001
        print(f"  请求失败：{e}")
        return
    raw = getattr(r, "text", "") or ""
    html = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    print(f"  长度 {len(html)}")
    low = html.lower()
    for kw in KEYWORDS:
        if kw.lower() in low:
            print(f"  ★ 命中关键词：{kw}")
    m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if m:
        print(f"  title: {H.unescape(m.group(1)).strip()[:90]}")
    m = re.search(r'id="thread_subject"[^>]*>(.*?)</', html, re.S | re.I)
    if m:
        t = H.unescape(re.sub("<[^>]+>", "", m.group(1))).strip()
        print(f"  主题标题: {t[:90]}")
    print(f"  楼层锚点 id=\"post_N\" 出现 {len(re.findall(r'id=\"post_[0-9]+\"', html))} 次")
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    print(f"  可见文字片段：{txt[:220]}")


if __name__ == "__main__":
    for t in sys.argv[1:]:
        probe(int(t))
