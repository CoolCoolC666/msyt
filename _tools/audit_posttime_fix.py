# -*- coding: utf-8 -*-
"""取证：楼层时间正则为何失配 + 一行修法是否有效（只验证，不改生产代码）。"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "程序"))
from msyt.config import Config       # noqa: E402
from msyt.http_client import Client  # noqa: E402
from msyt.detail import THREAD_URL    # noqa: E402
from msyt.parsers import parse_thread_page  # noqa: E402

OLD = re.compile(r"发表于\s*([0-9]{4}-[0-9]{2}-[0-9]{2}\s+[0-9]{2}:[0-9]{2}(?::[0-9]{2})?)")
NEW = re.compile(r"发表于\s*([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})\s+([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?")

cfg = Config.load()
client = Client(cfg)

print("%-9s %-7s %-7s %-9s %-9s" % ("tid", "楼层数", "旧正则中", "新正则中", "补零后样例"))
for tid, pages in ((8236013, (1, 2, 3)), (8294530, (1, 3, 6)), (8333204, (1, 40, 81))):
    n_floor = old_ok = new_ok = 0
    sample = ""
    for pg in pages:
        url = THREAD_URL.format(tid=tid, page=pg)
        html = client.get(url, referer=url).text
        tp = parse_thread_page(html, tid=tid, page_no=pg)
        for fm in tp.floors.values():
            n_floor += 1
        # 直接对页面里所有 authorposton 文本做两种正则比对
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for el in soup.select("em[id^=authorposton]"):
            txt = el.get_text(" ", strip=True)
            if OLD.search(txt):
                old_ok += 1
            m = NEW.search(txt)
            if m:
                new_ok += 1
                if not sample:
                    y, mo, d, hh, mm, ss = m.groups()
                    sample = "%s-%02d-%02d %02d:%02d" % (y, int(mo), int(d), int(hh), int(mm))
    print("%-9d %-7d %-7d %-9d %-9s" % (tid, n_floor, old_ok, new_ok, sample))

print()
print("--- 新正则对各种真实写法是否都中 ---")
for s in ("发表于 2025-5-23 19:55来自浙江",
          "发表于 2026-9-2 14:49来自浙江",
          "发表于 2026-10-29 15:22",
          "发表于 2026-10-29 15:22:07",
          "发表于 2024-12-1 08:00来自浙江",
          "发表于 2013-8-30 18:20"):
    m = NEW.search(s)
    print("  %-34s -> %s" % (s, (m.group(0) if m else "不匹配")))
