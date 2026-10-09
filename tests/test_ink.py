"""手写批注：安卓原生版在书页上手写 → ink 事件 → 按配置的引擎识别成文字 → 网页版显示。"""
from __future__ import annotations

import json
import struct
import zlib

import app
import ink

STROKES = [[0, 0, 0.5, 20, 5, 0.6, 40, 0, 0.5], [10, 20, 0.4, 30, 40, 0.7]]
XP = {"pos": "/body/DocFragment[2]/body/div/p[3]/text().5", "pos_kind": "crengine"}


def _ink(local_id="ink-1", book=5):
    return app.add_event("android-x", book, "ink", None, None,
                         {**XP, "ink_id": local_id, "strokes": STROKES, "context": "大臣要求全部告诉我", "chapter": "第6章"})


def test_book_state_lists_ink_with_strokes_and_context(events):
    eid = _ink()
    inks = app.book_state(5)["inks"]
    assert len(inks) == 1
    k = inks[0]
    assert k["id"] == eid and k["ink_id"] == "ink-1" and k["strokes"] == STROKES
    assert k["context"] == "大臣要求全部告诉我" and k["pos"] == XP["pos"] and k["recognized"] is None


def test_recognized_text_attaches_by_ink_id_latest_wins(events):
    _ink()
    app.add_event("ink", 5, "note", None, "恶意执行", {"ink_id": "ink-1", "source": "openai"})
    assert app.book_state(5)["inks"][0]["recognized"] == "恶意执行"
    app.add_event("android-x", 5, "note", None, "恶意服从", {"ink_id": "ink-1", "source": "manual"})
    k = app.book_state(5)["inks"][0]
    assert k["recognized"] == "恶意服从" and k["recognized_source"] == "manual"
    # 手写的识别文字不算普通划线评注
    assert app.book_state(5)["highlights"] == []


def test_delete_by_ink_id_removes_ink(events):
    _ink("a")
    _ink("b")
    app.add_event("android-x", 5, "delete", None, None, {"ink_id": "a"})
    assert [k["ink_id"] for k in app.book_state(5)["inks"]] == ["b"]


def test_pending_lists_only_unrecognized(events):
    _ink("a")
    _ink("b")
    app.add_event("ink", 5, "note", None, "识别过了", {"ink_id": "a", "source": "openai"})
    assert [p["ink_id"] for p in ink.pending()] == ["b"]
    app.add_event("android-x", 5, "delete", None, None, {"ink_id": "b"})
    assert ink.pending() == []


def _png_corner_and_min(path):
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    pos, w, h, raw = 8, None, None, b""
    while pos + 8 <= len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        tag, chunk = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", chunk[:8])
        elif tag == b"IDAT":
            raw += chunk
        elif tag == b"IEND":
            break
        pos += 12 + n
    scan = zlib.decompress(raw)
    stride = w * 3
    corner = scan[1:4]
    darkest = 255
    for y in range(h):
        row = scan[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
        darkest = min(darkest, min(row[i] for i in range(0, len(row), 3)))
    return corner, darkest


def test_render_png_is_white_with_black_strokes(tmp_path):
    out = ink.render_png(STROKES, tmp_path / "x.png")
    corner, darkest = _png_corner_and_min(out)
    assert corner == b"\xff\xff\xff" and darkest < 80


def test_recognize_one_writes_note_event(events, monkeypatch):
    _ink()
    seen = {}

    def fake_engine(png, context):
        seen["context"] = context
        return "恶意执行", "test-engine"

    monkeypatch.setattr(ink, "_engine", lambda: fake_engine)
    assert ink.recognize_pending() == 1
    k = app.book_state(5)["inks"][0]
    assert k["recognized"] == "恶意执行" and k["recognized_source"] == "test-engine"
    assert seen["context"] == "大臣要求全部告诉我"
    assert ink.recognize_pending() == 0  # 不重复识别


def test_engine_name_defaults_off_and_accepts_only_configured_values(monkeypatch):
    monkeypatch.delenv("OWN_READER_INK_ENGINE", raising=False)
    assert ink.engine_name() == "off"
    monkeypatch.setenv("OWN_READER_INK_ENGINE", "openai")
    assert ink.engine_name() == "openai"
    monkeypatch.setenv("OWN_READER_INK_ENGINE", "Claude")
    assert ink.engine_name() == "claude"
    monkeypatch.setenv("OWN_READER_INK_ENGINE", "local")
    assert ink.engine_name() == "off"


def test_off_engine_stores_ink_but_does_not_recognize(events, monkeypatch):
    monkeypatch.delenv("OWN_READER_INK_ENGINE", raising=False)
    _ink()
    assert ink.recognize_pending() == 0
    assert app.book_state(5)["inks"][0]["recognized"] is None


def test_openai_engine_posts_data_uri(tmp_path, monkeypatch):
    png = ink.render_png(STROKES, tmp_path / "x.png")
    monkeypatch.setenv("OWN_READER_INK_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OWN_READER_INK_MODEL", "vision-test")
    import io
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        body = json.dumps({"model": "vision-test", "choices": [{"message": {"content": "转写"}}]}).encode()
        r = io.BytesIO(body)
        r.__enter__ = lambda: r
        r.__exit__ = lambda *a: False
        return r

    monkeypatch.setattr(ink.urllib.request, "urlopen", fake_urlopen)
    text, source = ink._openai(png, "旁边的原文")
    assert text == "转写" and source == "vision-test"
    assert seen["url"] == "http://127.0.0.1:9/v1/chat/completions"
    part = seen["body"]["messages"][0]["content"][0]["image_url"]["url"]
    assert part.startswith("data:image/png;base64,")
    assert seen["body"]["model"] == "vision-test"


def test_claude_engine_uses_configured_model(tmp_path, monkeypatch):
    png = ink.render_png(STROKES, tmp_path / "x.png")
    monkeypatch.setenv("OWN_READER_INK_CLAUDE_MODEL", "claude-haiku-5-5")
    seen = {}

    def fake_api_call(system, user, *, model, effort):
        seen["model"] = model
        seen["user"] = user
        return "转写", model, 0.0

    monkeypatch.setattr(ink.llm, "api_call", fake_api_call)
    text, source = ink._claude(png, "旁边的原文")
    assert text == "转写" and source == "claude-haiku-5-5"
    assert seen["model"] == "claude-haiku-5-5"
    assert seen["user"][0]["type"] == "image" and seen["user"][0]["source"]["media_type"] == "image/png"
