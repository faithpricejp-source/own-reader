"""把下载好的历史地理数据整理成 geo.sqlite（一次性，数据更新时重跑）。

本仓库不含任何地名数据。数据需自行下载，放在 GEO_DIR/raw（OWN_READER_GEO_DIR，默认 <数据目录>/geo）：
  CHGIS V6（哈佛 Dataverse 下载；EULA 只许非商业的学术/教育用途、禁止再分发，使用时须引用 "CHGIS Version 6."）
    pref_pgn/  府级时间序列多边形   pref_pts/  府级治所点   cnty_pts/  县级治所点
    citas90_cnty_pgn/  1990 年县界（今地名 → 坐标，繁体）
    （各目录里是同名 shapefile：v6_time_pref_pgn_utf_wgs84.*、v6_time_pref_pts_utf_wgs84.*、
      v6_time_cnty_pts_utf_wgs84.*、v6_citas90_cnty_pgn_gbk.*，文件名见 build() 里的路径）
  Pleiades（CC-BY，地中海古代地名）pleiades-places.csv.gz / pleiades-names.csv.gz
生成的 geo.sqlite 同样受 CHGIS 许可约束，不要提交或分发。

运行（pyshp、pyproj 只在建库时用，服务器运行时纯标准库），在仓库根目录：
  .venv/bin/pip install pyshp pyproj
  .venv/bin/python server/geo_build.py
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import sqlite3
import sys
from pathlib import Path

import shapefile  # pyshp

sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402,F401  — 读 config.conf

DATA_DIR = Path(os.environ.get("OWN_READER_DATA", "~/Library/Application Support/own-reader")).expanduser()
GEO_DIR = Path(os.environ.get("OWN_READER_GEO_DIR") or DATA_DIR / "geo").expanduser()
RAW = GEO_DIR / "raw"
OUT = GEO_DIR / "geo.sqlite"
TOL = 0.004  # 多边形简化容差（度，约 400 米），够阅读用，库小一个数量级


def _simplify(pts: list, tol: float) -> list:
    """Douglas-Peucker，迭代版。闭合环首尾同点，先在离起点最远处切成两段各自简化。"""
    if len(pts) < 5:
        return pts
    if tuple(pts[0][:2]) == tuple(pts[-1][:2]):
        x0, y0 = pts[0][:2]
        k = max(range(len(pts)), key=lambda i: (pts[i][0] - x0) ** 2 + (pts[i][1] - y0) ** 2)
        if 0 < k < len(pts) - 1:
            return _simplify_open(pts[: k + 1], tol)[:-1] + _simplify_open(pts[k:], tol)
    return _simplify_open(pts, tol)


def _simplify_open(pts: list, tol: float) -> list:
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a][:2], pts[b][:2]
        dx, dy = x2 - x1, y2 - y1
        n = (dx * dx + dy * dy) ** 0.5 or 1e-12
        best, idx = 0.0, -1
        for i in range(a + 1, b):
            x, y = pts[i][:2]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / n
            if d > best:
                best, idx = d, i
        if best > tol and idx > 0:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    out = [[round(p[0], 4), round(p[1], 4)] for p, k in zip(pts, keep) if k]
    return out


def _simplify_geom(g: dict) -> dict:
    if g["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [_simplify(list(r), TOL) for r in g["coordinates"]]}
    if g["type"] == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[_simplify(list(r), TOL) for r in poly] for poly in g["coordinates"]]}
    return g


def _bbox(g: dict) -> tuple:
    xs, ys = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for s in c:
                walk(s)
    walk(g["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)


def _strip_type(name: str, type_ch: str) -> str:
    if type_ch and name.endswith(type_ch) and len(name) > len(type_ch):
        return name[: -len(type_ch)]
    return name


def build() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    c = sqlite3.connect(tmp)
    c.executescript("""
    CREATE TABLE hist(src TEXT, id TEXT, name TEXT, base TEXT, name_ft TEXT, type TEXT, lev TEXT,
                      beg INT, end INT, x REAL, y REAL, pres TEXT);
    CREATE TABLE pgn(id TEXT PRIMARY KEY, name TEXT, base TEXT, type TEXT, beg INT, end INT,
                     minx REAL, miny REAL, maxx REAL, maxy REAL, geojson TEXT);
    CREATE TABLE modern(name TEXT, pref TEXT, prov TEXT, x REAL, y REAL,
                        minx REAL, miny REAL, maxx REAL, maxy REAL);
    CREATE TABLE pleiades(id TEXT PRIMARY KEY, title TEXT, x REAL, y REAL, beg INT, end INT,
                          types TEXT, precision TEXT, descr TEXT);
    CREATE TABLE pleiades_name(id TEXT, name TEXT);
    CREATE TABLE t2s(t TEXT PRIMARY KEY, s TEXT);
    """)
    votes: dict[str, dict[str, int]] = {}

    for layer, src in (("cnty_pts/v6_time_cnty_pts_utf_wgs84", "chgis_cnty"),
                       ("pref_pts/v6_time_pref_pts_utf_wgs84", "chgis_pref")):
        r = shapefile.Reader(str(RAW / layer), encoding="utf-8")
        rows = []
        for rec in r.iterRecords():
            d = rec.as_dict()
            nm, ft = d["NAME_CH"].strip(), (d["NAME_FT"] or "").strip()
            if len(nm) == len(ft):
                for a, b in zip(ft, nm):
                    v = votes.setdefault(a, {})
                    v[b] = v.get(b, 0) + 1
            rows.append((src, f"hvd_{d['SYS_ID']}", nm, _strip_type(nm, d["TYPE_CH"]), ft, d["TYPE_CH"],
                         d["LEV_RANK"], d["BEG_YR"], d["END_YR"], d["X_COOR"], d["Y_COOR"],
                         (d.get("PRES_LOC") or "").strip()))
        c.executemany("INSERT INTO hist VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        print(src, len(rows), file=sys.stderr)

    r = shapefile.Reader(str(RAW / "pref_pgn/v6_time_pref_pgn_utf_wgs84"), encoding="utf-8")
    n = 0
    for sr in r.iterShapeRecords():
        d = sr.record.as_dict()
        g = _simplify_geom(sr.shape.__geo_interface__)
        bb = _bbox(g)
        nm = d["NAME_CH"].strip()
        c.execute("INSERT OR REPLACE INTO pgn VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (f"hvd_{d['SYS_ID']}", nm, _strip_type(nm, d["TYPE_CH"]), d["TYPE_CH"], d["BEG_YR"], d["END_YR"],
                   *bb, json.dumps(g, separators=(",", ":"))))
        n += 1
    print("pgn", n, file=sys.stderr)

    # 繁→简按多数票：NAME_FT 偶有录错（如把「湖」录成别的字），只认出现 ≥2 次且占多数的非恒等映射
    t2s = {}
    for a, v in votes.items():
        b, n_b = max(v.items(), key=lambda kv: kv[1])
        if b != a and n_b >= 2 and n_b > v.get(a, 0):
            t2s[a] = b
    c.executemany("INSERT INTO t2s VALUES (?,?)", t2s.items())
    print("t2s", len(t2s), file=sys.stderr)

    def s(text: str) -> str:
        return "".join(t2s.get(ch, ch) for ch in text)

    # 1990 县界是横轴墨卡托投影坐标（见 .prj：中央经线 111°、假东 19,500,000、IAG75 椭球），转回经纬度
    from pyproj import CRS, Transformer
    prj = (RAW / "citas90_cnty_pgn/v6_citas90_cnty_pgn_gbk.prj").read_text()
    to_ll = Transformer.from_crs(CRS.from_wkt(prj), CRS.from_epsg(4326), always_xy=True).transform
    r = shapefile.Reader(str(RAW / "citas90_cnty_pgn/v6_citas90_cnty_pgn_gbk"), encoding="gbk")
    rows = []
    for sr in r.iterShapeRecords():
        d = sr.record.as_dict()
        if not d["CNTY_HZ"] or not sr.shape.points:
            continue
        # 代表点：顶点均值比 bbox 中心更不容易落到凹形县的界外，够标点用
        ll = [to_ll(p[0], p[1]) for p in sr.shape.points]
        xs = [p[0] for p in ll]
        ys = [p[1] for p in ll]
        minx, miny, maxx, maxy = min(xs), min(ys), max(xs), max(ys)
        nospace = lambda t: "".join((t or "").split())  # noqa: E731 — 原表有「荆门市   洋区」这类空格
        rows.append((s(nospace(d["CNTY_HZ"])), s(nospace(d["PREF_HZ"])), s(nospace(d["PROV_HZ"])),
                     sum(xs) / len(xs), sum(ys) / len(ys), minx, miny, maxx, maxy))
    c.executemany("INSERT INTO modern VALUES (?,?,?,?,?,?,?,?,?)", rows)
    print("modern", len(rows), file=sys.stderr)

    csv.field_size_limit(10**8)
    with gzip.open(RAW / "pleiades-places.csv.gz", "rt", encoding="utf-8") as f:
        rows = []
        for d in csv.DictReader(f):
            if not d["reprLat"]:
                continue
            rows.append((d["path"].rsplit("/", 1)[-1], d["title"], float(d["reprLong"]), float(d["reprLat"]),
                         int(float(d["minDate"])) if d["minDate"] else None,
                         int(float(d["maxDate"])) if d["maxDate"] else None,
                         d["featureTypes"], d["locationPrecision"], d["description"][:400]))
        c.executemany("INSERT OR REPLACE INTO pleiades VALUES (?,?,?,?,?,?,?,?,?)", rows)
        print("pleiades", len(rows), file=sys.stderr)
    with gzip.open(RAW / "pleiades-names.csv.gz", "rt", encoding="utf-8") as f:
        rows = set()
        for d in csv.DictReader(f):
            pid = d["pid"].rsplit("/", 1)[-1]
            for k in ("nameTransliterated", "nameAttested", "title"):
                for v in (d.get(k) or "").split(","):
                    if v.strip():
                        rows.add((pid, v.strip().lower()))
        c.executemany("INSERT INTO pleiades_name VALUES (?,?)", rows)
        print("pleiades_name", len(rows), file=sys.stderr)
    c.execute("INSERT INTO pleiades_name SELECT id, lower(title) FROM pleiades")

    c.executescript("""
    CREATE INDEX hist_name ON hist(name); CREATE INDEX hist_base ON hist(base);
    CREATE INDEX pgn_name ON pgn(name); CREATE INDEX pgn_base ON pgn(base);
    CREATE INDEX modern_name ON modern(name);
    CREATE INDEX pl_name ON pleiades_name(name);
    """)
    c.commit()
    c.close()
    tmp.replace(OUT)
    print("ok", OUT, f"{OUT.stat().st_size / 1e6:.1f} MB", file=sys.stderr)


if __name__ == "__main__":
    build()
