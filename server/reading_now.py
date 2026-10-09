"""在读：最近在读、还没读完的书。

来源：微信读书（近 8 周按周、近 3 个月按月的「读得最多」排行 → 逐本 /book/getprogress 取进度与最后阅读时间）、
本 App 的阅读事件、ext_books 里 source='reading-log' 的手工阅读日志（没有时长数据）。微信读书 /shelf/sync 自 2026-09-21 恒 -202，所以不从书架取。
累计读满 1 小时（两边合计）才算在读。排行只列单周期读满 5 分钟的书，一周里读的书超过 10 本时排在后面的会漏。
用法：python reading_now.py refresh | list
"""
from __future__ import annotations

import datetime as dt
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import app  # noqa: E402
from import_history import gw, norm, ts  # noqa: E402

DAYS = 60            # 最后一次阅读在 60 天内才算「在读」
MIN_SECONDS = 3600   # 累计读满 1 小时（微信读书 + 本 App 合计）才算在读；不到 1 小时多是随便翻翻
DONE_PCT = 97        # 微信读书读到最后几页常不点「读完」，≥97% 当读完
IDLE_GAP = 600       # 本 App 没有阅读时长：相邻两次事件（翻页即记 progress）间隔 ≤10 分钟的累加为阅读时间，更长的算离开
SCHEMA = """CREATE TABLE IF NOT EXISTS weread_progress(
  book_id TEXT PRIMARY KEY, title TEXT, author TEXT, cover TEXT, progress INTEGER, read_seconds INTEGER,
  last_read_ts TEXT, calibre_id INTEGER, refreshed_ts TEXT, deeplink TEXT)"""


def _db():
    c = app.db()
    c.execute(SCHEMA)
    if "deeplink" not in {r[1] for r in c.execute("PRAGMA table_info(weread_progress)")}:
        c.execute("ALTER TABLE weread_progress ADD COLUMN deeplink TEXT")
    return c


def calibre_index() -> dict[str, list]:
    with app.calibre() as c:
        rows = c.execute(app.BOOK_SQL).fetchall()
    idx: dict[str, list] = {}
    for r in rows:
        if r["epub"]:
            idx.setdefault(norm(r["title"]), []).append(r)
    return idx


def match(idx, title: str, author: str) -> int | None:
    if " - " in title:  # 导入书名形如「Title - Author」
        title = title.rsplit(" - ", 1)[0]
    cands = idx.get(norm(title), []) if norm(title) else []
    if len(cands) > 1 and author:
        a = norm(author)[:4]
        cands = [r for r in cands if a and a in norm(r["authors"] or "")] or cands
    return cands[0]["id"] if len(cands) == 1 else None


def refresh() -> int:
    now = int(time.time())
    periods = [("weekly", now - 7 * 86400 * i) for i in range(8)] + [("monthly", now - 30 * 86400 * i) for i in range(3)]
    books: dict[str, dict] = {}
    for mode, base in periods:
        d = gw("/readdata/detail", mode=mode, baseTime=0 if base == now else base)
        for x in d.get("readLongest", []):
            b = x.get("book")
            if b and b.get("bookId"):
                books[b["bookId"]] = b
        time.sleep(0.3)
    idx = calibre_index()
    stamp = ts(now)
    with _db() as c:
        for bid, b in books.items():
            p = gw("/book/getprogress", bookId=bid).get("book", {})
            c.execute("""INSERT OR REPLACE INTO weread_progress VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (bid, b.get("title"), b.get("author"), b.get("cover"), p.get("progress"), p.get("readingTime"),
                       ts(p.get("updateTime")), match(idx, b.get("title") or "", b.get("author") or ""), stamp, b.get("deepLink")))
            time.sleep(0.3)
    print(f"weread_progress: {len(books)} books refreshed", flush=True)
    return len(books)


_lock = threading.Lock()


def refresh_if_stale(hours: int = 3) -> None:
    """后台刷新，不挡页面；正在刷就跳过。"""
    with _db() as c:
        last = c.execute("SELECT max(refreshed_ts) FROM weread_progress").fetchone()[0]
    if last and last > ts(time.time() - hours * 3600):
        return
    if not _lock.acquire(blocking=False):
        return
    def run():
        try:
            refresh()
        except Exception as e:  # noqa: BLE001
            print(f"reading_now refresh failed: {e}", flush=True)
        try:
            pair_editions()
        except Exception as e:  # noqa: BLE001
            print(f"reading_now pair_editions failed: {e}", flush=True)
        finally:
            _lock.release()
    threading.Thread(target=run, daemon=True).start()


def own_seconds(c) -> dict[int, float]:
    """本 App 里每本书的阅读秒数：同一本书相邻事件间隔 ≤IDLE_GAP 的累加。"""
    out: dict[int, float] = {}
    prev: dict[int, float] = {}
    for r in c.execute("""SELECT book_id, julianday(ts) * 86400 t FROM events WHERE book_id IS NOT NULL
                          AND device != 'test-cli' ORDER BY book_id, ts"""):
        b, t = r["book_id"], r["t"]
        if b in prev and t - prev[b] <= IDLE_GAP:
            out[b] = out.get(b, 0) + t - prev[b]
        prev[b] = t
    return {b: round(v) for b, v in out.items()}  # julianday 浮点误差，按整秒比门槛


EDITION_SCHEMA = """CREATE TABLE IF NOT EXISTS same_work(
  ka TEXT, kb TEXT, same INTEGER, ts TEXT, PRIMARY KEY(ka, kb))"""


def _collect() -> tuple[dict[str, dict], dict[str, float]]:
    """60 天内读过、没读完的所有书（还没按时长过滤），以及每个 key 的累计阅读秒数（微信读书 + 本 App）。"""
    since = ts(time.time() - DAYS * 86400)
    out: dict[str, dict] = {}
    secs: dict[str, float] = {}

    def put(key, item, seconds=0):
        secs[key] = secs.get(key, 0) + seconds
        old = out.get(key)
        if not old or (item["last_ts"] or "") > (old["last_ts"] or ""):
            if old:
                item["sources"] = sorted(set(old["sources"]) | set(item["sources"]))
                # bugfix-1007-B4: 更晚那条不能把旧条里非空的字段（cover_url/weread_url 等）顶掉
                for k, v in old.items():
                    if v not in (None, "", []) and item.get(k) in (None, ""):
                        item[k] = v
            out[key] = item
        else:
            old["sources"] = sorted(set(old["sources"]) | set(item["sources"]))

    with _db() as c:
        wr = c.execute("""SELECT * FROM weread_progress WHERE progress < ? AND last_read_ts >= ?""",
                       (DONE_PCT, since)).fetchall()
        own = c.execute("""SELECT book_id, max(ts) last_ts FROM events WHERE book_id IS NOT NULL
                           AND device != 'test-cli' GROUP BY book_id HAVING last_ts >= ?""", (since,)).fetchall()
        own_secs = own_seconds(c)
        fractions = {r[0]: r[1] for r in c.execute(
            "SELECT book_id, json_extract(payload,'$.fraction') FROM events WHERE id IN "
            "(SELECT max(id) FROM events WHERE type='progress' GROUP BY book_id)")}
        rwm = c.execute("""SELECT title, author, calibre_id, last_ts FROM ext_books
                           WHERE source='reading-log' AND status='reading' AND last_ts >= ?""", (since[:10],)).fetchall() \
            if app._has_ext(c) else []  # bugfix-1007-B2: 没跑过 import_history 的库没有 ext_books，照 app.history() 保护
    for r in wr:
        put(f"c{r['calibre_id']}" if r["calibre_id"] else f"t{norm(r['title'])}",
            {"id": r["calibre_id"], "title": r["title"], "authors": r["author"] or "", "fraction": (r["progress"] or 0) / 100,
             "last_ts": r["last_read_ts"], "cover_url": r["cover"], "weread_url": r["deeplink"], "sources": ["weread"]},
            r["read_seconds"] or 0)
    for r in own:
        f = fractions.get(r["book_id"])
        if f is not None and f >= 0.98:
            continue
        put(f"c{r['book_id']}", {"id": r["book_id"], "fraction": f, "last_ts": r["last_ts"], "sources": ["own-reader"]},
            own_secs.get(r["book_id"], 0))
    for r in rwm:
        put(f"c{r['calibre_id']}" if r["calibre_id"] else f"t{norm(r['title'])}",
            {"id": r["calibre_id"], "title": r["title"], "authors": r["author"] or "", "fraction": None,
             "last_ts": r["last_ts"], "sources": ["reading-log"]})
    ids = [x["id"] for x in out.values() if x.get("id")]
    if ids:
        with app.calibre() as c:
            cal = {r["id"]: r for r in c.execute(app.BOOK_SQL + f" WHERE b.id IN ({','.join('?' * len(ids))})", ids)}
        for x in out.values():
            r = cal.get(x.get("id"))
            if r:
                x.update(title=r["title"], authors=r["authors"] or "", epub=bool(r["epub"]), pdf=bool(r["pdf"]))
    return out, secs


def _edition_groups() -> dict[str, str]:
    """same_work 里判为同一本书不同语言版的 key → 组代表（并查集）。"""
    parent: dict[str, str] = {}

    def find(k):
        while parent.get(k, k) != k:
            k = parent[k]
        return k
    with _db() as c:
        c.execute(EDITION_SCHEMA)
        for a, b in c.execute("SELECT ka, kb FROM same_work WHERE same=1"):
            parent.setdefault(a, a)
            parent.setdefault(b, b)
            parent[find(a)] = find(b)
    return {k: find(k) for k in parent}


def list_now() -> list[dict]:
    out, secs = _collect()
    grp = _edition_groups()
    gsecs: dict[str, float] = {}
    for k in out:
        gsecs[grp.get(k, k)] = gsecs.get(grp.get(k, k), 0) + secs.get(k, 0)
    keep: dict[str, tuple[str, dict]] = {}  # 组代表 → 组里最近读的那一版
    for k, x in out.items():
        g = grp.get(k, k)
        # 阅读日志里的书没有时长数据且是主动登记在读，不受门槛约束
        if "reading-log" not in x["sources"] and gsecs[g] < MIN_SECONDS:
            continue
        x["read_minutes"] = round(gsecs[g] / 60)
        if g in keep:
            prev = keep[g][1]
            newer, older = (x, prev) if (x["last_ts"] or "") > (prev["last_ts"] or "") else (prev, x)
            newer["other_editions"] = newer.get("other_editions", []) + [older.get("title")] + older.get("other_editions", [])
            keep[g] = (k, newer)
        else:
            keep[g] = (k, x)
    items = sorted((x for _, x in keep.values()), key=lambda x: x["last_ts"] or "", reverse=True)
    for x in items:
        x.setdefault("epub", False)
        x.setdefault("pdf", False)
        x["last_day"] = (x["last_ts"] or "")[:10]
    return items


_CJK = __import__("re").compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def pair_editions(ask=None) -> int:
    """找出同一本书的中文版与外文版（中英文版合计满 1 小时也算在读）。
    只判还没判过的组合，结果存 same_work（同/不同都存，不重复问）。返回新判为同一本书的对数。"""
    out, secs = _collect()
    # 至少一边不满 1 小时才需要配：两边都满的各自就进在读了（另一边已满时也要配，好让界面显示最近读的那一版）
    read = {k: x for k, x in out.items() if secs.get(k, 0) > 0 and x.get("title")}
    short = {k for k in read if secs[k] < MIN_SECONDS}
    zh = {k: x for k, x in read.items() if _CJK.search(x["title"])}
    fo = {k: x for k, x in read.items() if k not in zh}
    with _db() as c:
        c.execute(EDITION_SCHEMA)
        done = {(a, b) for a, b in c.execute("SELECT ka, kb FROM same_work")}
    pairs = [tuple(sorted((a, b))) for a in zh for b in fo
             if (a in short or b in short) and tuple(sorted((a, b))) not in done]
    if not pairs:
        return 0
    lines = lambda d: "\n".join(f"{k}\t{x['title']}\t{x.get('authors') or ''}" for k, x in d.items())  # noqa: E731
    msgs = [{"role": "system", "content": "你判断书目对应关系，只输出 JSON。"},
            {"role": "user", "content": f"""下面两组书，A 组是中文/日文书名，B 组是外文书名（每行：key<TAB>书名<TAB>作者）。
找出哪些 A 和 B 是同一部作品的不同语言版本（译本与原著，或同一作品的两种译本）。不确定就不配对。
只输出 JSON 数组，每项 ["A的key","B的key"]，没有就输出 []。

A 组：
{lines({k: zh[k] for k in {a for p in pairs for a in p if a in zh}})}

B 组：
{lines({k: fo[k] for k in {b for p in pairs for b in p if b in fo}})}"""}]
    if ask is None:
        import llm
        ask = lambda m: llm.ask(llm.DEFAULT_BACKEND, m, sensitivity="personal", caller="reading_now.pair_editions")[0]  # noqa: E731
    raw = ask(msgs)
    import json
    import re
    m = re.search(r"\[.*\]", raw, re.S)
    same = {tuple(sorted(p)) for p in json.loads(m.group(0)) if len(p) == 2} if m else set()
    stamp = ts(time.time())
    with _db() as c:
        c.executemany("INSERT OR REPLACE INTO same_work VALUES(?,?,?,?)",
                      [(a, b, int((a, b) in same), stamp) for a, b in pairs])
    n = len(same & set(pairs))
    print(f"pair_editions: {len(pairs)} pairs judged, {n} same work", flush=True)
    return n


if __name__ == "__main__":
    app.init_db()
    if (sys.argv[1:] or ["list"])[0] == "refresh":
        refresh()
        pair_editions()
    for x in list_now():
        print(x["last_day"], x.get("title"), x.get("fraction"), x["sources"], "epub" if x["epub"] else "-")
