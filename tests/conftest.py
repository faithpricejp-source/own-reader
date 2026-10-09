"""Shared fixtures for own-reader tests.

Everything is offline and confined to a temp dir:
- OWN_READER_DATA / OWN_READER_CALIBRE are set before `app` is imported, so every
  module picks up temp paths (app resolves them at import time).
- A minimal Calibre metadata.db is built from scratch in the temp dir.
- llm.AUDIT_DB is monkeypatched per test.
- OWN_READER_CONFIG points at a non-existent file so a local config.conf never leaks into tests.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="own-reader-tests-"))
DATA_DIR = _TMP_ROOT / "data"
CALIBRE_DIR = _TMP_ROOT / "calibre"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CALIBRE_DIR.mkdir(parents=True, exist_ok=True)

os.environ["OWN_READER_DATA"] = str(DATA_DIR)
os.environ["OWN_READER_CALIBRE"] = str(CALIBRE_DIR)
os.environ["OWN_READER_CONFIG"] = str(_TMP_ROOT / "no-config.conf")
for _k in [k for k in os.environ if k.startswith("OWN_READER_") and k not in
           ("OWN_READER_DATA", "OWN_READER_CALIBRE", "OWN_READER_CONFIG")]:
    del os.environ[_k]  # 测试只用内置默认值

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))


import app  # noqa: E402
import llm  # noqa: E402

CALIBRE_DB = CALIBRE_DIR / "metadata.db"

CAL_SCHEMA = """
CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, path TEXT, timestamp TEXT,
                   has_cover INTEGER DEFAULT 0, pubdate TEXT);
CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_authors_link(book INTEGER, author INTEGER);
CREATE TABLE data(book INTEGER, format TEXT, name TEXT);
CREATE TABLE comments(book INTEGER PRIMARY KEY, text TEXT);
CREATE TABLE tags(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_tags_link(book INTEGER, tag INTEGER);
CREATE TABLE languages(id INTEGER PRIMARY KEY, lang_code TEXT);
CREATE TABLE books_languages_link(book INTEGER, lang_code INTEGER);
"""


def _reset_calibre() -> None:
    CALIBRE_DB.unlink(missing_ok=True)
    conn = sqlite3.connect(CALIBRE_DB)
    conn.executescript(CAL_SCHEMA)
    conn.commit()
    conn.close()


class Cal:
    """Helper to fill the throwaway Calibre db."""

    def __init__(self):
        self.conn = sqlite3.connect(CALIBRE_DB)
        self.conn.row_factory = sqlite3.Row
        self._author_id = 0

    def add(self, book_id: int, title: str, authors=(), epub=True, path: str | None = None,
            timestamp: str = "2026-01-01T00:00:00Z", has_cover: int = 0, blurb: str | None = None,
            pubdate: str | None = None) -> int:
        path = path or f"Author/Book {book_id}"
        self.conn.execute("INSERT INTO books VALUES(?,?,?,?,?,?)",
                          (book_id, title, path, timestamp, has_cover, pubdate))
        for a in authors:
            row = self.conn.execute("SELECT id FROM authors WHERE name=?", (a,)).fetchone()
            if row:
                aid = row[0]
            else:
                self._author_id += 1
                aid = self._author_id
                self.conn.execute("INSERT INTO authors VALUES(?,?)", (aid, a))
            self.conn.execute("INSERT OR IGNORE INTO books_authors_link VALUES(?,?)", (book_id, aid))
        if epub:
            self.conn.execute("INSERT INTO data VALUES(?,?,?)", (book_id, "EPUB", f"book{book_id}"))
        if blurb is not None:
            self.conn.execute("INSERT OR REPLACE INTO comments VALUES(?,?)", (book_id, blurb))
        self.conn.commit()
        return book_id

    def close(self):
        self.conn.close()


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Fresh reader.sqlite, fresh empty Calibre db, audit log in tmp_path."""
    monkeypatch.setattr(llm, "AUDIT_DB", tmp_path / "audit.sqlite")
    app.DB_PATH.unlink(missing_ok=True)
    _reset_calibre()
    yield


@pytest.fixture
def cal():
    c = Cal()
    yield c
    c.close()


@pytest.fixture
def events():
    """Empty events table (real schema via app.init_db)."""
    app.init_db()
    return app


def raw_events(rows):
    """Insert events with explicit ts: rows of (device, book_id, type, cfi, text, payload, ts)."""
    with app.db() as c:
        for r in rows:
            c.execute("INSERT INTO events(device,book_id,type,cfi,text,payload,ts) VALUES(?,?,?,?,?,?,?)",
                      (r[0], r[1], r[2], r[3], r[4], r[5], r[6]))
