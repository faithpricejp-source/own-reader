"""精读批注：读到哪批到哪，或整本预跑；批注追加在原文段落末尾。

五类（按「对这位读者」需要来批，读者画像在 <数据目录>/reader_profile.md，可在网页「批」面板里改）：
  背景  文化习俗、典故、制度、为什么这么做、某个词在当时的含义
  释义  术语、专名、数学物理工程书里缺的前置知识或跳掉的推导步骤
  存疑  作者说错了，或者有争议（必须写出依据和把握，标「模型判断」）
  双关  一语双关、文字游戏
  言外  阴阳话、明褒实贬、明贬实褒、话里有话、针对在场某人
原则同地理批注：不能编。批注引用的原文必须逐字出现在该段里，否则丢弃；没把握就不批。

- 段落级缓存（sha1 同 translate.h），批过的段永不重批；网页版与整本预跑共用。
- 后端默认 claude_api（Claude API，需 key，见 llm.py），失败时回落 OWN_READER_FALLBACK_BACKEND（没配就用
  OWN_READER_DEFAULT_BACKEND）；可用 OWN_READER_GLOSS_BACKEND 换。读者画像是个人信息（sensitivity=personal），不发给免费模型。
- 一次调用批一块（约 BLOCK_CHARS 字），每次调用有固定开销（系统提示），块不宜太小。
- 整本预跑走 Message Batches，只能用 Claude API。
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import os

import app
import llm
from translate import h

BLOCK_CHARS = 6000
FIRST_CHARS = 2500      # 第一块小一点，先出第一屏
WORKERS = 2
BACKEND = os.environ.get("OWN_READER_GLOSS_BACKEND", "claude_api")
FALLBACK = llm.FALLBACK_BACKEND or llm.DEFAULT_BACKEND  # 失败（含当月到 API 上限）时改用
PROFILE = app.DATA_DIR / "reader_profile.md"
TYPES = ("背景", "释义", "存疑", "双关", "言外")
SCHEMA = """
CREATE TABLE IF NOT EXISTS gloss_done(h TEXT PRIMARY KEY, n INT, model TEXT,
  ts TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE IF NOT EXISTS gloss_items(h TEXT, i INT, type TEXT, quote TEXT, note TEXT, conf TEXT, model TEXT,
  PRIMARY KEY(h, i));
CREATE TABLE IF NOT EXISTS gloss_book(h TEXT, book_id INT, PRIMARY KEY(h, book_id));
"""
DEFAULT_PROFILE = """# 读者画像（精读批注按这个判断「我可能不懂什么」。这是示例，请在阅读页「批」面板里改成你自己的）
- 母语与外语：例如「中文母语；英文原著能读，但俚语、行话、文化梗常常看不出来」。
- 专业背景：例如「理工科背景，数学、物理、工程书里跳步的推导需要补」。
- 熟悉与不熟悉的领域：例如「对本国历史熟悉；对外国政治制度、古典文学典故不熟」。
- 想要：背景、术语、作者可能错或有争议的地方、双关、阴阳话和言外之意。不要：常识性解释、复述原文、总结。
"""

PROMPT = """你是读者的精读助手，给一本书的若干段落写批注，写在段落下方。先读读者画像，只批这位读者可能看不懂、
或者看了也会错过的东西。五类：
- 背景：文化习俗、典故、制度、历史事件，为什么这么做；某个词在当时/当地的特别含义（如古代对某物的尊称）。
- 释义：术语、专名；数学物理工程书里缺的前置知识，或作者跳掉的推导步骤（写出补上的那一步）。
- 存疑：作者说的事实错了，或学界有争议。必须写「原文说…；实际/另一说…；依据…」，把握写 高/中/低。
- 双关：一语双关、文字游戏，写出两层意思。
- 言外：阴阳话、明褒实贬、明贬实褒、客气话里的真实意思、话中有话、暗指在场某人。写出字面意思和真实意思。
输出一个 JSON 数组，不要任何别的文字：
[{"p": 段落编号, "type": "背景|释义|存疑|双关|言外", "quote": "从该段原文逐字摘的触发词句（尽量短）",
  "note": "批注正文（中文，简洁，先给结论）", "conf": "高|中|低"}]
硬性规则：
1. quote 必须逐字出自第 p 段（原文语言，不要翻译），否则这条会被丢弃。
2. 宁缺勿编：没有把握的事实不写；不确定就写「不确定」或 conf=低。不要编引文、出处、数字。
3. 没什么可批的段就跳过；不要为凑数批常识，不要复述或总结原文。
4. 本书前文已经批过的词，不要重复解释。"""


def profile() -> str:
    if not PROFILE.exists():
        PROFILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE.write_text(DEFAULT_PROFILE)
    return PROFILE.read_text()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "})


def _loose(s: str) -> str:
    """只用于逐字比对：弯直引号、不换行空格视为相同（模型常把 ’ 写成 '，不算编造）。"""
    return _norm((s or "").translate(_QUOTES))


def blocks(paras: list[str]) -> list[list[str]]:
    out, cur, n, limit = [], [], 0, FIRST_CHARS
    for p in paras:
        cur.append(p)
        n += len(p)
        if n >= limit:
            out.append(cur)
            cur, n, limit = [], 0, BLOCK_CHARS
    if cur:
        out.append(cur)
    return out


def parse(raw: str, paras: list[str]) -> tuple[list[tuple[int, dict]], int]:
    """返回 ([(段下标, 批注)], 丢弃条数)。quote 不是该段原文的逐字子串就丢。"""
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise ValueError(f"没有 JSON 数组：{raw[:200]}")
    items, dropped = [], 0
    for it in json.loads(m.group(0)):
        try:
            i = int(it["p"]) - 1
            q, note, typ = _norm(it.get("quote")), (it.get("note") or "").strip(), it.get("type")
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        if not (0 <= i < len(paras)) or typ not in TYPES or not note or not q or _loose(q) not in _loose(paras[i]):
            dropped += 1
            continue
        items.append((i, {"type": typ, "quote": q, "note": note, "conf": it.get("conf") or ""}))
    return items, dropped


def seen_terms(book_id: int | None, limit: int = 60) -> list[str]:
    """本书已经批过的触发词，喂给模型避免重复解释。"""
    if not book_id:
        return []
    with app.db() as c:
        c.executescript(SCHEMA)
        rows = c.execute("""SELECT DISTINCT i.quote FROM gloss_items i JOIN gloss_book b ON b.h=i.h
                            WHERE b.book_id=? AND i.type IN ('背景','释义') ORDER BY i.rowid DESC LIMIT ?""",
                         (book_id, limit)).fetchall()
    return [r["quote"] for r in rows]


def messages(paras: list[str], title: str, seen: list[str] | None = None) -> tuple[str, str]:
    """(系统提示, 用户消息)。画像放进系统提示：每次都一样，能吃到提示缓存。"""
    system = f"{PROMPT}\n\n【读者画像】\n{profile()}"
    body = "\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(paras))
    user = (f"【书名】《{title}》\n\n"
            + (f"【本书前文已批过的词】{'、'.join(seen)}\n\n" if seen else "")
            + f"【段落】\n{body}")
    return system, user


def gloss_block(paras: list[str], title: str, book_id: int | None) -> tuple[list[tuple[int, dict]], str, int]:
    system, user = messages(paras, title, seen_terms(book_id))
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        raw, model, _ = llm.ask(BACKEND, msgs, sensitivity="personal", caller="reader.gloss")
    except llm.BackendError as e:
        if not FALLBACK or FALLBACK == BACKEND:
            raise
        print(f"gloss: {BACKEND} failed ({str(e)[:120]}), falling back to {FALLBACK}", flush=True)
        raw, model, _ = llm.ask(FALLBACK, msgs, sensitivity="personal", caller="reader.gloss.fallback")
    items, dropped = parse(raw, paras)
    return items, model, dropped


def _store(paras: list[str], items: list[tuple[int, dict]], model: str, book_id: int | None) -> None:
    with app.db() as c:
        c.executescript(SCHEMA)
        by: dict[int, list[dict]] = {}
        for i, it in items:
            by.setdefault(i, []).append(it)
        for i, p in enumerate(paras):
            hp = h(p)
            c.execute("DELETE FROM gloss_items WHERE h=? AND type!='本章'", (hp,))  # 本章导读由 chapters.py 管
            for k, it in enumerate(by.get(i, [])):
                c.execute("INSERT INTO gloss_items VALUES (?,?,?,?,?,?,?)",
                          (hp, k, it["type"], it["quote"], it["note"], it["conf"], model))
            c.execute("INSERT OR REPLACE INTO gloss_done(h, n, model) VALUES (?,?,?)", (hp, len(by.get(i, [])), model))
            if book_id:
                c.execute("INSERT OR IGNORE INTO gloss_book VALUES (?,?)", (hp, book_id))


def cached(hashes: list[str]) -> dict[str, list[dict]]:
    """已批过的段 → 批注列表（批过但没有可批的为空列表）。"""
    out: dict[str, list[dict]] = {}
    with app.db() as c:
        c.executescript(SCHEMA)
        for k in range(0, len(hashes), 500):
            part = hashes[k:k + 500]
            q = ",".join("?" * len(part))
            for r in c.execute(f"SELECT h FROM gloss_done WHERE h IN ({q})", part):
                out[r["h"]] = []
            for r in c.execute(f"SELECT * FROM gloss_items WHERE h IN ({q}) ORDER BY h, i", part):
                out.setdefault(r["h"], []).append({"type": r["type"], "quote": r["quote"], "note": r["note"],
                                                   "conf": r["conf"]})
    return out


_pool = ThreadPoolExecutor(WORKERS)
_inflight: set[str] = set()
_lock = threading.Lock()
_last_error: dict = {"msg": None, "ts": 0}


def _run(block: list[str], title: str, book_id: int | None) -> None:
    try:
        items, model, dropped = gloss_block(block, title, book_id)
        _store(block, items, model, book_id)
        if dropped:
            print(f"gloss: dropped {dropped} items with non-verbatim quotes", flush=True)
    except Exception as e:  # noqa: BLE001 — 这块下次再请求时重试
        _last_error.update(msg=str(e)[:300], ts=time.time())
        print(f"gloss failed: {e}", flush=True)
    finally:
        with _lock:
            _inflight.difference_update(h(p) for p in block)


def request(paras: list[str], title: str, book_id: int | None = None) -> dict:
    paras = [_norm(p) for p in paras if p and _norm(p)]
    hs = [h(p) for p in paras]
    have = cached(hs)
    in_batch = _batch_hashes()
    with _lock:
        todo = [p for p, x in zip(paras, hs) if x not in have and x not in _inflight and x not in in_batch]
        _inflight.update(h(p) for p in todo)
    for b in blocks(todo):
        _pool.submit(_run, b, title, book_id)
    return {"hashes": hs, "done": have, "pending": len(todo), "error": _recent_error()}


def get(hashes: list[str]) -> dict:
    in_batch = _batch_hashes()
    with _lock:
        pending = sum(1 for x in hashes if x in _inflight or x in in_batch)
    return {"done": cached(hashes), "pending": pending, "error": _recent_error()}


def _recent_error() -> str | None:
    return _last_error["msg"] if time.time() - _last_error["ts"] < 120 else None


# ---------- 整本预跑 ----------

# 段落切分：比翻译多认 div（Calibre 转出来的书常把段落写成 <div class="paragraph">，翻译那套会漏掉整本）。
# 规则与网页版 reader.js 的 glBlocks 一致：这些标签里不再含这些标签的叶子块，空白折叠后长度 >1。
GL_TAGS = {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt", "figcaption", "div"}


def paragraphs(epub: Path) -> list[str]:
    import zipfile

    import bilingual as bl
    out = []
    with zipfile.ZipFile(epub) as zf:
        names = set(zf.namelist())
        for name in bl._spine_docs(zf):
            if name not in names:
                continue
            root = bl._parse(zf.read(name))
            if root is None:
                continue
            for el in root.iter():
                if bl._local(el.tag) not in GL_TAGS:
                    continue
                if any(bl._local(d.tag) in GL_TAGS for d in el.iterdescendants()):
                    continue
                t = _norm("".join(el.itertext()))
                if len(t) > 1:
                    out.append(t)
    return out


# 整本预跑走 Claude API 的 Message Batches（五折，通用作业层见 batchjobs.py）：一次提交全书没批过的块；
# 收回之前，这些段对网页版算「在途」，翻到那里不会重复实时批。批内各块并发处理，拿不到前文已批词表，偶有重复解释。
EST_USD_PER_BLOCK = 0.04   # 作者实测：Opus 实时一块（6000 字）约 0.069 美元；Batch 约 0.026 美元


def _batch_hashes() -> set[str]:
    import batchjobs
    return {h(p) for r in batchjobs.pending("gloss") for p in r["payload"]["paras"]}


def _on_batch(payload: dict, book_id: int, text: str | None, err: str | None) -> str:
    if not text:
        return f"error: {err}"
    paras = payload["paras"]
    items, dropped = parse(text, paras)
    _store(paras, items, f"{llm.API_MODEL}:batch", book_id)
    if dropped:
        print(f"gloss batch: dropped {dropped} non-verbatim items", flush=True)
    return "done"


def _register() -> None:
    import batchjobs
    batchjobs.register("gloss", _on_batch)


def book_status(book_id: int, epub: Path) -> dict:
    import batchjobs
    paras = list(dict.fromkeys(paragraphs(epub)))  # 按去重计：书里常有完全相同的短段（如「Minister.」）
    done = cached([h(p) for p in paras])
    pend = batchjobs.pending("gloss", book_id)
    for bid in {r["batch_id"] for r in pend}:
        batchjobs.ensure_poller(bid)
    return {"total": len(paras), "done": len(done), "running": bool(pend),
            "batch_blocks": len(pend), "error": _book_errors.get(book_id)}


_book_errors: dict[int, str] = {}


def start_book(book_id: int, epub: Path, title: str, limit: int | None = None) -> dict:
    """limit：只提交前几块（试跑用）。"""
    import batchjobs
    if batchjobs.pending("gloss", book_id):
        return book_status(book_id, epub)
    paras = paragraphs(epub)
    have = cached([h(p) for p in paras])
    with _lock:
        todo = [p for p in paras if h(p) not in have and h(p) not in _inflight]
    blks = blocks(todo)[:limit]
    if not blks:
        return book_status(book_id, epub)
    stamp = time.strftime("%H%M%S")
    items = [(f"g{book_id}-{stamp}-{k}", *messages(b, title), {"paras": b}) for k, b in enumerate(blks)]
    try:
        batchjobs.submit("gloss", book_id, items, est_usd=EST_USD_PER_BLOCK * len(blks))
    except Exception as e:  # noqa: BLE001
        _book_errors[book_id] = f"提交失败：{str(e)[:160]}"
        return book_status(book_id, epub)
    _book_errors.pop(book_id, None)
    return book_status(book_id, epub)


def stop_book(book_id: int) -> None:
    """取消在途批次；已处理完的部分照常收回（取消后批次会变成 ended）。"""
    import batchjobs
    for bid in {r["batch_id"] for r in batchjobs.pending("gloss", book_id)}:
        try:
            llm._client().messages.batches.cancel(bid)
        except Exception as e:  # noqa: BLE001
            _book_errors[book_id] = f"取消失败：{str(e)[:120]}"


_register()
