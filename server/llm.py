"""AI backends for reading Q&A. Each backend takes chat messages and returns (answer, model).

free   — OpenRouter :free models, tried in FREE_MODELS order. Key read from OWN_READER_OPENROUTER_KEY_FILE.
local  — any OpenAI-compatible server (llama.cpp / LM Studio / Ollama / vLLM …) at OWN_READER_LOCAL_LLM_URL.
claude — the official `claude -p` CLI in headless mode; no tools except web search.
grok / kimi / gemini — those vendors' official CLIs in non-interactive mode (grok -p, kimi -p, agy -p), run in an
         empty working dir. One user-triggered question per call. Every CLI path can be overridden by env var.
No API keys live in this repo; each backend uses whatever credentials its own CLI / key file already has.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402,F401

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY_FILE = Path(os.environ.get("OWN_READER_OPENROUTER_KEY_FILE", "~/.config/openrouter/api-key.txt")).expanduser()
FREE_MODELS = [m.strip() for m in os.environ.get(
    "OWN_READER_FREE_MODELS",
    "nvidia/nemotron-3-super-120b-a12b:free,google/gemma-4-31b-it:free,openrouter/free").split(",") if m.strip()]
# OpenAI-compatible base URL, e.g. http://127.0.0.1:8080/v1 (llama.cpp) or http://127.0.0.1:11434/v1 (Ollama)
LOCAL_BASE = os.environ.get("OWN_READER_LOCAL_LLM_URL", "http://127.0.0.1:8080/v1").rstrip("/")
LOCAL_URL = LOCAL_BASE + "/chat/completions"
DEFAULT_BACKEND = os.environ.get("OWN_READER_DEFAULT_BACKEND", "local")
FALLBACK_BACKEND = os.environ.get("OWN_READER_FALLBACK_BACKEND", "")


def _bin(env: str, name: str, fallback: str) -> str:
    """CLI path: env var > on PATH > the vendor's usual install location."""
    return os.environ.get(env) or shutil.which(name) or str(Path(fallback).expanduser())


CLAUDE_BIN = _bin("OWN_READER_CLAUDE_BIN", "claude", "~/.local/bin/claude")
GROK_BIN = _bin("OWN_READER_GROK_BIN", "grok", "~/.grok/bin/grok")
KIMI_BIN = _bin("OWN_READER_KIMI_BIN", "kimi", "~/.kimi-code/bin/kimi")
AGY_BIN = _bin("OWN_READER_AGY_BIN", "agy", "~/.local/bin/agy")


class BackendError(RuntimeError):
    pass


def _post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _content(resp: dict) -> str:
    try:
        text = resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise BackendError(f"unexpected response: {json.dumps(resp)[:300]}")
    if not text.strip():
        raise BackendError("empty answer")
    return text.strip()


def ask_free(messages: list[dict]) -> tuple[str, str]:
    key = OPENROUTER_KEY_FILE.read_text().strip()
    errors = []
    for model in FREE_MODELS:
        try:
            resp = _post_json(OPENROUTER_URL,
                              {"model": model, "messages": messages, "reasoning": {"effort": "low"}},
                              {"Authorization": f"Bearer {key}", "X-Title": "own-reader"}, timeout=120)
            return _content(resp), resp.get("model", model)
        except Exception as e:  # noqa: BLE001 — try the next free model
            errors.append(f"{model}: {e}")
    raise BackendError("all free models failed: " + " | ".join(errors))


def ask_local(messages: list[dict]) -> tuple[str, str]:
    model = os.environ.get("OWN_READER_LOCAL_LLM_MODEL")
    if not model:
        with urllib.request.urlopen(LOCAL_BASE + "/models", timeout=10) as r:
            model = json.load(r)["data"][0]["id"]
    resp = _post_json(LOCAL_URL, {"model": model, "messages": messages, "max_tokens": 2000},
                      {}, timeout=300)
    return _content(resp), model


def ask_claude(messages: list[dict]) -> tuple[str, str]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    prompt = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
    # --tools 只决定有哪些工具；dontAsk 模式下还得 --allowedTools 放行，否则 WebSearch 被拒（2026-10-06 实测）
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--tools", "WebSearch", "--allowedTools", "WebSearch",
           "--permission-mode", "dontAsk", "--strict-mcp-config", "--no-session-persistence",
           "--system-prompt", system]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300,
                       cwd="/tmp")
    if p.returncode != 0:
        raise BackendError(f"claude exit {p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    out = json.loads(p.stdout)
    if out.get("is_error") or not (out.get("result") or "").strip():
        raise BackendError(f"claude error: {p.stdout[-400:]}")
    model = next(iter(out.get("modelUsage") or {}), "claude")
    return out["result"].strip(), model


DATA_DIR = Path(os.environ.get("OWN_READER_DATA", "~/Library/Application Support/own-reader")).expanduser()
CLI_CWD = DATA_DIR / "cli-cwd"


def _run_cli(cmd: list[str], timeout: int = 420) -> str:
    """Run a subscription CLI in an empty working dir so its file tools have nothing to touch."""
    CLI_CWD.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=CLI_CWD)
    if p.returncode != 0:
        raise BackendError(f"{Path(cmd[0]).name} exit {p.returncode}: {(p.stderr or p.stdout)[-400:]}")
    return p.stdout


def _flat_prompt(messages: list[dict]) -> str:
    return "\n\n".join(m["content"] for m in messages)


def ask_grok(messages: list[dict]) -> tuple[str, str]:
    out = _run_cli([GROK_BIN, "-p", _flat_prompt(messages),
                    "--cwd", str(CLI_CWD), "--output-format", "plain", "--permission-mode", "dontAsk"])
    if not out.strip():
        raise BackendError("grok: empty answer")
    return out.strip(), "grok"


def ask_kimi(messages: list[dict]) -> tuple[str, str]:
    out = _run_cli([KIMI_BIN, "-p", _flat_prompt(messages),
                    "--output-format", "stream-json"])
    answer = ""
    for line in out.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("role") == "assistant":
            c = msg.get("content")
            text = c if isinstance(c, str) else "".join(
                p.get("text", "") for p in c or [] if isinstance(p, dict) and p.get("type") == "text")
            if text.strip():
                answer = text
    if not answer.strip():
        raise BackendError(f"kimi: no answer in output: {out[-300:]}")
    return answer.strip(), "kimi-k3"


AGY_MODEL = os.environ.get("OWN_READER_AGY_MODEL", "gemini-3.8-flash-high")


def ask_gemini(messages: list[dict]) -> tuple[str, str]:
    out = _run_cli([AGY_BIN, "-p", _flat_prompt(messages),
                    "--model", AGY_MODEL, "--output-format", "text", "--print-timeout", "400s"])
    if not out.strip():
        raise BackendError("agy: empty answer")
    return out.strip(), AGY_MODEL


BACKENDS = {"free": ask_free, "local": ask_local, "claude": ask_claude,
            "grok": ask_grok, "kimi": ask_kimi, "gemini": ask_gemini}


# 按数据敏感度决定能交给哪个后端（强制拦截）。public=公开材料（书的正文）；personal=用户的笔记/提问/阅读观看记录；
# private=健康、财务、家人、账号类，只许本机模型。按自己的信任边界改这张表。
POLICY_DRAFT = {
    "public": {"free", "local", "claude", "grok", "kimi", "gemini"},
    "personal": {"local", "claude", "grok", "kimi"},
    "private": {"local"},
}
ENFORCE = True
AUDIT_DB = DATA_DIR / "reader.sqlite"  # 与 app.DB_PATH 同一个库


def _audit(row: tuple) -> None:
    """每次模型调用记一行审计（不存 prompt 原文，只存长度与元数据）。"""
    import sqlite3
    try:
        with sqlite3.connect(AUDIT_DB, timeout=10) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS llm_calls(id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), caller TEXT, backend TEXT,
                model TEXT, sensitivity TEXT, policy TEXT, chars_in INTEGER, chars_out INTEGER,
                latency_ms INTEGER, ok INTEGER, error TEXT)""")
            c.execute("""INSERT INTO llm_calls(caller,backend,model,sensitivity,policy,chars_in,chars_out,latency_ms,ok,error)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""", row)
    except Exception as e:  # noqa: BLE001 — 审计失败不能挡住调用，但要留痕
        import sys
        sys.stderr.write(f"llm audit failed: {e}\n")


def ask(backend: str, messages: list[dict], *, sensitivity: str = "personal",
        caller: str = "unknown") -> tuple[str, str, int]:
    if backend not in BACKENDS:
        raise BackendError(f"unknown backend {backend}")
    if sensitivity not in POLICY_DRAFT:
        raise BackendError(f"unknown sensitivity {sensitivity}")
    allowed = backend in POLICY_DRAFT[sensitivity]
    policy = "allowed" if allowed else ("blocked" if ENFORCE else "would_block")
    chars_in = sum(len(m["content"]) for m in messages)
    if not allowed and ENFORCE:
        _audit((caller, backend, None, sensitivity, policy, chars_in, 0, 0, 0, "blocked by policy"))
        raise BackendError(f"{sensitivity} 数据按规则不能发给 {backend}")
    t0 = time.monotonic()
    try:
        answer, model = BACKENDS[backend](messages)
    except Exception as e:
        _audit((caller, backend, None, sensitivity, policy, chars_in, 0,
                int((time.monotonic() - t0) * 1000), 0, str(e)[:300]))
        raise
    ms = int((time.monotonic() - t0) * 1000)
    _audit((caller, backend, model, sensitivity, policy, chars_in, len(answer), ms, 1, None))
    return answer, model, ms
