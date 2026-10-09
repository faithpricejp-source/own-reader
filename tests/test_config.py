"""公开版新增的配置层：config.conf 读取、默认后端与回退、类目表覆盖。"""
from __future__ import annotations

import json
import os

import app
import config
import llm
from test_regressions_a import call_handler


def test_config_file_sets_defaults_but_env_wins(tmp_path, monkeypatch):
    f = tmp_path / "c.conf"
    f.write_text("# comment\nOWN_READER_TEST_A = 1\nOWN_READER_TEST_B='two'\nnot a pair\n")
    monkeypatch.setenv("OWN_READER_TEST_A", "env")
    monkeypatch.delenv("OWN_READER_TEST_B", raising=False)
    config.load(f)
    assert os.environ["OWN_READER_TEST_A"] == "env" and os.environ["OWN_READER_TEST_B"] == "two"
    monkeypatch.delenv("OWN_READER_TEST_B")


def test_builtin_defaults():
    assert llm.DEFAULT_BACKEND == "local" and llm.FALLBACK_BACKEND == ""
    assert llm.LOCAL_URL == "http://127.0.0.1:8080/v1/chat/completions"
    assert llm.DATA_DIR / "reader.sqlite" == app.DB_PATH  # AUDIT_DB 本身被 conftest 换成临时库


def _ask_setup(cal, monkeypatch, fail: set[str]):
    cal.add(1, "书一", ["甲"])
    app.init_db()
    calls = []

    def fake_ask(backend, messages, sensitivity=None, caller=None):
        calls.append(backend)
        if backend in fail:
            raise RuntimeError("down")
        return "答", f"m-{backend}", 5
    monkeypatch.setattr(llm, "ask", fake_ask)
    return calls


def test_ask_uses_default_backend(cal, monkeypatch):
    calls = _ask_setup(cal, monkeypatch, set())
    status, out = call_handler("POST", "/api/ask", {"book_id": 1, "selection": "s"})
    assert status == 200 and json.loads(out)["backend"] == "local" and calls == ["local"]


def test_ask_falls_back_only_when_configured(cal, monkeypatch):
    calls = _ask_setup(cal, monkeypatch, {"local"})
    status, _ = call_handler("POST", "/api/ask", {"book_id": 1, "selection": "s"})
    assert status == 502 and calls == ["local"]
    monkeypatch.setattr(llm, "FALLBACK_BACKEND", "claude")
    calls.clear()
    status, out = call_handler("POST", "/api/ask", {"book_id": 1, "selection": "s"})
    body = json.loads(out)
    assert status == 200 and body["backend"] == "claude" and calls == ["local", "claude"] and body["note"]


def test_taxonomy_override(tmp_path, monkeypatch):
    import library_class
    f = tmp_path / "tax.json"
    f.write_text(json.dumps({"taxonomy": {"数学": ["分析", "代数"]}, "subjects": ["数学"]}, ensure_ascii=False))
    monkeypatch.setenv("OWN_READER_TAXONOMY", str(f))
    tax, subjects = library_class._load_taxonomy()
    assert tax == {"数学": ("分析", "代数")} and subjects == {"数学"}
    monkeypatch.delenv("OWN_READER_TAXONOMY")
    assert "商业与投资" in library_class._load_taxonomy()[0]
