import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import book_text  # noqa: E402

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>t</dc:title><dc:identifier id="id">x</dc:identifier></metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="c1"/><itemref idref="c2"/>
  </spine>
</package>"""

NCX = """<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
    <navPoint id="n1" playOrder="1"><navLabel><text>第一章 成长</text></navLabel><content src="c1.xhtml"/></navPoint>
    <navPoint id="n2" playOrder="2"><navLabel><text>Chapter Two</text></navLabel><content src="c2.xhtml"/></navPoint>
  </navMap>
</ncx>"""

C1 = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title></head><body>
<h1>第一章 成长</h1>
<p>这座港口城市在经济腾飞的道路上经历了许多挑战。</p>
<p>经济腾飞之后，社会结构发生了深刻的变化。</p>
<p>主人公回忆了童年时期在乡间小学读书的日子。</p>
<p>短</p>
<p>那段岁月塑造了他后来的性格。</p>
</body></html>"""

C2 = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Two</title></head><body>
<h2>Chapter Two</h2>
<p>The quick brown fox jumps over the lazy dog.</p>
<p>Photography and printing were his childhood hobbies.</p>
</body></html>"""


@pytest.fixture
def epub(tmp_path):
    f = tmp_path / "test.epub"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/toc.ncx", NCX)
        z.writestr("OEBPS/c1.xhtml", C1)
        z.writestr("OEBPS/c2.xhtml", C2)
    return f


def test_extract_paragraphs(epub):
    paras = book_text.extract_paragraphs(epub)
    texts = [p["text"] for p in paras]
    assert any("经济腾飞" in t for t in texts)
    assert any("Photography" in t for t in texts)
    # spine 顺序：中文段在前，英文段在后
    assert texts.index(next(t for t in texts if "经济腾飞" in t)) < \
           texts.index(next(t for t in texts if "Photography" in t))
    # 章节名来自 NCX
    ch1 = [p for p in paras if "经济腾飞" in p["text"]][0]
    assert ch1["chapter"] == "第一章 成长"
    # 过短段（“短”）被并入相邻段，不再单独成段
    assert not any(t == "短" for t in texts)
    assert any("短那段岁月" in t for t in texts)


def test_cache_and_invalidation(epub, tmp_path):
    data = tmp_path / "data"
    p1 = book_text.load_paragraphs(7, epub, data)
    cache = data / "booktext" / "7.json"
    assert cache.exists()
    assert json.loads(cache.read_text())["paragraphs"] == p1
    # 源文件没变：即使删掉缓存内容之外的部分也不用重建 —— 直接改 mtime 触发重建
    os.utime(epub, (epub.stat().st_atime, epub.stat().st_mtime + 10))
    p2 = book_text.load_paragraphs(7, epub, data)
    assert p2 == p1
    assert json.loads(cache.read_text())["src_mtime"] == epub.stat().st_mtime


def test_bm25_chinese_hit(epub, tmp_path):
    out = book_text.relevant_passages(7, epub, "经济腾飞的挑战是什么", data_dir=tmp_path)
    assert "【书中其他相关段落】" in out
    assert "经济腾飞" in out


def test_bm25_english_hit(epub, tmp_path):
    out = book_text.relevant_passages(7, epub, "What were his childhood hobbies?", data_dir=tmp_path)
    assert "Photography" in out


def test_overlapping_paragraph_skipped(epub, tmp_path):
    # 把最佳段全文塞给 context，它应被去重，退而返回同样命中查询的次优段
    ctx = "这座港口城市在经济腾飞的道路上经历了许多挑战。"
    out = book_text.relevant_passages(7, epub, "经济腾飞的挑战", context=ctx, data_dir=tmp_path)
    assert "经济腾飞的道路上" not in out
    assert "社会结构发生了深刻的变化" in out


def test_build_messages_survives_failure(epub, tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app, "book_file", lambda bid: (_ for _ in ()).throw(RuntimeError("no calibre")))
    req = {"book_id": 7, "selection": "选段", "question": "这说明了什么？",
           "context_before": "前文", "context_after": "后文"}
    msgs = app.build_messages(req, "测试书")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert "这说明了什么" in msgs[1]["content"]
    assert "书中其他相关段落" not in msgs[1]["content"]


def test_build_messages_with_passages(epub, tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app, "book_file", lambda bid: (epub, "epub"))  # 合并 PDF 支持后 book_file 返回 (路径, 格式)
    req = {"book_id": 7, "selection": "选段", "question": "经济腾飞的挑战",
           "context_before": "前文", "context_after": "后文"}
    msgs = app.build_messages(req, "测试书")
    assert "【书中其他相关段落】" in msgs[1]["content"]
    assert "经济腾飞" in msgs[1]["content"]
    assert len(msgs[1]["content"]) <= 6000 + 6000 + 4000 + 500


def test_build_messages_pdf_book_skips_passages(epub, tmp_path, monkeypatch):
    # 10-07 合并时发现：book_file 改返回元组后，这里曾把元组当路径用、异常被吞，相关段落恒为空
    import app
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app, "book_file", lambda bid: (epub, "pdf"))
    req = {"book_id": 7, "selection": "选段", "question": "问题", "context_before": "", "context_after": ""}
    assert "书中其他相关段落" not in app.build_messages(req, "测试书")[1]["content"]
