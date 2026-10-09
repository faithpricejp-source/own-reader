"""代码审查 B 组发现（B-1…B-8）的回归测试；B-7 的修复由 bugfix-1007-B4 覆盖，测试保留作回归。

用 conftest 的 autouse isolate + events/cal fixture。
"""
from __future__ import annotations

import json
import time

import app
import import_history as ih
import library_class
import library_feedback
import llm
import reading_now
import recommend

FMT = "%Y-%m-%dT%H:%M:%SZ"


def test_book_state_tolerates_bad_delete_payload(events):
    """B-2：payload 缺失/损坏的 delete 事件不能让 book_state 崩（/state 永久 500）；正常 delete 仍生效。"""
    h = app.add_event("test-pytest", 901, "highlight", cfi="cfi-x", text="划线")
    app.add_event("test-pytest", 901, "delete", payload=None)
    app.add_event("test-pytest", 901, "delete", payload={"foo": "bar"})
    app.add_event("test-pytest", 901, "delete", payload=json.dumps({"nope": 1}))
    st = app.book_state(901)
    assert [x["id"] for x in st["highlights"]] == [h], "坏 delete 行应被跳过，划线不受影响"
    app.add_event("test-pytest", 901, "delete", payload={"target_id": h})
    assert app.book_state(901)["highlights"] == [], "正常 delete 仍应删除对应事件"


_FULL = {"title": "书名甲", "author": "作者甲", "year": "2020", "lang": "zh",
         "reason_type": "延伸", "reason": "顺着他读的往下走", "hook": "讲什么"}


def test_generate_skips_book_without_title(events, monkeypatch):
    """B-4：模型输出里一本缺 title 不能把整批推荐拖成 failed；其余书照常入库。"""
    books = [dict(_FULL, title=f"书名{i}") for i in range(1, 5)] + [
        {"author": "作者乙", "year": "2001", "lang": "en", "reason_type": "盲区", "reason": "r", "hook": "h"}]
    monkeypatch.setattr(recommend, "build_context", lambda: "ctx")
    monkeypatch.setattr(llm, "ask", lambda backend, msgs, **kw:
                        (json.dumps({"books": books, "note": "n"}), "fake-model", 5))
    monkeypatch.setattr(recommend, "_calibre_index", lambda: {})
    monkeypatch.setattr(recommend, "gw", lambda api, **kw: {"results": []})
    monkeypatch.setattr(recommend, "openlibrary", lambda book: None)
    monkeypatch.setattr(recommend, "ndl_has", lambda title: False)
    bid = recommend.generate()
    with recommend._db() as c:
        batch = c.execute("SELECT status FROM rec_batches WHERE id=?", (bid,)).fetchone()
        n = c.execute("SELECT count(*) FROM recs WHERE batch_id=?", (bid,)).fetchone()[0]
    assert batch["status"] == "done", "缺 title 的一本被跳过后，整批应正常完成"
    assert n == 4


def test_refresh_survives_bad_rescore_ts(events, monkeypatch, capsys):
    """B-5②：lib_state.rescore_ts 被写坏时，夜间 refresh 不能崩在重算判断那一步。"""
    library_feedback.state("rescore_ts", "2026-13-99 瞎写的")
    monkeypatch.setattr(library_feedback, "busy", lambda: False)
    monkeypatch.setattr(ih, "main", lambda argv: 0)  # refresh 开头同步微信读书笔记，测试里不联网
    monkeypatch.setattr(reading_now, "refresh", lambda: 0)
    monkeypatch.setattr(library_feedback, "detect_implicit", lambda: 0)
    monkeypatch.setattr(library_feedback.lc, "library", lambda: [])
    library_feedback.refresh()
    assert "距上次 999 天" in capsys.readouterr().out, "坏时间戳应按 999 天处理并走完 refresh"


def test_days_since_uses_utc_not_local():
    """B-5①：rescore_ts 是 UTC（now() 用 gmtime），解析也必须按 UTC；本机 +0900，mktime 会偏 9 小时。"""
    s = time.strftime(FMT, time.gmtime(time.time() - 10 * 86400))
    d = library_feedback._days_since(s)
    assert 9.9 < d < 10.1, f"10 天前的 UTC 时间戳应算出 ≈10 天，实际 {d:.3f}"
    assert library_feedback._days_since("0") == 999
    assert library_feedback._days_since("not-a-date") == 999


def test_list_now_merge_keeps_title(events, cal):
    """B-7：本 App 事件比 weread 记录新、且书已不在 Calibre 时，合并不能丢掉 title/authors/cover_url。"""
    with app.db() as c:
        c.executescript(ih.SCHEMA)
    now = time.time()
    t_3d, t_2d, t_1h = (time.strftime(FMT, time.gmtime(now - x)) for x in (3 * 86400, 2 * 86400, 3600))
    with reading_now._db() as c:
        c.execute("INSERT INTO weread_progress(book_id,title,author,cover,progress,read_seconds,last_read_ts,"
                  "calibre_id,refreshed_ts,deeplink) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  ("wrb7", "微读在读书", "作者七", "http://cover/7", 50, 3600, t_2d, 707, t_2d, "http://dl/7"))
    with app.db() as c:
        ins = "INSERT INTO events(ts,device,book_id,type,payload) VALUES(?,?,?,?,?)"
        c.execute(ins, (t_3d, "test-pytest", 707, "open", None))
        c.execute(ins, (t_1h, "test-pytest", 707, "progress", json.dumps({"fraction": 0.5})))
    it = next(x for x in reading_now.list_now() if x.get("id") == 707)  # Calibre 空库 = 书已删
    assert it["sources"] == ["own-reader", "weread"]
    assert it["fraction"] == 0.5, "进度以更新的本 App 事件为准"
    assert it.get("title") == "微读在读书"
    assert it.get("authors") == "作者七"
    assert it.get("cover_url") == "http://cover/7"
    assert it.get("weread_url") == "http://dl/7"


def test_classify_batch_tolerates_bad_scores(events, monkeypatch):
    """B-8：模型给 "3.5"/"4 星" 这类分时，classify_batch 写库不能整批崩。"""
    def mk(i, t):
        return {"id": i, "title": t, "authors": "某作者", "epub": True, "tags": [], "lang": "", "year": "", "blurb": ""}
    books, anchors = [mk(1, "书一"), mk(2, "书二")], [mk(101, "锚一"), mk(102, "锚二")]
    rows = [{"id": 1, "p": "其他", "s": "无法判断", "w": "3.5", "n": "4 星", "sh": "2", "k": 0, "r": "依据一"},
            {"id": 2, "p": "其他", "s": "无法判断", "w": 4, "n": 2, "sh": 1, "k": "1.8", "r": ""},
            {"id": 101, "p": "其他", "s": "无法判断", "w": 3, "n": 3, "sh": 3, "k": 0, "r": ""},
            {"id": 102, "p": "其他", "s": "无法判断", "w": 3, "n": 3, "sh": 3, "k": 0, "r": ""}]
    monkeypatch.setitem(library_class.BACKEND_CALL, "claude", lambda system, prompt: (json.dumps(rows), "fake-model"))
    got = library_class.classify_batch("kimi-b8", books, anchors, "", {}, backend="claude", write=True)
    assert set(got) == {1, 2, 101, 102}
    with library_class._db() as c:
        r1 = c.execute("SELECT want, need, should, known FROM book_class WHERE calibre_id=1").fetchone()
        r2 = c.execute("SELECT want, need, should, known FROM book_class WHERE calibre_id=2").fetchone()
    assert tuple(r1) == (3, 1, 2, 0), '"3.5"→3，"4 星" 非法回落 1，"2"→2，0→0'
    assert tuple(r2) == (4, 2, 1, 1), '"1.8"→1'
