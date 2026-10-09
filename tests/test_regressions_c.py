"""四个回归 bug（B1–B4）：每个测试直接调用真函数，修复前必须失败。

- B1 app.book_state()：type='delete' 且 payload 为 NULL → json.loads(None) TypeError，整个 /api/books/<id>/state 500
- B2 reading_now.list_now()：无条件查 ext_books，没跑过 import_history 的库报 no such table
- B3 recommend._parse_json()：贪婪 \\{.*\\} 把两块 JSON/正文花括号拼成非法串
- B4 reading_now.list_now()：合并同一本书时较新那条把旧条的 cover_url/weread_url 顶掉
"""
from __future__ import annotations

import json
import time

import app
import import_history
import reading_now
import recommend


def _ts(days_ago: float) -> str:
    return import_history.ts(time.time() - days_ago * 86400)


def _wr(book_id: str, title: str, progress, read_seconds, days_ago=1.0, calibre_id=None,
        author="", cover=None, deeplink=None):
    with reading_now._db() as c:
        c.execute("""INSERT OR REPLACE INTO weread_progress VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (book_id, title, author, cover, progress, read_seconds, _ts(days_ago), calibre_id,
                   _ts(0), deeplink))


# --------------------------------------------------------------------------- #
# B1
# --------------------------------------------------------------------------- #
def test_b1_book_state_delete_with_null_payload_is_skipped(events):
    """delete 事件没带 payload 时不应抛错；同一本书其余状态照常返回。"""
    h = app.add_event("d", 5, "highlight", "cfi-h", "划线", {"color": "blue"})
    app.add_event("d", 5, "delete")  # payload NULL
    app.add_event("d", 5, "progress", "cfi-p", None, {"fraction": 0.3})
    st = app.book_state(5)
    assert [x["id"] for x in st["highlights"]] == [h]
    assert {k: v for k, v in st["progress"].items() if k != "ts"} == {"cfi": "cfi-p", "fraction": 0.3}


# --------------------------------------------------------------------------- #
# B2
# --------------------------------------------------------------------------- #
def test_b2_list_now_without_ext_books_table(events, cal):
    """没跑过 import_history（没有 ext_books 表）的库：list_now 不应报 no such table。"""
    cal.add(9, "微信读书在读", ["A"])
    _wr("w9", "微信读书在读", 40, 3600, calibre_id=9, cover="http://cover")  # 满 MIN_SECONDS 才算在读
    with app.db() as c:
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='ext_books'").fetchone() is None
    out = reading_now.list_now()
    assert [x["id"] for x in out] == [9]
    assert out[0]["cover_url"] == "http://cover"


# --------------------------------------------------------------------------- #
# B3
# --------------------------------------------------------------------------- #
def test_b3_parse_json_takes_first_complete_object():
    """输出里有两块 JSON：取第一个完整对象，不要贪婪拼到最后一个 } 。"""
    text = '{"books": [{"title": "甲"}], "note": "n1"}\n补充：{"books": [{"title": "乙"}], "note": "n2"}'
    assert recommend._parse_json(text) == {"books": [{"title": "甲"}], "note": "n1"}


def test_b3_parse_json_brace_in_prose():
    """正文里先出现一个孤立的花括号：仍要解析出后面那段合法 JSON。"""
    text = '先说个 { 符号，然后：\n{"books": [{"title": "丙"}], "note": "ok"}\n以上'
    assert recommend._parse_json(text) == {"books": [{"title": "丙"}], "note": "ok"}



# --------------------------------------------------------------------------- #
# B4
# --------------------------------------------------------------------------- #
def test_b4_merge_keeps_cover_and_weread_url_from_older_entry(events, cal):
    """同一本 Calibre 书：本 App 这条更晚，但合并时不能把微信读书那条的 cover_url/weread_url 丢掉。"""
    with app.db() as c:
        c.executescript(import_history.SCHEMA)
    cal.add(7, "两边都在读", ["M"])
    _wr("a", "两边都在读", 30, 3600, days_ago=5, calibre_id=7, author="M",
        cover="http://cover", deeplink="weread://7")
    with app.db() as c:
        c.execute("INSERT INTO events(device,book_id,type,cfi,text,payload,ts) VALUES(?,?,?,?,?,?,?)",
                  ("iphone", 7, "open", None, None, None, _ts(1.0)))
        c.execute("INSERT INTO events(device,book_id,type,cfi,text,payload,ts) VALUES(?,?,?,?,?,?,?)",
                  ("iphone", 7, "progress", "cfi", None, json.dumps({"fraction": 0.2}), _ts(0.9)))
    out = reading_now.list_now()
    assert len(out) == 1
    x = out[0]
    assert x["id"] == 7 and x["sources"] == ["own-reader", "weread"]
    assert x["fraction"] == 0.2 and x["last_day"] == _ts(0.9)[:10]  # 进度/时间取更晚的本 App
    assert x["cover_url"] == "http://cover" and x["weread_url"] == "weread://7"
