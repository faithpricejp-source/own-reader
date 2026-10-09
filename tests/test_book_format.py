"""服务端格式选择逻辑测试：EPUB 优先、否则 PDF（tests/test_book_format.py）。

运行：cd project && /opt/homebrew/bin/python3 -m pytest tests/test_book_format.py -v
纯标准库 + pytest，不联网；用临时目录搭一个最小假 Calibre 库。
"""
import os
import sqlite3
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TMP = Path(tempfile.mkdtemp(prefix="own-reader-fmt-"))
CAL = TMP / "calibre"

# id: (title, 格式列表)
BOOKS = {
    101: ("Epub Only", ["epub"]),
    102: ("Pdf Only", ["pdf"]),
    103: ("Both Formats", ["epub", "pdf"]),
    104: ("Neither", ["jpeg"]),
}


def build_fake_calibre() -> None:
    for bid, (title, fmts) in BOOKS.items():
        d = CAL / f"{title} ({bid})"
        d.mkdir(parents=True, exist_ok=True)
        for fmt in fmts:
            ext = "jpg" if fmt == "jpeg" else fmt
            body = (b"PK\x03\x04fake-epub" if fmt == "epub"
                    else b"%PDF-1.4 fake" if fmt == "pdf" else b"\xff\xd8jpeg")
            (d / f"{bid}.{ext}").write_bytes(body)
    c = sqlite3.connect(CAL / "metadata.db")
    c.executescript("""
    CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, path TEXT, timestamp TEXT, has_cover INTEGER DEFAULT 0);
    CREATE TABLE data(id INTEGER PRIMARY KEY AUTOINCREMENT, book INTEGER, format TEXT, name TEXT, content TEXT);
    CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
    CREATE TABLE books_authors_link(id INTEGER PRIMARY KEY AUTOINCREMENT, book INTEGER, author INTEGER);
    """)
    for bid, (title, fmts) in BOOKS.items():
        c.execute("INSERT INTO books(id,title,path,timestamp) VALUES(?,?,?,?)",
                  (bid, title, f"{title} ({bid})", "2026-10-01T00:00:00+00:00"))
        c.execute("INSERT INTO authors(id,name,sort) VALUES(?,?,?)", (bid, "A", "A"))
        c.execute("INSERT INTO books_authors_link(book,author) VALUES(?,?)", (bid, bid))
        for fmt in fmts:
            if fmt == "jpeg":
                continue
            c.execute("INSERT INTO data(book,format,name) VALUES(?,?,?)",
                      (bid, fmt.upper(), str(bid)))
    c.commit()
    c.close()


build_fake_calibre()
os.environ["OWN_READER_CALIBRE"] = str(CAL)
os.environ["OWN_READER_DATA"] = str(TMP / "data")
sys.path.insert(0, str(ROOT / "server"))
import app  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _own_calibre(monkeypatch):
    # conftest 先 import 了 app 并把 CALIBRE_ROOT 指向它自己的空库；本文件用自己的假库
    monkeypatch.setattr(app, "CALIBRE_ROOT", CAL)
    app.init_db()  # conftest 每个用例删掉事件库，/state 需要表在


def row(bid: int):
    with app.calibre() as c:
        return c.execute(app.BOOK_SQL + " WHERE b.id=?", (bid,)).fetchone()


def test_pick_format_epub_preferred():
    assert app.pick_format(row(101)) == "epub"
    assert app.pick_format(row(103)) == "epub"  # EPUB 优先


def test_pick_format_pdf_fallback():
    assert app.pick_format(row(102)) == "pdf"


def test_pick_format_none():
    assert app.pick_format(row(104)) is None
    assert app.book_format(104) is None
    assert app.book_file(104) is None


def test_book_file_returns_path_and_format():
    f, fmt = app.book_file(102)
    assert fmt == "pdf" and f.name == "102.pdf" and f.exists()
    f, fmt = app.book_file(103)
    assert fmt == "epub" and f.name == "103.epub"


def test_book_row_flags_both_formats():
    r = app.book_row(row(102))
    assert r["pdf"] and not r["epub"]


def test_list_books_marks_pdf_books_openable():
    books = {b["id"]: b for b in app.list_books("")}
    assert books[102]["pdf"] is True
    # 首页「最近入库」过滤逻辑：EPUB 或 PDF 都要能上榜
    visible = [b for b in app.list_books("", 150) if b["epub"] or b["pdf"]]
    assert {b["id"] for b in visible} >= {101, 102, 103}
    assert 104 not in {b["id"] for b in visible}


# ---- HTTP 层：file 的 Content-Type、state 的 format 字段 ----

_srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
_port = _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
app.init_db()


def get(path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{_port}{path}") as r:
        return r.headers.get("Content-Type", ""), r.read()


def test_http_file_content_type_pdf():
    ctype, body = get("/api/books/102/file")
    assert ctype == "application/pdf"
    assert body.startswith(b"%PDF")


def test_http_file_content_type_epub_preferred():
    ctype, body = get("/api/books/103/file")
    assert ctype == "application/epub+zip"
    assert body.startswith(b"PK\x03\x04")


def test_http_state_reports_format():
    import json
    ctype, body = get("/api/books/102/state")
    assert json.loads(body)["format"] == "pdf"
    _, body = get("/api/books/103/state")
    assert json.loads(body)["format"] == "epub"
