# -*- coding: utf-8 -*-
"""Web layout guard for the four sub-panels of the collect tab.

Root cause of the 2026-11-08 "annotation console is blank" bug:
  the #sub-anno block was written INSIDE #sub-assist. showSub() only removes
  the `hidden` class of the panel itself, so the ancestor #sub-assist stayed
  display:none and the whole panel stayed invisible (blank page).

This guard checks the DOM nesting of index.html so that cannot regress.
Usage:  python test_web_layout.py [path/to/index.html]
"""
import sys
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "\u7a0b\u5e8f" / "msyt" / "web" / "index.html"

PANELS = ["sub-jobs", "sub-assist", "sub-anno", "sub-me"]

#: id -> which sub-panel it must live in
OWNER = {
    "job-log": "sub-jobs",
    "t-assist": "sub-assist",
    "t-online": "sub-assist",
    "t-members": "sub-assist",
    "conflicts-card": "sub-assist",
    "anno-claim": "sub-anno",
    "t-anno-queue": "sub-anno",
    "anno-batches-card": "sub-anno",
    "me-info": "sub-me",
    "btn-pw-change": "sub-me",
}

NESTED_SNIPPET = (
    '<section id="tab-collect" class="panel">'
    '<div class="subpanel hidden" id="sub-assist">'
    '<div class="subpanel hidden" id="sub-anno"><div id="anno-claim"></div></div>'
    '</div>'
    '</section>'
)


class Tree(HTMLParser):
    """Record the ancestor chain of every id, and the open stack at EOF."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.chain = {}

    def handle_starttag(self, tag, attrs):
        ident = dict(attrs).get("id") or ""
        self.stack.append((tag, ident))
        if ident and ident not in self.chain:
            self.chain[ident] = list(self.stack)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def violations(src):
    """Return a list of layout problems found in the given HTML source."""
    t = Tree()
    t.feed(src)
    bad = []

    if t.stack:
        bad.append("unclosed tags at EOF: %s" % [x[1] or x[0] for x in t.stack])

    for p in PANELS:
        chain = t.chain.get(p)
        if not chain:
            bad.append("missing panel #%s" % p)
            continue
        panel_ids = [i for _, i in chain]
        # a sub-panel must not sit inside another sub-panel
        nested = [i for i in panel_ids if i.startswith("sub-") and i != p]
        if nested:
            bad.append("#%s is nested inside #%s" % (p, nested[0]))
        # ... and must live directly under the collect tab
        if "tab-collect" not in panel_ids:
            bad.append("#%s is not inside #tab-collect" % p)
        elif panel_ids[-2] != "tab-collect":
            bad.append("#%s parent is #%s, expected #tab-collect" % (p, panel_ids[-2]))

    for ident, owner in OWNER.items():
        chain = t.chain.get(ident)
        if not chain:
            bad.append("missing element #%s" % ident)
            continue
        ids = [i for _, i in chain]
        want = [i for i in ids if i.startswith("sub-")]
        if not want or want[-1] != owner:
            bad.append("#%s should live in #%s, found %s" % (ident, owner, want or "no panel"))

    return bad


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else WEB
    src = path.read_text(encoding="utf-8")
    bad = violations(src)

    # --- reverse verification: the same check must FAIL on a nested panel ---
    if not violations(NESTED_SNIPPET):
        print("FAIL  reverse check: nested panel was NOT reported")
        return 1

    if bad:
        print("FAIL  %d layout problem(s) in %s" % (len(bad), path))
        for b in bad:
            print("   -", b)
        return 1

    print("OK    web layout: %d sub-panels are siblings, %d owned ids in place" % (len(PANELS), len(OWNER)))
    return 0


if __name__ == "__main__":
    sys.exit(main())