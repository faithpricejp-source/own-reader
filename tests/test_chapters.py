"""server/chapters.py：短章合并、本章导读与逐段批注互不覆盖。"""
from __future__ import annotations

import chapters
import gloss
from translate import h


def test_merge_short_folds_toc_and_front_matter():
    paras = ["Contents", "1 ONE", "2 TWO"] + ["x" * 800] * 4 + ["Two"] + ["y" * 800] * 3
    chs = [{"title": "Contents", "start": 0}, {"title": "1 ONE", "start": 1}, {"title": "2 TWO", "start": 2},
           {"title": "One", "start": 3}, {"title": "Two", "start": 7}]
    out = chapters._merge_short(chs, paras)
    assert [c["start"] for c in out] == [3, 7] and out[0]["title"] == "One"   # 章首留在章标题，不前移到目录


def test_chapter_guide_survives_paragraph_gloss(events):
    hp = h("Open Government")
    chapters._store_guide(hp, "### 这一章在干什么\n新大臣被驯化。", "opus", 1)
    gloss._store(["Open Government"], [(0, {"type": "背景", "quote": "Open", "note": "口号", "conf": "高"})], "opus", 1)
    items = gloss.cached([hp])[hp]
    assert {it["type"] for it in items} == {"本章", "背景"}
    gloss._store(["Open Government"], [], "opus", 1)            # 重批段落不删本章导读
    assert [it["type"] for it in gloss.cached([hp])[hp]] == ["本章"]
