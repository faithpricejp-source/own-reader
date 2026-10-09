"""每章导读（「把书读厚」第二层）：每章在全书里干什么、论证/情节链、跳步与存疑、留给你的。

切章：EPUB 目录（NCX/nav）能对上 3 个以上正文文件就按文件切；否则把「像标题的短行」交给 Sonnet 挑章首，
返回的行号必须在候选里（不认就丢）。切章结果缓存在 chapter_seg。挑章首与生成都走 Claude API（需 key，见 llm.py）。
生成：每章一个请求走 Message Batches（五折），带全书导读（有的话）做上下文；结果作为 type='本章' 的批注
挂在章首那一段（段落哈希同精读批注），网页开着「批」时显示在章首。
"""
from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path

import app
import gloss
import llm
from translate import h

SEG_MODEL = "claude-sonnet-5-5"
MIN_CHAPTER_CHARS = 1500     # 比这短的「章」并进前一章（目录页、扉页）
SCHEMA = """
CREATE TABLE IF NOT EXISTS chapter_seg(book_id INTEGER PRIMARY KEY, chapters TEXT, method TEXT);
"""

PROMPT = """你是读者的导读人，现在写某一章的「本章导读」，放在这一章开头。读者想把书读厚。
用中文，Markdown，按下面四个小标题（三级标题），总长 250–500 字：
### 这一章在干什么
它在全书里承担什么（承上启下、提出/推进/反转了什么），一两句说清。
### 要点
论证链或情节推进的关键几步；读者容易漏掉的关键句（原文短引）。
### 跳步与存疑
作者跳过的步骤、站不住或有争议的地方（引原文，写依据和把握 高/中/低）；没有就写「无明显问题」。
### 留给你的
1–2 个只能读者自己判断的问题。
规则：不要逐段复述；不编引文；书外事实没把握就写「未核」；你的判断句首标【判断】。"""

SEG_PROMPT = """下面是一本书里「像标题的短行」候选，格式「[行号] 文字」，按书中顺序。另附书开头的一段文字（常含目录）。
找出其中真正的章节开头（不含目录页里的条目、扉页、版权页；前言/序/后记若独立成章可以算）。
只输出 JSON 数组，元素是行号整数，按顺序。不要别的文字。"""


def _db():
    c = app.db()
    c.executescript(gloss.SCHEMA + SCHEMA)  # 本章导读写进 gloss_items，表要先在
    return c


def _paras_with_files(epub: Path) -> list[tuple[str, str]]:
    """[(spine 文件名, 段落文本)]，切段规则与 gloss.paragraphs 完全一致（哈希要对得上）。"""
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
                if bl._local(el.tag) not in gloss.GL_TAGS:
                    continue
                if any(bl._local(d.tag) in gloss.GL_TAGS for d in el.iterdescendants()):
                    continue
                t = gloss._norm("".join(el.itertext()))
                if len(t) > 1:
                    out.append((name, t))
    return out


def _toc_files(epub: Path) -> list[tuple[str, str]]:
    """目录条目 [(文件名, 标题)]，按目录顺序，同一文件只取第一个。"""
    import book_text
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(epub) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        opf_href = next(r.get("full-path") for r in container.iter() if book_text._localname(r.tag) == "rootfile")
        opf_dir = str(Path(opf_href).parent)
        opf_dir = "" if opf_dir == "." else opf_dir
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        labels = book_text._read_toc_labels(zf, opf_dir, ET.fromstring(zf.read(opf_href)), ns)
    return list(labels.items())


def _merge_short(chs: list[dict], paras: list[str]) -> list[dict]:
    out: list[dict] = []
    for i, ch in enumerate(chs):
        end = chs[i + 1]["start"] if i + 1 < len(chs) else len(paras)
        ch["chars"] = sum(len(p) for p in paras[ch["start"]:end])
        if out and ch["chars"] < MIN_CHAPTER_CHARS:
            out[-1]["chars"] += ch["chars"]
            continue
        out.append(ch)
    if len(out) > 1 and out[0]["chars"] < MIN_CHAPTER_CHARS:  # 开头的目录/扉页不算一章；章首仍留在章标题上
        out = out[1:]
    return out


def segment(book_id: int, epub: Path) -> list[dict]:
    with _db() as c:
        r = c.execute("SELECT chapters FROM chapter_seg WHERE book_id=?", (book_id,)).fetchone()
    if r:
        return json.loads(r["chapters"])
    pf = _paras_with_files(epub)
    paras = [t for _, t in pf]
    first_idx: dict[str, int] = {}
    for i, (f, _) in enumerate(pf):
        first_idx.setdefault(f, i)
    toc = [(f, t) for f, t in _toc_files(epub) if f in first_idx]
    if len(toc) >= 3:
        chs = sorted(({"title": t, "start": first_idx[f]} for f, t in toc), key=lambda x: x["start"])
        method = "toc"
    else:
        cand = [(i, p) for i, p in enumerate(paras)
                if len(p) <= 60 and not re.search(r"[.?!,;:。？！，；：\"'’”…)]$", p)]
        head = " / ".join(paras[:80])[:3000]
        user = f"【书开头】\n{head}\n\n【候选】\n" + "\n".join(f"[{i}] {p}" for i, p in cand)
        text, _, _ = llm.api_call(SEG_PROMPT, user, model=SEG_MODEL, effort="low")
        ok = {i for i, _ in cand}
        idx = [i for i in json.loads(re.search(r"\[.*\]", text, re.S).group(0)) if isinstance(i, int) and i in ok]
        chs = [{"title": paras[i], "start": i} for i in sorted(set(idx))]
        if not chs or chs[0]["start"] > 0:
            chs.insert(0, {"title": "开头", "start": 0})
        method = "model"
    chs = _merge_short(chs, paras)
    for ch in chs:
        ch["h"] = h(paras[ch["start"]])
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO chapter_seg VALUES (?,?,?)", (book_id, json.dumps(chs, ensure_ascii=False), method))
    return chs


def status(book_id: int) -> dict:
    import batchjobs
    with _db() as c:
        seg = c.execute("SELECT chapters, method FROM chapter_seg WHERE book_id=?", (book_id,)).fetchone()
        chs = json.loads(seg["chapters"]) if seg else []
        done = 0
        if chs:
            q = ",".join("?" * len(chs))
            done = c.execute(f"SELECT COUNT(DISTINCT h) n FROM gloss_items WHERE type='本章' AND h IN ({q})",
                             [ch["h"] for ch in chs]).fetchone()["n"]
    pend = batchjobs.pending("chapter", book_id)
    for bid in {r["batch_id"] for r in pend}:
        batchjobs.ensure_poller(bid)
    return {"chapters": len(chs), "done": done, "pending": len(pend), "method": seg["method"] if seg else None}


def _store_guide(hp: str, md: str, model: str, book_id: int) -> None:
    with _db() as c:
        c.execute("DELETE FROM gloss_items WHERE h=? AND type='本章'", (hp,))
        c.execute("INSERT INTO gloss_items VALUES (?,?,?,?,?,?,?)", (hp, 999, "本章", "", md, "", model))
        c.execute("INSERT OR IGNORE INTO gloss_book VALUES (?,?)", (hp, book_id))


def start(book_id: int, epub: Path, title: str) -> dict:
    import batchjobs
    import dossier
    if batchjobs.pending("chapter", book_id):
        return status(book_id)
    chs = segment(book_id, epub)
    paras = [t for _, t in _paras_with_files(epub)]
    with _db() as c:
        have = {r["h"] for r in c.execute("SELECT h FROM gloss_items WHERE type='本章'")}
    d = dossier.get(book_id)
    ctx = f"\n\n【全书导读（供参考）】\n{d['md'][:20000]}" if d and d.get("md") else ""
    system = f"{PROMPT}\n\n【读者画像】\n{gloss.profile()}{ctx}"
    items = []
    stamp = time.strftime("%H%M%S")
    for k, ch in enumerate(chs):
        if ch["h"] in have:
            continue
        end = chs[k + 1]["start"] if k + 1 < len(chs) else len(paras)
        body = "\n\n".join(paras[ch["start"]:end])[:400_000]
        items.append((f"c{book_id}-{stamp}-{k}", system,
                      f"书名：《{title}》\n本章：{ch['title']}（第 {k + 1}/{len(chs)} 章）\n\n{body}",
                      {"h": ch["h"], "title": ch["title"]}))
    if not items:
        return status(book_id)
    est = sum(len(u) for _, _, u, _ in items) / 3 * 2 / 1e6 + len(items) * 0.02
    batchjobs.submit("chapter", book_id, items, est_usd=est)
    return status(book_id)


def _on_batch(payload: dict, book_id: int, text: str | None, err: str | None) -> str:
    if not text:
        return f"error: {err}"
    _store_guide(payload["h"], text, f"{llm.API_MODEL}:batch", book_id)
    return "done"


def _register() -> None:
    import batchjobs
    batchjobs.register("chapter", _on_batch)


_register()
