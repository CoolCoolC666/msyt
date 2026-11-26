# -*- coding: utf-8 -*-
"""
evidence.py —— 舆情存证域（P0：表结构 + 哈希链 + 证据包）
================================================================================
设计原则（与《存证改造方案 2026-11-26》一致）
------------------------------------------------
1. **证据域与采集域分离**：不给 threads / posts 加列，只在「案件」这一层建证据。
   采集是面（61 万帖、天天跑），证据是点（几十条、要封存）。
2. **哈希链而非"区块链"**：custody_chain 是 append-only，每行串上一行的 hash。
   它能回答「导出的东西此后有没有被改过」，**不能**替代第三方存证平台。
   真要司法级时间证明，得上 RFC3161 TSA 或 OpenTimestamps（P2）。
3. **不自欺**：拿不到的载体（restricted / login-wall）写进 exceptions，
   绝不生成"看起来像页面"的假截图。

P0 能做的
---------
    msyt case new / add / list
    msyt evidence export --case <id>     → zip + 外部 .sha256.txt + 链侧车
    msyt custody verify --case <id>      → 三查：zip 哈希 / 包内文件 / 哈希链

P0 还**没有**载体（MHTML/截图/录屏）—— 那是 P1 的 `capture`。包里会写明这一点。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple

ALGO = "sha256"
GENESIS = "0" * 64
TOOL = "msyt-evidence"
ROLES = ("first", "reply", "quote", "repost", "comment")
LEVELS = ("一般", "较大", "重大", "特别重大")


# ------------------------------------------------------------------ 基础
def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canon(obj: Any) -> str:
    """规范化 JSON（键排序、无空格）—— 链上的 payload 用它算，保证可复算。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evidence_root(db_path: str) -> str:
    """证据根目录：跟库放在一起（库被搬走，证据跟着走）。"""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "evidence")


def _tool_ver() -> str:
    try:
        from . import __version__
        return str(__version__)
    except Exception:                                  # noqa: BLE001
        return "unknown"


# ------------------------------------------------------------------ 哈希链
class Chain:
    """custody_chain 的读写。

    this_hash = SHA256( seq | at | kind | ref_table | ref_id | payload_sha256 | prev_hash )

    ★ 为什么要把 seq / at / kind 都串进去：
      只串 payload 的话，攻击者可以「调换两条记录的时间」而链照样对得上。
      把元数据一起进来，任何字段改动都会断链。
    """

    def __init__(self, conn):
        self.conn = conn

    def head(self) -> Tuple[int, str]:
        r = self.conn.execute(
            "SELECT seq, this_hash FROM custody_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if not r:
            return 0, GENESIS
        return int(r[0]), str(r[1])

    def _calc(self, seq: int, at: str, kind: str, ref_table: str,
              ref_id: str, payload: str, prev: str) -> str:
        raw = "|".join([str(seq), at, kind, ref_table or "", ref_id or "",
                        payload, prev])
        return sha256_bytes(raw.encode("utf-8"))

    def append(self, kind: str, ref_table: str = "", ref_id: str = "",
               payload_sha256: str = "") -> Dict[str, Any]:
        seq, prev = self.head()
        seq += 1
        at = now_str()
        payload = payload_sha256 or sha256_bytes(
            ("%s|%s|%s" % (kind, ref_table, ref_id)).encode("utf-8"))
        this = self._calc(seq, at, kind, ref_table, ref_id, payload, prev)
        self.conn.execute(
            "INSERT INTO custody_chain(seq,at,kind,ref_table,ref_id,"
            "payload_sha256,prev_hash,this_hash) VALUES(?,?,?,?,?,?,?,?)",
            (seq, at, kind, ref_table or "", ref_id or "", payload, prev, this))
        self.conn.commit()
        return {"seq": seq, "at": at, "kind": kind, "payload_sha256": payload,
                "prev_hash": prev, "this_hash": this}

    def entries(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT seq,at,kind,ref_table,ref_id,payload_sha256,prev_hash,this_hash "
            "FROM custody_chain ORDER BY seq ASC").fetchall()
        return [dict(r) for r in rows]

    def verify(self) -> Dict[str, Any]:
        """从 seq=1 逐条重算。返回 {ok, total, broken_at, head}。"""
        entries = self.entries()
        if not entries:
            return {"ok": True, "total": 0, "broken_at": 0, "head": GENESIS,
                    "note": "链为空"}
        prev = GENESIS
        for e in entries:
            if e["prev_hash"] != prev:
                return {"ok": False, "total": len(entries),
                        "broken_at": e["seq"], "head": entries[-1]["this_hash"],
                        "reason": "prev_hash 与上一条 this_hash 不一致"}
            calc = self._calc(e["seq"], e["at"], e["kind"], e["ref_table"],
                              e["ref_id"], e["payload_sha256"], e["prev_hash"])
            if calc != e["this_hash"]:
                return {"ok": False, "total": len(entries),
                        "broken_at": e["seq"], "head": entries[-1]["this_hash"],
                        "reason": "this_hash 重算不一致（该条记录被改过）"}
            prev = e["this_hash"]
        return {"ok": True, "total": len(entries), "broken_at": 0,
                "head": entries[-1]["this_hash"]}


def ensure_genesis(db) -> Dict[str, Any]:
    """第一次用存证域时写一条创世记录（同时把库路径与工具版本钉进去）。"""
    chain = Chain(db.conn)
    seq, _ = chain.head()
    if seq == 0:
        with db.lock:
            return chain.append("chain.genesis", "meta", os.path.basename(db.path),
                                sha256_bytes(canon({"tool": TOOL,
                                                    "ver": _tool_ver(),
                                                    "at": now_str()}).encode("utf-8")))
    return {}


# ------------------------------------------------------------------ 案件
def _next_case_id(conn) -> str:
    year = time.strftime("%Y")
    n = conn.execute("SELECT COUNT(*) FROM cases WHERE case_id LIKE ?",
                     ("MSYT-%s-%%" % year,)).fetchone()[0]
    return "MSYT-%s-%04d" % (year, int(n) + 1)


def create_case(db, title: str, level: str = "一般", category: str = "",
                summary: str = "", owner: str = "", created_by: str = "",
                note: str = "") -> Dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise ValueError("案件标题不能为空")
    if level and level not in LEVELS:
        raise ValueError("level 只能是 %s" % "/".join(LEVELS))
    ensure_genesis(db)
    with db.lock:
        case_id = _next_case_id(db.conn)
        at = now_str()
        row = {"case_id": case_id, "title": title, "summary": summary,
               "level": level, "category": category, "status": "open",
               "owner": owner, "created_by": created_by, "created_at": at,
               "note": note}
        db.conn.execute(
            "INSERT INTO cases(case_id,title,summary,level,category,status,"
            "owner,created_by,created_at,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (case_id, title, summary, level, category, "open", owner,
             created_by, at, note))
        db.conn.commit()
    Chain(db.conn).append("case.create", "cases", case_id,
                          sha256_bytes(canon(row).encode("utf-8")))
    return get_case(db, case_id)


def get_case(db, case_id: str) -> Dict[str, Any]:
    r = db.conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if not r:
        raise KeyError("没有这个案件：%s" % case_id)
    return dict(r)


def add_item(db, case_id: str, tid: int, role: str = "first",
             added_by: str = "", note: str = "") -> Dict[str, Any]:
    role = (role or "first").strip()
    if role not in ROLES:
        raise ValueError("role 只能是 %s" % "/".join(ROLES))
    case = get_case(db, case_id)
    if (case.get("status") or "") == "sealed":
        raise ValueError("案件已封存（%s），不能再加条目" % case.get("sealed_at"))
    t = db.conn.execute("SELECT tid,title,url FROM threads WHERE tid=?",
                        (int(tid),)).fetchone()
    if not t:
        raise KeyError("库里没有这个 tid：%s" % tid)
    with db.lock:
        db.conn.execute(
            "INSERT INTO case_items(case_id,tid,role,added_by,added_at,note) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(case_id,tid) DO UPDATE SET "
            "role=excluded.role, note=excluded.note",
            (case_id, int(tid), role, added_by, now_str(), note))
        db.conn.commit()
    Chain(db.conn).append("item.add", "case_items", "%s:%s" % (case_id, tid),
                          sha256_bytes(canon({"case_id": case_id, "tid": int(tid),
                                              "role": role}).encode("utf-8")))
    return {"case_id": case_id, "tid": int(tid), "role": role,
            "title": t["title"], "url": t["url"]}


def list_cases(db) -> List[Dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM case_items i WHERE i.case_id=c.case_id) AS items "
        "FROM cases c ORDER BY c.created_at DESC").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 载体：随采集归档
def case_ids_of(conn, tid: int) -> List[str]:
    """这个 tid 属于哪些案件（没有就返回空 → 归档直接 no-op）。"""
    return [r[0] for r in conn.execute(
        "SELECT case_id FROM case_items WHERE tid=?", (int(tid),)).fetchall()]


def archive_page(db, tid: int, page_no: int, url: str, html: str,
                 http_status: int = 200, capture_limit: str = "",
                 run_id: str = "", note: str = "") -> List[Dict[str, Any]]:
    """把**采集器手里刚拿到的页面 HTML** 归档为载体证据（零额外请求）。

    ★ 为什么在采集里做，而不是事后再抓一遍
      用户实测：站方大量历史帖现在**已经打不开**（restricted / 删帖 / 提示页）。
      事后再抓只会得到 404 或"仅作者可见"，那不是证据，是二次失败。
      证据必须在**拿到的那一刻**固定 —— 这里用的就是 detail 采集器刚收到的
      服务器原始响应体，不截图、不重渲染、不二次加工。

    ★ 代价：只有 tid 在案件里才落盘（612 万次调用里绝大多数是一次带索引的
      空查询），不会把 61 万帖都存成 HTML。
    """
    cases = case_ids_of(db.conn, tid)
    if not cases or not html:
        return []
    data = html.encode("utf-8")
    sha = sha256_bytes(data)
    root = evidence_root(db.path)
    out = []
    for case_id in cases:
        pat = "pages/%s/p%s_%%" % (int(tid), int(page_no))
        dup = db.conn.execute(
            "SELECT rel_path FROM evidence_files WHERE case_id=? AND tid=? AND "
            "sha256=? AND rel_path LIKE ?", (case_id, int(tid), sha, pat)).fetchone()
        if dup:
            continue
        rel = "pages/%s/p%s_%s.html" % (int(tid), int(page_no),
                                        time.strftime("%Y%m%dT%H%M%S"))
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
        at = now_str()
        with db.lock:
            db.conn.execute(
                "INSERT OR IGNORE INTO evidence_files(case_id,tid,run_id,kind,rel_path,"
                "bytes,sha256,algo,captured_at,source_url,http_status,capture_limit,note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, int(tid), run_id, "html", rel, len(data), sha, ALGO,
                 at, url, int(http_status), capture_limit, note))
            db.conn.commit()
        Chain(db.conn).append("file.add", "evidence_files",
                              "%s:%s" % (case_id, rel),
                              sha256_bytes(canon({"case_id": case_id, "rel": rel,
                                                  "sha256": sha}).encode("utf-8")))
        out.append({"case_id": case_id, "tid": int(tid), "page": int(page_no),
                    "rel_path": rel, "sha256": sha, "bytes": len(data)})
    return out


def note_missing(db, case_id: str, tid: int, url: str, reason: str,
                 run_id: str = "") -> Dict[str, Any]:
    """拿不到载体时**如实记一条**（不生成任何"看起来像页面"的东西）。"""
    with db.lock:
        db.conn.execute(
            "INSERT INTO evidence_files(case_id,tid,run_id,kind,rel_path,bytes,sha256,"
            "algo,captured_at,source_url,http_status,capture_limit,note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id, int(tid), run_id, "absent", "", 0,
             sha256_bytes(("absent|%s|%s" % (tid, reason)).encode("utf-8")),
             ALGO, now_str(), url, 0, reason, reason))
        db.conn.commit()
    return {"case_id": case_id, "tid": int(tid), "absent": reason}


def record_snapshot(db, tid: int, run_id: str = "") -> Optional[Dict[str, Any]]:
    """互动数据的时间点快照（views/replies）。只在案件条目上写。"""
    if not case_ids_of(db.conn, tid):
        return None
    r = db.conn.execute("SELECT tid,views,replies FROM threads WHERE tid=?",
                        (int(tid),)).fetchone()
    if not r:
        return None
    at = now_str()
    raw = json.dumps({"tid": int(tid), "views": r["views"], "replies": r["replies"]},
                     ensure_ascii=False)
    with db.lock:
        db.conn.execute(
            "INSERT OR REPLACE INTO interaction_snapshots(tid,captured_at,views,"
            "replies,raw,run_id) VALUES(?,?,?,?,?,?)",
            (int(tid), at, r["views"], r["replies"], raw, run_id))
        db.conn.commit()
    return {"tid": int(tid), "at": at, "views": r["views"], "replies": r["replies"]}


def capture_case(db, cfg, case_id: str, pages: int = 3,
                 run_id: str = "") -> Dict[str, Any]:
    """**补采**载体（针对已经在库里的案件条目）。

    ★ 这是补救手段，不是主路径：主路径是采集器边采边归档（archive_page）。
      页面已经打不开时，这里只会写下 `absent` 记录 + exceptions，绝不伪造。
    """
    import platform as _plat
    from .config import THREAD_URL
    from .http_client import Client, HttpError

    run_id = run_id or ("CAP-" + time.strftime("%Y%m%dT%H%M%S"))
    items = _rows(db.conn, "SELECT * FROM case_items WHERE case_id=? ORDER BY tid",
                  (case_id,))
    started = now_str()
    with db.lock:
        db.conn.execute(
            "INSERT INTO capture_runs(run_id,case_id,operator,device,os,browser,ip,"
            "network,tool,tool_ver,started_at,finished_at,clock_source,steps,"
            "exceptions,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, case_id, "", _plat.node(), "%s %s" % (_plat.system(), _plat.release()),
             "requests（无浏览器，取服务器原始 HTML）", "", "", TOOL, _tool_ver(),
             started, "", "local",
             "对案件条目逐页 GET 详情页并归档原始 HTML；页面不可达时记 absent。", "",
             started))
        db.conn.commit()

    client = Client(cfg, verbose=False)
    got, absent, errs = 0, 0, []
    for it in items:
        tid = int(it["tid"])
        total = db.conn.execute(
            "SELECT pages_total FROM threads WHERE tid=?", (tid,)).fetchone()
        n = min(pages, int((total[0] if total else 1) or 1))
        for page_no in range(1, n + 1):
            url = THREAD_URL.format(tid=tid, page=page_no)
            try:
                html = client.get(url, referer=url).text
            except HttpError as e:
                absent += 1
                errs.append("tid=%s p%s：%s" % (tid, page_no, e))
                note_missing(db, case_id, tid, url, "http_error: %s" % e, run_id)
                continue
            rows = archive_page(db, tid, page_no, url, html, run_id=run_id)
            if rows:
                got += len(rows)
            else:
                note_missing(db, case_id, tid, url, "no_change（同内容已归档）", run_id)
        record_snapshot(db, tid, run_id)

    with db.lock:
        db.conn.execute("UPDATE capture_runs SET finished_at=?, exceptions=? "
                        "WHERE run_id=?", (now_str(), "\n".join(errs), run_id))
        db.conn.commit()
    return {"run_id": run_id, "archived": got, "absent": absent,
            "errors": errs[:10], "items": len(items)}


def _rows(conn, sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _bundle_text(title: str, thread: Dict[str, Any],
                 posts: List[Dict[str, Any]]) -> str:
    lines = ["标题：%s" % (title or ""),
             "URL ：%s" % (thread.get("url") or ""),
             "发布：%s（%s）" % (thread.get("author") or "", thread.get("pub_time") or ""),
             "=" * 60, ""]
    for p in posts:
        body = (p.get("content") or "").strip()
        if not body:
            continue
        lines.append("[#%s] %s  %s" % (p.get("floor"), p.get("author") or "",
                                       p.get("post_time") or ""))
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def export_case(db, case_id: str, out_dir: str = "",
                operator: str = "") -> Dict[str, Any]:
    """导出一个案件的证据包。返回 zip 路径 / 哈希 / 链位置。

    ★ 为什么 zip 的哈希放在 **zip 外面**（.sha256.txt）：
      manifest 没法包含自己的容器哈希（先有鸡还是先有蛋）。
      所以：包内 manifest 证明"包内每个文件"，
            包外 .sha256.txt 证明"这个 zip 本身"，
            链侧车证明"这个 zip 是哪一刻进的链"。
    """
    case = get_case(db, case_id)
    items = _rows(db.conn, "SELECT * FROM case_items WHERE case_id=? ORDER BY tid",
                  (case_id,))
    if not items:
        raise ValueError("案件 %s 还没有条目，先 case add 再导出" % case_id)

    root = out_dir or evidence_root(db.path)
    os.makedirs(root, exist_ok=True)
    bundle_id = "%s_%s" % (case_id, time.strftime("%Y%m%dT%H%M%S"))

    # ---- 组包内容 ----
    threads, posts_all, changes, flows, pages = [], [], [], [], []
    snaps, annos = [], []
    for it in items:
        tid = int(it["tid"])
        th = db.conn.execute("SELECT * FROM threads WHERE tid=?", (tid,)).fetchone()
        if th:
            threads.append(dict(th))
        posts_all.extend(_rows(db.conn, "SELECT * FROM posts WHERE tid=? ORDER BY pid", (tid,)))
        changes.extend(_rows(db.conn, "SELECT * FROM list_changes WHERE tid=? ORDER BY ts", (tid,)))
        flows.extend(_rows(db.conn, "SELECT * FROM flows WHERE tid=? ORDER BY step", (tid,)))
        pages.extend(_rows(db.conn, "SELECT * FROM thread_pages WHERE tid=? ORDER BY page_no", (tid,)))
        snaps.extend(_rows(db.conn, "SELECT * FROM interaction_snapshots WHERE tid=?", (tid,)))

    files: Dict[str, Any] = {}
    files["content/threads.json"] = json.dumps(threads, ensure_ascii=False, indent=1).encode("utf-8")
    files["content/posts.json"] = json.dumps(posts_all, ensure_ascii=False, indent=1).encode("utf-8")
    files["content/list_changes.json"] = json.dumps(changes, ensure_ascii=False, indent=1).encode("utf-8")
    files["content/flows.json"] = json.dumps(flows, ensure_ascii=False, indent=1).encode("utf-8")
    files["content/thread_pages.json"] = json.dumps(pages, ensure_ascii=False, indent=1).encode("utf-8")
    files["content/interaction_snapshots.json"] = json.dumps(snaps, ensure_ascii=False, indent=1).encode("utf-8")

    # 研判痕迹（标签 / 人工标注 / AI 初筛 / 核验结论）—— 一并入包，便于复核
    tids = [int(i["tid"]) for i in items]
    q = ",".join("?" * len(tids))
    annos = {
        "thread_tags": _rows(db.conn, "SELECT * FROM thread_tags WHERE tid IN (%s)" % q, tuple(tids)),
        "thread_anno_tags": _rows(db.conn, "SELECT * FROM thread_anno_tags WHERE tid IN (%s)" % q, tuple(tids)),
        "thread_ai_anno": _rows(db.conn, "SELECT * FROM thread_ai_anno WHERE tid IN (%s)" % q, tuple(tids)),
        "sample_review": _rows(db.conn, "SELECT * FROM sample_review WHERE tid IN (%s)" % q, tuple(tids)),
    }
    files["content/annotations.json"] = json.dumps(annos, ensure_ascii=False, indent=1).encode("utf-8")

    # 全文（便于人读；不作为原件，原件是 posts.json）
    for th in threads:
        tid = int(th["tid"])
        ps = [p for p in posts_all if int(p["tid"]) == tid]
        files["content/text/%s.txt" % tid] = _bundle_text(
            th.get("title") or "", th, ps).encode("utf-8")

    # ---- 载体：从 evidence_files 取（采集时已归档的原始 HTML）----
    car_meta: Dict[str, Dict[str, Any]] = {}
    missing = []
    ev_root = evidence_root(db.path)
    for row in _rows(db.conn, "SELECT * FROM evidence_files WHERE case_id=? "
                              "ORDER BY id", (case_id,)):
        rel = row.get("rel_path") or ""
        if (row.get("kind") or "") == "absent" or not rel:
            missing.append({"tid": row.get("tid"), "reason": row.get("note") or "",
                            "url": row.get("source_url") or ""})
            continue
        full = os.path.join(ev_root, rel)
        if not os.path.exists(full):
            missing.append({"tid": row.get("tid"), "reason": "文件不在磁盘（%s）" % rel,
                            "url": row.get("source_url") or ""})
            continue
        with open(full, "rb") as f:
            data = f.read()
        zpath = "carrier/" + rel
        files[zpath] = data
        car_meta[zpath] = row
    if not car_meta:
        files["carrier/README.txt"] = (
            "本包没有网页载体文件。\n"
            "原因见 manifest.json 的 missing_carriers：\n"
            "  · 采集时该帖尚未进入案件（事后页面已打不开，无法补采）\n"
            "  · 站方 restricted（仅作者本人可见）\n"
            "★ 没有 carrier 文件时，请勿对外宣称「已固定网页载体」。\n"
        ).encode("utf-8")

    chain = Chain(db.conn)
    entries = chain.entries()
    files["chain/chain_slice.json"] = json.dumps(entries, ensure_ascii=False, indent=1).encode("utf-8")
    head = entries[-1] if entries else {"seq": 0, "this_hash": GENESIS}
    files["chain/chain_head.txt"] = ("seq=%s\nthis_hash=%s\n" %
                                     (head["seq"], head["this_hash"])).encode("utf-8")

    # ---- manifest ----
    file_entries = []
    for path, data in sorted(files.items()):
        meta = car_meta.get(path) or {}
        file_entries.append({
            "path": path, "bytes": len(data), "sha256": sha256_bytes(data),
            "kind": "carrier" if meta else path.split("/")[0],
            "captured_at": meta.get("captured_at") or now_str(),
            "source_url": meta.get("source_url") or "",
            "http_status": meta.get("http_status"),
            "capture_limit": meta.get("capture_limit") or "",
        })
    _limits = [
        "时间取自采集机本机时钟，未经第三方时间戳锚定（P2 补）",
        "站方 restricted 帖无法取得页面，已按平台限制处理，未伪造",
    ]
    if car_meta:
        _limits.insert(0, "载体为采集时刻的**服务器原始 HTML**（非浏览器渲染截图，"
                          "非 MHTML，未内联图片/CSS）")
    else:
        _limits.insert(0, "本包不含网页载体（采集时未进入案件，事后页面已不可达）")
    manifest = {
        "bundle_id": bundle_id,
        "case_id": case_id,
        "case_title": case.get("title"),
        "created_at": now_str(),
        "clock_source": "local",          # ★ 本机时钟，未做 NTP/TSA 校时（P2 补）
        "tool": TOOL, "tool_ver": _tool_ver(),
        "platform": "bbs.cnnb.com.cn / 群众留言板（fid=1176）",
        "scope": {"threads": len(threads), "posts": len(posts_all),
                  "changes": len(changes), "flows": len(flows)},
        "items": [{"tid": int(i["tid"]), "role": i.get("role"),
                   "added_by": i.get("added_by"), "added_at": i.get("added_at")} for i in items],
        "files": file_entries,
        "carriers": len(car_meta),
        "missing_carriers": missing,
        "chain": {"slice_start": entries[0]["seq"] if entries else 0,
                  "slice_end": head["seq"], "head_hash": head["this_hash"]},
        "limitations": _limits,
        "operator": operator or "",
    }
    man_bytes = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
    files["manifest.json"] = man_bytes
    files["manifest.sha256"] = (sha256_bytes(man_bytes) + "  manifest.json\n").encode("utf-8")

    # ---- 笔录模板 ----
    files["custody.md"] = _custody_md(case, items, operator).encode("utf-8")
    files["README.txt"] = _readme_txt(bundle_id).encode("utf-8")
    files["verify.py"] = _verify_src().encode("utf-8")

    # ---- 打包 ----
    zip_path = os.path.join(root, bundle_id + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in sorted(files.items()):
            z.writestr(path, data)

    zip_sha = sha256_file(zip_path)
    side_sha = os.path.join(root, bundle_id + ".zip.sha256.txt")
    with open(side_sha, "w", encoding="utf-8", newline="\n") as f:
        f.write("%s  %s\n" % (zip_sha, os.path.basename(zip_path)))

    # ---- 入链 + 回写案件 ----
    rec = Chain(db.conn).append(
        "bundle.export", "cases", case_id,
        sha256_bytes(canon({"case_id": case_id, "zip_sha256": zip_sha,
                            "bundle": bundle_id}).encode("utf-8")))
    with db.lock:
        db.conn.execute("UPDATE cases SET bundle_path=?, bundle_sha256=? WHERE case_id=?",
                        (zip_path, zip_sha, case_id))
        db.conn.commit()
    with open(os.path.join(root, bundle_id + ".export.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump({"bundle_id": bundle_id, "case_id": case_id,
                   "zip_sha256": zip_sha, "chain_seq": rec["seq"],
                   "chain_hash": rec["this_hash"], "at": rec["at"]},
                  f, ensure_ascii=False, indent=1)

    return {"bundle_id": bundle_id, "zip": zip_path, "zip_sha256": zip_sha,
            "sha256_file": side_sha, "chain_seq": rec["seq"],
            "chain_hash": rec["this_hash"], "files": len(files),
            "posts": len(posts_all)}


# ------------------------------------------------------------------ 校验
def _verify_zip(path: str) -> Dict[str, Any]:
    """包内校验：manifest 自身 → 每个文件 → 链片段。纯标准库，和包里的 verify.py 同源。"""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        man = json.loads(z.read("manifest.json").decode("utf-8"))
        bad = []
        if "manifest.sha256" in names:
            want = z.read("manifest.sha256").decode("utf-8").split("  ")[0].strip()
            if want != sha256_bytes(z.read("manifest.json")):
                bad.append("manifest.json 与 manifest.sha256 不符")
        for fe in man["files"]:
            if fe["path"] not in names:
                bad.append("缺文件：%s" % fe["path"])
                continue
            data = z.read(fe["path"])
            if len(data) != fe["bytes"]:
                bad.append("大小不符：%s（期望 %s，实际 %s）" % (fe["path"], fe["bytes"], len(data)))
            if sha256_bytes(data) != fe["sha256"]:
                bad.append("哈希不符：%s" % fe["path"])
        # 链片段连续性
        chain_bad = ""
        entries: List[Dict[str, Any]] = []
        if "chain/chain_slice.json" in names:
            entries = json.loads(z.read("chain/chain_slice.json").decode("utf-8"))
            prev = GENESIS
            for e in entries:
                raw = "|".join([str(e["seq"]), e["at"], e["kind"], e["ref_table"],
                                e["ref_id"], e["payload_sha256"], e["prev_hash"]])
                if e["prev_hash"] != prev:
                    chain_bad = "链在 seq=%s 处断开（prev_hash 不符）" % e["seq"]
                    break
                if sha256_bytes(raw.encode("utf-8")) != e["this_hash"]:
                    chain_bad = "链在 seq=%s 处被改过（this_hash 不符）" % e["seq"]
                    break
                prev = e["this_hash"]
        head_txt = z.read("chain/chain_head.txt").decode("utf-8") if "chain/chain_head.txt" in names else ""
        head_ok = ("this_hash=%s" % (entries[-1]["this_hash"] if entries else GENESIS)) in head_txt
    return {"ok": not bad and not chain_bad and head_ok,
            "bad": bad, "chain_bad": chain_bad, "head_ok": head_ok,
            "chain_total": len(entries)}


def verify_bundle(path: str) -> Dict[str, Any]:
    """只凭一个 zip 校验（不连库）：外部哈希 + 包内文件 + 链片段。"""
    if not os.path.exists(path):
        raise FileNotFoundError("没有这个包：%s" % path)
    zip_sha = sha256_file(path)
    side = (path[:-4] + ".zip.sha256.txt") if path.lower().endswith(".zip") else ""
    match_sidecar = None
    if side and os.path.exists(side):
        with open(side, "r", encoding="utf-8") as f:
            match_sidecar = f.read().strip().split("  ")[0] == zip_sha
    inner = _verify_zip(path)
    return {"ok": bool(inner["ok"] and (match_sidecar is not False)),
            "zip_sha256": zip_sha, "match_sidecar": match_sidecar,
            "inner": inner, "zip": path}


def verify_case(db, case_id: str) -> Dict[str, Any]:
    case = get_case(db, case_id)
    zip_path = case.get("bundle_path") or ""
    if not zip_path or not os.path.exists(zip_path):
        raise FileNotFoundError("案件 %s 还没有导出包" % case_id)
    zip_sha = sha256_file(zip_path)
    outside = {"zip_sha256": zip_sha,
               "match_db": zip_sha == (case.get("bundle_sha256") or "")}
    print_sha = os.path.join(os.path.dirname(zip_path),
                             os.path.basename(zip_path)[:-4] + ".zip.sha256.txt")
    outside["match_sidecar"] = True
    if os.path.exists(print_sha):
        with open(print_sha, "r", encoding="utf-8") as f:
            outside["match_sidecar"] = f.read().strip().split("  ")[0] == zip_sha
    inner = _verify_zip(zip_path)
    chain = Chain(db.conn).verify()
    return {"ok": bool(inner["ok"] and outside["match_db"]
                       and outside["match_sidecar"] and chain["ok"]),
            "zip_sha256": zip_sha, "outside": outside, "inner": inner,
            "chain": chain, "zip": zip_path}


# ------------------------------------------------------------------ 包内文本
def _readme_txt(bundle_id: str) -> str:
    return """%s —— 舆情存证证据包
=================================================

【包里有什么】
  manifest.json          每个文件的 SHA256、大小、采集时间、链位置
  manifest.sha256        manifest 自己的哈希
  custody.md             取证笔录（人填部分留空，机器补环境）
  content/               正文证据（站方 API 原始字段，未加工）
    threads.json         帖子元数据（含 tid / url / 作者 / 发布时间 / 阅读 / 回复）
    posts.json           全部楼层（原样，未裁剪 × ★ 关键）
    list_changes.json    标题 / 状态 / 回复数的变更史（站方侧改动痕迹）
    flows.json           站方受理流程时间线
    thread_pages.json    分页结构（每页含哪些 pid，用于校验楼层不丢）
    interaction_snapshots.json  互动数据的时间点快照（P0 可能为空）
    annotations.json     研判痕迹（标签 / 人工标注 / AI 初筛 / 核验结论）
    text/<tid>.txt       可读全文（**不是**原件，原件是 posts.json）
  carrier/README.txt     P0 尚未采集网页载体，这里没有 MHTML/截图/录屏
  chain/                哈希链片段 + 链头
  verify.py             校验脚本（纯 Python 标准库，无需安装依赖）

【怎么验】
  1) 先验这个 zip 本身（哈希在 zip 外面的 .sha256.txt 里）：
       Windows:  certutil -hashfile <zip> SHA256
       Linux  :  sha256sum <zip>
     和 .sha256.txt 里的值比 —— 对不上说明传输过程被改过，直接退回。
  2) 再验包内每个文件：
       python verify.py <zip>
     它会逐个文件重算 SHA256，并校验哈希链片段是否连续。
  3) 输出 OK 表示：包内文件未变、链未断。
     输出 FAILED 会指明是哪个文件、还是链在哪一条断了。

【边界（别过度宣称）】
  · 本包含内容证据，不含网页载体（MHTML/截图/录屏）→ P1 补齐
  · 时间取自采集机本机时钟，未做第三方时间戳锚定 → P2 补齐
  · 哈希链能证明「导出后未被改」，不能证明「导出前未被改」
  · 要对抗"你们自己改的"这类质疑，需要第三方 TSA / 公证

bundle: %s
""" % ("msyt 证据包", bundle_id)


def _custody_md(case: Dict[str, Any], items: List[Dict[str, Any]],
                operator: str) -> str:
    rows = "\n".join("| %s | %s | %s |" % (i["tid"], i.get("role") or "", i.get("note") or "")
                     for i in items)
    return """# 电子数据取证笔录

> 本文件由系统生成框架，**带 ★ 的字段必须由取证人员手工填写并签字**。
> 机器填的内容仅作辅助，不代替人的签署。

## 一、案件
- 案件编号：%s
- 案件标题：%s
- 风险等级：%s
- 责任（经办）人：★ __________
- 取证时间：%s（本机时钟，clock_source=local）
- 取证地点：★ __________

## 二、取证对象
| tid | 角色 | 备注 |
|---|---|---|
%s

## 三、取证环境（★ 手工核对后填写）
- 取证设备（主机名/型号）：★ __________
- 操作系统：★ __________
- 浏览器及版本：★ __________
- 出口公网 IP：★ __________
- 网络环境（家宽 / 4G / 代理 / 隧道）：★ __________
- 取证工具：msyt-evidence（版本见 manifest.json）

## 四、操作过程（★ 逐步写清访问路径与操作）
1. ★ 打开 __________
2. ★ __________

## 五、无法获取的内容及原因（★ 有就写，没有写"无"）
- ★ __________（例：该帖为站方 restricted，仅作者本人可见，页面返回提示页，非我方遗漏）

## 六、校验结果
- zip SHA256：见同名 `.zip.sha256.txt`
- 包内校验：`python verify.py <zip>`
- 哈希链：seq / hash 见 `chain/chain_head.txt`

## 七、签署
- 取证人员签字：★ __________　　日期：★ __________
- 复核人员签字：★ __________　　日期：★ __________
""" % (case.get("case_id"), case.get("title"), case.get("level") or "",
       case.get("created_at") or "", rows)


def _verify_src() -> str:
    """包内 verify.py 的源码（与 _verify_zip 同逻辑，独立可跑）。"""
    return r'''# -*- coding: utf-8 -*-
"""证据包校验：python verify.py <zip 或解压后的目录>"""
import hashlib, json, os, sys, zipfile

GENESIS = "0" * 64

def sha(b):
    return hashlib.sha256(b).hexdigest()

def load(base, name):
    if os.path.isdir(base):
        with open(os.path.join(base, name), "rb") as f:
            return f.read()
    with zipfile.ZipFile(base) as z:
        return z.read(name)

def names(base):
    if os.path.isdir(base):
        out = []
        for root, _, fs in os.walk(base):
            for fn in fs:
                out.append(os.path.relpath(os.path.join(root, fn), base).replace(os.sep, "/"))
        return set(out)
    with zipfile.ZipFile(base) as z:
        return set(z.namelist())

def main(target):
    bad, chain_bad = [], ""
    man = json.loads(load(target, "manifest.json").decode("utf-8"))
    if "manifest.sha256" in names(target):
        want = load(target, "manifest.sha256").decode("utf-8").split("  ")[0].strip()
        if want != sha(load(target, "manifest.json")):
            bad.append("manifest.json 与 manifest.sha256 不符")
    for fe in man["files"]:
        p = fe["path"]
        if p not in names(target):
            bad.append("缺文件：" + p); continue
        data = load(target, p)
        if len(data) != fe["bytes"]:
            bad.append("大小不符：" + p)
        elif sha(data) != fe["sha256"]:
            bad.append("哈希不符：" + p)
    entries, prev = [], GENESIS
    if "chain/chain_slice.json" in names(target):
        entries = json.loads(load(target, "chain/chain_slice.json").decode("utf-8"))
        for e in entries:
            raw = "|".join([str(e["seq"]), e["at"], e["kind"], e["ref_table"],
                            e["ref_id"], e["payload_sha256"], e["prev_hash"]])
            if e["prev_hash"] != prev:
                chain_bad = "链在 seq=%s 处断开" % e["seq"]; break
            if sha(raw.encode("utf-8")) != e["this_hash"]:
                chain_bad = "链在 seq=%s 处被改过" % e["seq"]; break
            prev = e["this_hash"]
    head_ok = True
    if "chain/chain_head.txt" in names(target):
        txt = load(target, "chain/chain_head.txt").decode("utf-8")
        head_ok = ("this_hash=%s" % (entries[-1]["this_hash"] if entries else GENESIS)) in txt
    ok = not bad and not chain_bad and head_ok
    print("bundle : " + man.get("bundle_id", "?"))
    print("files  : %d" % len(man["files"]))
    print("chain  : %d 段" % len(entries))
    if bad:
        for x in bad[:20]:
            print("  [X] " + x)
    if chain_bad:
        print("  [X] " + chain_bad)
    if not head_ok:
        print("  [X] chain_head.txt 与片段末条不一致")
    print("RESULT : " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python verify.py <bundle.zip|dir>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
'''
