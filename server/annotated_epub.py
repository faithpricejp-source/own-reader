"""「完全导读版」EPUB：原书 + 卷首全书导读 + 每章开头本章导读 + 段落下精读批注，给墨水屏离线读。

批注来自 gloss_items（段落哈希同网页版，未批的段不插），导读来自 book_dossier。批注作为兄弟 <div class="or-gl">
插在段落元素之后；卷首导读是新加的 or-dossier.xhtml，放在 spine 第一个文档之后，并加进目录。书名后缀「（导读版）」。
Markdown → XHTML 用 markdown 库，lxml 已在 requirements.txt 里。不把 markdown 写进 requirements.txt，用 uv 跑：

  uv run --no-project --with markdown --with lxml python -I server/annotated_epub.py <calibre_id> [输出路径]

在仓库根目录执行。不传输出路径时写到 <OWN_READER_DATA>/annotated/<书号> 导读版 [or<书号>].epub。
文件名里的 [or<书号>] 给安卓原生版回写划线时认书。安卓 App 不在本仓库。
"""
from __future__ import annotations

import html
import posixpath
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lxml import etree  # noqa: E402

import app  # noqa: E402
import bilingual as bl  # noqa: E402
import dossier  # noqa: E402
import gloss  # noqa: E402
from translate import h  # noqa: E402

XHTML = "http://www.w3.org/1999/xhtml"
OPF = "http://www.idpf.org/2007/opf"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
CSS = """.or-gl { margin: .3em 0 .8em 1.2em; padding: .2em 0 .2em .6em; border-left: 2px solid #000;
  font-size: .82em; line-height: 1.45; text-indent: 0; text-align: left; }
.or-gl p { margin: .25em 0; text-indent: 0; }
.or-gl .t { font-weight: bold; }
.or-gl .q { font-style: italic; }
.or-gl .c { font-size: .85em; }
.or-gl-ch { margin: .5em 0 1.2em 0; padding: .4em .6em; border: 1px solid #000; font-size: .85em; line-height: 1.5;
  text-indent: 0; text-align: left; }
.or-gl-ch h3 { font-size: 1em; margin: .6em 0 .2em; }
.or-gl-ch p, .or-gl-ch li { text-indent: 0; margin: .2em 0; }
.or-dossier { text-indent: 0; line-height: 1.6; }
.or-dossier p { text-indent: 0; margin: .5em 0; }
.or-dossier table { border-collapse: collapse; font-size: .85em; }
.or-dossier td, .or-dossier th { border: 1px solid #000; padding: .2em .4em; vertical-align: top; }"""


def md(text: str) -> str:
    """Markdown → XHTML。模型常在「**小标题**」下一行直接起列表，python-markdown 要求列表前有空行，补上。"""
    import re
    import markdown
    lines, prev = [], ""
    for line in (text or "").splitlines():
        is_item = bool(re.match(r"\s*([-*+]|\d+\.)\s", line))
        prev_item = bool(re.match(r"\s*([-*+]|\d+\.)\s", prev))
        if is_item and prev.strip() and not prev_item and not line.startswith(("  ", "\t")):
            lines.append("")
        lines.append(line)
        prev = line
    return markdown.markdown("\n".join(lines), extensions=["tables"], output_format="xhtml")


def _frag(html_text: str, cls: str):
    """Markdown 产出的 XHTML 片段 → 带命名空间的 <div class=cls>；解析失败就退成纯文本。"""
    try:
        return etree.fromstring(f'<div xmlns="{XHTML}" class="{cls}">{html_text}</div>')
    except etree.XMLSyntaxError:
        d = etree.Element(f"{{{XHTML}}}div", {"class": cls})
        d.text = etree.fromstring(f"<x>{html_text}</x>", etree.HTMLParser()).xpath("string()") if html_text else ""
        return d


def _gloss_div(items: list[dict]):
    out = []
    chap = [it for it in items if it["type"] == "本章"]
    rest = [it for it in items if it["type"] != "本章"]
    for it in chap:
        out.append(_frag("<p><b>本章导读</b></p>" + md(it["note"]), "or-gl-ch"))
    if rest:
        parts = []
        for it in rest:
            note = f"〔模型判断{('，把握' + it['conf']) if it.get('conf') else ''}〕" if it["type"] == "存疑" or it.get("conf") == "低" else ""
            parts.append(f'<p><span class="t">{html.escape(it["type"])}</span> <span class="q">{html.escape(it["quote"])}</span>'
                         f' — {html.escape(it["note"])}{f" <span class=\"c\">{html.escape(note)}</span>" if note else ""}</p>')
        out.append(_frag("".join(parts), "or-gl"))
    return out


def _add_css(root) -> None:
    head = root.find(f"{{{XHTML}}}head")
    if head is None:
        head = root.find("head")
    if head is not None:
        st = etree.SubElement(head, f"{{{XHTML}}}style" if root.tag.startswith("{") else "style", {"type": "text/css"})
        st.text = CSS


def build(book_id: int, out: Path | None = None) -> tuple[Path, dict]:
    f = app.book_file(book_id)
    if not f or f[1] != "epub":
        raise SystemExit("只支持 EPUB")
    epub = f[0]
    d = dossier.get(book_id)
    stats = {"paras_with_gloss": 0, "chapter_guides": 0, "dossier": bool(d and d.get("md"))}
    # 文件名带「[or<书号>]」：安卓原生版回写划线/评注时靠它认出是哪本书
    out = out or app.DATA_DIR / "annotated" / f"{book_id} 导读版 [or{book_id}].epub"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub) as zf:
        container = etree.fromstring(zf.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").get("full-path")
        opf_dir = posixpath.dirname(opf_path)
        opf = etree.fromstring(zf.read(opf_path))
        spine_docs = bl._spine_docs(zf)
        # 收集全书段落的批注
        docs = {}
        for name in spine_docs:
            if name not in zf.namelist():
                continue
            root = bl._parse(zf.read(name))
            if root is None:
                continue
            els = [el for el in root.iter() if bl._local(el.tag) in gloss.GL_TAGS
                   and not any(bl._local(x.tag) in gloss.GL_TAGS for x in el.iterdescendants())
                   and len(gloss._norm("".join(el.itertext()))) > 1]
            docs[name] = (root, els)
        hashes = [h(gloss._norm("".join(el.itertext()))) for _, els in docs.values() for el in els]
        cached = gloss.cached(hashes)
        # 目录页那一行常与正文章标题同文字（同哈希）：本章导读只挂在最后一次出现处，也就是正文里的章标题
        last_pos = {hp: i for i, hp in enumerate(hashes)}
        pos = -1
        for name, (root, els) in docs.items():
            changed = False
            for el in els:
                pos += 1
                hp = hashes[pos]
                items = cached.get(hp) or []
                if pos != last_pos[hp]:
                    items = [it for it in items if it["type"] != "本章"]
                if not items:
                    continue
                for node in reversed(_gloss_div(items)):
                    el.addnext(node)
                changed = True
                if any(it["type"] == "本章" for it in items):
                    stats["chapter_guides"] += 1
                if any(it["type"] != "本章" for it in items):
                    stats["paras_with_gloss"] += 1
            if changed:
                _add_css(root)
        # 卷首导读页
        dossier_name = posixpath.join(opf_dir, "or-dossier.xhtml") if opf_dir else "or-dossier.xhtml"
        title_el = opf.find(".//{http://purl.org/dc/elements/1.1/}title")
        title = title_el.text if title_el is not None else ""
        if title_el is not None:
            title_el.text = f"{title}（导读版）"
        dossier_xhtml = None
        if stats["dossier"]:
            dossier_xhtml = (f'<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="{XHTML}"><head><title>全书导读</title>'
                             f'<style type="text/css">{CSS}</style></head><body><div class="or-dossier">'
                             f'<h1>全书导读</h1><p>《{html.escape(title)}》 · 由 {html.escape(d.get("model") or "")} 读全书并联网核对后写成；'
                             f'书外事实附来源，查不到的标「未核」，【判断】为模型判断。</p>{md(d["md"])}</div></body></html>')
            manifest = opf.find(f"{{{OPF}}}manifest")
            etree.SubElement(manifest, f"{{{OPF}}}item", {"id": "or-dossier", "href": "or-dossier.xhtml",
                                                         "media-type": "application/xhtml+xml"})
            spine = opf.find(f"{{{OPF}}}spine")
            first = spine.find(f"{{{OPF}}}itemref")
            ref = etree.Element(f"{{{OPF}}}itemref", {"idref": "or-dossier"})
            (first.addnext(ref) if first is not None else spine.insert(0, ref))
        # 写出
        tmp = out.with_suffix(".tmp")
        ncx_name = None
        toc_id = opf.find(f"{{{OPF}}}spine").get("toc")
        if toc_id:
            it = opf.find(f"{{{OPF}}}manifest/{{{OPF}}}item[@id='{toc_id}']")
            if it is not None:
                ncx_name = posixpath.normpath(posixpath.join(opf_dir, it.get("href")))
        with zipfile.ZipFile(tmp, "w") as zo:
            zo.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            for info in zf.infolist():
                if info.filename == "mimetype":
                    continue
                data = zf.read(info.filename)
                if info.filename in docs:
                    data = etree.tostring(docs[info.filename][0], xml_declaration=True, encoding="utf-8")
                elif info.filename == opf_path:
                    data = etree.tostring(opf, xml_declaration=True, encoding="utf-8")
                elif info.filename == ncx_name and dossier_xhtml:
                    ncx = etree.fromstring(data)
                    nav = ncx.find(f"{{{NCX}}}navMap")
                    np_ = etree.Element(f"{{{NCX}}}navPoint", {"id": "or-dossier", "playOrder": "0"})
                    lab = etree.SubElement(np_, f"{{{NCX}}}navLabel")
                    etree.SubElement(lab, f"{{{NCX}}}text").text = "全书导读"
                    etree.SubElement(np_, f"{{{NCX}}}content", {"src": "or-dossier.xhtml"})
                    nav.insert(0, np_)
                    data = etree.tostring(ncx, xml_declaration=True, encoding="utf-8")
                zo.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
            if dossier_xhtml:
                zo.writestr(dossier_name, dossier_xhtml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
        tmp.replace(out)
    return out, stats


if __name__ == "__main__":
    bid = int(sys.argv[1])
    path, st = build(bid, Path(sys.argv[2]) if len(sys.argv) > 2 else None)
    print(path, st)
