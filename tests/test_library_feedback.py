"""server/library_feedback.py: 隐式反馈判定（读完 / 读完且笔记多 / 搁置）的阈值边界与反馈文本。"""
from __future__ import annotations

import json
import time

import pytest

import app
import import_history
import library_class as lc
import library_feedback as lf
import reading_now


@pytest.fixture(autouse=True)
def ext_tables(events):
    with app.db() as c:
        c.executescript(import_history.SCHEMA)


def _weread(**kw):
    with reading_now._db() as c:  # 建 weread_progress 表
        c.execute("""INSERT OR REPLACE INTO weread_progress
                     (book_id,title,progress,read_seconds,last_read_ts,calibre_id) VALUES(?,?,?,?,?,?)""",
                  (kw.get("book_id", "w"), kw.get("title", "t"), kw.get("progress"),
                   kw.get("read_seconds"), kw.get("last_read_ts"), kw.get("calibre_id")))


def _days_ago(n: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - n * 86400))


def _implicit() -> dict:
    with lf._db() as c:
        return {(r[0], r[1]): r[2] for r in c.execute("SELECT calibre_id, signal, detail FROM implicit_fb")}


def _ext_book(calibre_id, status, highlights, thoughts):
    with app.db() as c:
        c.execute("""INSERT OR REPLACE INTO ext_books(source,source_book_id,title,calibre_id,status,
                     first_ts,last_ts,highlight_count,thought_count)
                     VALUES('weread',?,?,?,?,?,?,?,?)""",
                  (f"s{calibre_id}", f"书{calibre_id}", calibre_id, status,
                   _days_ago(30), _days_ago(10), highlights, thoughts))


def _feedback(cid, verdict, comment=None, pred=None):
    app.add_event("web", cid, "book_feedback", payload={"calibre_id": cid, "verdict": verdict,
                                                       "comment": comment, "pred": json.dumps(pred) if pred else None})


# --------------------------------------------------------------------------- #
# 常量 / 小工具
# --------------------------------------------------------------------------- #
def test_constants():
    assert (lf.BACKEND, lf.MAX_RESCORE, lf.RESCORE_EVERY_DAYS, lf.RESCORE_MIN_FEEDBACK) == ("claude", 2000, 7, 10)
    assert lf.VERDICT_CN["finished_noted"] == "读完且笔记多" and lf.VERDICT_CN["abandoned"] == "开读后搁置"


def test_now_and_state():
    assert lf.now().endswith("Z") and len(lf.now()) == 20
    assert lf.state("nope") is None
    assert lf.state("k", "v") == "v"
    assert lf.state("k") == "v"
    lf.state("k", "v2")
    assert lf.state("k") == "v2"


# --------------------------------------------------------------------------- #
# 读完：笔记 ≥5 条才算「很对胃口」
# --------------------------------------------------------------------------- #
def test_finished_note_count_boundary():
    _ext_book(1, "finished", 3, 2)   # 5 条
    _ext_book(2, "finished", 2, 2)   # 4 条
    assert lf.detect_implicit() == 2
    got = _implicit()
    assert got[(1, "finished_noted")] == "笔记 5 条"
    assert got[(2, "finished")] == "笔记 4 条"


def test_not_finished_no_signal():
    _ext_book(1, "reading", 9, 9)
    assert lf.detect_implicit() == 0
    assert _implicit() == {}


def test_without_calibre_id_no_signal():
    with app.db() as c:
        c.execute("""INSERT INTO ext_books(source,source_book_id,title,status,first_ts,last_ts,
                     highlight_count,thought_count) VALUES('weread','x','没对上的书','finished',?,?,9,9)""",
                  (_days_ago(5), _days_ago(1)))
    assert lf.detect_implicit() == 0


# --------------------------------------------------------------------------- #
# 微信读书：≥97% 当读完
# --------------------------------------------------------------------------- #
def test_weread_progress_boundary():
    _weread(book_id="a", progress=96, read_seconds=99999, last_read_ts=_days_ago(1), calibre_id=1)
    _weread(book_id="b", progress=97, read_seconds=99999, last_read_ts=_days_ago(1), calibre_id=2)
    _weread(book_id="c", progress=None, read_seconds=99999, last_read_ts=_days_ago(1), calibre_id=3)
    assert lf.detect_implicit() == 1
    assert _implicit() == {(2, "finished"): "微信读书进度 ≥97%"}


# --------------------------------------------------------------------------- #
# 搁置：进度 1–9%、读了 10 分钟–2 小时、两周没再碰
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("progress,seconds,days,abandoned", [
    (1, 600, 15, True),      # 下边界：1%、10 分钟、15 天没碰
    (9, 7199, 15, True),     # 上边界：9%、不到 2 小时
    (0, 600, 15, False),     # 进度 0：上传书不给进度，判不了
    (10, 600, 15, False),    # 进度 10%：不算搁置
    (5, 599, 15, False),     # 只读了 9 分 59 秒
    (5, 7200, 15, False),    # 读了 2 小时以上：大部头慢读
    (5, 600, 13, False),     # 两周内还碰过
    (5, None, 15, False),    # 时长未知 → 0 秒
    (None, 600, 15, False),  # 进度未知
])
def test_abandoned_bounds(progress, seconds, days, abandoned):
    _weread(book_id="a", progress=progress, read_seconds=seconds, last_read_ts=_days_ago(days), calibre_id=1)
    assert lf.detect_implicit() == (1 if abandoned else 0)
    if abandoned:
        detail = _implicit()[(1, "abandoned")]
        assert detail.startswith(f"微信读书读了 {seconds // 60} 分钟、进度 {progress}%")
        assert detail.endswith(f"最后一次 {_days_ago(days)[:10]}")


def test_abandoned_skips_rows_without_calibre_id():
    _weread(book_id="a", progress=5, read_seconds=600, last_read_ts=_days_ago(20), calibre_id=None)
    assert lf.detect_implicit() == 0


def test_told_read_suppresses_abandoned():
    _weread(book_id="a", progress=5, read_seconds=600, last_read_ts=_days_ago(20), calibre_id=1)
    _weread(book_id="b", progress=5, read_seconds=600, last_read_ts=_days_ago(20), calibre_id=2)
    _feedback(1, "read")
    assert lf.detect_implicit() == 1
    assert list(_implicit()) == [(2, "abandoned")]


def test_told_read_deletes_already_recorded_abandoned():
    _weread(book_id="a", progress=5, read_seconds=600, last_read_ts=_days_ago(20), calibre_id=1)
    lf.detect_implicit()
    assert list(_implicit()) == [(1, "abandoned")]
    _feedback(1, "read")
    assert lf.detect_implicit() == 0
    assert _implicit() == {}


def test_other_verdicts_do_not_suppress_abandoned():
    _weread(book_id="a", progress=5, read_seconds=600, last_read_ts=_days_ago(20), calibre_id=1)
    _feedback(1, "no")
    assert lf.detect_implicit() == 1


def test_detect_implicit_is_idempotent():
    _ext_book(1, "finished", 5, 5)
    _weread(book_id="a", progress=5, read_seconds=600, last_read_ts=_days_ago(20), calibre_id=2)
    assert lf.detect_implicit() == 2
    assert lf.detect_implicit() == 0
    assert len(_implicit()) == 2


def test_finished_wins_over_abandoned_for_same_book():
    """同一本既有「读完」又有「搁置」时两条都记（主键是 (calibre_id, signal)）。"""
    _ext_book(1, "finished", 1, 1)
    _weread(book_id="a", progress=5, read_seconds=600, last_read_ts=_days_ago(20), calibre_id=1)
    assert lf.detect_implicit() == 2
    assert sorted(_implicit()) == [(1, "abandoned"), (1, "finished")]


# --------------------------------------------------------------------------- #
# explicit_rows / feedback_lines
# --------------------------------------------------------------------------- #
def test_explicit_rows_latest_order():
    _feedback(1, "love", comment="很对胃 A", pred={"want": 5})
    _feedback(2, "no", pred={"want": 2})
    rows = lf.explicit_rows(app.db())
    assert [(r["cid"], r["v"], r["cm"]) for r in rows] == [(1, "love", "很对胃 A"), (2, "no", None)]
    assert json.loads(rows[0]["pred"]) == {"want": 5}


def test_feedback_lines_empty():
    assert lf.feedback_lines() == "（还没有）"


def test_feedback_lines_last_verdict_per_book(cal):
    cal.add(1, "书架上的书", ["A"])
    _feedback(1, "no", comment="不感兴趣")
    _feedback(1, "love", comment="很对胃口")
    assert lf.feedback_lines() == "- 《书架上的书》→ 很对胃口；他说：很对胃口"


def test_feedback_lines_includes_abandoned_and_respects_exclude(cal):
    cal.add(1, "读完的书", ["A"])
    cal.add(2, "搁置的书", ["A"])
    _ext_book(2, "reading", 0, 0)
    _weread(book_id="a", progress=5, read_seconds=600, last_read_ts=_days_ago(20), calibre_id=2)
    lf.detect_implicit()
    _feedback(1, "read")
    lines = lf.feedback_lines()
    assert lines.startswith("- 《读完的书》→ 读过了")
    assert "《搁置的书》→ 开读后搁置两周以上（微信读书读了 10 分钟、进度 5%，最后一次" in lines
    assert "弱负面信号" in lines
    assert lf.feedback_lines(exclude={2}) == "- 《读完的书》→ 读过了"


def test_feedback_lines_unknown_verdict_falls_back(cal):
    cal.add(3, "奇怪的书", ["A"])
    _feedback(3, "whatever")
    assert lf.feedback_lines() == "- 《奇怪的书》→ whatever"


# --------------------------------------------------------------------------- #
# neighbors：同作者 > 同命中对象 > 同二级分类
# --------------------------------------------------------------------------- #
def test_neighbors_empty():
    assert lf.neighbors([]) == []


def test_neighbors_same_author(cal):
    cal.add(1, "反馈书", ["甲"])
    cal.add(2, "同作者一", ["甲"])
    cal.add(3, "同作者二", ["甲"])
    cal.add(4, "别的作者", ["乙"])
    assert lf.neighbors([1]) == [1, 2, 3]


def test_neighbors_dedupe_and_cap(cal):
    cal.add(1, "反馈书", ["甲"])
    cal.add(2, "同作者", ["甲"])
    assert lf.neighbors([1, 2]) == [1, 2]  # 自身也在结果里，去重后按初次出现顺序
    assert len(lf.neighbors(list(range(1, 3)))) == 2


def test_neighbors_same_secondary_cat_by_score(cal):
    cal.add(1, "反馈书", ["甲"])
    cal.add(2, "同分类高分", ["乙"])
    cal.add(3, "同分类低分", ["乙"])
    with lc._db() as c:
        for cid, w, n, sh in ((1, 3, 3, 3), (2, 5, 4, 5), (3, 1, 1, 1)):
            c.execute("""INSERT INTO book_class(calibre_id,primary_cat,secondary_cat,cat_source,want,need,reason,
                         batch,model,ts,should,known) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (cid, "商业与投资", "投资", "claude", w, n, "", "b1", "m", "2026-01-01T00:00:00Z", sh, 0))
    got = lf.neighbors([1])
    assert got[0] == 1 and got[1] == 2 and got[2] == 3
