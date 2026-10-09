"""server/bilingual.py：段落切分与网页版一致、译文插入、缓存复用。"""
from __future__ import annotations

import zipfile

import bilingual
import translate

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>
<h1>Chapter One</h1>
<p>First   paragraph
 here.</p>
<blockquote><p>Quoted line.</p></blockquote>
<p>x</p>
<ul><li>Item <b>bold</b> text</li></ul>
</body></html>"""


def _epub(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?><container version="1.0"
 xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>
 <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        z.writestr("OEBPS/content.opf", """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">
 <manifest><item id="c1" href="text/c1.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="c1"/></spine></package>""")
        z.writestr("OEBPS/text/c1.xhtml", XHTML)
    return path


def test_paragraphs_match_web_rules(tmp_path):
    ps = bilingual.paragraphs(_epub(tmp_path / "b.epub"))
    # 叶子块、空白折叠、长度 >1 的才算；blockquote 里有 p，所以只取里面的 p
    assert ps == ["Chapter One", "First paragraph here.", "Quoted line.", "Item bold text"]


def test_build_inserts_cached_translations_and_reuses(events, tmp_path):
    src = _epub(tmp_path / "b.epub")
    translate._store([(translate.h("First paragraph here."), "First paragraph here.", "第一段。", "ultra"),
                      (translate.h("Item bold text"), "Item bold text", "粗体条目", "local:x")])
    st = bilingual.status(1, src, kick=False)
    assert st == {"lang": "en", "total": 4, "done": 2, "pending": 0}
    out = bilingual.build(1, src)
    with zipfile.ZipFile(out) as z:
        assert z.namelist()[0] == "mimetype" and z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        html = z.read("OEBPS/text/c1.xhtml").decode()
    assert html.count('class="or-tr"') == 2
    assert html.index("第一段。") > html.index("First") and html.index("粗体条目") > html.index("bold")
    assert "Chapter One</h1>" in html                      # 没译的段原样
    mtime = out.stat().st_mtime_ns
    assert bilingual.build(1, src).stat().st_mtime_ns == mtime   # 已译段数没变：复用
    translate._store([(translate.h("Quoted line."), "Quoted line.", "引文。", "ultra")])
    with zipfile.ZipFile(bilingual.build(1, src)) as z:       # 多译了一段：重建
        assert z.read("OEBPS/text/c1.xhtml").decode().count('class="or-tr"') == 3
