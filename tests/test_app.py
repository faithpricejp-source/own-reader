"""server/app.py: build_messages / list_books / book_state / add_event / history (+ book_row, ext_notes)."""
from __future__ import annotations

import json
import sqlite3

import pytest

import app
import import_history


# --------------------------------------------------------------------------- #
# build_messages
# --------------------------------------------------------------------------- #
def test_build_messages_layout():
    msgs = app.build_messages({"context_before": "前文", "context_after": "后文", "chapter": "第一章",
                               "selection": "选中文字", "question": "为什么？"}, "三体")
    assert msgs[0] == {"role": "system", "content": app.SYSTEM_PROMPT}
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == (
        "书名：《三体》\n章节：第一章\n\n【选段之前的原文】\n前文\n\n【用户选中的文字】\n选中文字\n\n"
        "【选段之后的原文】\n后文\n\n【用户的问题】\n为什么？")


def test_build_messages_defaults():
    msgs = app.build_messages({}, "无名书")
    assert msgs[1]["content"] == (
        "书名：《无名书》\n章节：未知\n\n【选段之前的原文】\n\n\n【用户选中的文字】\n\n\n"
        "【选段之后的原文】\n\n\n【用户的问题】\n请解释这段话。")


def test_build_messages_truncates_context():
    msgs = app.build_messages({"context_before": "B" * 7000, "context_after": "A" * 7000,
                               "selection": "S", "question": "Q"}, "T")
    body = msgs[1]["content"]
    before = body.split("【选段之前的原文】\n")[1].split("\n\n【用户选中的文字】")[0]
    after = body.split("【选段之后的原文】\n")[1].split("\n\n【用户的问题】")[0]
    assert (len(before), len(after)) == (6000, 6000)
    assert before == "B" * 6000 and after == "A" * 6000


def test_build_messages_question_default_when_empty():
    msgs = app.build_messages({"question": ""}, "X")
    assert msgs[1]["content"].endswith("请解释这段话。")


# --------------------------------------------------------------------------- #
# add_event
# --------------------------------------------------------------------------- #
def test_add_event_rejects_unknown_type(events):
    with pytest.raises(ValueError, match="bad event type nope"):
        app.add_event("dev", 1, "nope")
    assert app.EVENT_TYPES == {"open", "progress", "highlight", "note", "ask", "feedback", "delete",
                              "rec_feedback", "rec_comment", "rec_memo_edit", "book_feedback", "search"}


def test_recent_searches_dedup_newest_first_per_tab(events):
    for q, tab in [("Projections", "shelf"), ("三体", "shelf"), ("笔记词", "notes"),
                   ("Projections", "shelf"), ("", "shelf")]:
        app.add_event("tablet", None, "search", text=q, payload={"tab": tab})
    assert app.recent_searches("shelf") == ["Projections", "三体"]
    assert app.recent_searches("notes") == ["笔记词"]
    assert app.recent_searches("shelf", limit=1) == ["Projections"]


def test_add_event_roundtrip(events):
    eid = app.add_event("iphone", 7, "highlight", "cfi-1", "原文", {"color": "blue"})
    app.add_event("iphone", 7, "progress", "cfi-2", None, None)
    with app.db() as c:
        rows = c.execute("SELECT * FROM events ORDER BY id").fetchall()
    assert [r["id"] for r in rows][0] == eid
    assert rows[0]["device"] == "iphone" and rows[0]["book_id"] == 7
    assert rows[0]["type"] == "highlight" and rows[0]["cfi"] == "cfi-1" and rows[0]["text"] == "原文"
    assert rows[0]["payload"] == '{"color": "blue"}'
    assert rows[1]["payload"] is None and rows[1]["text"] is None
    assert rows[0]["ts"].endswith("Z") and len(rows[0]["ts"]) == 24  # %Y-%m-%dT%H:%M:%fZ (milliseconds)


def test_add_event_keeps_unicode_in_payload(events):
    app.add_event("d", None, "rec_comment", text="中文理由", payload={"verdict": "no", "comment": "不感兴趣"})
    with app.db() as c:
        r = c.execute("SELECT payload FROM events").fetchone()
    assert json.loads(r["payload"]) == {"verdict": "no", "comment": "不感兴趣"}
    assert "\\u" not in r["payload"]


def test_add_event_ids_increment(events):
    a = app.add_event("d", 1, "open")
    b = app.add_event("d", 1, "open")
    assert b == a + 1


# --------------------------------------------------------------------------- #
# list_books
# --------------------------------------------------------------------------- #
def test_list_books_row_shape_and_order(cal):
    cal.add(1, "三体", ["刘慈欣"], timestamp="2026-01-01T00:00:00Z")
    cal.add(2, "球状闪电", ["刘慈欣"], timestamp="2026-05-05T00:00:00Z")
    cal.add(3, "No EPUB", ["X"], epub=False, timestamp="2026-09-09T00:00:00Z")
    assert app.list_books("") == [
        {"id": 3, "title": "No EPUB", "authors": "X", "epub": False, "pdf": False},
        {"id": 2, "title": "球状闪电", "authors": "刘慈欣", "epub": True, "pdf": False},
        {"id": 1, "title": "三体", "authors": "刘慈欣", "epub": True, "pdf": False},
    ]


def test_list_books_multiple_authors_joined(cal):
    cal.add(4, "合著", ["甲", "乙"])
    assert app.list_books("合著")[0]["authors"] == "甲 & 乙"


def test_list_books_limit(cal):
    for i in range(5):
        cal.add(i + 1, f"书{i + 1}", [], timestamp=f"2026-01-0{i + 1}T00:00:00Z")
    assert [b["id"] for b in app.list_books("", 2)] == [5, 4]
    assert len(app.list_books("")) == 5


def test_list_books_query_hits_title_or_author(cal):
    cal.add(1, "三体", ["刘慈欣"])
    cal.add(2, "球状闪电", ["刘慈欣"])
    cal.add(3, "其他", ["Someone"])
    assert [b["id"] for b in app.list_books("三")] == [1]
    assert sorted(b["id"] for b in app.list_books("刘慈欣")) == [1, 2]
    assert app.list_books("没有这本书") == []


# --------------------------------------------------------------------------- #
# book_state
# --------------------------------------------------------------------------- #
def test_book_state_empty(events):
    assert app.book_state(42) == {"progress": None, "progress_xp": None, "highlights": [], "asks": []}


def test_book_state_full_cycle(events):
    h = app.add_event("d", 5, "highlight", "cfi-h", "划线原文", {"color": "green"})
    app.add_event("d", 5, "note", None, "我的批注", {"highlight_id": h})
    app.add_event("d", 5, "progress", "cfi-p", None, {"fraction": 0.25})
    app.add_event("d", 5, "ask", "cfi-a", "选中句", {"question": "Q?", "answer": "A!", "model": "claude"})
    st = app.book_state(5)
    assert st["progress"] == {"cfi": "cfi-p", "fraction": 0.25, "ts": st["progress"]["ts"]}
    assert st["highlights"] == [{"id": h, "cfi": "cfi-h", "text": "划线原文", "color": "green",
                                "note": "我的批注", "ts": st["highlights"][0]["ts"], "pos": None, "pos_end": None, "pos_kind": None, "chapter": None}]
    assert st["asks"][0]["question"] == "Q?" and st["asks"][0]["answer"] == "A!"
    assert st["asks"][0]["model"] == "claude" and st["asks"][0]["selection"] == "选中句"


def test_book_state_highlight_defaults_and_latest_progress(events):
    app.add_event("d", 5, "highlight", "c1", "t1")
    app.add_event("d", 5, "progress", "c-old", None, {"fraction": 0.1})
    app.add_event("d", 5, "progress", "c-new", None, {"fraction": 0.9})
    st = app.book_state(5)
    assert st["highlights"][0]["color"] == "yellow" and st["highlights"][0]["note"] is None
    assert st["progress"]["cfi"] == "c-new" and st["progress"]["fraction"] == 0.9


def test_book_state_delete_hides_highlight_and_note(events):
    h1 = app.add_event("d", 5, "highlight", "c1", "t1")
    h2 = app.add_event("d", 5, "highlight", "c2", "t2")
    app.add_event("d", 5, "delete", None, None, {"target_id": h1})
    st = app.book_state(5)
    assert [x["id"] for x in st["highlights"]] == [h2]


def test_book_state_note_to_deleted_highlight_is_dropped(events):
    h = app.add_event("d", 5, "highlight", "c1", "t1")
    app.add_event("d", 5, "delete", None, None, {"target_id": h})
    app.add_event("d", 5, "note", None, "迟到的批注", {"highlight_id": h})
    assert app.book_state(5)["highlights"] == []


def test_book_state_delete_without_payload_is_skipped(events):
    """bugfix-1007-B1：delete 事件没带 payload 时跳过它，不再抛 TypeError。
    （原名 test_book_state_delete_without_payload_crashes，钉的是修复前的崩溃行为。）"""
    h = app.add_event("d", 5, "highlight", "c1", "t1")
    app.add_event("d", 5, "delete")
    assert app.book_state(5)["highlights"] == [{"id": h, "cfi": "c1", "text": "t1", "color": "yellow",
                                                "note": None, "ts": app.book_state(5)["highlights"][0]["ts"],
                                                "pos": None, "pos_end": None, "pos_kind": None, "chapter": None}]


def test_book_state_only_this_book(events):
    app.add_event("d", 1, "highlight", "c1", "t1")
    app.add_event("d", 2, "highlight", "c2", "t2")
    assert [x["cfi"] for x in app.book_state(2)["highlights"]] == ["c2"]


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #
def _ext_tables():
    with app.db() as c:
        c.executescript(import_history.SCHEMA)


def test_history_empty(events):
    assert app.history() == []


def test_history_from_own_events(events, cal):
    cal.add(11, "在读的书", ["A"], timestamp="2026-01-01T00:00:00Z")
    cal.add(12, "没 EPUB 的书", ["B"], epub=False, timestamp="2026-01-01T00:00:00Z")
    with app.db() as c:
        c.executescript(import_history.SCHEMA)
        # 显式 ts：history 按 last_ts 倒序，不能靠 now() 的毫秒
        for i, t in enumerate(("highlight", "highlight", "note", "progress")):
            c.execute("INSERT INTO events(device,book_id,type,cfi,text,payload,ts) VALUES(?,?,?,?,?,?,?)",
                      ("d", 11, t, "c", "x",
                       json.dumps({"fraction": 0.4}) if t == "progress" else None,
                       f"2026-05-0{i + 1}T00:00:00.000Z"))
        c.execute("INSERT INTO events(device,book_id,type,ts) VALUES('d',12,'open','2026-06-01T00:00:00.000Z')")
    out = app.history()
    assert [h["key"] for h in out] == ["c12", "c11"]
    assert out[0]["epub"] is False          # 没 EPUB 的书，最后读的一本在前
    own = out[1]
    assert own["calibre_id"] == 11
    assert own["title"] == "在读的书" and own["author"] == "A" and own["epub"] is True
    assert own["highlights"] == 2 and own["thoughts"] == 1
    assert own["status"] is None
    assert own["sources"] == [{"source": "own-reader", "progress": 0.4}]
    assert own["first_ts"] == "2026-05-01T00:00:00.000Z"
    assert own["last_ts"] == "2026-05-04T00:00:00.000Z"


def test_history_merges_external_source(events, cal):
    cal.add(20, "外部书", ["W"])
    with app.db() as c:
        c.executescript(import_history.SCHEMA)
        c.execute("""INSERT INTO ext_books(source,source_book_id,title,author,calibre_id,status,progress,
                     first_ts,last_ts,highlight_count,thought_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  ("weread", "wr-1", "外部书", "W", 20, "finished", 100.0,
                   "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z", 4, 3))
    h = app.history()[0]
    assert h["key"] == "c20" and h["status"] == "finished"
    assert h["highlights"] == 4 and h["thoughts"] == 3
    assert h["sources"] == [{"source": "weread", "id": "wr-1", "status": "finished", "progress": 100.0}]
    assert h["first_ts"] == "2026-02-01T00:00:00Z" and h["last_ts"] == "2026-03-01T00:00:00Z"


def test_history_merges_same_calibre_book(events, cal):
    cal.add(30, "两边都读过的书", ["M"])
    with app.db() as c:
        c.executescript(import_history.SCHEMA)
        c.execute("""INSERT INTO ext_books(source,source_book_id,title,author,calibre_id,status,
                     first_ts,last_ts,highlight_count,thought_count)
                     VALUES('weread','wr-30','两边都读过的书','M',30,'finished','2026-01-01T00:00:00Z',
                            '2026-01-05T00:00:00Z',2,2)""")
    app.add_event("d", 30, "highlight", "c", "t")
    h = app.history()[0]
    assert h["sources"] == [{"source": "weread", "id": "wr-30", "status": "finished", "progress": None},
                           {"source": "own-reader", "progress": None}]
    assert h["highlights"] == 3 and h["thoughts"] == 2


def test_history_unmatched_external_key(events):
    with app.db() as c:
        c.executescript(import_history.SCHEMA)
        c.execute("""INSERT INTO ext_books(source,source_book_id,title,first_ts,last_ts,highlight_count,thought_count)
                     VALUES('reading-log','log-1','没对上的书','2026-01-01T00:00:00Z','2026-01-02T00:00:00Z',1,0)""")
    h = app.history()[0]
    assert h["key"] == "reading-log:log-1" and h["calibre_id"] is None and "epub" not in h


def test_history_sorted_by_last_ts_desc(events, cal):
    with app.db() as c:
        c.executescript(import_history.SCHEMA)
        for i in (1, 2, 3):
            c.execute("""INSERT INTO ext_books(source,source_book_id,title,first_ts,last_ts,
                         highlight_count,thought_count) VALUES('weread',?,?,?,?,0,0)""",
                      (f"w{i}", f"书{i}", f"2026-0{i}-01T00:00:00Z", f"2026-0{i}-02T00:00:00Z"))
    assert [h["title"] for h in app.history()] == ["书3", "书2", "书1"]


def test_history_ignores_test_cli_device(events):
    app.add_event("test-cli", 99, "open")
    assert app.history() == []


# --------------------------------------------------------------------------- #
# book_row / book_title / ext_notes
# --------------------------------------------------------------------------- #
def test_book_row_falls_back_to_empty_authors():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT 1 AS id, 'T' AS title, NULL AS authors, NULL AS epub, NULL AS pdf").fetchone()
    assert app.book_row(r) == {"id": 1, "title": "T", "authors": "", "epub": False, "pdf": False}


def test_book_title_unknown_when_missing(cal):
    assert app.book_title(123) == "未知"
    cal.add(1, "有名字", [])
    assert app.book_title(1) == "有名字"


def test_ext_notes_empty_without_table(events):
    assert app.ext_notes() == []


def test_ext_notes_filters(events):
    with app.db() as c:
        c.executescript(import_history.SCHEMA)
        c.execute("""INSERT INTO ext_books(source,source_book_id,title,author,calibre_id)
                     VALUES('weread','b1','书一','甲',7)""")
        c.execute("""INSERT INTO ext_notes(source,source_note_id,source_book_id,kind,chapter,quote,text,created_ts,star)
                     VALUES('weread','n1','b1','thought','第一章','引文','想法内容','2026-04-01T00:00:00Z',1)""")
    n = app.ext_notes()[0]
    assert n["title"] == "书一" and n["calibre_id"] == 7 and n["text"] == "想法内容"
    assert sorted(app.ext_notes(calibre_id=7)[0]) == ["author", "calibre_id", "chapter", "created_ts", "kind",
                                                      "quote", "source", "source_book_id", "star", "text", "title"]
    assert app.ext_notes(calibre_id=7)[0]["source_book_id"] == "b1"
    assert app.ext_notes(calibre_id=8) == []
    assert app.ext_notes(q="引文")[0]["quote"] == "引文"
    assert app.ext_notes(q="没有") == []


def test_book_state_keeps_cfi_and_crengine_progress_apart(events):
    """安卓 crengine 版的进度（cfi 为空、payload.pos_kind=crengine）不覆盖网页版的 cfi 进度，反之亦然。"""
    app.add_event("web", 5, "progress", "cfi-web", None, {"fraction": 0.2})
    app.add_event("android-x", 5, "progress", None, None,
                  {"pos": "/body/DocFragment[3]/body/p[7].0", "pos_kind": "crengine", "fraction": 0.3, "chapter": "Ch1"})
    app.add_event("web", 5, "progress", None, None, {"fraction": 0.9})   # 没有 cfi 也没有 pos_kind：两边都不认
    st = app.book_state(5)
    assert st["progress"]["cfi"] == "cfi-web" and st["progress"]["fraction"] == 0.2
    assert st["progress_xp"]["pos"] == "/body/DocFragment[3]/body/p[7].0" and st["progress_xp"]["fraction"] == 0.3
    assert st["progress_xp"]["ts"] >= st["progress"]["ts"]


def test_book_state_crengine_highlight_carries_position(events):
    h = app.add_event("android-x", 5, "highlight", None, "原文",
                      {"color": "gray", "pos": "/p[1].0", "pos_end": "/p[1].12", "pos_kind": "crengine", "chapter": "序"})
    hl = app.book_state(5)["highlights"][0]
    assert hl["id"] == h and hl["cfi"] is None and hl["pos"] == "/p[1].0" and hl["pos_end"] == "/p[1].12"
    assert hl["pos_kind"] == "crengine" and hl["chapter"] == "序"


def test_book_state_links_crengine_note_and_delete_by_pos(events):
    """安卓原生版不知道服务器事件号：评注按 pos/pos_end 挂回划线，delete 按 pos/pos_end 删掉当时那条划线。"""
    xp = {"pos": "/p[2].0", "pos_end": "/p[2].9", "pos_kind": "crengine"}
    h1 = app.add_event("android-x", 5, "highlight", None, "原文一", {**xp, "chapter": "序"})
    app.add_event("android-x", 5, "note", None, "我的评注", {**xp, "quote": "原文一"})
    other = app.add_event("android-x", 5, "highlight", None, "别处", {"pos": "/p[3].0", "pos_end": "/p[3].4", "pos_kind": "crengine"})
    st = app.book_state(5)
    assert {h["id"]: h["note"] for h in st["highlights"]} == {h1: "我的评注", other: None}
    app.add_event("android-x", 5, "delete", None, "原文一", dict(xp))
    assert [h["id"] for h in app.book_state(5)["highlights"]] == [other]
    # 删了以后在同一位置重新划：新划线不受先前 delete 影响
    h2 = app.add_event("android-x", 5, "highlight", None, "原文一", dict(xp))
    assert sorted(h["id"] for h in app.book_state(5)["highlights"]) == sorted([other, h2])
