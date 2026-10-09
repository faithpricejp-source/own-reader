"""server/dossier.py：正文截断、引用链接拼接、已完成不重跑、pause_turn 续跑与拒答/空答/截断处理。

SDK client 一律是假对象，不联网。
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

import book_text
import dossier
import gloss
import llm


class _Usage(dict):
    def model_dump(self):
        return dict(self)


def _resp(stop, blocks, model="claude-opus-5-5"):
    return NS(stop_reason=stop, stop_details={"type": "policy"} if stop == "refusal" else None, model=model,
              content=blocks, usage=_Usage({"input_tokens": 1000, "output_tokens": 100,
                                            "server_tool_use": {"web_search_requests": 2}}))


def _text(t, urls=()):
    return NS(type="text", text=t, citations=[NS(url=u) for u in urls])


class FakeClient:
    """messages.create 依次返回预设响应，并记下每轮发出的 messages。"""

    def __init__(self, resps):
        self.resps, self.sent = list(resps), []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.sent.append(kw)
        return self.resps.pop(0)


@pytest.fixture
def fake_build(monkeypatch):
    monkeypatch.setattr(dossier, "_book_text", lambda epub: ("正文", False))
    monkeypatch.setattr(gloss, "profile", lambda: "画像")
    monkeypatch.setattr(llm, "api_spent", lambda month=None: 0.0)

    def use(resps):
        client = FakeClient(resps)
        monkeypatch.setattr(llm, "_client", lambda: client)
        return client
    return use


def test_book_text_marks_chapters_once(monkeypatch):
    """同一章的连续段落只插一次章标题。"""
    monkeypatch.setattr(book_text, "extract_paragraphs", lambda epub: [
        {"chapter": "一", "text": "a"}, {"chapter": "一", "text": "b"}, {"chapter": "二", "text": "c"}])
    text, truncated = dossier._book_text("x.epub")
    assert text.count("【章】一") == 1 and text.count("【章】二") == 1 and not truncated


def test_book_text_truncates_latin_at_latin_cap(monkeypatch):
    monkeypatch.setattr(dossier, "MAX_CHARS_LATIN", 50)
    monkeypatch.setattr(book_text, "extract_paragraphs", lambda epub: [{"chapter": "", "text": "x" * 80}])
    text, truncated = dossier._book_text("x.epub")
    assert truncated and len(text) == 50


def test_book_text_cjk_uses_cjk_cap(monkeypatch):
    """前 5 万字里 CJK 超过 1 万字的按中日文上限截。"""
    monkeypatch.setattr(dossier, "MAX_CHARS_CJK", 12000)
    monkeypatch.setattr(dossier, "MAX_CHARS_LATIN", 10 ** 9)
    monkeypatch.setattr(book_text, "extract_paragraphs", lambda epub: [{"chapter": "", "text": "字" * 20000}])
    text, truncated = dossier._book_text("x.epub")
    assert truncated and len(text) == 12000


def test_book_text_short_not_truncated(monkeypatch):
    monkeypatch.setattr(book_text, "extract_paragraphs", lambda epub: [{"chapter": None, "text": "短"}])
    assert dossier._book_text("x.epub") == ("短", False)


def test_render_appends_dedup_citation_links_max_three():
    """文本块后附引用链接：去重、最多 3 条；非文本块跳过。"""
    content = [_text("甲。", ["u1", "u1", "u2", "u3", "u4"]), NS(type="server_tool_use"),
               _text("乙。"), NS(type="text", text="丙", citations=None)]
    out = dossier._render(content)
    assert out == "甲。 [〔来源〕](u1) [〔来源〕](u2) [〔来源〕](u3)乙。丙"


def test_start_done_returns_cache_without_thread(events, monkeypatch):
    """已完成的导读直接返回，不起线程、不调 build。"""
    with dossier._db() as c:
        c.execute("INSERT INTO book_dossier(book_id, state, md) VALUES (7, 'done', '旧导读')")
    monkeypatch.setattr(dossier, "build", lambda *a: pytest.fail("不该重新生成"))
    r = dossier.start(7, "x.epub", "书", "作者")
    assert r["state"] == "done" and r["md"] == "旧导读" and 7 not in dossier._running


def test_start_records_error_state(events, monkeypatch):
    """build 抛错时该书记为 error 并保留原因。"""
    def boom(*a):
        raise llm.BackendError("坏了")
    monkeypatch.setattr(dossier, "build", boom)
    dossier.start(8, "x.epub", "书", "作者", force=True)
    dossier._running[8].join(5)
    r = dossier.get(8)
    assert r["state"] == "error" and r["error"] == "坏了"


def test_build_continues_after_pause_turn(events, fake_build):
    """pause_turn 后把上一轮回答接进 messages 续跑；两轮内容拼起来，两轮花费都记账。"""
    client = fake_build([_resp("pause_turn", [_text("前半，")]), _resp("end_turn", [_text("后半。", ["u"])])])
    r = dossier.build(1, "x.epub", "书", "作者")
    assert r["md"] == "前半，后半。 [〔来源〕](u)" and r["truncated"] is False
    assert len(client.sent) == 2
    assert client.sent[1]["messages"][-1]["role"] == "assistant"
    with llm._spend_db() as c:
        rows = c.execute("SELECT model, usd FROM api_spend").fetchall()
    assert [m for m, _ in rows] == ["claude-opus-5-5:dossier"] * 2
    assert r["usd"] == pytest.approx(sum(u for _, u in rows))


def test_build_refusal_raises(events, fake_build):
    fake_build([_resp("refusal", [])])
    with pytest.raises(llm.BackendError, match="拒答"):
        dossier.build(1, "x.epub", "书", "作者")


def test_build_empty_answer_raises(events, fake_build):
    fake_build([_resp("end_turn", [NS(type="server_tool_use")])])
    with pytest.raises(llm.BackendError, match="空答"):
        dossier.build(1, "x.epub", "书", "作者")


def test_build_max_tokens_keeps_text_with_note(events, fake_build):
    """max_tokens 不抛错，保留已写的部分并注明截断。"""
    fake_build([_resp("max_tokens", [_text("写到一半")])])
    assert dossier.build(1, "x.epub", "书", "作者")["md"] == "写到一半\n\n（输出被截断）"


def test_build_refuses_over_monthly_cap(events, fake_build, monkeypatch):
    """预估花费会超当月上限时，不建 client 直接拒绝。"""
    monkeypatch.setattr(llm, "api_spent", lambda month=None: llm.API_MONTHLY_CAP)
    monkeypatch.setattr(llm, "_client", lambda: pytest.fail("不该调用 API"))
    with pytest.raises(llm.BackendError, match="会超上限"):
        dossier.build(1, "x.epub", "书", "作者")


def test_stale_running_after_restart_becomes_error(events):
    """库里是 running 但没有线程在跑（服务器中途重启）：读的时候改成可重新生成的失败状态。"""
    with dossier._db() as c:
        c.execute("INSERT INTO book_dossier(book_id, state) VALUES (11, 'running')")
    dossier._running.pop(11, None)
    r = dossier.get(11)
    assert r["state"] == "error" and "中断" in r["error"]
    assert dossier.get(11)["state"] == "error"           # 已写回库


def test_dense_script_detection_covers_kana_and_hangul():
    """汉字、假名、谚文都按密集文字算截断上限与成本；拉丁文不算。"""
    assert dossier._dense("これは日本語の文章です。" * 100)
    assert dossier._dense("이것은 한국어 문장입니다. " * 100)
    assert dossier._dense("这是一本中文书。" * 100)
    assert not dossier._dense("This is an English book. " * 100)
