"""server/reading_now.py: calibre matching, 在读门槛（60 天 / 两边合计 1 小时 / 97% / 10 分钟离开间隔）与合并。"""
from __future__ import annotations

import json
import time

import pytest

import app
import import_history
import reading_now


@pytest.fixture(autouse=True)
def ext_tables():
    """list_now() 无条件查 ext_books（reading-log 那一支），所以每次都建表。"""
    with app.db() as c:
        c.executescript(import_history.SCHEMA)


def _ts(days_ago: float) -> str:
    return import_history.ts(time.time() - days_ago * 86400)


def _wr(book_id: str, title: str, progress, read_seconds, days_ago=1.0, calibre_id=None,
        author="", cover=None, deeplink=None):
    """Insert one weread_progress row using reading_now's own schema."""
    with reading_now._db() as c:
        c.execute("""INSERT OR REPLACE INTO weread_progress VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (book_id, title, author, cover, progress, read_seconds, _ts(days_ago), calibre_id,
                   _ts(0), deeplink))


def _own(book_id: int, first_days_ago: float, last_days_ago: float, fraction=None, device="iphone", minutes=65):
    """first_days_ago 一次 open；结束于 last_days_ago 的一段连续阅读：每 5 分钟一次 progress，共 minutes 分钟。"""
    with app.db() as c:
        c.execute("INSERT INTO events(device,book_id,type,cfi,text,payload,ts) VALUES(?,?,?,?,?,?,?)",
                  (device, book_id, "open", None, None, None, _ts(first_days_ago)))
        for m in range(minutes, -1, -5):
            c.execute("INSERT INTO events(device,book_id,type,cfi,text,payload,ts) VALUES(?,?,?,?,?,?,?)",
                      (device, book_id, "progress", "cfi", None, json.dumps({"fraction": fraction}),
                       _ts(last_days_ago + m / 1440)))


# --------------------------------------------------------------------------- #
# 常量（现行门槛）
# --------------------------------------------------------------------------- #
def test_thresholds():
    assert (reading_now.DAYS, reading_now.MIN_SECONDS, reading_now.DONE_PCT, reading_now.IDLE_GAP) == (60, 3600, 97, 600)


# --------------------------------------------------------------------------- #
# calibre_index / match
# --------------------------------------------------------------------------- #
def test_calibre_index_only_epubs(cal):
    cal.add(1, "有 EPUB", ["A"])
    cal.add(2, "只有 PDF", ["A"], epub=False)
    idx = reading_now.calibre_index()
    assert sorted(idx) == ["有epub"] and idx["有epub"][0]["id"] == 1


def test_match_single_candidate(cal):
    cal.add(1, "三体", ["刘慈欣"])
    idx = reading_now.calibre_index()
    assert reading_now.match(idx, "三体", "刘慈欣") == 1


def test_match_strips_author_suffix_in_title(cal):
    cal.add(1, "三体", ["刘慈欣"])
    idx = reading_now.calibre_index()
    assert reading_now.match(idx, "三体 - 刘慈欣", "") == 1


def test_match_disambiguates_by_author(cal):
    cal.add(1, "同名书", ["甲"])
    cal.add(2, "同名书", ["乙"])
    idx = reading_now.calibre_index()
    assert reading_now.match(idx, "同名书", "乙") == 2
    assert reading_now.match(idx, "同名书", "") is None  # 两本都候选 → 不猜


def test_match_no_candidate(cal):
    cal.add(1, "三体", ["刘慈欣"])
    assert reading_now.match(reading_now.calibre_index(), "没有这本", "X") is None
    assert reading_now.match({}, "三体", "刘慈欣") is None


# --------------------------------------------------------------------------- #
# 微信读书：progress / read_seconds / 60 天
# --------------------------------------------------------------------------- #
def test_list_now_weread_progress_boundary(events):
    _wr("a", "读到 96%", 96, 3600, calibre_id=1)
    _wr("b", "读到 97%", 97, 3600, calibre_id=2)
    _wr("c", "读到 98%", 98, 3600, calibre_id=3)
    _wr("d", "进度未知", None, 3600, calibre_id=4)
    assert [x["id"] for x in reading_now.list_now()] == [1]


def test_list_now_weread_seconds_boundary(events):
    _wr("a", "读了 3599 秒", 50, 3599, calibre_id=1)
    _wr("b", "读了 3600 秒", 50, 3600, calibre_id=2)
    _wr("c", "时长未知", 50, None, calibre_id=3)
    assert [x["id"] for x in reading_now.list_now()] == [2]


def test_list_now_weread_day_window(events):
    _wr("a", "昨天读的", 50, 3600, days_ago=59, calibre_id=1)
    _wr("b", "61 天前读的", 50, 3600, days_ago=61, calibre_id=2)
    assert [x["id"] for x in reading_now.list_now()] == [1]


def test_list_now_weread_fields(events, cal):
    cal.add(9, "在读的书", ["作者"])
    _wr("a", "在读的书", 42, 3600, calibre_id=9, author="作者", cover="http://cover", deeplink="weread://x")
    x = reading_now.list_now()[0]
    assert x["fraction"] == 0.42 and x["cover_url"] == "http://cover" and x["weread_url"] == "weread://x"
    assert x["sources"] == ["weread"] and x["epub"] is True
    assert x["title"] == "在读的书" and x["authors"] == "作者"
    assert x["last_day"] == x["last_ts"][:10] == _ts(1)[:10]


# --------------------------------------------------------------------------- #
# 本 App：相邻事件 ≤10 分钟累加满 1 小时 / fraction ≥0.98 算读完
# --------------------------------------------------------------------------- #
def test_list_now_own_seconds_boundary(events, cal):
    cal.add(1, "读满一小时", ["A"])
    cal.add(2, "只读了 55 分钟", ["A"])
    _own(1, first_days_ago=1.0, last_days_ago=1.0, fraction=0.5, minutes=60)
    _own(2, first_days_ago=1.0, last_days_ago=1.0, fraction=0.5, minutes=55)
    assert [x["id"] for x in reading_now.list_now()] == [1]


def test_own_seconds_skips_idle_gaps(events):
    # 两次打开相隔一周：首末跨度很大，但实际只读了两段 30 分钟，中间的间隔不算
    _own(1, first_days_ago=8.0, last_days_ago=8.0, minutes=30)
    _own(1, first_days_ago=1.0, last_days_ago=1.0, minutes=30)
    with app.db() as c:
        assert round(reading_now.own_seconds(c)[1]) == 3600


def test_list_now_weread_plus_own_seconds_combined(events, cal):
    cal.add(7, "两边各读半小时", ["M"])
    cal.add(8, "两边加起来不够", ["M"])
    _wr("a", "两边各读半小时", 30, 1800, days_ago=5, calibre_id=7)
    _own(7, 1.0, 1.0, fraction=0.3, minutes=30)
    _wr("b", "两边加起来不够", 30, 1200, days_ago=5, calibre_id=8)
    _own(8, 1.0, 1.0, fraction=0.3, minutes=30)
    assert [x["id"] for x in reading_now.list_now()] == [7]


def test_list_now_own_fraction_done_is_skipped(events, cal):
    cal.add(1, "读到 98%", ["A"])
    cal.add(2, "读到 97.9%", ["A"])
    _own(1, 1.0, 0.5, fraction=0.98)
    _own(2, 1.0, 0.5, fraction=0.979)
    assert [x["id"] for x in reading_now.list_now()] == [2]


def test_list_now_own_ignores_test_cli(events, cal):
    cal.add(1, "测试设备读的", ["A"])
    _own(1, 1.0, 0.5, fraction=0.3, device="test-cli")
    assert reading_now.list_now() == []


def test_list_now_own_outside_window(events, cal):
    cal.add(1, "两个月前读的", ["A"])
    _own(1, first_days_ago=70, last_days_ago=61, fraction=0.3)
    assert reading_now.list_now() == []


# --------------------------------------------------------------------------- #
# reading-log
# --------------------------------------------------------------------------- #
def test_list_now_read_with_me(events, cal):
    cal.add(5, "伴读中的书", ["R"])
    with app.db() as c:
        c.executescript(import_history.SCHEMA)
        c.execute("""INSERT INTO ext_books(source,source_book_id,title,author,calibre_id,status,first_ts,last_ts)
                     VALUES('reading-log','r1','日志在读的书','R',5,'reading',?,?)""", (_ts(3), _ts(2)))
        c.execute("""INSERT INTO ext_books(source,source_book_id,title,status,first_ts,last_ts)
                     VALUES('reading-log','r2','读完了的','finished',?,?)""", (_ts(3), _ts(2)))
        c.execute("""INSERT INTO ext_books(source,source_book_id,title,status,first_ts,last_ts)
                     VALUES('reading-log','r3','很久以前在读的','reading',?,?)""", (_ts(90), _ts(90)))
    out = reading_now.list_now()
    assert [x["title"] for x in out] == ["伴读中的书"]
    assert out[0]["sources"] == ["reading-log"] and out[0]["fraction"] is None


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
def test_list_now_merges_sources_same_calibre_book(events, cal):
    cal.add(7, "两边都在读", ["M"])
    _wr("a", "两边都在读", 30, 3600, days_ago=5, calibre_id=7, author="M",
        cover="http://cover", deeplink="weread://7")
    _own(7, 1.0, 0.9, fraction=0.2)
    out = reading_now.list_now()
    assert len(out) == 1
    x = out[0]
    assert x["id"] == 7 and x["sources"] == ["own-reader", "weread"]
    assert x["fraction"] == 0.2 and x["last_day"] == _ts(0.9)[:10]
    # bugfix-1007-B4：本 App 的条目更晚（进度/时间以它为准），但微信读书的封面和链接要保留
    assert x["cover_url"] == "http://cover" and x["weread_url"] == "weread://7"


def test_list_now_sorted_by_last_ts_desc(events, cal):
    for i in (1, 2, 3):
        cal.add(i, f"书{i}", ["A"])
        _wr(f"w{i}", f"书{i}", 10, 3600, days_ago=float(i), calibre_id=i)
    assert [x["id"] for x in reading_now.list_now()] == [1, 2, 3]


def test_list_now_key_without_calibre_id(events):
    _wr("a", "没对上 Calibre 的书", 10, 3600, days_ago=1)
    out = reading_now.list_now()
    assert out[0]["id"] is None and out[0]["epub"] is False and out[0]["title"] == "没对上 Calibre 的书"


def test_list_now_empty(events):
    assert reading_now.list_now() == []


def test_list_now_without_ext_books_table(events):
    """bugfix-1007-B2：ext_books 不存在时不再抛错（照 history() 的 _has_ext 保护），reading-log 那一支当空。
    （原名 test_list_now_needs_ext_books_table，钉的是修复前 no such table 的行为。）"""
    with app.db() as c:
        c.execute("DROP TABLE ext_books")
    assert reading_now.list_now() == []


# --------------------------------------------------------------------------- #
# refresh_if_stale
# --------------------------------------------------------------------------- #
def test_refresh_if_stale_skips_when_fresh(events, monkeypatch):
    _wr("a", "刚刷过", 10, 3600, days_ago=1)
    monkeypatch.setattr(reading_now, "refresh", lambda: pytest.fail("should not refresh"))
    reading_now.refresh_if_stale()  # refreshed_ts 刚写入 → 不刷新
    assert reading_now._lock.acquire(blocking=False)  # 没有后台线程起跑
    reading_now._lock.release()


# --------------------------------------------------------------------------- #
# 中文版 + 外文版合计
# --------------------------------------------------------------------------- #
def test_pair_editions_then_list_merges_language_editions(events, cal):
    cal.add(20474, "Projections: A Story of Human Emotions", ["Karl Deisseroth"])
    _wr("zh", "照亮破碎之心", 6, 41 * 60, days_ago=1.0, author="卡尔·戴瑟罗斯")
    _own(20474, 0.5, 0.5, fraction=0.05, minutes=25)
    _wr("x", "无关的中文书", 10, 20 * 60, days_ago=2.0)
    assert reading_now.list_now() == []
    asked = []

    def fake(msgs):
        asked.append(msgs[1]["content"])
        return '好的：[["c20474", "t照亮破碎之心"]]'
    assert reading_now.pair_editions(ask=fake) == 1
    assert "照亮破碎之心" in asked[0] and "Projections" in asked[0]
    out = reading_now.list_now()
    assert len(out) == 1 and out[0]["id"] == 20474           # 显示最近读的那一版
    assert out[0]["other_editions"] == ["照亮破碎之心"] and out[0]["read_minutes"] == 66
    # 已判过的组合不再问
    assert reading_now.pair_editions(ask=lambda m: 1 / 0) == 0


def test_pair_editions_includes_long_side_when_other_is_short(events, cal):
    cal.add(20474, "Projections", ["Karl Deisseroth"])
    _wr("zh", "照亮破碎之心", 6, 61 * 60, days_ago=1.0)            # 中文版自己已满 1 小时
    _own(20474, 0.5, 0.5, fraction=0.05, minutes=10)               # 英文版刚开始读、更近
    assert reading_now.pair_editions(ask=lambda m: '[["t照亮破碎之心","c20474"]]') == 1
    out = reading_now.list_now()
    assert [x["id"] for x in out] == [20474] and out[0]["other_editions"] == ["照亮破碎之心"]
