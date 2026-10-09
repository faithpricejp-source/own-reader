"""Claude API Message Batches 的通用作业层：提交、落库、轮询、收结果，交给各业务的处理函数。

精读批注（gloss）、每章导读（chapter）、整本翻译（translate）共用。批次与每个请求的业务数据存在 batch_job，
服务器重启后 resume() 给没收回的批次重新挂轮询。业务方用 register(kind, handler) 登记：
handler(payload: dict, book_id: int, text: str | None, err: str | None) -> 新状态（"done" / "error: …"）。
"""
from __future__ import annotations

import json
import threading
import time

import app
import llm

POLL_SEC = 60
SCHEMA = """CREATE TABLE IF NOT EXISTS batch_job(batch_id TEXT, custom_id TEXT, kind TEXT, book_id INT,
  payload TEXT, state TEXT DEFAULT 'pending', ts TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(batch_id, custom_id))"""
HANDLERS: dict = {}
_pollers: dict[str, threading.Thread] = {}


def register(kind: str, handler) -> None:
    HANDLERS[kind] = handler


def _db():
    c = app.db()
    c.execute(SCHEMA)
    return c


def pending(kind: str, book_id: int | None = None) -> list[dict]:
    with _db() as c:
        q = "SELECT * FROM batch_job WHERE state='pending' AND kind=?" + (" AND book_id=?" if book_id else "")
        rows = c.execute(q, (kind, book_id) if book_id else (kind,)).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def submit(kind: str, book_id: int, items: list[tuple[str, str, str, dict]], *, model: str = llm.API_MODEL,
           effort: str = "medium", est_usd: float = 0.0) -> str:
    """items = [(custom_id, system, user, payload)]。"""
    bid = llm.batch_submit([(cid, s, u) for cid, s, u, _ in items], model=model, effort=effort, est_usd=est_usd)
    with _db() as c:
        c.executemany("INSERT INTO batch_job(batch_id, custom_id, kind, book_id, payload) VALUES (?,?,?,?,?)",
                      [(bid, cid, kind, book_id, json.dumps(p, ensure_ascii=False)) for cid, _, _, p in items])
    print(f"{kind} batch {bid}: book {book_id}, {len(items)} requests, est ${est_usd:.2f}", flush=True)
    ensure_poller(bid)
    return bid


def collect(batch_id: str) -> str:
    if llm.batch_status(batch_id)["status"] != "ended":
        return "running"
    with _db() as c:
        rows = {r["custom_id"]: r for r in c.execute("SELECT * FROM batch_job WHERE batch_id=? AND state='pending'",
                                                     (batch_id,))}
    for cid, text, err in llm.batch_results(batch_id):
        r = rows.get(cid)
        if not r:
            continue
        try:
            state = HANDLERS[r["kind"]](json.loads(r["payload"]), r["book_id"], text, err)
        except Exception as e:  # noqa: BLE001
            state = f"error: {str(e)[:120]}"
        with _db() as c:
            c.execute("UPDATE batch_job SET state=? WHERE batch_id=? AND custom_id=?", (state, batch_id, cid))
    with _db() as c:  # 结果里缺的（极少见）标失败，下次重提
        c.execute("UPDATE batch_job SET state='error: missing' WHERE batch_id=? AND state='pending'", (batch_id,))
    return "ended"


def _poll(batch_id: str) -> None:
    while True:
        try:
            if collect(batch_id) == "ended":
                print(f"batch {batch_id} collected", flush=True)
                return
        except Exception as e:  # noqa: BLE001 — 网络抖动，下一轮再试
            print(f"batch {batch_id} poll failed: {e}", flush=True)
        time.sleep(POLL_SEC)


def ensure_poller(batch_id: str) -> None:
    t = _pollers.get(batch_id)
    if not (t and t.is_alive()):
        _pollers[batch_id] = threading.Thread(target=_poll, args=(batch_id,), daemon=True)
        _pollers[batch_id].start()


def resume() -> None:
    with _db() as c:
        ids = {r[0] for r in c.execute("SELECT DISTINCT batch_id FROM batch_job WHERE state='pending'")}
    for bid in ids:
        ensure_poller(bid)
