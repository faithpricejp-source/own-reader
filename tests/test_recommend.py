"""server/recommend.py: 离线可测的解析与打分辅助（_parse_json / verify / has_batch_today / build_context / generate / distill）。"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

import app
import import_history
import llm
import recommend


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    """荐书的外部查询全部离线：微信读书书城 / Open Library / 国立国会図書館 / 本地服务。"""
    def boom(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(recommend, "gw", boom)
    monkeypatch.setattr(recommend, "openlibrary", boom)
    monkeypatch.setattr(recommend, "ndl_has", boom)
    monkeypatch.setattr(recommend, "pil_context", lambda *a, **k: "（新闻：离线）")
    monkeypatch.setattr(recommend, "MEMO", tmp_path / "rec_memo.md")
    monkeypatch.setattr(recommend, "HC_DB", tmp_path / "no-homecinema.db")  # 真 hc_context 走「没有观看记录」
    with app.db() as c:
        c.executescript(import_history.SCHEMA)


# --------------------------------------------------------------------------- #
# _parse_json
# --------------------------------------------------------------------------- #
def test_parse_json_plain():
    assert recommend._parse_json('{"books": [{"title": "T"}], "note": "n"}') == \
        {"books": [{"title": "T"}], "note": "n"}


def test_parse_json_inside_prose():
    assert recommend._parse_json('好的：\n{"books": []}\n以上') == {"books": []}


def test_parse_json_two_objects_takes_first():
    """bugfix-1007-B3：原来贪婪正则取第一个 { 到最后一个 }，两块 JSON 拼不出合法 JSON。
    （原名 test_parse_json_greedy_match_breaks_on_two_objects，钉的是修复前抛 JSONDecodeError 的行为。）"""
    assert recommend._parse_json('{"a": 1} tail {"b": 2}') == {"a": 1}


def test_parse_json_no_json():
    with pytest.raises(ValueError, match="no JSON in model output"):
        recommend._parse_json("完全没有 JSON")


def test_parse_json_broken_json():
    with pytest.raises(json.JSONDecodeError):
        recommend._parse_json('{"books": [}')


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def test_verify_calibre_hit(cal):
    cal.add(1, "三体", ["刘慈欣"])
    idx = recommend._calibre_index()
    assert recommend.verify({"title": "三体", "author": "刘慈欣"}, idx) == {
        "calibre_id": 1, "weread_id": None, "cover": None, "verified": "calibre"}


def test_verify_calibre_prefers_epub(cal):
    cal.add(1, "同名", ["甲"], epub=False)
    cal.add(2, "同名", ["甲"], epub=True)
    assert recommend.verify({"title": "同名", "author": "甲"}, recommend._calibre_index())["calibre_id"] == 2


def test_verify_author_mismatch_is_not_a_hit(cal):
    cal.add(1, "三体", ["刘慈欣"])
    out = recommend.verify({"title": "三体", "author": "别人"}, recommend._calibre_index())
    assert out["calibre_id"] is None and out["verified"] == "unverified"


def test_verify_normalizes_title(cal):
    cal.add(1, "三体（全集）", ["刘慈欣"])
    assert recommend.verify({"title": "三体", "author": "刘慈欣"}, recommend._calibre_index())["calibre_id"] == 1


def test_verify_unverified_when_nothing_found(cal):
    out = recommend.verify({"title": "编造的书", "author": "没人"}, recommend._calibre_index())
    assert out == {"calibre_id": None, "weread_id": None, "cover": None, "verified": "unverified"}


def test_verify_weread_hit(cal, monkeypatch):
    monkeypatch.setattr(recommend, "gw", lambda api, **kw: {"results": [
        {"books": [{"bookInfo": {"title": "三体", "author": "刘慈欣", "bookId": "wr-9", "cover": "c.jpg"}}]}]})
    out = recommend.verify({"title": "三体", "author": "刘慈欣"}, recommend._calibre_index())
    assert out["weread_id"] == "wr-9" and out["cover"] == "c.jpg" and out["verified"] == "weread"


def test_verify_weread_hit_without_bookinfo_wrapper(cal, monkeypatch):
    monkeypatch.setattr(recommend, "gw", lambda api, **kw: {"results": [
        {"books": [{"title": "三体", "author": "刘慈欣", "bookId": "wr-1", "cover": None}]}]})
    assert recommend.verify({"title": "三体", "author": "刘慈欣"}, recommend._calibre_index())["weread_id"] == "wr-1"


def test_verify_openlibrary_and_ndl_fallbacks(cal, monkeypatch):
    monkeypatch.setattr(recommend, "openlibrary", lambda b: {"cover": "http://ol/c.jpg", "year": 2008})
    assert recommend.verify({"title": "外文书", "author": "Someone"}, {})["verified"] == "openlibrary"
    monkeypatch.setattr(recommend, "openlibrary", lambda b: None)
    monkeypatch.setattr(recommend, "ndl_has", lambda t: True)
    assert recommend.verify({"title": "日文书", "author": "著者"}, {})["verified"] == "ndl"


def test_verify_calibre_beats_weread(cal, monkeypatch):
    cal.add(1, "三体", ["刘慈欣"])
    monkeypatch.setattr(recommend, "gw", lambda api, **kw: {"results": [
        {"books": [{"bookInfo": {"title": "三体", "author": "刘慈欣", "bookId": "wr-9", "cover": "c.jpg"}}]}]})
    out = recommend.verify({"title": "三体", "author": "刘慈欣"}, recommend._calibre_index())
    assert out["verified"] == "calibre" and out["weread_id"] == "wr-9" and out["cover"] == "c.jpg"


# --------------------------------------------------------------------------- #
# has_batch_today / hc_context
# --------------------------------------------------------------------------- #
def test_has_batch_today(events):
    assert recommend.has_batch_today() is False
    with recommend._db() as c:
        c.execute("INSERT INTO rec_batches(model,status,prompt_chars) VALUES('claude','running',10)")
    assert recommend.has_batch_today() is False  # 只有 running
    with recommend._db() as c:
        c.execute("INSERT INTO rec_batches(model,status,prompt_chars,created_ts) VALUES('claude','done',10,?)",
                  (time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime()),))
    assert recommend.has_batch_today() is True
    with recommend._db() as c:
        c.execute("INSERT INTO rec_batches(model,status,prompt_chars,created_ts) VALUES('claude','done',10,?)",
                  ("2020-01-01T00:00:00Z",))
    assert recommend.has_batch_today() is True  # 只看今天有没有 done


def test_hc_context_without_db():
    assert recommend.hc_context() == "（没有观看记录）"


def test_hc_context_read_error(monkeypatch, tmp_path):
    db = tmp_path / "library.db"
    db.write_bytes(b"not a database")
    monkeypatch.setattr(recommend, "HC_DB", db)
    assert recommend.hc_context().startswith("（观看记录读取失败")


# --------------------------------------------------------------------------- #
# build_context
# --------------------------------------------------------------------------- #
def test_build_context_sections(events, cal):
    recommend.MEMO.write_text("- 少推投资书")
    cal.add(1, "读过的书", ["甲"])
    with app.db() as c:
        c.execute("""INSERT INTO ext_books(source,source_book_id,title,status,first_ts,last_ts,
                     highlight_count,thought_count) VALUES('weread','b1','读过的书','finished',?,?,1,1)""",
                  ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"))
        c.execute("""INSERT INTO ext_notes(source,source_note_id,source_book_id,kind,quote,text,created_ts)
                     VALUES('weread','n1','b1','thought','原文','这是一段足够长的想法','2026-02-01T00:00:00Z')""")
    app.add_event("d", 1, "ask", "cfi", "选中句", {"question": "这句什么意思？"})
    ctx = recommend.build_context()
    for head in ("## 推荐偏好备忘（由用户反馈总结，用户可能改过）",
                 "## 对过往推荐的反馈（同一本以最后一次为准）",
                 "## 用户对整批推荐说的话",
                 "## 以前推荐过的书（不要再推）",
                 "## 最近在本阅读器里选中提问（最新在前）",
                 "## 最近写下的想法（微信读书，最新在前；引号里是他划的原文）",
                 "## 新闻阅读器（他的打分与批注）",
                 "## Home Cinema 观看记录（最近 90 天）"):
        assert head in ctx
    assert "（还没有）" in ctx                       # 没有反馈时的占位
    assert "（没有观看记录）" in ctx
    assert "- 少推投资书" in ctx
    assert "《读过的书》选中「选中句」问：这句什么意思？" in ctx
    assert "这是一段足够长的想法" in ctx
    # 两条：微信读书那条（读完）+ 本 App 的提问事件（读过，key=c1）
    assert "## 读过的书（共 2 条" in ctx and "读完" in ctx
    assert ctx.index("## 推荐偏好备忘") < ctx.index("## 读过的书")


def test_build_context_no_history(events):
    ctx = recommend.build_context()
    assert "## 读过的书（共 0 条" in ctx


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #
def _answer(books, note="整体思路"):
    return json.dumps({"books": books, "note": note}, ensure_ascii=False)


def _book(i, lang="zh", **kw):
    return {"title": f"书{i}", "author": f"作者{i}", "year": 2000 + i, "lang": lang,
            "reason_type": "延伸", "reason": f"理由{i}", "hook": f"讲什么{i}", **kw}


def test_generate_stores_batch(events, cal, monkeypatch):
    books = [_book(i) for i in range(1, 6)]
    monkeypatch.setattr(llm, "ask", lambda backend, messages, sensitivity=None, caller=None:
                        (_answer(books), "claude-x", 1234))
    monkeypatch.setattr(recommend, "verify", lambda b, idx: {"calibre_id": None, "weread_id": None,
                                                             "cover": None, "verified": "unverified"})
    expected_chars = len(recommend.build_context())
    bid = recommend.generate()
    with recommend._db() as c:
        batch = c.execute("SELECT * FROM rec_batches WHERE id=?", (bid,)).fetchone()
        recs = c.execute("SELECT * FROM recs WHERE batch_id=? ORDER BY pos", (bid,)).fetchall()
    assert batch["status"] == "done" and batch["model"] == "claude-x" and batch["note"] == "整体思路"
    assert batch["prompt_chars"] == expected_chars
    assert [r["pos"] for r in recs] == [0, 1, 2, 3, 4]
    assert recs[0]["title"] == "书1" and recs[0]["lang"] == "zh" and recs[0]["year"] == "2001"
    assert json.loads(recs[0]["payload"])["reason"] == "理由1"
    assert recs[0]["verified"] == "unverified"


def test_generate_caps_at_ten_books(events, monkeypatch):
    monkeypatch.setattr(llm, "ask", lambda backend, messages, sensitivity=None, caller=None:
                        (_answer([_book(i) for i in range(12)]), "m", 1))
    monkeypatch.setattr(recommend, "verify", lambda b, idx: {"calibre_id": None, "weread_id": None,
                                                             "cover": None, "verified": "weread"})
    bid = recommend.generate()
    with recommend._db() as c:
        assert c.execute("SELECT count(*) FROM recs WHERE batch_id=?", (bid,)).fetchone()[0] == 10


def test_generate_warns_without_chinese_book(events, monkeypatch):
    monkeypatch.setattr(llm, "ask", lambda backend, messages, sensitivity=None, caller=None:
                        (_answer([_book(i, lang="en") for i in range(5)]), "m", 1))
    monkeypatch.setattr(recommend, "verify", lambda b, idx: {"calibre_id": None, "weread_id": None,
                                                             "cover": None, "verified": "unverified"})
    bid = recommend.generate()
    with recommend._db() as c:
        note = c.execute("SELECT note FROM rec_batches WHERE id=?", (bid,)).fetchone()[0]
    assert note == "整体思路（注意：这批没有中文原创书，违反了规则 2b）"


def test_generate_too_few_books_is_failure(events, monkeypatch):
    monkeypatch.setattr(llm, "ask", lambda backend, messages, sensitivity=None, caller=None:
                        (_answer([_book(1), _book(2), _book(3)]), "m", 1))
    with pytest.raises(ValueError, match="only 3 books in output"):
        recommend.generate()
    with recommend._db() as c:
        b = c.execute("SELECT * FROM rec_batches ORDER BY id DESC LIMIT 1").fetchone()
        n = c.execute("SELECT count(*) FROM recs").fetchone()[0]
    assert b["status"] == "failed" and b["error"].startswith("only 3 books in output")
    assert n == 0


# --------------------------------------------------------------------------- #
# distill
# --------------------------------------------------------------------------- #
def test_distill_writes_memo(events, monkeypatch):
    with recommend._db() as c:
        c.execute("""INSERT INTO recs(id,batch_id,pos,title,author,reason_type,reason)
                     VALUES(1,1,0,'书一','甲','延伸','推荐理由')""")
    app.add_event("d", None, "rec_feedback", payload={"rec_id": 1, "verdict": "no", "comment": "不感兴趣"})
    app.add_event("d", None, "rec_comment", text="这批太学术了")
    monkeypatch.setattr(llm, "ask", lambda backend, messages, sensitivity=None, caller=None:
                        ("- 少推学术书（依据：书一）", "claude", 10))
    assert recommend.distill() == "- 少推学术书（依据：书一）"
    assert recommend.MEMO.read_text() == "- 少推学术书（依据：书一）\n"
