"""按需翻译：外文书读到哪翻到哪，译文插在原文段落下面。

- 默认主力：OpenRouter 免费档（OWN_READER_TRANSLATE_MODEL，默认 nemotron-3-ultra :free）。
- 默认兜底：本机 llama.cpp 跑一个翻译模型（OWN_READER_MT_GGUF 指定 GGUF 文件；按需拉起、闲置 30 分钟自动关）。
  小模型偶尔会译错数字，界面会标「本机模型翻译，数字请对照原文」。
- 后端顺序可用 OWN_READER_TRANSLATE_CHAIN 改。
- 段落级缓存（按原文 sha1），同一段永不重翻；书本文是公开材料（sensitivity=public）。
- 一节（EPUB 的一个 section）拆成小块并发翻：第一块很小，先出第一屏；其余每块约 BLOCK_WORDS 词。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import app
import llm

ULTRA = os.environ.get("OWN_READER_TRANSLATE_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
MT_GGUF = Path(os.environ.get("OWN_READER_MT_GGUF", "~/models/translate.gguf")).expanduser()
LLAMA_SERVER = os.environ.get("OWN_READER_LLAMA_SERVER", "llama-server")
MT_PORT = int(os.environ.get("OWN_READER_MT_PORT", "8131"))
MT_IDLE = 1800
FIRST_WORDS = 120       # 第一块：先出第一屏
BLOCK_WORDS = 450
WORKERS = 4             # OpenRouter 免费档 20 次/分钟
SCHEMA = """CREATE TABLE IF NOT EXISTS translations(
  h TEXT PRIMARY KEY, src TEXT, zh TEXT, model TEXT, ts TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"""

PROMPT = {
    "en": "将下面的英文书籍段落翻译成简体中文。",
    "ja": "将下面的日文书籍段落翻译成简体中文。",
    "other": "将下面的外文书籍段落翻译成简体中文。",
}
RULES = """要求：
1. 每段以 [编号] 开头，译文保持同样的编号，一段对一段，不合并、不拆分、不遗漏、不概括、不添加。
2. 数字、年份、人名、术语照原文准确翻译；人名地名可保留原文在括号里。
3. 只输出译文，不要任何解释。"""


def h(text: str) -> str:
    return hashlib.sha1(text.strip().encode()).hexdigest()


def lang(paras: list[str]) -> str | None:
    """None = 中文书不用翻。"""
    s = "".join(paras)[:4000]
    if not s:
        return None
    kana = len(re.findall(r"[぀-ヿ]", s))
    han = len(re.findall(r"[一-鿿]", s))
    if kana > 20:
        return "ja"
    if han > len(s) * 0.3:
        return None
    return "en" if len(re.findall(r"[A-Za-z]", s)) > len(s) * 0.5 else "other"


def cached(hashes: list[str]) -> dict[str, dict]:
    with app.db() as c:
        c.execute(SCHEMA)
        out = {}
        for i in range(0, len(hashes), 500):
            part = hashes[i:i + 500]
            for r in c.execute(f"SELECT h, zh, model FROM translations WHERE h IN ({','.join('?' * len(part))})", part):
                out[r["h"]] = {"zh": r["zh"], "local": r["model"].startswith("local:")}
    return out


def blocks(paras: list[str]) -> list[list[str]]:
    out, cur, words, limit = [], [], 0, FIRST_WORDS
    for p in paras:
        cur.append(p)
        words += len(p.split())
        if words >= limit:
            out.append(cur)
            cur, words, limit = [], 0, BLOCK_WORDS
    if cur:
        out.append(cur)
    return out


def parse(raw: str, n: int) -> list[str | None]:
    got: dict[int, str] = {}
    for m in re.finditer(r"^\s*\[(\d+)\]\s*(.*?)(?=^\s*\[\d+\]|\Z)", raw, re.S | re.M):
        i = int(m.group(1))
        if 1 <= i <= n and m.group(2).strip():
            got[i] = m.group(2).strip()
    return [got.get(i + 1) for i in range(n)]


def _messages(paras: list[str], language: str) -> list[dict]:
    body = "\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(paras))
    return [{"role": "user", "content": f"{PROMPT[language]}\n{RULES}\n\n{body}"}]


def mt_free(messages: list[dict]) -> tuple[str, str]:
    key = llm.OPENROUTER_KEY_FILE.read_text().strip()
    resp = llm._post_json(llm.OPENROUTER_URL, {"model": ULTRA, "messages": messages, "temperature": 0,
                                               "max_tokens": 6000, "reasoning": {"effort": "low"}},
                          {"Authorization": f"Bearer {key}", "X-Title": "own-reader"}, timeout=180)
    return llm._content(resp), ULTRA


_local_lock = threading.Lock()
_local_used = 0.0
_reaper_started = False


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", MT_PORT)) == 0


def _ensure_local() -> None:
    global _reaper_started
    with _local_lock:
        if not _reaper_started:
            _reaper_started = True
            threading.Thread(target=_idle_reaper, daemon=True).start()
        if _port_open():
            return
        subprocess.Popen(
            [LLAMA_SERVER, "-m", str(MT_GGUF), "--port", str(MT_PORT),
             "-c", "16384", "-ngl", "99", "--jinja"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(120):
            time.sleep(1)
            try:
                with llm.urllib.request.urlopen(f"http://127.0.0.1:{MT_PORT}/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:  # noqa: BLE001 — still loading
                pass


def _idle_reaper() -> None:
    """闲置 MT_IDLE 秒就按命令行关掉本机翻译模型（按命令行杀，own-reader 重启后留下的也能收）。"""
    while True:
        time.sleep(60)
        if time.time() - _local_used > MT_IDLE and _port_open():
            subprocess.run(["/usr/bin/pkill", "-f", f"llama-server -m {MT_GGUF}"], check=False)


def mt_local(messages: list[dict]) -> tuple[str, str]:
    global _local_used
    _ensure_local()
    _local_used = time.time()
    resp = llm._post_json(f"http://127.0.0.1:{MT_PORT}/v1/chat/completions",
                          {"model": "local", "messages": messages, "temperature": 0, "max_tokens": 6000,
                           "chat_template_kwargs": {"enable_thinking": False}}, {}, timeout=300)
    _local_used = time.time()
    return llm._content(resp), f"local:{MT_GGUF.stem}"


llm.BACKENDS.update(mt_free=mt_free, mt_local=mt_local)
llm.POLICY_DRAFT["public"] |= {"mt_free", "mt_local"}

# 翻译后端顺序：逗号分隔，依次尝试，前一个缺的段落交给下一个补。可用值见 llm.BACKENDS（claude / kimi / … 也行，
# 书的正文按 public 处理）。默认：OpenRouter 免费档 → 本机 llama.cpp。
CHAIN = [b.strip() for b in os.environ.get("OWN_READER_TRANSLATE_CHAIN", "mt_free,mt_local").split(",") if b.strip()]


def translate_block(paras: list[str], language: str) -> list[tuple[str, str] | None]:
    """返回每段 (译文, model)；缺的段为 None。按 CHAIN 顺序，前一个失败或缺段再交给下一个。"""
    out: list[tuple[str, str] | None] = [None] * len(paras)
    for backend in CHAIN:
        todo = [i for i, x in enumerate(out) if x is None]
        if not todo:
            break
        try:
            raw, model, _ = llm.ask(backend, _messages([paras[i] for i in todo], language),
                                    sensitivity="public", caller="reader.translate")
        except Exception as e:  # noqa: BLE001 — 换下一个后端
            print(f"translate {backend} failed: {e}", flush=True)
            continue
        for i, zh in zip(todo, parse(raw, len(todo))):
            if zh:
                out[i] = (zh, model)
    return out


def _store(rows: list[tuple[str, str, str, str]]) -> None:
    with app.db() as c:
        c.execute(SCHEMA)
        c.executemany("INSERT OR REPLACE INTO translations(h, src, zh, model) VALUES(?,?,?,?)", rows)


_pool = ThreadPoolExecutor(WORKERS)
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def request(paras: list[str]) -> dict:
    """排队翻译还没缓存的段落（按给的顺序），立即返回已有的。"""
    paras = [p.strip() for p in paras if p and p.strip()]
    language = lang(paras)
    if language is None:
        return {"lang": "zh", "done": {}}
    hs = [h(p) for p in paras]
    have = cached(hs)
    with _inflight_lock:
        todo = [p for p, x in zip(paras, hs) if x not in have and x not in _inflight]
        _inflight.update(h(p) for p in todo)

    def run(block):
        try:
            res = translate_block(block, language)
            _store([(h(p), p, r[0], r[1]) for p, r in zip(block, res) if r])
        finally:
            with _inflight_lock:
                _inflight.difference_update(h(p) for p in block)
    for b in blocks(todo):
        _pool.submit(run, b)
    return {"lang": language, "hashes": hs, "done": have, "pending": len(todo)}


def get(hashes: list[str]) -> dict:
    with _inflight_lock:
        pending = sum(1 for x in hashes if x in _inflight)
    return {"done": cached(hashes), "pending": pending}


if __name__ == "__main__":
    import sys
    paras = [p for p in Path(sys.argv[1]).read_text().split("\n\n") if p.strip()][:int(sys.argv[2]) if len(sys.argv) > 2 else None]
    t = time.time()
    print(json.dumps(request(paras), ensure_ascii=False)[:200])
    while get([h(p) for p in paras])["pending"]:
        time.sleep(2)
    print(f"{time.time() - t:.0f}s", len(get([h(p) for p in paras])["done"]), "/", len(paras))
