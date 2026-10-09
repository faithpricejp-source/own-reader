"""server/translate.py：分块、解析、语言判断、缓存与后端回退。"""
from __future__ import annotations

import time

import llm
import translate


def test_lang():
    assert translate.lang(["This is an English paragraph about the brain."]) == "en"
    assert translate.lang(["これは日本語の文章です。ひらがなとカタカナがたくさんあります。とても長い文章になりますね。"]) == "ja"
    assert translate.lang(["这是一本中文书，不用翻译。"]) is None
    assert translate.lang([]) is None


def test_blocks_first_small_then_big():
    paras = ["word " * 100] * 8
    b = translate.blocks(paras)
    assert len(b[0]) == 2 and sum(len(x) for x in b) == 8          # 第一块满 120 词就切
    assert all(len(x) <= 5 for x in b[1:])


def test_parse_numbered_and_missing():
    raw = "[1] 第一段\n\n[3] 第三段\n多行也行\n[9] 越界"
    assert translate.parse(raw, 3) == ["第一段", None, "第三段\n多行也行"]


def test_translate_block_falls_back_to_local_for_missing(monkeypatch):
    calls = []

    def fake_free(msgs):
        calls.append("free")
        return "[1] 一\n[3] 三", "ultra"

    def fake_local(msgs):
        calls.append("local")
        assert "[1] b" in msgs[0]["content"] and "[2]" not in msgs[0]["content"]   # 只补缺的那段
        return "[1] 二", "local:translate"
    monkeypatch.setitem(llm.BACKENDS, "mt_free", fake_free)
    monkeypatch.setitem(llm.BACKENDS, "mt_local", fake_local)
    out = translate.translate_block(["a", "b", "c"], "en")
    assert out == [("一", "ultra"), ("二", "local:translate"), ("三", "ultra")] and calls == ["free", "local"]


def test_request_caches_and_skips_chinese(events, monkeypatch):
    monkeypatch.setitem(llm.BACKENDS, "mt_free",
                        lambda m: ("\n".join(f"[{i}] 译{i}" for i in range(1, 4)), "ultra"))
    paras = ["First paragraph here.", "Second paragraph here.", "Third paragraph here."]
    r = translate.request(paras)
    assert r["lang"] == "en" and len(r["hashes"]) == 3
    for _ in range(50):
        if not translate.get(r["hashes"])["pending"]:
            break
        time.sleep(0.05)
    done = translate.get(r["hashes"])["done"]
    assert [done[h]["zh"] for h in r["hashes"]] == ["译1", "译2", "译3"]
    assert translate.request(paras)["pending"] == 0                       # 已缓存不重翻
    assert translate.request(["中文段落，不需要翻译。"]) == {"lang": "zh", "done": {}}


def test_chain_follows_configured_order(monkeypatch):
    calls = []
    monkeypatch.setattr(translate, "CHAIN", ["claude", "mt_free", "grok", "mt_local"])
    for b in ("claude", "grok", "mt_free", "mt_local"):
        monkeypatch.setitem(llm.BACKENDS, b, lambda m, b=b: (calls.append(b), (_ for _ in ()).throw(RuntimeError("x")))[1])
    assert translate.translate_block(["a"], "en") == [None]
    assert calls == ["claude", "mt_free", "grok", "mt_local"]


def test_default_chain_is_free_then_local():
    assert translate.CHAIN == ["mt_free", "mt_local"]
