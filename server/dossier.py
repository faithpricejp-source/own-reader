"""全书导读（「把书读厚」的总入口）。

走 Claude API（需 key，见 llm.py）：把整本书（带章节名）放进 Opus 5.5 的 1M 上下文，开联网搜索核书外事实，写一份固定结构的导读：
来龙去脉、想干嘛、结构、独特之处、偏颇与争议、反响、和你的关系、留给你的（残差）。
原则同精读批注：不能编。书外事实只认联网搜索结果并附链接，书内依据引章名与原文；查不到写「未核」。

成本：作者实测一本 36 万 token 的英文书 2.71 美元、370 秒；书的正文打缓存标记，
联网搜索的多轮内部循环与 pause_turn 续跑按缓存价重读。结果按书缓存在 book_dossier，不重复生成。
"""
from __future__ import annotations

import json
import re
import threading
import time

import app
import llm

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_SEARCH = 8
MAX_CHARS_CJK = 900_000      # 约 70 万 token 以内，给输出和搜索结果留余量
MAX_CHARS_LATIN = 2_400_000
SCHEMA = """CREATE TABLE IF NOT EXISTS book_dossier(book_id INTEGER PRIMARY KEY, state TEXT, md TEXT,
  model TEXT, usd REAL, error TEXT, truncated INT, ts TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"""

PROMPT = """你是读者的导读人。读者想「把书读厚」：把这本书能总结的都总结出来，剩下的残差留给他自己。
你手里有整本书的正文（按章节）、读者画像、读者读过和做过笔记的书。可以联网搜索。
用中文写一份导读，严格按下面的小标题和顺序（Markdown 二级标题）：

## 一句话
## 来龙去脉
作者是谁（与本书相关的经历和立场）；什么时候、在什么处境下写的；为什么写。
## 想干嘛
核心主张或要讲的事；写给谁看；作者希望读者读完怎么想、怎么做。
## 结构与论证路线
全书怎么推进，各部分各自承担什么；关键论证链条；哪里跳步。引用章名。
## 独特之处
和同题的前作、同类书相比，新在哪里；哪些东西只有这本书有。
## 偏颇、盲区与争议
作者的立场带来的偏向；没讲或回避的；事实或推理有问题的地方；学界/书评的主要争议。
每条写清「书里怎么说（章名+原文短引）→ 问题在哪 → 依据」。
## 后来的反响
出版后的评价、影响、被修正或被证伪的部分。
## 和你的关系
结合读者画像、读者读过和做过笔记的书：哪些会相互印证、哪些相互冲突、哪些补盲区。没有就写「没有明显关联」。
## 留给你的
AI 总结不了、需要读者自己读原文或自己判断的问题，3–7 条，每条一句话说清为什么只能自己来。

硬性规则：
1. 书外事实（生平、年份、销量、书评、争议、后续）只能来自联网搜索结果，并且要被引用；搜不到就写「未核」，不要凭记忆写具体年份数字人名。
2. 书内依据引章名和原文短句（原文语言），不要编造引文。
3. 区分「作者说」「书评说」「我的判断」，我的判断句首标【判断】。
4. 不要复述情节或逐章摘要，不要客套。"""


def _db():
    c = app.db()
    c.execute(SCHEMA)
    return c


def get(book_id: int) -> dict | None:
    with _db() as c:
        r = c.execute("SELECT * FROM book_dossier WHERE book_id=?", (book_id,)).fetchone()
    return dict(r) if r else None


def _book_text(epub) -> tuple[str, bool]:
    import book_text
    paras = book_text.extract_paragraphs(epub)
    out, last = [], None
    for p in paras:
        if p["chapter"] and p["chapter"] != last:
            out.append(f"\n\n### 【章】{p['chapter']}\n")
            last = p["chapter"]
        out.append(p["text"])
    text = "\n".join(out)
    cjk = len(re.findall(r"[一-鿿぀-ヿ]", text[:50000])) > 10000
    cap = MAX_CHARS_CJK if cjk else MAX_CHARS_LATIN
    return (text[:cap], True) if len(text) > cap else (text, False)


def _context(book_id: int) -> str:
    """读者读过/做过笔记的书（标题，来自导入的外部阅读记录）。没有就空。"""
    parts = []
    with app.db() as c:
        try:
            rows = c.execute("""SELECT title, author FROM ext_books WHERE thought_count>0 OR highlight_count>0
                                OR status IN ('read','finished') ORDER BY last_ts DESC LIMIT 400""").fetchall()
            if rows:
                parts.append("【读者读过/做过笔记的书】" + "；".join(f"{r['title']}（{r['author'] or ''}）" for r in rows))
        except Exception:  # noqa: BLE001 — 表不存在就跳过
            pass
    return "\n\n".join(parts)


def _render(content) -> str:
    """拼正文；有引用的段落后附来源链接。"""
    out = []
    for b in content:
        if b.type != "text":
            continue
        out.append(b.text)
        urls = []
        for ci in getattr(b, "citations", None) or []:
            u = getattr(ci, "url", None)
            if u and u not in urls:
                urls.append(u)
        if urls:
            out.append(" " + " ".join(f"[〔来源〕]({u})" for u in urls[:3]))
    return "".join(out).strip()


def build(book_id: int, epub, title: str, authors: str) -> dict:
    import gloss
    text, truncated = _book_text(epub)
    # 作者实测（一本 36 万 token 的英文书）：缓存写入 1.78 + 联网多轮缓存重读 0.42 + 输出 0.39 ≈ 2.7 美元，约 7.6 美元/百万 token
    tokens = len(text) / (1.5 if len(re.findall(r"[一-鿿]", text[:20000])) > 5000 else 2.6)
    est = tokens * 7.6 / 1e6 + 0.3
    if llm.api_spent() + est > llm.API_MONTHLY_CAP:
        raise llm.BackendError(f"本月 API 估算已花 {llm.api_spent():.2f} 美元，这本书导读预估 {est:.2f}，会超上限")
    client = llm._client()
    system = f"{PROMPT}\n\n【读者画像】\n{gloss.profile()}"
    ctx = _context(book_id)
    head = f"书名：《{title}》\n作者：{authors or '未知'}\n" + ("（正文过长，已截断到前面部分）\n" if truncated else "")
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": head + "\n【正文】\n" + text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": (ctx + "\n\n" if ctx else "") + "请按要求写导读。"}]}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCH}]
    usd, content = 0.0, []
    for _ in range(6):  # pause_turn 续跑
        resp = client.messages.create(model=MODEL, max_tokens=32000, output_config={"effort": EFFORT},
                                      system=system, messages=msgs, tools=tools)
        u = resp.usage.model_dump()
        cost = llm.api_cost(resp.model, u) + 0.01 * ((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
        usd += cost
        with llm._spend_db() as c:
            c.execute("INSERT INTO api_spend(month, model, usd, usage) VALUES (?,?,?,?)",
                      (time.strftime("%Y-%m"), resp.model + ":dossier", cost, json.dumps(u, default=str)))
        content += list(resp.content)
        if resp.stop_reason != "pause_turn":
            break
        msgs = msgs + [{"role": "assistant", "content": resp.content}]
    if resp.stop_reason == "refusal":
        raise llm.BackendError(f"拒答：{resp.stop_details}")
    md = _render(content)
    if resp.stop_reason == "max_tokens":
        md += "\n\n（输出被截断）"
    if not md:
        raise llm.BackendError(f"空答 stop_reason={resp.stop_reason}")
    return {"md": md, "model": resp.model, "usd": usd, "truncated": truncated}


_running: dict[int, threading.Thread] = {}


def start(book_id: int, epub, title: str, authors: str, force: bool = False) -> dict:
    cur = get(book_id)
    if cur and cur["state"] == "done" and not force:
        return cur
    t = _running.get(book_id)
    if t and t.is_alive():
        return cur or {"state": "running"}

    def run():
        with _db() as c:
            c.execute("INSERT OR REPLACE INTO book_dossier(book_id, state) VALUES (?, 'running')", (book_id,))
        try:
            r = build(book_id, epub, title, authors)
            with _db() as c:
                c.execute("""INSERT OR REPLACE INTO book_dossier(book_id, state, md, model, usd, truncated)
                             VALUES (?, 'done', ?, ?, ?, ?)""", (book_id, r["md"], r["model"], r["usd"], int(r["truncated"])))
        except Exception as e:  # noqa: BLE001
            with _db() as c:
                c.execute("INSERT OR REPLACE INTO book_dossier(book_id, state, error) VALUES (?, 'error', ?)",
                          (book_id, str(e)[:500]))
    _running[book_id] = threading.Thread(target=run, daemon=True)
    _running[book_id].start()
    return {"state": "running"}
