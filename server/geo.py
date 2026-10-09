"""地理批注：选中一段 → 抽出地名 → 查历史地名库定位 → 地图 + 文字。

原则：精度可以一层层往后退，但不能编。
  ① 地名库定位：CHGIS（秦至清的府县治所，带「今地」考释）/ Pleiades（地中海古代地名），年代对得上才算
  ② 模型给出今地：模型说「在今某县」，点位取 1990 年该县的中心（或 OSM 查今地名），不是遗址精确位置
  ③ 只有方位文字：模型只能说个大概方位，或同名多处分不清，不上图
  ④ 未能定位
事实表（今地、年代、来源）由程序从库里直接生成，模型只写「形势」解说，而且只能用表里的地名和距离。

数据由 geo_build.py 建在 GEO_DIR/geo.sqlite（OWN_READER_GEO_DIR，默认 <数据目录>/geo）。CHGIS 许可不允许再分发，
本仓库不含任何地名数据，需自行下载后建库，见 README 与 geo_build.py。没建库时地理批注接口会报错，其他功能不受影响。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("OWN_READER_DATA", "~/Library/Application Support/own-reader")).expanduser()
GEO_DIR = Path(os.environ.get("OWN_READER_GEO_DIR") or DATA_DIR / "geo").expanduser()
GEO_DB = GEO_DIR / "geo.sqlite"
YEAR_TOL = 10        # 年代容差：书里的年份常是约数
CLUSTER_KM = 30      # 同名多条记录都在这个距离内，才当作同一个地方
CHGIS_URI = "http://tgaz.fudan.edu.cn/tgaz/placename/{}"
PLEIADES_URI = "https://pleiades.stoa.org/places/{}"
ADMIN_KINDS = {"行政区", "城邑"}  # 只有这两类去查 CHGIS（它收的是府县治所，不收山川关隘和政权）
LEVEL_LABEL = {1: "① 地名库定位", 2: "② 模型给出今地", 3: "③ 仅方位文字", 4: "④ 未能定位"}

_t2s: dict[str, str] | None = None


def _db() -> sqlite3.Connection:
    if not GEO_DB.exists():
        raise RuntimeError(f"地名库 {GEO_DB} 还没建：先下载 CHGIS V6 / Pleiades 数据并运行 server/geo_build.py（见 README）")
    c = sqlite3.connect(f"file:{GEO_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def t2s(text: str) -> str:
    global _t2s
    if _t2s is None:
        with _db() as c:
            _t2s = dict(c.execute("SELECT t, s FROM t2s").fetchall())
    return "".join(_t2s.get(ch, ch) for ch in text or "")


def km(a: tuple, b: tuple) -> float:
    (x1, y1), (x2, y2) = a, b
    p1, p2 = math.radians(y1), math.radians(y2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(x2 - x1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _clustered(pts: list[tuple]) -> bool:
    return all(km(pts[0], p) <= CLUSTER_KM for p in pts[1:])


def _in_years(beg, end, year) -> bool:
    return year is None or ((beg is None or beg - YEAR_TOL <= year) and (end is None or year <= end + YEAR_TOL))


def hist_lookup(keys: list[str], year: int | None) -> dict:
    """CHGIS。返回 {"hit": row|None, "ambiguous": [rows], "other_years": [rows]}。
    同名精确命中优先于去掉通名（郡/县…）后的命中；年代不在 [beg, end] 内的不算（如 417 年的长安县在太原）。"""
    keys = [t2s(k).strip() for k in keys if k and k.strip()]
    if not keys:
        return {"hit": None, "ambiguous": [], "other_years": []}
    with _db() as c:
        q = ",".join("?" * len(keys))
        exact = [dict(r) for r in c.execute(f"SELECT * FROM hist WHERE name IN ({q})", keys)]
        loose = [dict(r) for r in c.execute(f"SELECT * FROM hist WHERE base IN ({q}) AND name NOT IN ({q})",
                                            keys + keys)]
    for rows in (exact, loose):
        ok = [r for r in rows if _in_years(r["beg"], r["end"], year)]
        if not ok:
            continue
        pts = [(r["x"], r["y"]) for r in ok]
        if _clustered(pts):
            # 同一地点多条（郡、县同治）：取时间跨度最贴近的一条，今地考释最全的优先
            best = min(ok, key=lambda r: (0 if r["pres"] else 1, (r["end"] - r["beg"])))
            return {"hit": best, "ambiguous": [], "other_years": []}
        return {"hit": None, "ambiguous": ok[:6], "other_years": []}
    return {"hit": None, "ambiguous": [], "other_years": (exact or loose)[:4]}


def pleiades_lookup(keys: list[str], year: int | None) -> dict:
    keys = [k.strip().lower() for k in keys if k and k.strip() and k.isascii()]
    if not keys:
        return {"hit": None, "ambiguous": []}
    with _db() as c:
        q = ",".join("?" * len(keys))
        rows = [dict(r) for r in c.execute(
            f"SELECT DISTINCT p.* FROM pleiades p JOIN pleiades_name n ON n.id=p.id WHERE n.name IN ({q})", keys)]
    ok = [r for r in rows if _in_years(r["beg"], r["end"], year)]
    precise = [r for r in ok if r["precision"] == "precise"] or ok
    if not precise:
        return {"hit": None, "ambiguous": []}
    if _clustered([(r["x"], r["y"]) for r in precise]):
        return {"hit": precise[0], "ambiguous": []}
    return {"hit": None, "ambiguous": precise[:6]}


_SUFFIX = re.compile(r"(自治县|自治旗|市辖区|地区|市|县|区|旗)$")


def modern_lookup(name: str, prov: str | None = None) -> dict | None:
    """今地名 → 1990 年县级行政区中心。同名分布在多处又没给省份时返回 None（不猜）。"""
    q = _SUFFIX.sub("", t2s(name or "").replace(" ", ""))
    if len(q) < 2:
        return None
    p = _SUFFIX.sub("", t2s(prov or "").replace("省", ""))
    with _db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM modern WHERE name IN (?,?,?,?,?,?) OR name LIKE ? OR pref LIKE ?",
            (q, q + "市", q + "县", q + "区", q + "旗", q + "自治县", q + "市%", q + "%"))]
    if p:
        rows = [r for r in rows if r["prov"].startswith(p[:2])]
    if not rows:
        return None
    exact = [r for r in rows if _SUFFIX.sub("", r["name"]) == q] or rows
    pts = [(r["x"], r["y"]) for r in exact]
    cx, cy = sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)
    if any(km((cx, cy), pt) > 80 for pt in pts):
        return None
    r0 = exact[0]
    return {"x": cx, "y": cy, "label": f"{r0['prov']}{r0['name'] if len(exact) == 1 else q}", "src": "CHGIS 1990 县界"}


def osm_lookup(name: str) -> dict | None:
    """中国以外的今地名用 OpenStreetMap Nominatim（每次批注最多查几次，符合其 1 次/秒的使用规定）。"""
    try:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": name, "format": "json", "limit": 3, "accept-language": "zh"})
        req = urllib.request.Request(url, headers={"User-Agent": "own-reader/0.1 (personal reading app)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.load(r)
        time.sleep(1.1)
    except Exception:  # noqa: BLE001
        return None
    if not res:
        return None
    pts = [(float(x["lon"]), float(x["lat"])) for x in res]
    if not all(km(pts[0], p) <= 80 for p in pts[1:]):
        return None
    return {"x": pts[0][0], "y": pts[0][1], "label": res[0]["display_name"][:60], "src": "OpenStreetMap"}


def polygon_for(name: str, year: int | None) -> str | None:
    if year is None:
        return None
    with _db() as c:
        r = c.execute("SELECT id FROM pgn WHERE name=? AND beg-?<=? AND ?<=end+?",
                      (name, YEAR_TOL, year, year, YEAR_TOL)).fetchone()
    return r["id"] if r else None


def polygons(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    with _db() as c:
        q = ",".join("?" * len(ids))
        return [{"id": r["id"], "name": r["name"], "beg": r["beg"], "end": r["end"], "geojson": json.loads(r["geojson"])}
                for r in c.execute(f"SELECT * FROM pgn WHERE id IN ({q})", ids[:20])]


def _fmt_years(beg, end) -> str:
    f = lambda y: f"前{-y}" if y is not None and y < 0 else str(y)  # noqa: E731
    return f"{f(beg)}–{f(end)}"


def resolve(place: dict, year: int | None) -> dict:
    """按 ①→④ 逐级退。place 是模型抽出来的一条：name/kind/lookup/en/modern/modern_prov/direction。"""
    out = {"name": place.get("name", ""), "kind": place.get("kind", ""), "level": 4}
    if place.get("kind") in ADMIN_KINDS:
        h = hist_lookup([place.get("name", "")] + list(place.get("lookup") or []), year)
        if h["hit"]:
            r = h["hit"]
            return {**out, "level": 1, "x": r["x"], "y": r["y"], "today": r["pres"] or "（库中无今地考释）",
                    "matched": r["name"], "years": _fmt_years(r["beg"], r["end"]), "src": "CHGIS V6",
                    "uri": CHGIS_URI.format(r["id"]), "pgn": polygon_for(r["name"], year)}
        if h["ambiguous"]:
            out["note"] = "地名库里这个年代有同名多处：" + "；".join(
                f"{r['name']}（{_fmt_years(r['beg'], r['end'])}，{r['pres'] or '无考释'}）" for r in h["ambiguous"])
        elif h["other_years"]:
            out["note"] = "地名库里有同名，但年代对不上，不采用：" + "；".join(
                f"{r['name']}（{_fmt_years(r['beg'], r['end'])}，{r['pres'] or '无考释'}）" for r in h["other_years"])
    if place.get("en"):
        h = pleiades_lookup([place["en"]] + list(place.get("en_alt") or []), year)
        if h["hit"]:
            r = h["hit"]
            return {**out, "level": 1, "x": r["x"], "y": r["y"], "today": r["descr"][:160],
                    "matched": r["title"], "years": _fmt_years(r["beg"], r["end"]), "src": "Pleiades",
                    "uri": PLEIADES_URI.format(r["id"])}
        if h["ambiguous"]:
            out["note"] = "Pleiades 里有同名多处：" + "；".join(r["title"] for r in h["ambiguous"])
    if place.get("modern"):
        m = None
        for nm in place.get("modern_names") or []:
            m = modern_lookup(nm, place.get("modern_prov"))
            if m:
                break
        if not m and place.get("modern_osm"):
            m = osm_lookup(place["modern_osm"])
        if m:
            return {**out, "level": 2, "x": m["x"], "y": m["y"], "today": place["modern"],
                    "matched": m["label"], "src": f"模型说法；点位取{m['src']}中心", "certainty": place.get("certainty"),
                    "note": out.get("note")}
        out.update(level=3, today=place["modern"], src="模型说法（今地名没在今县库里找到，不上图）",
                   certainty=place.get("certainty"))
        return out
    if place.get("kind") == "政权":
        out.update(level=3, today=place.get("modern") or place.get("direction") or "—",
                   src="政权不定位为一个点；都城若在原文中出现，另列一条")
        return out
    if place.get("direction"):
        out.update(level=3, today=place["direction"], src="模型说法（只有方位）")
    return out


# ---------- 模型两步 ----------

EXTRACT_PROMPT = """你在帮读者给一段书做「地理批注」。只做抽取，输出一个 JSON 对象，不要任何别的文字。
格式：
{"year": 整数或 null（这段讲的事件发生的大致公历年，公元前用负数）, "year_basis": "依据",
 "places": [{
   "name": "原文里的写法",
   "kind": "政权|行政区|城邑|山|水|关隘|地区|其他",
   "lookup": ["该年代的标准政区名，用来查历史地名库，如 会稽郡、山阴县；不是政区就给空数组"],
   "en": "若是中国以外的古地名，给其通行拉丁/英文名，否则 null", "en_alt": ["其他拼法"],
   "modern": "今地，如「今江苏苏州」；没有把握就 null，宁缺勿编",
   "modern_names": ["今县市名，不带省，最具体的县级名在前（如 富阳 在 杭州 前）；用来在今地名库里定位"], "modern_prov": "今省名或 null",
   "modern_osm": "中国以外的今地名（英文，含国名），否则 null",
   "certainty": "通说|有争议|不确定",
   "direction": "说不出今地、只能说大致方位时写在这里（如「太湖以东」），否则 null"
 }]}
规则：
1. 只收原文里出现的地名（含国名、山川、关隘），最多 12 个。
2. 绝对不要给经纬度、距离。
3. 「政权/国」本身不是一个点：不要给它的 modern_names；它的都城如果原文提到，单列一条。
4. 今地只写你确有把握的通说；有争议写 certainty=有争议，并在 modern 里写最主流的一种。"""

COMMENT_PROMPT = """你在帮读者理解一段书里的地理形势。下面给你：原文、地名表（已由程序从地名库核对，带可信等级）、程序算出的点间直线距离。
写一段简短的「形势」解说（中文，200–400 字），讲清楚：这些地方的相对位置、交通和地形关系，为什么会在这里打仗/经过这里。
硬性规则：
1. 只能使用地名表里的地名、今地和距离；表里标「④ 未能定位」或「③」的地方，不要替它说出具体今地。
2. 不要写任何经纬度，不要写表里没有的距离数字。exact=false 的距离含「模型给出的今地」点，只是粗略参考，绝不能拿它来判断有争议的地点在哪。
3. 地形只讲大尺度、你有把握的事实（山系、水系、平原、海岸）。每一句属于你的推断的话，句首加「【推断】」。
4. 没把握就说「不确定」，宁缺勿编。不要客套，不要重复地名表。"""


def _json_obj(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"模型没有返回 JSON：{text[:200]}")
    return json.loads(m.group(0))


def table_md(year, year_basis, places: list[dict], dists: list[dict]) -> str:
    yr = (f"前 {-year} 年" if year < 0 else f"{year} 年") if isinstance(year, int) else "年代未定"
    lines = [f"**年代**：{yr}" + (f"（{year_basis}）" if year_basis else ""), "",
             "| 地名 | 可信等级 | 今地 | 来源 |", "|---|---|---|---|"]
    for p in places:
        src = p.get("src", "—")
        if p.get("uri"):
            src = f"[{src}]({p['uri']})"
        if p.get("matched") and p["level"] == 1:
            src += f"：{p['matched']}（{p.get('years', '')}）"
        cert = f"（{p['certainty']}）" if p.get("certainty") and p["level"] >= 2 else ""
        today = (p.get("today") or "—").replace("|", "／")
        lines.append(f"| {p['name']} | {LEVEL_LABEL[p['level']]} | {today}{cert} | {src} |")
    notes = [f"- {p['name']}：{p['note']}" for p in places if p.get("note")]
    if notes:
        lines += ["", *notes]
    if dists:
        lines += ["", "直线距离：" + "；".join(f"{d['a']}—{d['b']} 约 {d['km']:.0f} 公里" + ("" if d.get("exact") else "（含今县中心点，粗略）")
                                           for d in dists)]
    lines += ["", "*① 年代对得上的地名库记录；② 今地是模型说的，图上点是今县中心，不是遗址位置；"
              "③ 只有文字，不上图；④ 没找到。CHGIS 数据引用：\"CHGIS Version 6.\" (c) Fairbank Center for "
              "Chinese Studies and the Institute for Chinese Historical Geography at Fudan University, Dec 2016.*"]
    return "\n".join(lines)


def annotate(req: dict, title: str, ask) -> dict:
    """ask(messages, caller) -> (text, model, ms)，由 app.py 注入（复用提问的后端与回退）。"""
    t0 = time.time()
    _db().close()  # 没建地名库就先报错，不白花一次模型调用
    passage = (f"书名：《{title}》\n章节：{req.get('chapter') or '未知'}\n\n"
               f"【前文】\n{(req.get('context_before') or '')[-2500:]}\n\n【选中的段落】\n{req.get('selection', '')}\n\n"
               f"【后文】\n{(req.get('context_after') or '')[:1500]}")
    text, model1, _ = ask([{"role": "system", "content": EXTRACT_PROMPT}, {"role": "user", "content": passage}],
                          "reader.geo.extract")
    ex = _json_obj(text)
    year = ex.get("year") if isinstance(ex.get("year"), int) else None
    places = [resolve(p, year) for p in (ex.get("places") or [])[:12] if isinstance(p, dict) and p.get("name")]
    on_map = [p for p in places if p["level"] in (1, 2)]
    dists = [{"a": a["name"], "b": b["name"], "km": km((a["x"], a["y"]), (b["x"], b["y"])),
              "exact": a["level"] == 1 and b["level"] == 1}
             for i, a in enumerate(on_map[:6]) for b in on_map[i + 1:6]][:6]  # 两两最多列 6 条，多了读不过来
    table = table_md(year, ex.get("year_basis"), places, dists)
    commentary, model2, err = None, None, None
    try:
        fact = json.dumps([{k: p.get(k) for k in ("name", "kind", "today", "certainty")} | {"等级": LEVEL_LABEL[p["level"]]}
                           for p in places], ensure_ascii=False)
        d = json.dumps([{**x, "km": round(x["km"])} for x in dists], ensure_ascii=False)
        commentary, model2, _ = ask([{"role": "system", "content": COMMENT_PROMPT},
                                     {"role": "user", "content": f"{passage}\n\n【地名表】\n{fact}\n\n【直线距离】\n{d}"}],
                                    "reader.geo.comment")
    except Exception as e:  # noqa: BLE001 — 解说失败不影响事实表
        err = str(e)[:200]
    answer = table + ("\n\n### 形势（模型解说，【推断】为推测）\n" + commentary if commentary else
                      f"\n\n（形势解说生成失败：{err}）")
    return {"year": year, "places": places, "distances": dists, "answer": answer,
            "model": " + ".join(m for m in (model1, model2) if m), "latency_ms": int((time.time() - t0) * 1000)}
