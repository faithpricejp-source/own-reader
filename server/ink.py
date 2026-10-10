"""手写批注识别：安卓原生版在书页上的手写（ink 事件）→ 画成 PNG → 识别成文字 → 存成带 ink_id 的 note 事件。

引擎由 OWN_READER_INK_ENGINE 选择，默认 off：
- off：笔迹照常入库、网页照常显示，不调用任何模型；
- openai：任意 OpenAI 兼容的视觉接口（OWN_READER_INK_URL、OWN_READER_INK_MODEL），标准库 urllib，图片用 data URI；
- claude：走 llm.api_call，模型 OWN_READER_INK_CLAUDE_MODEL（默认 claude-sonnet-5-5）。

识别时把手写旁边的原文一起给模型，只转写手写本身。后台线程每 20 秒看一次；新 ink 事件进来时 kick() 立即唤醒。
服务器启动时 start()。引擎为 off 时线程仍在，但 recognize_pending 直接返回。
"""
from __future__ import annotations

import base64
import json
import os
import struct
import tempfile
import threading
import traceback
import urllib.request
import zlib
from pathlib import Path

import app
import llm

POLL_S = 20
PROMPT = ("这是读者在电子墨水屏的书页上手写的一条批注。手写旁边的原文是：「{context}」。"
          "请逐字转写手写的内容（不是原文），保留原有的标点；认不准的字用［?］标出。只输出转写结果。")
_ENGINES = {"claude", "openai", "off"}
_wake = threading.Event()
_thread: threading.Thread | None = None


def engine_name() -> str:
    name = os.environ.get("OWN_READER_INK_ENGINE", "off").strip().lower()
    return name if name in _ENGINES else "off"


def claude_model() -> str:
    return os.environ.get("OWN_READER_INK_CLAUDE_MODEL", "claude-sonnet-5-5").strip() or "claude-sonnet-5-5"


def pending() -> list[dict]:
    """所有书里还没识别、也没被删的手写。"""
    with app.db() as c:
        rows = c.execute("SELECT id, book_id, type, payload FROM events WHERE type IN ('ink','note','delete') "
                         "AND payload LIKE '%ink_id%' ORDER BY id").fetchall()
    inks: dict[str, dict] = {}
    for r in rows:
        try:
            p = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(p, dict) or not p.get("ink_id"):
            continue
        if r["type"] == "ink":
            inks[p["ink_id"]] = {"id": r["id"], "book_id": r["book_id"], "ink_id": p["ink_id"],
                                 "strokes": p.get("strokes") or [], "context": p.get("context") or ""}
        else:  # 识别过的（note）和删掉的（delete）都不用再识别
            inks.pop(p["ink_id"], None)
    return list(inks.values())


def _png(width: int, height: int, rgb: bytearray) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")


def _plot(rgb: bytearray, width: int, height: int, x: int, y: int, radius: int) -> None:
    for yy in range(y - radius, y + radius + 1):
        if yy < 0 or yy >= height:
            continue
        for xx in range(x - radius, x + radius + 1):
            if xx < 0 or xx >= width:
                continue
            i = (yy * width + xx) * 3
            rgb[i:i + 3] = b"\x00\x00\x00"


def _line(rgb: bytearray, width: int, height: int, x0: float, y0: float, x1: float, y1: float, thick: int) -> None:
    ax, ay, bx, by = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    dx, dy = abs(bx - ax), abs(by - ay)
    sx, sy = (1 if ax < bx else -1), (1 if ay < by else -1)
    err = dx - dy
    radius = max(1, thick // 2)
    while True:
        _plot(rgb, width, height, ax, ay, radius)
        if ax == bx and ay == by:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            ax += sx
        if e2 < dx:
            err += dx
            ay += sy


def render_png(strokes: list, out: Path, pad: int = 24) -> Path:
    """笔画（[x, y, pressure, ...]，相对锚点的像素）画成白底黑字 PNG。只用标准库，不依赖 Pillow。"""
    xs = [s[i] for s in strokes for i in range(0, len(s) - 2, 3)]
    ys = [s[i + 1] for s in strokes for i in range(0, len(s) - 2, 3)]
    if not xs:
        xs, ys = [0.0], [0.0]
    x0, y0 = min(xs) - pad, min(ys) - pad
    w = max(int(max(xs) - x0 + pad) + 1, 32)
    h = max(int(max(ys) - y0 + pad) + 1, 32)
    rgb = bytearray(b"\xff" * (w * h * 3))
    for s in strokes:
        pts = [(s[i] - x0, s[i + 1] - y0, s[i + 2]) for i in range(0, len(s) - 2, 3)]
        for a, b in zip(pts, pts[1:]):
            _line(rgb, w, h, a[0], a[1], b[0], b[1], max(2, int(2 + 4 * b[2])))
    out.write_bytes(_png(w, h, rgb))
    return out


def _openai(png: Path, context: str) -> tuple[str, str]:
    base = os.environ.get("OWN_READER_INK_URL", "").strip().rstrip("/")
    model = os.environ.get("OWN_READER_INK_MODEL", "").strip()
    if not base or not model:
        raise RuntimeError("openai ink engine needs OWN_READER_INK_URL and OWN_READER_INK_MODEL")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    data_uri = "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()
    body = {"model": model, "max_tokens": 400, "messages": [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": PROMPT.format(context=context[:300])},
        ],
    }]}
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("OWN_READER_INK_API_KEY", "").strip()  # 托管接口要密钥时用；本机服务一般不用
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    try:
        text = (resp["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected ink response: {json.dumps(resp)[:300]}")
    return text, resp.get("model") or model


def _claude(png: Path, context: str) -> tuple[str, str]:
    img = base64.b64encode(png.read_bytes()).decode()
    user = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
            {"type": "text", "text": PROMPT.format(context=context[:300])}]
    text, model, _ = llm.api_call("你在转写读者的手写读书批注。", user, model=claude_model(), effort="low")
    return text.strip(), model


def _engine():
    name = engine_name()
    if name == "claude":
        return _claude
    if name == "openai":
        return _openai
    return None


def recognize_pending(limit: int = 20) -> int:
    eng = _engine()
    if eng is None:
        return 0
    n = 0
    for p in pending()[:limit]:
        with tempfile.TemporaryDirectory() as td:
            png = render_png(p["strokes"], Path(td) / "ink.png")
            text, source = eng(png, p["context"])
        if not text:
            continue
        if app.save_ink_recognition(p, text, source):
            n += 1
    return n


def kick() -> None:
    _wake.set()


def _loop() -> None:
    while True:
        try:
            if recognize_pending():
                continue
        except Exception:  # noqa: BLE001 —— 模型没起来、网络断：下一轮再试
            traceback.print_exc()
        _wake.wait(POLL_S)
        _wake.clear()


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="ink-recognizer", daemon=True)
    _thread.start()
