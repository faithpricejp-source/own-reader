"""server/batchjobs.py：resume 只挂 pending 批次、单行 handler 出错不连坐、结果缺行标 error: missing。"""
from __future__ import annotations

import json

import pytest

import batchjobs


def _rows(batch_id):
    with batchjobs._db() as c:
        return dict(c.execute("SELECT custom_id, state FROM batch_job WHERE batch_id=?", (batch_id,)).fetchall())


def _insert(batch_id, cid, kind="t", state="pending", payload=None):
    with batchjobs._db() as c:
        c.execute("INSERT INTO batch_job(batch_id, custom_id, kind, book_id, payload, state) VALUES (?,?,?,?,?,?)",
                  (batch_id, cid, kind, 1, json.dumps(payload or {"cid": cid}), state))


@pytest.fixture
def handler(monkeypatch):
    """登记一个测试用 handler：payload 里 boom=True 就抛错，否则记下并返回 done。"""
    seen = []

    def h(payload, book_id, text, err):
        if payload.get("boom"):
            raise ValueError("handler 爆了")
        seen.append((payload["cid"], text, err))
        return "done"
    monkeypatch.setitem(batchjobs.HANDLERS, "t", h)
    return seen


def test_resume_only_polls_pending_batches(events, monkeypatch):
    """只有还有 pending 行的批次重新挂轮询，已收完的不挂。"""
    _insert("mb_pending", "a")
    _insert("mb_pending", "b", state="done")
    _insert("mb_done", "c", state="done")
    _insert("mb_err", "d", state="error: missing")
    polled = []
    monkeypatch.setattr(batchjobs, "ensure_poller", polled.append)
    batchjobs.resume()
    assert polled == ["mb_pending"]


def test_collect_not_ended_leaves_rows(events, monkeypatch, handler):
    """批次没结束：返回 running，不碰任何行。"""
    _insert("mb", "a")
    monkeypatch.setattr(batchjobs.llm, "batch_status", lambda bid: {"status": "in_progress"})
    monkeypatch.setattr(batchjobs.llm, "batch_results", lambda bid: pytest.fail("不该取结果"))
    assert batchjobs.collect("mb") == "running"
    assert _rows("mb") == {"a": "pending"}


def test_collect_handler_error_marks_only_that_row(events, monkeypatch, handler):
    """一行 handler 抛错只把该行标 error，其余行照常 done。"""
    _insert("mb", "a")
    _insert("mb", "b", payload={"cid": "b", "boom": True})
    monkeypatch.setattr(batchjobs.llm, "batch_status", lambda bid: {"status": "ended"})
    monkeypatch.setattr(batchjobs.llm, "batch_results", lambda bid: iter([("a", "甲", None), ("b", "乙", None)]))
    assert batchjobs.collect("mb") == "ended"
    rows = _rows("mb")
    assert rows["a"] == "done" and rows["b"] == "error: handler 爆了"
    assert handler == [("a", "甲", None)]


def test_collect_passes_api_error_to_handler(events, monkeypatch, handler):
    """API 侧失败的条目把 err 交给 handler，由 handler 决定状态。"""
    _insert("mb", "a")
    monkeypatch.setattr(batchjobs.llm, "batch_status", lambda bid: {"status": "ended"})
    monkeypatch.setattr(batchjobs.llm, "batch_results", lambda bid: iter([("a", None, "errored: x")]))
    batchjobs.collect("mb")
    assert handler == [("a", None, "errored: x")]


def test_collect_missing_rows_marked_error_missing(events, monkeypatch, handler):
    """结果里没有的行标 error: missing；结果里多出的未知 custom_id 忽略；已处理过的行不再动。"""
    _insert("mb", "a")
    _insert("mb", "gone")
    _insert("mb", "old", state="done")
    monkeypatch.setattr(batchjobs.llm, "batch_status", lambda bid: {"status": "ended"})
    monkeypatch.setattr(batchjobs.llm, "batch_results",
                        lambda bid: iter([("a", "甲", None), ("stranger", "?", None), ("old", "再来", None)]))
    batchjobs.collect("mb")
    assert _rows("mb") == {"a": "done", "gone": "error: missing", "old": "done"}
    assert [s[0] for s in handler] == ["a"]


def test_submit_stores_rows_and_starts_poller(events, monkeypatch):
    """提交后每个请求落一行 pending（payload 存 JSON），并挂轮询。"""
    monkeypatch.setattr(batchjobs.llm, "batch_submit", lambda items, model, effort, est_usd: "mb_new")
    polled = []
    monkeypatch.setattr(batchjobs, "ensure_poller", polled.append)
    bid = batchjobs.submit("t", 3, [("c1", "S", "U", {"k": "中"})])
    assert bid == "mb_new" and polled == ["mb_new"]
    assert batchjobs.pending("t", 3) == [{**batchjobs.pending("t", 3)[0], "custom_id": "c1", "payload": {"k": "中"}}]
