"""server/gloss.py：原文逐字门槛、分块、段落级缓存不重批、整本切段认 div。"""
from __future__ import annotations

import json
import time
import zipfile

import batchjobs
import gloss

PARAS = ["Sir Humphrey said it was a ‘courageous’ decision, Minister.",
         "Bernard nodded.",
         "The Thirty-Year Rule applies to all these minutes."]


def test_parse_drops_non_verbatim_and_bad_rows():
    raw = "前言\n" + json.dumps([
        {"p": 1, "type": "言外", "quote": "'courageous' decision", "note": "文官话里 courageous = 会丢选票的蠢事", "conf": "高"},
        {"p": 1, "type": "言外", "quote": "brave decision", "note": "原文里没有这个词"},       # 不是逐字原文
        {"p": 3, "type": "背景", "quote": "Thirty-Year  Rule", "note": "档案 30 年解密"},     # 空白差异可容
        {"p": 9, "type": "背景", "quote": "x", "note": "越界"},
        {"p": 2, "type": "闲聊", "quote": "Bernard", "note": "类型不在五类里"},
        {"p": 2, "type": "背景", "quote": "Bernard", "note": ""},
    ]) + "\n结束"
    items, dropped = gloss.parse(raw, PARAS)
    assert [(i, it["quote"]) for i, it in items] == [(0, "'courageous' decision"), (2, "Thirty-Year Rule")]
    assert dropped == 4


def test_blocks_first_small_then_big(monkeypatch):
    monkeypatch.setattr(gloss, "FIRST_CHARS", 100)
    monkeypatch.setattr(gloss, "BLOCK_CHARS", 300)
    b = gloss.blocks(["x" * 60] * 12)
    assert [len(x) for x in b] == [2, 5, 5]


def test_request_caches_and_never_reglosses(events, monkeypatch):
    calls = []

    def fake_ask(backend, messages, sensitivity, caller):
        calls.append((backend, sensitivity))
        assert "读者画像" in messages[0]["content"]
        return json.dumps([{"p": 1, "type": "言外", "quote": "decision, Minister", "note": "会丢选票"}]), "opus", 1
    monkeypatch.setattr(gloss.llm, "ask", fake_ask)
    r = gloss.request(PARAS, "Yes Minister", 7)
    for _ in range(100):
        if not gloss.get(r["hashes"])["pending"]:
            break
        time.sleep(0.02)
    done = gloss.get(r["hashes"])["done"]
    assert done[r["hashes"][0]][0]["note"] == "会丢选票"
    assert done[r["hashes"][1]] == [] and done[r["hashes"][2]] == []    # 批过但没可批的也算完成
    assert calls == [("claude_api", "personal")]                          # 画像是个人信息，不走免费档
    assert gloss.request(PARAS, "Yes Minister", 7)["pending"] == 0       # 永不重批
    assert len(calls) == 1
    assert gloss.seen_terms(7) == []                                      # 只收背景/释义类
    assert gloss.profile().startswith("# 读者画像")


def test_failure_leaves_paras_retryable(events, monkeypatch):
    monkeypatch.setattr(gloss.llm, "ask", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("weekly limit")))
    r = gloss.request(PARAS[:1], "t", 1)
    for _ in range(100):
        if not gloss.get(r["hashes"])["pending"]:
            break
        time.sleep(0.02)
    assert gloss.get(r["hashes"])["done"] == {} and "limit" in gloss.get(r["hashes"])["error"]


def _epub(tmp_path, body):
    epub = tmp_path / "b.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
        z.writestr("c.opf", '<package><manifest><item id="a" href="a.xhtml"/></manifest>'
                            '<spine><itemref idref="a"/></spine></package>')
        z.writestr("a.xhtml", f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{body}</body></html>')
    return epub


def test_whole_book_batch_submit_pending_collect(events, tmp_path, monkeypatch):
    epub = _epub(tmp_path, "<p>" + "</p><p>".join(PARAS) + "</p>")
    sent = {}

    def fake_submit(items, model, effort, est_usd):
        sent["items"] = items
        return "msgbatch_1"
    monkeypatch.setattr(batchjobs.llm, "batch_submit", fake_submit)
    monkeypatch.setattr(batchjobs, "ensure_poller", lambda bid: None)
    st = gloss.start_book(9, epub, "Yes Minister")
    assert st["running"] and st["batch_blocks"] == 1 and "读者画像" in sent["items"][0][1]
    # 在途批次里的段，网页版实时请求不重复派
    monkeypatch.setattr(gloss.llm, "ask", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该实时批")))
    r = gloss.request(PARAS, "Yes Minister", 9)
    assert r["pending"] == 0 and gloss.get(r["hashes"])["pending"] == 3
    assert gloss.start_book(9, epub, "Yes Minister")["batch_blocks"] == 1     # 不重复提交
    cid = sent["items"][0][0]
    monkeypatch.setattr(batchjobs.llm, "batch_status", lambda bid: {"status": "ended"})
    monkeypatch.setattr(batchjobs.llm, "batch_results", lambda bid: iter([(cid, json.dumps(
        [{"p": 3, "type": "背景", "quote": "Thirty-Year Rule", "note": "30 年解密"}]), None)]))
    assert batchjobs.collect("msgbatch_1") == "ended"
    done = gloss.get(r["hashes"])
    assert done["pending"] == 0 and done["done"][r["hashes"][2]][0]["note"] == "30 年解密"
    assert gloss.book_status(9, epub) | {} == {"total": 3, "done": 3, "running": False, "batch_blocks": 0, "error": None}


def test_paragraphs_include_leaf_divs(tmp_path):
    epub = tmp_path / "b.epub"
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
        z.writestr("c.opf", '<package><manifest><item id="a" href="a.xhtml"/></manifest>'
                            '<spine><itemref idref="a"/></spine></package>')
        z.writestr("a.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                              '<div class="paragraph">Calibre style   paragraph.</div>'
                              '<div><p>Real p.</p><p>Second p.</p></div><p>x</p></body></html>')
    assert gloss.paragraphs(epub) == ["Calibre style paragraph.", "Real p.", "Second p."]
