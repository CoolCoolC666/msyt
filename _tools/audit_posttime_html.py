# -*- coding: utf-8 -*-
"""取证：楼层时间到底站方给没给 / 我们解析到没有。"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "程序"))
from msyt.config import Config       # noqa: E402
from msyt.http_client import Client  # noqa: E402
from msyt.detail import THREAD_URL    # noqa: E402
from msyt.parsers import parse_thread_page  # noqa: E402
from bs4 import BeautifulSoup         # noqa: E402

cfg = Config.load()
client = Client(cfg)

for tid in (8236013, 8294530, 8333204):
    url = THREAD_URL.format(tid=tid, page=1)
    html = client.get(url, referer=url).text
    print("=" * 68)
    print("tid=%d  长度=%d" % (tid, len(html)))
    print("  authorposton 次数=%d  '发表于' 次数=%d  postnum 次数=%d"
          % (len(re.findall("authorposton", html)),
             len(re.findall("发表于", html)),
             len(re.findall('id="postnum', html))))
    soup = BeautifulSoup(html, "html.parser")
    ems = soup.select("em[id^=authorposton]")
    print("  soup.select 取到 em 数 =", len(ems))
    for el in ems[:3]:
        print("     id=%r text=%r" % (el.get("id"), el.get_text(strip=True)[:36]))
    tp = parse_thread_page(html, tid=tid, page_no=1)
    with_t = sum(1 for f in tp.floors.values() if f.post_time)
    print("  parse_thread_page: pids=%d floors=%d 其中有 post_time 的=%d"
          % (len(tp.pids), len(tp.floors), with_t))
    for pid, fm in list(tp.floors.items())[:3]:
        print("     pid=%s floor=%s post_time=%r post_ts=%r"
              % (pid, fm.floor, fm.post_time, fm.post_ts))
