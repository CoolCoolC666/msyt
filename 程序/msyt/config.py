# -*- coding: utf-8 -*-
"""配置：站点参数、限速、路径、采集策略默认值。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict

BASE = "https://bbs.cnnb.com.cn"
FID = 1176

#: 列表页 URL 模板
LIST_URL = f"{BASE}/forum.php?mod=forumdisplay&fid={FID}"
#: 部门/维度筛选页（每个维度一条独立的列表，带自己的分页）
FILTER_URL = f"{BASE}/forum.php?mod=forumdisplay&fid={FID}&filter=typeid&typeid={{typeid}}"
#: 详情页
THREAD_URL = f"{BASE}/forum.php?mod=viewthread&tid={{tid}}&page={{page}}"
#: 楼层正文
AJAX_URL = f"{BASE}/ajax_getpost.php"
#: 站方统计脚本（区县市上月留言/受理量、全站总量）
STATS_JS = f"{BASE}/html/yp/ms_sjszxsqtotal.js"

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # ---- 路径 ----
    db_path: str = str(ROOT / "data" / "msyt.db")
    export_dir: str = str(ROOT / "data" / "exports")
    log_dir: str = str(ROOT / "docs" / "采集日志")

    # ---- 站点 ----
    base_url: str = BASE
    fid: int = FID

    # ---- 采集行为 ----
    #: 列表全局页上限（Discuz 分页上限就是 500；超出回绕到第 1 页）
    list_page_cap: int = 500
    #: 列表单页最多取多少页（部门视图可能远小于 500）
    list_max_pages: int = 500
    #: 部门视图是否也遵守 list_page_cap
    dept_page_cap: int = 500

    #: 帖与帖之间的限速（秒）。**详情页→ajax 之间不得等待**，
    #: 因为 token 时效 < 30 秒。
    interval: float = 1.2
    #: 列表页之间的限速（秒）
    list_interval: float = 1.0
    timeout: int = 25
    retry: int = 3

    #: 详情先旧后新：True=按发帖时间升序（老的先来）
    detail_oldest_first: bool = True
    #: 一帖最多采多少页（保护上限；正常帖子 1~3 页，极端长帖更少）
    detail_max_pages: int = 50
    #: 详情失败的帖最多重试几次
    detail_max_attempts: int = 3

    user_agent: str = DEFAULT_UA

    extra: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        cfg = cls()
        p = Path(path) if path else ROOT / "config.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            known = {f for f in cls.__dataclass_fields__}
            for k, v in data.items():
                if k in known:
                    setattr(cfg, k, v)
                else:
                    cfg.extra[k] = v
        return cfg

    def save(self, path: str | os.PathLike | None = None) -> Path:
        p = Path(path) if path else ROOT / "config.json"
        data = asdict(self)
        data.pop("extra", None)
        data.update(self.extra)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    def ensure_dirs(self) -> None:
        for d in (Path(self.db_path).parent, Path(self.export_dir),
                  Path(self.log_dir)):
            d.mkdir(parents=True, exist_ok=True)
