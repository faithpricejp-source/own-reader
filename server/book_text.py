"""从 EPUB 提取全文段落并按书缓存，提问时检索书中相关段落。Stdlib only。

- 用 zipfile 按 OPF spine 顺序读 xhtml，HTMLParser 去标签、按块级元素切段，过短段并入相邻段。
- 缓存到 <data_dir>/booktext/<book_id>.json，记录源文件 mtime，变了就重建。
- BM25 检索：中文按字二元组，英文按词小写。
"""
from __future__ import annotations

import json
import re
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

MIN_PARA = 20          # 短于这个长度的段并入相邻段
TOP_N = 5              # 默认取前 5 段
MAX_CHARS = 4000       # 拼进提示词的总长上限
OVERLAP_WIN = 40       # 与已给前后文有 ≥40 字的公共子串就算重叠

_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
               "dd", "dt", "div", "section", "article", "tr", "pre", "figcaption"}


class _ParaParser(HTMLParser):
    """把 xhtml 切成纯文本段落；记录第一个标题当章节名兜底。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paras: list[str] = []
        self.buf: list[str] = []
        self.heading: str | None = None
        self._in_heading = False
        self._skip = 0

    def flush(self):
        t = "".join(self.buf).strip()
        t = re.sub(r"\s+", " ", t)
        self.buf = []
        if t:
            self.paras.append(t)

    def handle_starttag(self, tag, attrs):
        if tag in ("title", "script", "style"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag in _BLOCK_TAGS:
            self.flush()
        if tag in ("h1", "h2", "h3"):
            self._in_heading = True

    def handle_endtag(self, tag):
        if tag in ("title", "script", "style"):
            self._skip -= 1
            return
        if self._skip:
            return
        if tag in _BLOCK_TAGS:
            self.flush()
        if tag in ("h1", "h2", "h3"):
            self._in_heading = False

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_heading and self.heading is None:
            t = data.strip()
            if t:
                self.heading = t
        self.buf.append(data)


def _merge_short(paras: list[str]) -> list[str]:
    out: list[str] = []
    for p in paras:
        if len(p) < MIN_PARA and out:
            out[-1] = out[-1] + p
        elif len(p) < MIN_PARA:
            out.append(p)  # 段首短段：先留下，下一段会并进来
        else:
            out.append(p)
    return [p for p in out if p]


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_toc_labels(zf: zipfile.ZipFile, opf_dir: str, opf_root: ET.Element, ns: dict) -> dict[str, str]:
    """href(去 fragment、相对 EPUB 根) -> 章节名，来自 NCX 或 nav。"""
    labels: dict[str, str] = {}

    def norm(href: str) -> str:
        href = href.split("#")[0]
        p = str(Path(opf_dir) / href) if opf_dir else href
        return str(Path(p)) if p.startswith("/") else str(Path("/", p).resolve())[1:].replace("\\", "/")

    # epub2: NCX
    spine_toc = opf_root.find("opf:spine", ns).get("toc") if opf_root.find("opf:spine", ns) is not None else None
    if spine_toc:
        item = opf_root.find(f"opf:manifest/opf:item[@id='{spine_toc}']", ns)
        if item is not None:
            try:
                root = ET.fromstring(zf.read(norm(item.get("href"))))
                for np in root.iter():
                    if _localname(np.tag) == "navPoint":
                        src, text = None, None
                        for ch in np.iter():
                            if _localname(ch.tag) == "content":
                                src = ch.get("src")
                            elif _localname(ch.tag) == "text" and text is None:
                                text = (ch.text or "").strip()
                        if src and text:
                            labels.setdefault(norm(src), text)
            except Exception:
                pass
    # epub3: nav
    for item in opf_root.findall("opf:manifest/opf:item", ns):
        if "nav" in (item.get("properties") or ""):
            try:
                root = ET.fromstring(zf.read(norm(item.get("href"))))
                for a in root.iter():
                    if _localname(a.tag) == "a" and a.get("href"):
                        text = "".join(a.itertext()).strip()
                        if text:
                            labels.setdefault(norm(a.get("href")), text)
            except Exception:
                pass
    return labels


def extract_paragraphs(epub_path: str | Path) -> list[dict]:
    """EPUB -> [{"chapter": 章节名, "text": 段落文本}]，按 spine 顺序。"""
    epub_path = Path(epub_path)
    with zipfile.ZipFile(epub_path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        opf_href = next(r.get("full-path") for r in container.iter() if _localname(r.tag) == "rootfile")
        opf_dir = str(Path(opf_href).parent)
        if opf_dir == ".":
            opf_dir = ""
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        opf = ET.fromstring(zf.read(opf_href))
        manifest = {it.get("id"): it.get("href") for it in opf.findall("opf:manifest/opf:item", ns)}
        spine = [it.get("idref") for it in opf.findall("opf:spine/opf:itemref", ns)]
        labels = _read_toc_labels(zf, opf_dir, opf, ns)

        def full(href: str) -> str:
            p = str(Path(opf_dir) / href) if opf_dir else href
            return str(Path("/", p).resolve())[1:].replace("\\", "/")

        out: list[dict] = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            name = full(href)
            try:
                raw = zf.read(name).decode("utf-8", "replace")
            except KeyError:
                continue
            parser = _ParaParser()
            try:
                parser.feed(raw)
            except Exception:
                pass
            parser.flush()
            chapter = labels.get(name) or parser.heading or ""
            for text in _merge_short(parser.paras):
                out.append({"chapter": chapter, "text": text})
        return out


def load_paragraphs(book_id: int, epub_path: str | Path, data_dir: str | Path) -> list[dict]:
    """带缓存的段落提取；源文件 mtime 变了就重建缓存。"""
    epub_path = Path(epub_path)
    cache = Path(data_dir) / "booktext" / f"{book_id}.json"
    mtime = epub_path.stat().st_mtime
    try:
        data = json.loads(cache.read_text())
        if data.get("src_mtime") == mtime:
            return data["paragraphs"]
    except Exception:
        pass
    paragraphs = extract_paragraphs(epub_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps({"src_mtime": mtime, "paragraphs": paragraphs}, ensure_ascii=False))
    tmp.replace(cache)
    return paragraphs


_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]+")


def tokens(text: str) -> list[str]:
    """中文按字二元组，英文按词小写。"""
    out = _WORD_RE.findall(text.lower())
    for run in _CJK_RE.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _bm25(paragraphs: list[list[str]], query: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    n = len(paragraphs)
    if not n or not query:
        return [0.0] * n
    avgdl = sum(len(p) for p in paragraphs) / n or 1
    df: dict[str, int] = {}
    for p in paragraphs:
        for t in set(p):
            df[t] = df.get(t, 0) + 1
    scores = []
    for p in paragraphs:
        tf: dict[str, int] = {}
        for t in p:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        dl = len(p)
        for t in set(query):
            f = tf.get(t, 0)
            if not f:
                continue
            idf = 1 + (n - df.get(t, 0) + 0.5) / (df.get(t, 0) + 0.5)
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl))
        scores.append(s)
    return scores


def _overlaps(text: str, context: str) -> bool:
    """段落与已给前后文是否重叠（公共子串 ≥ OVERLAP_WIN 字）。"""
    t = re.sub(r"\s+", "", text)
    c = re.sub(r"\s+", "", context)
    if not t or not c:
        return False
    if len(t) <= OVERLAP_WIN:
        return t in c
    return any(t[i:i + OVERLAP_WIN] in c for i in range(len(t) - OVERLAP_WIN + 1))


def relevant_passages(book_id: int, epub_path: str | Path, query: str, context: str = "",
                      data_dir: str | Path = ".", top_n: int = TOP_N, max_chars: int = MAX_CHARS) -> str:
    """检索并拼成「【书中其他相关段落】」小节；任何失败都返回空串，不影响提问。"""
    try:
        paras = load_paragraphs(book_id, epub_path, data_dir)
        if not paras:
            return ""
        scores = _bm25([tokens(p["text"]) for p in paras], tokens(query))
        ranked = sorted(zip(scores, paras), key=lambda x: -x[0])
        parts: list[str] = []
        total = 0
        for score, p in ranked:
            if score <= 0 or len(parts) >= top_n:
                break
            text = p["text"]
            if _overlaps(text, context):
                continue
            head = f"〔{p['chapter']}〕" if p["chapter"] else ""
            entry = f"{head}\n{text}" if head else text
            if total + len(entry) + 2 > max_chars:
                break
            parts.append(entry)
            total += len(entry) + 2
        if not parts:
            return ""
        return "【书中其他相关段落】\n" + "\n\n".join(parts)
    except Exception:
        return ""
