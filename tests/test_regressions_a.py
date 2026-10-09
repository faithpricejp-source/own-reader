"""代码审查发现 C-1…C-6 的复现测试（注释里的 Kimi-N / C-N 是审查条目编号）。

JS/Swift 的 UI 行为无法在测试里跑浏览器复现；这里只放「能直接调用真文件里真函数」的那部分证据。
"""
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import app  # server/app.py（conftest 已把 server/ 加入 sys.path）


class _Headers:
    def __init__(self, body: bytes):
        self._d = {"Content-Length": str(len(body))}

    def get(self, k, default=None):
        return self._d.get(k, default)


def call_handler(method: str, path: str, payload=None):
    """直接驱动 server/app.py 里真实的 Handler.do_GET / do_POST，返回 (状态码, 响应体)。"""
    body = b"" if payload is None else json.dumps(payload).encode()
    h = app.Handler.__new__(app.Handler)
    h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
    h.headers, h.path = _Headers(body), path
    h.requestline = f"{method} {path} HTTP/1.1"
    h.request_version = "HTTP/1.1"
    h.client_address = ("127.0.0.1", 0)
    getattr(h, f"do_{method}")()
    raw = h.wfile.getvalue()
    head, _, out = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, out


def test_C1_write_failure_returns_non200_valid_json():
    """C-1 的前提：写接口失败时服务器返回的是「非 200 + 合法 JSON」。

    真链路：前端 post('/api/events', ...) → Handler.do_POST → app.add_event
    对未知 type 抛 ValueError → do_POST 的 except 分支 self._json({"error": ...}, 500)。
    因为 body 仍是合法 JSON，index.html 里的 `post = ... .then(r => r.json())`
    不会 reject、也不会 throw，调用方拿到 {error: "..."} 却当成功用。
    """
    status, out = call_handler("POST", "/api/events", {"device": "test", "type": "no_such_type"})
    assert status == 500, "do_POST 的失败路径应当返回 500"
    body = json.loads(out)  # 能解析成功 == 前端 r.json() 正常 resolve
    assert "error" in body, f"失败响应应带 error 字段，实际 {body!r}"


def test_C1_read_failure_returns_non200_valid_json():
    """C-1 的 GET 侧前提：读接口出错时同样是「非 200 + 合法 JSON」，body 里没有集合字段。

    真链路：Handler.do_GET 处理 /api/notes?calibre_id=abc 时 int("abc") 抛 ValueError
    → except 分支 self._json({"error": str(e)}, 500)。
    此时 index.html:36 的 api() resolve 成 {"error": ...}，`.books`/`.notes` 是 undefined，
    historyView 里 historyCache.filter(...) 抛 TypeError（未处理 rejection、区域空白）。
    """
    status, out = call_handler("GET", "/api/notes?calibre_id=abc")
    assert status == 500
    body = json.loads(out)
    assert "notes" not in body and "error" in body


NODE_SRC = """
// 加载仓库里真那份 vendor/marked.min.js + 页面共用的 web/safe_md.js，md() 照搬 web/reader.js
globalThis.marked = require(process.argv[1]).marked;
require(process.argv[2]);
const md = s => globalThis.marked.parse(String(s ?? ""), { breaks: true });
for (const c of ["[点我](javascript:alert(1))", "[x](jav&#x09;ascript:alert(2))",
                 "[a](data:text/html;base64,PHNjcmlwdD5hbGVydCgzKTwvc2NyaXB0Pg==)",
                 "![i](javascript:alert(4))", "参考 [官方](https://example.com/a)",
                 "<script>alert(5)</script>", "<img src=x onerror=alert(6)>"])
  console.log(JSON.stringify(md(c)));
"""


def run_real_marked():
    """用 node 跑仓库里真实的 vendor/marked.min.js（不联网、不装依赖），返回每条输入的渲染结果。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 node，无法跑真 vendored marked")
    lib = str(ROOT / "vendor" / "marked.min.js")
    out = subprocess.run([node, "-e", NODE_SRC, lib, str(ROOT / "web" / "safe_md.js")], capture_output=True, text=True, check=True)
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def test_C5_dangerous_link_protocols_are_stripped():
    """C-5：AI 回答/笔记里的 Markdown 链接只放行 http/https。
    跑真 vendor/marked.min.js + web/safe_md.js：危险协议链接只留文字、图片去掉，原始 HTML 仍丢弃，https 链接照常。"""
    src = (ROOT / "vendor" / "marked.min.js").read_text(encoding="utf-8", errors="replace")
    assert "marked v15" in src.splitlines()[1]
    rendered = run_real_marked()
    js_link, entity_link, data_link, img_link, https_link, script_raw, img_raw = rendered
    for out in (js_link, entity_link, data_link, img_link):
        assert "href=" not in out and "src=" not in out, out
    assert "点我" in js_link
    assert '<a href="https://example.com/a">官方</a>' in https_link
    assert script_raw.strip() == "" and img_raw.strip() == ""


def test_C5_pages_load_safe_md_after_marked():
    for page in ("reader.html", "index.html"):
        html = (ROOT / "web" / page).read_text(encoding="utf-8")
        assert html.index("/vendor/marked.min.js") < html.index("/web/safe_md.js"), page

