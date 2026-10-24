# -*- coding: utf-8 -*-
"""
正文抽取与编码判定（移植自 cacx collector/base.py，行为保持一致）
================================================================
为什么不能直接用 requests 的 r.text：
  requests 见到 `Content-Type: text/html`（没有 charset）会**擅自**把
  r.encoding 设成 ISO-8859-1，于是每一个中文字符都变成「Ã¥â»¨」。
  这里不会报任何错 —— 乱码会一路写进数据库，直到人真去读才发现。
  所以编码判定必须自己来。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import List

_DEFAULT_ENCODINGS = {"iso-8859-1", "latin-1", "latin1", "ascii"}


def decode_response(r) -> str:
    """拿到响应正文（str），编码判定顺序：
        1) 响应头里**真的**声明了 charset（不是 requests 的默认猜测）→ 用它
        2) 内容能严格按 UTF-8 解码 → utf-8
        3) 能严格按 gb18030 解码 → gb18030（GBK/GB2312 的超集）
        4) 兜底用 requests 的 apparent_encoding
    """
    enc = (r.encoding or "").lower().strip()
    if enc and enc not in _DEFAULT_ENCODINGS:
        try:
            return r.content.decode(enc, errors="replace")
        except (LookupError, TypeError):
            pass
    for cand in ("utf-8", "gb18030"):
        try:
            text = r.content.decode(cand)          # strict，失败就换下一个
        except (UnicodeDecodeError, LookupError):
            continue
        return text
    return r.text


def absolutize(src: str, base_url: str) -> str:
    if not src:
        return ""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return base_url.rstrip("/") + src
    if not src.startswith("http"):
        return base_url.rstrip("/") + "/" + src.lstrip("./")
    return src


class TextCollector(HTMLParser):
    """HTML 正文 → 干净纯文本。段落间换行，图片记为 [图:URL]。"""

    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.buf: List[str] = []
        self.skip = 0

    def _abs(self, src):
        return absolutize(src, self.base)

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.skip:
            return
        d = dict(attrs)
        if tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.buf.append("\n")
        elif tag == "img":
            # Discuz 附件真址在 zoomfile 上，src 只是 none.gif 占位
            src = d.get("zoomfile") or d.get("file") or d.get("src", "")
            self.buf.append(f" [图:{self._abs(src)}] ")
        elif tag == "td":
            self.buf.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
            return
        if not self.skip and tag in ("p", "div", "tr", "li", "h1", "h2", "h3",
                                     "h4", "h5", "h6"):
            self.buf.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)

    def text(self) -> str:
        raw = "".join(self.buf).replace("\xa0", " ").replace("　", " ")
        lines = [re.sub(r"[ \t]+", " ", l).strip() for l in raw.split("\n")]
        out, blank = [], 0
        for l in lines:
            if not l:
                blank += 1
                if blank:
                    continue
            else:
                blank = 0
            out.append(l)
        return "\n".join(out).strip()


def html_to_text(content_html: str, base_url: str = "") -> str:
    c = TextCollector(base_url)
    try:
        c.feed(content_html or "")
        c.close()
    except Exception:
        pass
    return c.text()


def extract_images(content_html: str, base_url: str = "") -> List[str]:
    """抽取正文里的附件图真址（Discuz 用 zoomfile，src 是 none.gif 占位）。"""
    out: List[str] = []
    for m in re.finditer(r'zoomfile="([^"]+)"', content_html or ""):
        u = absolutize(m.group(1), base_url)
        if u and u not in out:
            out.append(u)
    if not out:
        for m in re.finditer(r'<img[^>]+src="([^"]+)"', content_html or ""):
            u = absolutize(m.group(1), base_url)
            if u and "none.gif" not in u and u not in out:
                out.append(u)
    return out
