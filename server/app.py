"""own-reader server: Calibre bookshelf + event log + AI Q&A (http.server + sqlite3).

Run: python3 server/app.py   (from the repo root; config via env vars or config.conf, see config.example.conf)
Listens on 127.0.0.1 only (OWN_READER_PORT, default 8460). To reach it from other devices, put your own
reverse proxy / VPN in front (e.g. `tailscale serve`); there is no login, it is single-user by design.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402,F401  — loads config.conf into the environment before anything reads it
import llm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CALIBRE_ROOT = Path(os.environ.get("OWN_READER_CALIBRE", "~/Calibre Library")).expanduser()
DATA_DIR = Path(os.environ.get("OWN_READER_DATA",
                               "~/Library/Application Support/own-reader")).expanduser()
DB_PATH = DATA_DIR / "reader.sqlite"
PORT = int(os.environ.get("OWN_READER_PORT", "8460"))
STATIC = {"/web/": ROOT / "web", "/vendor/": ROOT / "vendor"}
EVENT_TYPES = {"open", "progress", "highlight", "note", "ask", "feedback", "delete",
               "rec_feedback", "rec_comment", "rec_memo_edit", "book_feedback", "search"}

SYSTEM_PROMPT = """你是用户的伴读助手。用户在读一本书时选中了一段文字并提问。
回答规则：
1. 先看给你的书中上下文（选段前后的原文）：如果书里本身就回答了或部分回答了这个问题，先指出来，并原样引用那几句。
2. 再用书外知识补充。书外知识要和书内原文分开写，并标明哪些是公认史实、哪些是你的推断。
3. 如果用户的问题里有和原文不符的前提，直接指出。
4. 用中文回答，简洁，先给结论。不要客套。"""

_db_lock = threading.Lock()


class _Conn(sqlite3.Connection):
    """`with db() as c:` 退出时提交并关闭。原生 Connection 只提交不关，连接靠 GC 回收，
    10-07 服务器攒到 248 个未关连接撞 launchd 256 文件上限，全部请求失败。"""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, factory=_Conn)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            device TEXT, book_id INTEGER, type TEXT NOT NULL,
            cfi TEXT, text TEXT, payload TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ev_book ON events(book_id, type)")


def add_event(device, book_id, type_, cfi=None, text=None, payload=None) -> int:
    if type_ not in EVENT_TYPES:
        raise ValueError(f"bad event type {type_}")
    with _db_lock, db() as c:
        cur = c.execute("INSERT INTO events(device,book_id,type,cfi,text,payload) VALUES(?,?,?,?,?,?)",
                        (device, book_id, type_, cfi, text,
                         json.dumps(payload, ensure_ascii=False) if payload is not None else None))
        return cur.lastrowid


def recent_searches(tab: str, limit: int = 8) -> list[str]:
    """该标签页最近搜过的词，去重、新的在前；跨设备共用（存在事件库里）。"""
    with db() as c:
        rows = c.execute("""SELECT text, MAX(id) AS last FROM events
                            WHERE type='search' AND json_extract(payload,'$.tab')=? AND text<>''
                            GROUP BY text ORDER BY last DESC LIMIT ?""", (tab, limit)).fetchall()
    return [r["text"] for r in rows]


def calibre() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{CALIBRE_ROOT / 'metadata.db'}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


BOOK_SQL = """SELECT b.id, b.title, b.path, b.timestamp,
  (SELECT group_concat(a.name, ' & ') FROM books_authors_link l JOIN authors a ON a.id=l.author
     WHERE l.book=b.id) AS authors,
  (SELECT d.name FROM data d WHERE d.book=b.id AND d.format='EPUB') AS epub,
  (SELECT d.name FROM data d WHERE d.book=b.id AND d.format='PDF') AS pdf
FROM books b"""


def book_row(r) -> dict:
    return {"id": r["id"], "title": r["title"], "authors": r["authors"] or "",
            "epub": bool(r["epub"]), "pdf": bool(r["pdf"])}


def pick_format(r) -> str | None:
    """EPUB 优先，否则 PDF；都读不了返回 None。"""
    if r["epub"]:
        return "epub"
    if r["pdf"]:
        return "pdf"
    return None


def list_books(q: str, limit: int = 50) -> list[dict]:
    with calibre() as c:
        if q:
            like = f"%{q}%"
            rows = c.execute(BOOK_SQL + """ WHERE b.title LIKE ? OR b.id IN (
                SELECT l.book FROM books_authors_link l JOIN authors a ON a.id=l.author WHERE a.name LIKE ?)
                ORDER BY b.timestamp DESC LIMIT ?""", (like, like, limit)).fetchall()
        else:
            rows = c.execute(BOOK_SQL + " ORDER BY b.timestamp DESC LIMIT ?", (limit,)).fetchall()
    return [book_row(r) for r in rows]


def book_cover(book_id: int) -> Path | None:
    with calibre() as c:
        r = c.execute("SELECT path, has_cover FROM books WHERE id=?", (book_id,)).fetchone()
    return CALIBRE_ROOT / r["path"] / "cover.jpg" if r and r["has_cover"] else None


def book_file(book_id: int) -> tuple[Path, str] | None:
    """返回 (文件路径, 格式)；EPUB 优先，否则 PDF。"""
    with calibre() as c:
        r = c.execute(BOOK_SQL + " WHERE b.id=?", (book_id,)).fetchone()
    if not r:
        return None
    fmt = pick_format(r)
    if not fmt:
        return None
    return CALIBRE_ROOT / r["path"] / f"{r[fmt]}.{fmt}", fmt


def book_format(book_id: int) -> str | None:
    with calibre() as c:
        r = c.execute(BOOK_SQL + " WHERE b.id=?", (book_id,)).fetchone()
    return pick_format(r) if r else None


def book_state(book_id: int) -> dict:
    with db() as c:
        rows = c.execute("SELECT * FROM events WHERE book_id=? ORDER BY id", (book_id,)).fetchall()
    # bugfix-1007-B1 / Kimi-B-2: delete 事件的 payload 可能是 NULL/坏 JSON/缺 target_id（手工 POST、旧客户端写入），
    # 坏行直接跳过，否则本书 /state 接口从此永久 500
    deleted = set()
    for r in rows:
        if r["type"] != "delete" or not r["payload"]:
            continue
        try:
            p = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        if isinstance(p, dict) and "target_id" in p:
            deleted.add(p["target_id"])
    # 两种客户端的位置互不覆盖（2026-10-07 安卓原生版）：网页版用 EPUB CFI（cfi 列），
    # 安卓 crengine 版 cfi 为空、位置放 payload.pos（xpointer）且 pos_kind="crengine"。
    # 各自取最新一条；对方的位置读不懂时用 fraction 跳转（比较 ts 决定谁更新）。
    progress = None
    progress_xp = None
    highlights: dict[int, dict] = {}
    asks = []
    for r in rows:
        p = json.loads(r["payload"]) if r["payload"] else {}
        if r["id"] in deleted:
            continue
        if r["type"] == "progress":
            if p.get("pos_kind") == "crengine":
                progress_xp = {**p, "ts": r["ts"]}
            elif r["cfi"]:
                progress = {"cfi": r["cfi"], **p, "ts": r["ts"]}
        elif r["type"] == "highlight":
            highlights[r["id"]] = {"id": r["id"], "cfi": r["cfi"], "text": r["text"],
                                   "color": p.get("color", "yellow"), "note": None, "ts": r["ts"],
                                   "pos": p.get("pos"), "pos_end": p.get("pos_end"), "pos_kind": p.get("pos_kind"),
                                   "chapter": p.get("chapter")}
        elif r["type"] == "note":
            h = highlights.get(p.get("highlight_id"))
            if h:
                h["note"] = r["text"]
        elif r["type"] == "ask":
            asks.append({"id": r["id"], "cfi": r["cfi"], "selection": r["text"],
                         "question": p.get("question"), "answer": p.get("answer"),
                         "model": p.get("model"), "ts": r["ts"],
                         "pos": p.get("pos"), "pos_end": p.get("pos_end"), "pos_kind": p.get("pos_kind"),
                         "chapter": p.get("chapter")})
    return {"progress": progress, "progress_xp": progress_xp, "highlights": list(highlights.values()), "asks": asks}


def _has_ext(c) -> bool:
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='ext_books'").fetchone())


def history() -> list[dict]:
    """Books I've read anywhere (imported WeRead history, this reader), merged by Calibre id when known."""
    merged: dict[str, dict] = {}
    with db() as c:
        ext = c.execute("SELECT * FROM ext_books").fetchall() if _has_ext(c) else []
        own = c.execute("""SELECT book_id, min(ts) first_ts, max(ts) last_ts,
            sum(type='highlight') hl, sum(type IN ('note','ask')) th FROM events
            WHERE book_id IS NOT NULL AND device != 'test-cli' GROUP BY book_id""").fetchall()
        fractions = {r[0]: r[1] for r in c.execute(
            "SELECT book_id, json_extract(payload,'$.fraction') FROM events WHERE id IN "
            "(SELECT max(id) FROM events WHERE type='progress' GROUP BY book_id)")}
    def slot(key, **init):
        return merged.setdefault(key, {"key": key, "sources": [], "highlights": 0, "thoughts": 0,
                                       "first_ts": None, "last_ts": None, "status": None, **init})
    for r in ext:
        key = f"c{r['calibre_id']}" if r["calibre_id"] else f"{r['source']}:{r['source_book_id']}"
        h = slot(key, title=r["title"], author=r["author"], calibre_id=r["calibre_id"])
        h["sources"].append({"source": r["source"], "id": r["source_book_id"], "status": r["status"],
                             "progress": r["progress"]})
        h["highlights"] += r["highlight_count"] or 0
        h["thoughts"] += r["thought_count"] or 0
        if r["status"] == "finished":
            h["status"] = "finished"
        for k in ("first_ts", "last_ts"):
            v = r[k]
            if v and (not h[k] or (v < h[k] if k == "first_ts" else v > h[k])):
                h[k] = v
    for r in own:
        h = slot(f"c{r['book_id']}", title=None, author=None, calibre_id=r["book_id"])
        h["sources"].append({"source": "own-reader", "progress": fractions.get(r["book_id"])})
        h["highlights"] += r["hl"] or 0
        h["thoughts"] += r["th"] or 0
        h["first_ts"] = min(filter(None, [h["first_ts"], r["first_ts"]]))
        h["last_ts"] = max(filter(None, [h["last_ts"], r["last_ts"]]))
    ids = [h["calibre_id"] for h in merged.values() if h["calibre_id"]]
    if ids:
        with calibre() as c:
            cal = {r["id"]: r for r in c.execute(BOOK_SQL + f" WHERE b.id IN ({','.join('?' * len(ids))})", ids)}
        for h in merged.values():
            r = cal.get(h["calibre_id"])
            if r:
                h["title"] = h["title"] or r["title"]
                h["author"] = h["author"] or r["authors"]
                h["epub"] = bool(r["epub"])
                h["pdf"] = bool(r["pdf"])
    return sorted(merged.values(), key=lambda h: h["last_ts"] or "", reverse=True)


def ext_notes(calibre_id: int | None = None, source: str | None = None, source_book_id: str | None = None,
              q: str | None = None, limit: int = 500) -> list[dict]:
    with db() as c:
        if not _has_ext(c):
            return []
        sql = """SELECT n.*, b.title, b.author, b.calibre_id FROM ext_notes n
                 JOIN ext_books b ON b.source=n.source AND b.source_book_id=n.source_book_id WHERE 1=1"""
        args: list = []
        if calibre_id is not None:
            sql += " AND b.calibre_id=?"
            args.append(calibre_id)
        if source and source_book_id:
            sql += " AND n.source=? AND n.source_book_id=?"
            args += [source, source_book_id]
        if q:
            sql += " AND (n.quote LIKE ? OR n.text LIKE ? OR b.title LIKE ?)"
            args += [f"%{q}%"] * 3
        sql += " ORDER BY n.created_ts DESC LIMIT ?"
        args.append(limit)
        return [{k: r[k] for k in ("source", "kind", "chapter", "quote", "text", "created_ts", "star",
                                   "title", "author", "calibre_id", "source_book_id")}
                for r in c.execute(sql, args)]


FONT_DIRS = [DATA_DIR / "fonts", Path("~/Library/Fonts").expanduser()]
_fonts_cache: list[dict] | None = None
_fonts_sig: tuple = ()


def list_fonts() -> list[dict]:
    """Font families from FONT_DIRS (ttf/otf only; browsers can't load .ttc), tagged zh if they cover CJK."""
    global _fonts_cache, _fonts_sig
    sig = tuple(d.stat().st_mtime if d.is_dir() else 0 for d in FONT_DIRS)
    if _fonts_cache is not None and sig == _fonts_sig:
        return _fonts_cache
    _fonts_sig = sig
    from fontTools.ttLib import TTFont
    fams: dict[str, dict] = {}
    for d in FONT_DIRS:
        for f in sorted(d.glob("*")) if d.is_dir() else []:
            if f.suffix.lower() not in (".ttf", ".otf") or "[" in f.name:
                continue
            try:
                t = TTFont(f, lazy=True)
                name = t["name"]
                fam = str(name.getDebugName(16) or name.getDebugName(1))
                weight = t["OS/2"].usWeightClass if "OS/2" in t else 400
                italic = bool(t["OS/2"].fsSelection & 1) if "OS/2" in t else False
                cmap = t.getBestCmap() or {}
                zh = 0x4E2D in cmap and 0x6587 in cmap
                mono = bool(t["post"].isFixedPitch) if "post" in t else False
                t.close()
            except Exception:  # noqa: BLE001 — skip unreadable fonts
                continue
            if mono:
                continue
            fam_d = fams.setdefault(fam, {"family": fam, "zh": zh, "files": []})
            fam_d["files"].append({"url": f"/fonts/{d.name}/{f.name}", "weight": weight, "italic": italic,
                                   "size": f.stat().st_size})
    _fonts_cache = sorted(fams.values(), key=lambda x: (not x["zh"], x["family"]))
    return _fonts_cache


def font_file(dirname: str, fname: str) -> Path | None:
    for d in FONT_DIRS:
        if d.name == dirname:
            f = (d / fname).resolve()
            if f.parent == d.resolve() and f.is_file():
                return f
    return None


_rec_job: dict = {"thread": None, "kind": None, "error": None}


def _start_rec_job(kind: str) -> bool:
    import recommend
    t = _rec_job["thread"]
    if t and t.is_alive():
        return False
    def run():
        try:
            _rec_job["error"] = None
            recommend.generate() if kind == "generate" else recommend.distill()
        except Exception as e:  # noqa: BLE001
            _rec_job["error"] = f"{kind}: {e}"
            sys.stderr.write(f"rec job failed: {e}\n")
    _rec_job.update(kind=kind, thread=threading.Thread(target=run, daemon=True))
    _rec_job["thread"].start()
    return True


def recs_state() -> dict:
    import recommend
    with recommend._db() as c:
        batch = c.execute("SELECT * FROM rec_batches WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
        n_batches = c.execute("SELECT count(*) FROM rec_batches WHERE status='done'").fetchone()[0]
        recs = c.execute("SELECT * FROM recs WHERE batch_id=? ORDER BY pos", (batch["id"],)).fetchall() if batch else []
        fb = {}
        for r in c.execute("""SELECT json_extract(payload,'$.rec_id') rid, json_extract(payload,'$.verdict') v,
                json_extract(payload,'$.comment') cm FROM events WHERE type='rec_feedback' ORDER BY id"""):
            fb[r["rid"]] = {"verdict": r["v"], "comment": r["cm"]}
    t = _rec_job["thread"]
    return {"batch": dict(batch) if batch else None, "batches": n_batches,
            "recs": [{**dict(r), "feedback": fb.get(r["id"])} for r in recs],
            "running": _rec_job["kind"] if t and t.is_alive() else None, "error": _rec_job["error"],
            "memo": recommend.MEMO.read_text() if recommend.MEMO.exists() else ""}


def build_messages(req: dict, title: str) -> list[dict]:
    before = (req.get("context_before") or "")[-6000:]
    after = (req.get("context_after") or "")[:6000]
    extra = _related_passages(req, before + after)
    extra_block = f"{extra}\n\n" if extra else ""
    user = (f"书名：《{title}》\n章节：{req.get('chapter') or '未知'}\n\n"
            f"【选段之前的原文】\n{before}\n\n【用户选中的文字】\n{req.get('selection','')}\n\n"
            f"【选段之后的原文】\n{after}\n\n"
            f"{extra_block}"
            f"【用户的问题】\n{req.get('question') or '请解释这段话。'}")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def _related_passages(req: dict, context: str) -> str:
    """用「选中文字 + 问题」检索书中其他相关段落；任何失败都静默跳过，不影响提问。"""
    try:
        import book_text
        bid = req.get("book_id")
        f = book_file(int(bid)) if bid else None
        if not f or f[1] != "epub":  # book_file 返回 (路径, 格式)；PDF 暂不做书内检索
            return ""
        epub = f[0]
        query = f"{req.get('selection') or ''} {req.get('question') or ''}"
        return book_text.relevant_passages(int(bid), epub, query, context, data_dir=DATA_DIR)
    except Exception:  # noqa: BLE001
        return ""


def book_title(book_id: int) -> str:
    with calibre() as c:
        r = c.execute("SELECT title FROM books WHERE id=?", (book_id,)).fetchone()
    return r["title"] if r else "未知"


class Handler(BaseHTTPRequestHandler):
    server_version = "own-reader/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{time.strftime('%H:%M:%S')} {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        extra = extra or {}
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-cache")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self._static(ROOT / "web" / "index.html")
            if path == "/read":
                return self._static(ROOT / "web" / "reader.html")
            for prefix, base in STATIC.items():
                if path.startswith(prefix):
                    target = (base / path[len(prefix):]).resolve()
                    if base.resolve() not in target.parents:
                        return self._json({"error": "forbidden"}, 403)
                    return self._static(target)
            if path == "/api/fonts":
                return self._json({"fonts": list_fonts()})
            m = re.fullmatch(r"/fonts/([^/]+)/([^/]+)", path)
            if m:
                from urllib.parse import unquote
                f = font_file(unquote(m.group(1)), unquote(m.group(2)))
                if not f:
                    return self._json({"error": "no font"}, 404)
                ctype = "font/otf" if f.suffix.lower() == ".otf" else "font/ttf"
                return self._send(200, f.read_bytes(), ctype, {"Cache-Control": "max-age=31536000"})
            if path == "/api/recs":
                return self._json(recs_state())
            if path == "/api/searches":
                return self._json({"queries": recent_searches((qs.get("tab") or ["shelf"])[0])})
            if path == "/api/history":
                return self._json({"books": history()})
            if path == "/api/notes":
                one = lambda k: (qs.get(k) or [None])[0]  # noqa: E731
                cid = one("calibre_id")
                return self._json({"notes": ext_notes(int(cid) if cid else None, one("source"), one("book"),
                                                      (one("q") or "").strip() or None)})
            if path == "/api/books":
                q = (qs.get("q") or [""])[0].strip()
                # 不带搜索词时是「最近入库」：只列有 EPUB 或 PDF 的，打不开的书不占首页
                return self._json({"books": list_books(q) if q else
                                   [b for b in list_books("", 150) if b["epub"] or b["pdf"]][:50]})
            if path == "/api/library/cats":
                import library_class
                return self._json({"cats": library_class.categories()})
            if path == "/api/library":
                import library_class
                one = lambda k, d=None: (qs.get(k) or [d])[0]  # noqa: E731
                return self._json(library_class.list_cat(one("primary", ""), one("secondary"), one("sort", "both"),
                                                         int(one("offset", "0"))))
            if path == "/api/reading":
                import reading_now
                reading_now.refresh_if_stale()
                return self._json({"books": reading_now.list_now()})
            m = re.fullmatch(r"/api/books/(\d+)/bilingual(/status)?", path)
            if m:
                # 安卓原生版的对照翻译：整本译文插进 EPUB（2026-10-07）
                import bilingual
                f = book_file(int(m.group(1)))
                if not f or f[1] != "epub" or not f[0].exists():
                    return self._json({"error": "only EPUB books can be translated"}, 404)
                if m.group(2):
                    return self._json(bilingual.status(int(m.group(1)), f[0]))
                out = bilingual.build(int(m.group(1)), f[0])
                return self._send(200, out.read_bytes(), "application/epub+zip")
            m = re.fullmatch(r"/api/books/(\d+)/(file|state|cover)", path)
            if m:
                bid = int(m.group(1))
                if m.group(2) == "cover":
                    f = book_cover(bid)
                    if not f or not f.exists():
                        return self._json({"error": "no cover"}, 404)
                    return self._send(200, f.read_bytes(), "image/jpeg", {"Cache-Control": "max-age=604800"})
                if m.group(2) == "state":
                    return self._json({"title": book_title(bid), "format": book_format(bid), **book_state(bid),
                                       "imported": ext_notes(calibre_id=bid)})
                f = book_file(bid)
                if not f or not f[0].exists():
                    return self._json({"error": "no readable file"}, 404)
                ctype = "application/epub+zip" if f[1] == "epub" else "application/pdf"
                return self._send(200, f[0].read_bytes(), ctype)
            self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            req = self._body()
            if path == "/api/events":
                eid = add_event(req.get("device"), req.get("book_id"), req["type"], req.get("cfi"),
                                req.get("text"), req.get("payload"))
                return self._json({"id": eid})
            if path in ("/api/recs/generate", "/api/recs/distill"):
                started = _start_rec_job(path.rsplit("/", 1)[1])
                return self._json({"started": started, **recs_state()})
            if path == "/api/recs/memo":
                import recommend
                text = str(req.get("text") or "")
                recommend.MEMO.write_text(text)
                add_event(req.get("device"), None, "rec_memo_edit", text=text)
                return self._json({"ok": True})
            if path == "/api/translate":
                import translate
                return self._json(translate.request(req.get("paras") or []))
            if path == "/api/translate/get":
                import translate
                return self._json(translate.get(req.get("hashes") or []))
            if path == "/api/ask":
                bid = int(req["book_id"])
                # 不指定后端时用 OWN_READER_DEFAULT_BACKEND；默认后端失败且配置了 OWN_READER_FALLBACK_BACKEND 时自动改用它
                backend = req.get("backend") or llm.DEFAULT_BACKEND
                messages = build_messages(req, book_title(bid))
                fallback_note = None
                try:
                    answer, model, ms = llm.ask(backend, messages, sensitivity="personal", caller="reader.ask")
                except Exception as e:  # noqa: BLE001
                    fb = llm.FALLBACK_BACKEND
                    if backend != llm.DEFAULT_BACKEND or not fb or fb == backend:
                        return self._json({"error": f"{backend} 失败：{e}"}, 502)
                    try:
                        answer, model, ms = llm.ask(fb, messages, sensitivity="personal", caller="reader.ask.fallback")
                        backend, fallback_note = fb, f"{backend} 失败（{str(e)[:120]}），改用 {fb}"
                    except Exception as e2:  # noqa: BLE001
                        return self._json({"error": f"{backend} 失败：{e}；{fb} 也失败：{e2}"}, 502)
                eid = add_event(req.get("device"), bid, "ask", req.get("cfi"), req.get("selection"),
                                {"question": req.get("question"), "answer": answer, "backend": backend,
                                 "model": model, "latency_ms": ms, "chapter": req.get("chapter"),
                                 "context_chars": len(messages[1]["content"]),
                                 "upgrade_of": req.get("upgrade_of"),
                                 **{k: req[k] for k in ("pos", "pos_end", "pos_kind", "client_ts") if req.get(k)}})
                return self._json({"id": eid, "answer": answer, "model": model, "latency_ms": ms,
                                   "backend": backend, "note": fallback_note})
            self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def _static(self, f: Path):
        if not f.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.suffix in (".js", ".mjs"):
            ctype = "text/javascript"
        self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))


def main():
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"own-reader on http://127.0.0.1:{PORT}  db={DB_PATH}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
