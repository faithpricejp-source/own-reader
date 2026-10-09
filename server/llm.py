"""AI backends for reading Q&A. Each backend takes chat messages and returns (answer, model).

free   — OpenRouter :free models, tried in FREE_MODELS order. Key read from OWN_READER_OPENROUTER_KEY_FILE.
local  — any OpenAI-compatible server (llama.cpp / LM Studio / Ollama / vLLM …) at OWN_READER_LOCAL_LLM_URL.
claude — the official `claude -p` CLI in headless mode; no tools except web search.
claude_api — the Claude API via the official `anthropic` SDK (key file OWN_READER_ANTHROPIC_KEY_FILE), with a local
         spend ledger and monthly cap (OWN_READER_API_MONTHLY_CAP); also Message Batches for whole-book jobs.
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


# ---- Claude API（claude_api 后端；精读批注、全书/每章导读、整本批量翻译用）----
# 官方 SDK（pip install anthropic），调用时才导入：SDK 没装或依赖坏了只让这个后端失败，服务器照常起，调用方可回落。
# key 从 OWN_READER_ANTHROPIC_KEY_FILE 读（文件里只放 key 一行）。系统提示打 cache_control。
# 每次调用按挂牌价把估算花费记进本地账本 api_spend，当月超过 API_MONTHLY_CAP 就拒绝调用。
# 这是本地估算，不是官方账单（API 没有查余额的接口）；价格按 2026-10 的官方价目表，变了就改 API_PRICE。
API_KEY_FILE = Path(os.environ.get("OWN_READER_ANTHROPIC_KEY_FILE", "~/.config/anthropic/api-key.txt")).expanduser()
API_MODEL = os.environ.get("OWN_READER_API_MODEL", "claude-opus-5-5")
API_PRICE = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0),
             "claude-haiku-5-5": (0.10, 0.50)}  # 每百万 token 美元：输入、输出；缓存写 1.25×，读按各型号表
API_CACHE_READ = {"claude-opus-5-5": 0.20, "claude-sonnet-5-5": 0.20}  # 每百万；未列的按 0.1× 输入价
API_MONTHLY_CAP = float(os.environ.get("OWN_READER_API_MONTHLY_CAP", "20"))  # 美元/月，按本地账本估算


def _spend_db():
    import sqlite3
    c = sqlite3.connect(AUDIT_DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS api_spend(id INTEGER PRIMARY KEY AUTOINCREMENT, month TEXT, "
              "ts TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), model TEXT, usd REAL, usage TEXT)")
    return c


def api_spent(month: str | None = None) -> float:
    try:
        with _spend_db() as c:
            r = c.execute("SELECT COALESCE(SUM(usd),0) FROM api_spend WHERE month=?",
                          (month or time.strftime("%Y-%m"),)).fetchone()
        return float(r[0])
    except Exception:  # noqa: BLE001
        return float("inf")  # 账本读不了就当已超限，宁可不调


def api_cost(model: str, u: dict) -> float:
    """一次调用的估算美元。u 是 usage 字典；含 advisor 时按 iterations 分型号计。"""
    its = u.get("iterations") or [dict(u, model=model)]
    total = 0.0
    for it in its:
        m = it.get("model") or model
        pin, pout = API_PRICE.get(m, (5.0, 25.0))
        cr = API_CACHE_READ.get(m, pin * 0.1)
        total += (it.get("input_tokens", 0) * pin + it.get("cache_creation_input_tokens", 0) * pin * 1.25
                  + it.get("cache_read_input_tokens", 0) * cr + it.get("output_tokens", 0) * pout) / 1e6
    return total


def api_call(system: str, user: str, *, model: str = API_MODEL, effort: str = "medium",
             tools: list | None = None, betas: list | None = None) -> tuple[str, str, float]:
    """返回 (正文, 模型, 估算美元)。超当月上限、拒答、截断、空答都抛 BackendError。"""
    spent = api_spent()
    if spent >= API_MONTHLY_CAP:
        raise BackendError(f"本月 API 估算花费 {spent:.2f} 美元，已到上限 {API_MONTHLY_CAP:.0f}，停用到下月")
    try:
        import anthropic
    except Exception as e:  # noqa: BLE001
        raise BackendError(f"anthropic SDK 导入失败：{e}")
    client = anthropic.Anthropic(api_key=API_KEY_FILE.read_text().strip(), timeout=600)
    kw = dict(model=model, max_tokens=16000, output_config={"effort": effort},
              system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
              messages=[{"role": "user", "content": user}])
    try:
        if tools or betas:
            resp = client.beta.messages.create(**kw, tools=tools or [], betas=betas or [])
        else:
            resp = client.messages.create(**kw)
    except anthropic.RateLimitError as e:
        raise BackendError(f"claude api 429：{e}")
    except anthropic.APIStatusError as e:
        raise BackendError(f"claude api {e.status_code}：{str(e)[:300]}")
    except anthropic.APIConnectionError as e:
        raise BackendError(f"claude api 连不上：{e}")
    u = resp.usage.model_dump()
    usd = api_cost(resp.model, u)
    with _spend_db() as c:
        c.execute("INSERT INTO api_spend(month, model, usd, usage) VALUES (?,?,?,?)",
                  (time.strftime("%Y-%m"), resp.model, usd, json.dumps(u, default=str)))
    if resp.stop_reason == "refusal":
        raise BackendError(f"claude api refusal: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        raise BackendError("claude api: 输出被 max_tokens 截断")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise BackendError(f"claude api: empty answer, stop_reason={resp.stop_reason}")
    return text, resp.model, usd


# ---- Message Batches：整本预跑这类不急的活，五折价，通常 1 小时内出结果，最长 24 小时 ----
BATCH_DISCOUNT = 0.5


def _client():
    try:
        import anthropic
    except Exception as e:  # noqa: BLE001
        raise BackendError(f"anthropic SDK 导入失败：{e}")
    return anthropic.Anthropic(api_key=API_KEY_FILE.read_text().strip(), timeout=600)


def batch_submit(items: list[tuple[str, str, str]], *, model: str = API_MODEL, effort: str = "medium",
                 est_usd: float = 0.0) -> str:
    """items = [(custom_id, system, user)]。预估花费超当月剩余额度就拒绝提交。返回 batch id。"""
    spent = api_spent()
    if spent + est_usd > API_MONTHLY_CAP:
        raise BackendError(f"本月已花 {spent:.2f} 美元，这批预估 {est_usd:.2f}，会超上限 {API_MONTHLY_CAP:.0f}")
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    reqs = [Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
        model=model, max_tokens=16000, output_config={"effort": effort},
        # 批内请求并发、乱序处理，用 1 小时缓存提高共享系统提示的命中率
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        messages=[{"role": "user", "content": user}])) for cid, system, user in items]
    return _client().messages.batches.create(requests=reqs).id


def batch_status(batch_id: str) -> dict:
    b = _client().messages.batches.retrieve(batch_id)
    return {"status": b.processing_status, **b.request_counts.model_dump()}


def batch_results(batch_id: str):
    """逐条产出 (custom_id, 正文或 None, 错误或 None)，并把每条花费按五折记账。"""
    rows = []
    client = _client()  # 必须在整个流式读取期间持有：临时对象被回收会关掉连接（实测会 EBADF）
    for r in client.messages.batches.results(batch_id):
        res = r.result
        if res.type != "succeeded":
            err = getattr(getattr(res, "error", None), "error", None)
            yield r.custom_id, None, f"{res.type}: {getattr(err, 'type', '')}"
            continue
        msg = res.message
        u = msg.usage.model_dump()
        rows.append((time.strftime("%Y-%m"), msg.model + ":batch", api_cost(msg.model, u) * BATCH_DISCOUNT,
                     json.dumps(u, default=str)))
        if msg.stop_reason in ("refusal", "max_tokens"):
            yield r.custom_id, None, f"stop_reason={msg.stop_reason}"
            continue
        yield r.custom_id, "".join(b.text for b in msg.content if b.type == "text").strip() or None, None
    if rows:
        with _spend_db() as c:
            c.executemany("INSERT INTO api_spend(month, model, usd, usage) VALUES (?,?,?,?)", rows)


def ask_claude_api(messages: list[dict]) -> tuple[str, str]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
    text, model, _ = api_call(system, user)
    return text, model


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


BACKENDS = {"free": ask_free, "local": ask_local, "claude": ask_claude, "claude_api": ask_claude_api,
            "grok": ask_grok, "kimi": ask_kimi, "gemini": ask_gemini}


# 按数据敏感度决定能交给哪个后端（强制拦截）。public=公开材料（书的正文）；personal=用户的笔记/提问/阅读观看记录；
# private=健康、财务、家人、账号类，只许本机模型。按自己的信任边界改这张表。
POLICY_DRAFT = {
    "public": {"free", "local", "claude", "claude_api", "grok", "kimi", "gemini"},
    "personal": {"local", "claude", "claude_api", "grok", "kimi"},
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
