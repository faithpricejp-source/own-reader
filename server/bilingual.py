"""双语 EPUB：把段落译文插进原书，给安卓原生版（crengine）用（2026-10-07）。

- 段落切分与网页版 reader.js 的 trBlocks 一致：p/li/blockquote/h1-6/dd/dt/figcaption 里不再含这些块的叶子块，
  文本空白折叠后长度 >1。哈希同 translate.h（sha1(strip)），所以与网页版共用 translations 缓存。
- 译文作为 <span class="or-tr" style="display:block">…</span> 追加在原段末尾，与网页版同形。
- status() 统计全书段落的翻译进度，并把没翻的段交给 translate.request 排队（幂等，在途的不重复派）。
- build() 用当前已有的译文生成 EPUB，缓存到 <DATA_DIR>/bilingual/<id>.epub；已译段数没变就直接复用。
"""
from __future__ import annotations

import json
import posixpath
import re
import zipfile
from pathlib import Path

from lxml import etree

import app
import translate

TR_TAGS = {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt", "figcaption"}
XHTML_NS = "http://www.w3.org/1999/xhtml"
OUT_DIR = app.DATA_DIR / "bilingual"


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _spine_docs(zf: zipfile.ZipFile) -> list[str]:
    container = etree.fromstring(zf.read("META-INF/container.xml"))
    opf_path = container.find(".//{*}rootfile").get("full-path")
    opf = etree.fromstring(zf.read(opf_path))
    base = posixpath.dirname(opf_path)
    items = {i.get("id"): i.get("href") for i in opf.iterfind(".//{*}manifest/{*}item")}
    docs = []
    for ref in opf.iterfind(".//{*}spine/{*}itemref"):
        href = items.get(ref.get("idref"))
        if href:
            docs.append(posixpath.normpath(posixpath.join(base, href.split("#")[0])))
    return docs


def _parse(data: bytes):
    return etree.fromstring(data, etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True))


def _blocks(root) -> list:
    out = []
    for el in root.iter():
        if _local(el.tag) not in TR_TAGS:
            continue
        if any(_local(d.tag) in TR_TAGS for d in el.iterdescendants()):
            continue
        if len(_norm("".join(el.itertext()))) > 1:
            out.append(el)
    return out


def paragraphs(epub_path: str | Path) -> list[str]:
    """全书待译段落（按 spine 顺序，已规范化）。"""
    paras = []
    with zipfile.ZipFile(epub_path) as zf:
        names = set(zf.namelist())
        for name in _spine_docs(zf):
            if name not in names:
                continue
            root = _parse(zf.read(name))
            if root is None:
                continue
            paras += [_norm("".join(el.itertext())) for el in _blocks(root)]
    return paras


def status(book_id: int, epub_path: str | Path, kick: bool = True) -> dict:
    paras = paragraphs(epub_path)
    language = translate.lang(paras)
    if language is None:
        return {"lang": "zh", "total": len(paras), "done": len(paras), "pending": 0}
    hs = [translate.h(p) for p in paras]
    have = translate.cached(hs)
    pending = 0
    if kick and len(have) < len(hs):
        pending = translate.request([p for p, x in zip(paras, hs) if x not in have]).get("pending", 0)
    return {"lang": language, "total": len(hs), "done": sum(1 for x in hs if x in have), "pending": pending}


def build(book_id: int, epub_path: str | Path) -> Path:
    """生成（或复用）双语 EPUB，返回文件路径。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{book_id}.epub"
    meta = OUT_DIR / f"{book_id}.json"
    src_mtime = Path(epub_path).stat().st_mtime
    with zipfile.ZipFile(epub_path) as zf:
        spine = set(_spine_docs(zf))
        docs = {}
        hashes = []
        for name in zf.namelist():
            if name in spine:
                root = _parse(zf.read(name))
                if root is not None:
                    els = _blocks(root)
                    hashes += [translate.h(_norm("".join(e.itertext()))) for e in els]
                    docs[name] = (root, els)
        done = translate.cached(hashes)
        sig = {"src_mtime": src_mtime, "done": len(done)}
        if out.exists() and meta.exists() and json.loads(meta.read_text()) == sig:
            return out
        tmp = out.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w") as zo:
            for info in zf.infolist():
                data = zf.read(info.filename)
                if info.filename in docs:
                    root, els = docs[info.filename]
                    for el in els:
                        t = done.get(translate.h(_norm("".join(el.itertext()))))
                        if not t:
                            continue
                        span = etree.SubElement(el, f"{{{XHTML_NS}}}span" if el.tag.startswith("{") else "span")
                        span.set("class", "or-tr")
                        span.set("style", "display:block; margin-top:0.3em; text-indent:0")
                        span.text = t["zh"]
                        span.tail = None
                    data = etree.tostring(root, xml_declaration=True, encoding="utf-8")
                comp = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED
                zo.writestr(info.filename, data, compress_type=comp)
        tmp.replace(out)
        meta.write_text(json.dumps(sig))
    return out
