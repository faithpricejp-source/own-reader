"""书库评分的反馈回路：书架上的逐本反馈 + 读书行为（读完 / 弃读）→ 每晚增量重算。

- 显式反馈：书架书卡上点的按钮，事件 type='book_feedback'，payload 带当时的预测分（记准确度用）。
- 隐式反馈：读书行为。读完且笔记 ≥5 条 = 很对胃口；读完 = 读完；开读过（累计 10 分钟–2 小时）但进度 1–9%、两周没再碰 = 搁置（弱负面）。
- 每晚 refresh：同步微信读书笔记与进度（可选，没配 key 就跳过）→ 检测隐式反馈 → 新入库的书分类打分 → 有新反馈且距上次重算 ≥7 天
  （或攒够 10 条）就重算反馈书的「邻居」（同作者、同二级分类里分最高的），每次 ≤2,000 本。
- 后端：OWN_READER_RESCORE_BACKEND（默认同 library_class 的后端）。适合换成便宜的后端做增量；
  分类沿用库里已有的，不让它重分。每批夹锚点，与首轮分数偏差 ≥0.5 星时整体平移回首轮的尺。
用法：python library_feedback.py refresh | eval | implicit
"""
from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import app  # noqa: E402
import library_class as lc  # noqa: E402

BACKEND = os.environ.get("OWN_READER_RESCORE_BACKEND") or lc.LIBRARY_BACKEND
MAX_RESCORE = 2000
RESCORE_EVERY_DAYS = 7
RESCORE_MIN_FEEDBACK = 10
VERDICT_CN = {"love": "很对胃口", "want": "想读", "read": "读过了", "no": "不感兴趣", "wrong_reason": "书行但理由不对",
              "finished_noted": "读完且笔记多", "finished": "读完", "abandoned": "开读后搁置"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS implicit_fb(calibre_id INTEGER, signal TEXT, detail TEXT, ts TEXT, PRIMARY KEY(calibre_id, signal));
CREATE TABLE IF NOT EXISTS lib_state(k TEXT PRIMARY KEY, v TEXT);
"""


def _db():
    c = lc._db()
    c.executescript(SCHEMA)
    return c


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _days_since(last: str) -> float:
    # Kimi-B-5: rescore_ts 存的是 UTC（now() 用 gmtime），解析也必须按 UTC——mktime 按本地时区
    # 解释，JST 下 days 恒偏大 0.375；值坏了回落 999，别让夜间 refresh 崩在最后一步
    if last == "0":
        return 999
    try:
        return (time.time() - calendar.timegm(time.strptime(last, "%Y-%m-%dT%H:%M:%SZ"))) / 86400
    except (TypeError, ValueError):
        return 999


def state(k: str, v: str | None = None) -> str | None:
    with _db() as c:
        if v is not None:
            c.execute("INSERT OR REPLACE INTO lib_state VALUES(?,?)", (k, v))
            return v
        r = c.execute("SELECT v FROM lib_state WHERE k=?", (k,)).fetchone()
        return r[0] if r else None


def detect_implicit() -> int:
    """从读书行为里找读完 / 弃读。只增不删：同一本同一种信号只记第一次。"""
    two_weeks = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 14 * 86400))
    found = []
    sig = lc.read_signal()
    for cid, h in sig.items():
        if h["status"] == "finished":
            n = h["highlights"] + h["thoughts"]
            found.append((cid, "finished_noted" if n >= 5 else "finished", f"笔记 {n} 条"))
    with _db() as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='weread_progress'").fetchone():
            for r in c.execute("""SELECT calibre_id, progress, read_seconds, last_read_ts FROM weread_progress
                                  WHERE calibre_id IS NOT NULL"""):
                if r["progress"] is not None and r["progress"] >= 97:
                    found.append((r["calibre_id"], "finished", "微信读书进度 ≥97%"))
                # 进度 0 = 上传书微信读书不给进度，判不了；读过 2 小时以上的是大部头慢读，不算搁置
                elif 0 < (r["progress"] or 0) < 10 and 600 <= (r["read_seconds"] or 0) < 7200 and (r["last_read_ts"] or "") < two_weeks:
                    found.append((r["calibre_id"], "abandoned",
                                  f"微信读书读了 {(r['read_seconds'] or 0) // 60} 分钟、进度 {r['progress']}%，最后一次 {r['last_read_ts'][:10]}"))
        # 用户亲口说读过/读完的书（比如在 Kindle 原生阅读器读的，微信读书没痕迹），不再判搁置
        told_read = {r["cid"] for r in explicit_rows(c) if r["v"] == "read"}
        c.execute(f"DELETE FROM implicit_fb WHERE signal='abandoned' AND calibre_id IN ({','.join('?' * len(told_read)) or 'NULL'})",
                  list(told_read))
        found = [f for f in found if not (f[1] == "abandoned" and f[0] in told_read)]
        before = c.execute("SELECT count(*) FROM implicit_fb").fetchone()[0]
        for cid, s, d in found:
            c.execute("INSERT OR IGNORE INTO implicit_fb VALUES(?,?,?,?)", (cid, s, d, now()))
        added = c.execute("SELECT count(*) FROM implicit_fb").fetchone()[0] - before
    print(f"隐式反馈：新增 {added} 条", flush=True)
    return added


def explicit_rows(c) -> list:
    return c.execute("""SELECT id, ts, json_extract(payload,'$.calibre_id') cid, json_extract(payload,'$.verdict') v,
        json_extract(payload,'$.comment') cm, json_extract(payload,'$.pred') pred FROM events
        WHERE type='book_feedback' ORDER BY id""").fetchall()


def feedback_lines(exclude: set[int] = frozenset()) -> str:
    """给评分上下文用：书架上的显式反馈（同一本以最后一次为准）+ 读书行为。"""
    with _db() as c:
        last = {}
        for r in explicit_rows(c):
            last[r["cid"]] = r
        imp = c.execute("SELECT calibre_id, signal, detail FROM implicit_fb WHERE signal='abandoned'").fetchall()
    ids = [i for i in list(last) + [r[0] for r in imp] if i not in exclude]
    if not ids:
        return "（还没有）"
    with app.calibre() as c:
        t = {r[0]: r[1] for r in c.execute(f"SELECT id, title FROM books WHERE id IN ({','.join('?' * len(ids))})", ids)}
    lines = [f"- 《{t.get(r['cid'], r['cid'])}》→ {VERDICT_CN.get(r['v'], r['v'])}" + (f"；他说：{r['cm']}" if r["cm"] else "")
             for r in last.values() if r["cid"] not in exclude]
    lines += [f"- 《{t.get(r[0], r[0])}》→ 开读后搁置两周以上（{r[2]}；可能只是暂停，弱负面信号）" for r in imp if r[0] not in exclude]
    return "\n".join(lines)


def neighbors(fb_ids: list[int]) -> list[int]:
    """反馈书的邻居，按优先级：同作者 > 同二级分类里综合分最高的。"""
    if not fb_ids:
        return []
    ph = ",".join("?" * len(fb_ids))
    out: list[int] = []
    with app.calibre() as c:
        out += [r[0] for r in c.execute(f"""SELECT DISTINCT l2.book FROM books_authors_link l1
            JOIN books_authors_link l2 ON l2.author = l1.author WHERE l1.book IN ({ph})""", fb_ids)]
    with _db() as c:
        out += [r[0] for r in c.execute(f"""SELECT b2.calibre_id FROM book_class b1 JOIN book_class b2
            ON b2.secondary_cat = b1.secondary_cat AND b2.primary_cat = b1.primary_cat
            WHERE b1.calibre_id IN ({ph}) ORDER BY b2.want + b2.need + COALESCE(b2.should, 0) DESC LIMIT ?""",
                                         (*fb_ids, MAX_RESCORE))]
    seen, uniq = set(), []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq[:MAX_RESCORE]


def opus_anchor_ref() -> dict[int, dict]:
    with _db() as c:
        rows = c.execute("""SELECT calibre_id, avg(want) w, avg(need) n, avg(should) sh FROM book_class_anchor
                            WHERE batch LIKE 'b%' AND should IS NOT NULL GROUP BY calibre_id""").fetchall()
    return {r[0]: {"w": r[1], "n": r[2], "sh": r[3]} for r in rows}


def rescore(ids: list[int], label: str) -> int:
    """用 BACKEND 重算这些书的三个分；分类沿用库里已有的（新书除外）。锚点偏差 ≥0.5 星整体平移回首轮的尺。"""
    if not ids:
        return 0
    lib = {b["id"]: b for b in lc.library()}
    books = [lib[i] for i in ids if i in lib]
    anchors = lc.pick_anchors(list(lib.values()))
    with _db() as c:
        fixed = {r[0]: (r[1], r[2]) for r in c.execute("SELECT * FROM book_class_fixed")}
        fixed.update({r[0]: (r[1], r[2]) for r in c.execute("SELECT calibre_id, primary_cat, secondary_cat FROM book_class")})
    ctx = lc.want_context(set())
    ref = opus_anchor_ref()
    done = 0
    for k in range(0, len(books), lc.BATCH):
        chunk = books[k:k + lc.BATCH]
        name = f"bn-{label}-{k // lc.BATCH}"
        try:
            got = lc.classify_batch(name, chunk, anchors, ctx, fixed, backend=BACKEND, write=False)
        except Exception as e:  # noqa: BLE001
            print(f"  {name} FAILED {e}", flush=True)
            continue
        shift = {}
        for key in ("w", "n", "sh"):
            d = [lc._score(got[a["id"]].get(key), 1, 5) - ref[a["id"]][key] for a in anchors if a["id"] in got and a["id"] in ref]  # Kimi-B-8
            off = sum(d) / len(d) if d else 0.0
            shift[key] = round(off) if abs(off) >= 0.5 else 0
        stamp = now()
        with _db() as c:
            for b in chunk:
                r = got.get(b["id"])
                if not r:
                    continue
                p, s = fixed.get(b["id"], (str(r.get("p", "")), str(r.get("s", ""))))
                if p not in lc.TAXONOMY:
                    p, s = "其他", "无法判断"
                val = {key: min(5, max(1, lc._score(r.get(key), 1, 5) - shift[key])) for key in ("w", "n", "sh")}  # Kimi-B-8
                c.execute("""INSERT OR REPLACE INTO book_class(calibre_id,primary_cat,secondary_cat,cat_source,want,need,
                             reason,batch,model,ts,should,known) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (b["id"], p, s, "fixed" if b["id"] in fixed and fixed[b["id"]] == (p, s) else BACKEND, val["w"], val["n"],
                           str(r.get("r") or "")[:100], name, BACKEND, stamp, val["sh"], lc._score(r.get("k"), 0, 2)))  # Kimi-B-8
                done += 1
        print(f"  {name}: {len(got)} 本，锚点平移 {shift}", flush=True)
    return done


def busy() -> bool:
    """全库首轮还在跑时，夜间增量让路。"""
    ps = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
    return "library_class.py run" in ps


def refresh() -> None:
    try:  # 笔记同步很轻，不受全库任务让路影响
        import import_history
        import_history.main(["weread", "match"])
    except Exception as e:  # noqa: BLE001
        print(f"微信读书笔记同步失败：{e}", flush=True)
    if busy():
        print(f"{now()} 全库任务还在跑，今晚跳过", flush=True)
        return
    try:
        import reading_now
        reading_now.refresh()
    except Exception as e:  # noqa: BLE001 — 微信读书接口不稳，不挡后面
        print(f"微信读书进度刷新失败：{e}", flush=True)
    detect_implicit()
    with _db() as c:
        have = {r[0] for r in c.execute("SELECT calibre_id FROM book_class")}
    new = [b["id"] for b in lc.library() if b["id"] not in have]
    print(f"新入库未分类：{len(new)} 本", flush=True)
    rescore(new, "new")
    last = state("rescore_ts") or "0"
    with _db() as c:
        fb = [r["cid"] for r in explicit_rows(c) if r["ts"] > last]
        fb += [r[0] for r in c.execute("SELECT calibre_id FROM implicit_fb WHERE ts > ?", (last,))]
    fb = list(dict.fromkeys(i for i in fb if i))
    days = _days_since(last)  # Kimi-B-5
    print(f"上次重算后新反馈 {len(fb)} 条，距上次 {days:.0f} 天", flush=True)
    if fb and (days >= RESCORE_EVERY_DAYS or len(fb) >= RESCORE_MIN_FEEDBACK):
        # 反馈书本身不重算：显式反馈直接显示用户的结论；「搁置」是弱信号，实测模型会把它直接打成 1 星，过度反应
        n = rescore([i for i in neighbors(fb) if i not in set(fb)], time.strftime("%Y%m%d"))
        state("rescore_ts", now())
        print(f"邻居重算 {n} 本", flush=True)
    eval_report()


def eval_report() -> None:
    """准确度记录：每条显式反馈和反馈当时的预测分比。看「不感兴趣」的预测想读是不是越来越低、「很对胃口」是不是越来越高。"""
    with _db() as c:
        rows = explicit_rows(c)
        imp = c.execute("""SELECT f.signal, b.want FROM implicit_fb f JOIN book_class b ON b.calibre_id = f.calibre_id""").fetchall()
    by: dict[str, list] = {}
    for r in rows:
        pred = json.loads(r["pred"]) if r["pred"] else {}
        if pred.get("want"):
            by.setdefault(r["v"], []).append(pred["want"])
    print("显式反馈 vs 当时预测的想读：" + ("；".join(f"{VERDICT_CN.get(v, v)} {len(x)} 条 均值 {sum(x) / len(x):.2f}"
                                          for v, x in by.items()) or "（还没有）"))
    imp_by: dict[str, list] = {}
    for s, w in imp:
        imp_by.setdefault(s, []).append(w)
    print("读书行为 vs 现在的想读：" + ("；".join(f"{VERDICT_CN.get(s, s)} {len(x)} 本 均值 {sum(x) / len(x):.2f}"
                                       for s, x in imp_by.items()) or "（还没有）"))


if __name__ == "__main__":
    app.init_db()
    cmd = (sys.argv[1:] or ["eval"])[0]
    if cmd == "refresh":
        refresh()
    elif cmd == "implicit":
        detect_implicit()
    else:
        eval_report()
