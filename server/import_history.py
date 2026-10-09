"""Import past reading history into own-reader: WeRead (微信读书) notebooks / highlights / thoughts.

Optional. Needs a WeRead agent-gateway API key in OWN_READER_WEREAD_KEY_FILE (default ~/.config/weread/api-key.txt);
without it, skip this module — nothing else depends on it having run.
Writes ext_books / ext_notes in reader.sqlite (idempotent upsert by source ids). WeRead raw JSON is cached
per book under <data>/import/weread/ so a rerun resumes instead of refetching.

Run: PYTHONUNBUFFERED=1 python3 server/import_history.py [weread] [match]
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import app  # noqa: E402

GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
KEY_FILE = Path(os.environ.get("OWN_READER_WEREAD_KEY_FILE", "~/.config/weread/api-key.txt")).expanduser()
SKILL_VERSION = "1.0.4"
CACHE = app.DATA_DIR / "import" / "weread"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ext_books(
  source TEXT NOT NULL, source_book_id TEXT NOT NULL, title TEXT, author TEXT, calibre_id INTEGER,
  status TEXT, progress REAL, first_ts TEXT, last_ts TEXT, highlight_count INTEGER, thought_count INTEGER,
  payload TEXT, PRIMARY KEY(source, source_book_id));
CREATE TABLE IF NOT EXISTS ext_notes(
  source TEXT NOT NULL, source_note_id TEXT NOT NULL, source_book_id TEXT NOT NULL, kind TEXT,
  chapter TEXT, quote TEXT, text TEXT, created_ts TEXT, star INTEGER, payload TEXT,
  PRIMARY KEY(source, source_note_id));
CREATE INDEX IF NOT EXISTS ext_notes_book ON ext_notes(source, source_book_id);
"""


def ts(sec) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(sec))) if sec else None


def gw(api_name: str, **params) -> dict:
    body = json.dumps({"api_name": api_name, **params, "skill_version": SKILL_VERSION}).encode()
    req = urllib.request.Request(GATEWAY, data=body, method="POST", headers={
        "Authorization": f"Bearer {KEY_FILE.read_text().strip()}", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            if d.get("upgrade_info"):
                print("WARN upgrade_info:", d["upgrade_info"], flush=True)
            if d.get("errcode") not in (None, 0):
                raise RuntimeError(f"{api_name} errcode {d.get('errcode')} {d.get('errmsg')}")
            return d
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            print(f"  retry {api_name}: {e}", flush=True)
            time.sleep(3 * (attempt + 1))
    raise AssertionError


def fetch_weread() -> list[dict]:
    """Notebook list (all books with notes) + per-book highlights and thoughts, cached per book."""
    CACHE.mkdir(parents=True, exist_ok=True)
    books, last = [], None
    while True:
        d = gw("/user/notebooks", count=100, **({"lastSort": last} if last else {}))
        books += d.get("books", [])
        print(f"notebooks: {len(books)}/{d.get('totalBookCount')}", flush=True)
        if not d.get("hasMore") or not d.get("books"):
            break
        last = d["books"][-1]["sort"]
    (CACHE / "_notebooks.json").write_text(json.dumps(books, ensure_ascii=False))
    for i, b in enumerate(books, 1):
        bid = b["bookId"]
        f = CACHE / f"{bid}.json"
        if f.exists():
            cached = json.loads(f.read_text())
            if cached.get("sort") == b.get("sort") and len(cached["reviews"]) >= (b.get("reviewCount") or 0):
                continue  # unchanged since last fetch and complete
        marks = gw("/book/bookmarklist", bookId=bid)
        # 2026-10-06 实测：count=50 时只回 50 条且 hasMore=0（totalCount=72），synckey 翻页回空；一次取大页
        r = gw("/review/list/mine", bookid=bid, count=1000, synckey=0)
        reviews = r.get("reviews", [])
        if len(reviews) < (r.get("totalCount") or 0):
            print(f"  WARN {bid}: got {len(reviews)} of {r.get('totalCount')} reviews", flush=True)
        f.write_text(json.dumps({"sort": b.get("sort"), "marks": marks, "reviews": reviews}, ensure_ascii=False))
        print(f"[{i}/{len(books)}] {b['book'].get('title')}: {len(marks.get('updated', []))} 划线, {len(reviews)} 想法", flush=True)
        time.sleep(0.4)
    return books


def store_weread(books: list[dict]) -> None:
    with app.db() as c:
        for b in books:
            bid, info = b["bookId"], b.get("book", {})
            raw = json.loads((CACHE / f"{bid}.json").read_text())
            chapters = {ch["chapterUid"]: ch.get("title") for ch in raw["marks"].get("chapters", [])}
            notes = []
            for m in raw["marks"].get("updated", []):
                notes.append((f"hl:{m['bookmarkId']}", "highlight", chapters.get(m.get("chapterUid")),
                              m.get("markText"), None, ts(m.get("createTime")), None,
                              {"range": m.get("range"), "chapterUid": m.get("chapterUid")}))
            for r in raw["reviews"]:
                rv = r.get("review", r)
                kind = "thought" if rv.get("abstract") else "review"
                notes.append((f"rv:{rv['reviewId']}", kind, rv.get("chapterName") or chapters.get(rv.get("chapterUid")),
                              rv.get("abstract"), rv.get("content"), ts(rv.get("createTime")), rv.get("star"),
                              {"range": rv.get("range"), "chapterUid": rv.get("chapterUid"), "isFinish": rv.get("isFinish")}))
            times = sorted(n[5] for n in notes if n[5])
            status = "finished" if b.get("markedStatus") == 1 else "reading"
            c.execute("""INSERT INTO ext_books(source,source_book_id,title,author,status,progress,first_ts,last_ts,
                         highlight_count,thought_count,payload) VALUES('weread',?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(source,source_book_id) DO UPDATE SET title=excluded.title, author=excluded.author,
                         status=excluded.status, progress=excluded.progress, first_ts=excluded.first_ts,
                         last_ts=excluded.last_ts, highlight_count=excluded.highlight_count,
                         thought_count=excluded.thought_count, payload=excluded.payload""",
                      (bid, info.get("title"), info.get("author"), status, b.get("readingProgress"),
                       times[0] if times else None, times[-1] if times else ts(b.get("sort")),
                       sum(n[1] == "highlight" for n in notes), sum(n[1] != "highlight" for n in notes),
                       json.dumps({"cover": info.get("cover"), "deepLink": info.get("deepLink"),
                                   "bookmarkCount": b.get("bookmarkCount")}, ensure_ascii=False)))
            for nid, kind, chapter, quote, text, created, star, payload in notes:
                c.execute("""INSERT INTO ext_notes(source,source_note_id,source_book_id,kind,chapter,quote,text,
                             created_ts,star,payload) VALUES('weread',?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(source,source_note_id) DO UPDATE SET kind=excluded.kind, chapter=excluded.chapter,
                             quote=excluded.quote, text=excluded.text, created_ts=excluded.created_ts,
                             star=excluded.star, payload=excluded.payload""",
                          (nid, bid, kind, chapter, quote, text, created, star, json.dumps(payload, ensure_ascii=False)))
    print(f"stored weread: {len(books)} books", flush=True)


def norm(s: str) -> str:
    s = re.sub(r"[（(\[【].*?[)）\]】]", "", s or "")
    s = re.split(r"[:：—\-–|/]| - ", s)[0]
    return re.sub(r"[\s·・,，.。'\"“”‘’!！?？]", "", s).lower()


def match_calibre() -> None:
    """Fill ext_books.calibre_id by normalized title (+ author check when titles collide)."""
    with app.calibre() as c:
        rows = c.execute(app.BOOK_SQL).fetchall()
    by_title: dict[str, list] = {}
    for r in rows:
        if r["epub"]:
            by_title.setdefault(norm(r["title"]), []).append(r)
    with app.db() as c:
        todo = c.execute("SELECT source, source_book_id, title, author FROM ext_books WHERE calibre_id IS NULL").fetchall()
        hit = 0
        for t in todo:
            title = t["title"] or ""
            if t["source"] == "weread" and " - " in title:  # uploaded EPUBs are named "Title - Author"
                title = title.rsplit(" - ", 1)[0]
            cands = by_title.get(norm(title), []) if norm(title) else []
            if len(cands) > 1 and t["author"]:
                a = norm(t["author"])[:4]
                cands = [r for r in cands if a and a in norm(r["authors"] or "")] or cands
            if len(cands) == 1:
                c.execute("UPDATE ext_books SET calibre_id=? WHERE source=? AND source_book_id=?",
                          (cands[0]["id"], t["source"], t["source_book_id"]))
                hit += 1
    print(f"calibre match: {hit}/{len(todo)} newly matched", flush=True)


def main(argv: list[str]) -> None:
    steps = set(argv) or {"weread", "match"}
    with app.db() as c:
        c.executescript(SCHEMA)
    if "weread" in steps:
        store_weread(fetch_weread())
    if "match" in steps:
        match_calibre()


if __name__ == "__main__":
    main(sys.argv[1:])
