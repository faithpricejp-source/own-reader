"""server/geo.py：年代门槛、同名多处、逐级退、事实表不经模型。用小型假库，不依赖 CHGIS 真数据。"""
from __future__ import annotations

import json
import sqlite3

import pytest

import geo


@pytest.fixture
def fake_db(tmp_path, monkeypatch):
    p = tmp_path / "geo.sqlite"
    c = sqlite3.connect(p)
    c.executescript("""
    CREATE TABLE hist(src TEXT, id TEXT, name TEXT, base TEXT, name_ft TEXT, type TEXT, lev TEXT,
                      beg INT, end INT, x REAL, y REAL, pres TEXT);
    CREATE TABLE pgn(id TEXT PRIMARY KEY, name TEXT, base TEXT, type TEXT, beg INT, end INT,
                     minx REAL, miny REAL, maxx REAL, maxy REAL, geojson TEXT);
    CREATE TABLE modern(name TEXT, pref TEXT, prov TEXT, x REAL, y REAL, minx REAL, miny REAL, maxx REAL, maxy REAL);
    CREATE TABLE pleiades(id TEXT PRIMARY KEY, title TEXT, x REAL, y REAL, beg INT, end INT,
                          types TEXT, precision TEXT, descr TEXT);
    CREATE TABLE pleiades_name(id TEXT, name TEXT);
    CREATE TABLE t2s(t TEXT PRIMARY KEY, s TEXT);
    """)
    c.executemany("INSERT INTO hist VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("c", "hvd_1", "会稽郡", "会稽", "會稽郡", "郡", "2", -222, 128, 120.62, 31.31, "今江苏苏州市区"),
        ("c", "hvd_2", "会稽郡", "会稽", "會稽郡", "郡", "2", 129, 588, 120.58, 30.00, "今浙江绍兴市"),
        ("c", "hvd_3", "会稽县", "会稽", "會稽縣", "县", "6", 295, 561, 96.03, 40.52, "今甘肃安西县东"),
        ("c", "hvd_4", "长安县", "长安", "長安縣", "县", "6", 417, 447, 112.39, 37.73, "今山西太原市柴村"),
        ("c", "hvd_5", "长安县", "长安", "長安縣", "县", "6", 703, 1911, 108.93, 34.26, "今西安市西市"),
    ])
    c.execute("INSERT INTO pgn VALUES ('hvd_2','会稽郡','会稽','郡',129,588,0,0,1,1,?)",
              (json.dumps({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}),))
    c.executemany("INSERT INTO modern VALUES (?,?,?,?,?,?,?,?,?)", [
        ("灵宝", "三门峡市", "河南", 110.77, 34.42, 0, 0, 0, 0),
        ("朝阳", "朝阳市", "辽宁", 120.37, 41.48, 0, 0, 0, 0),
        ("朝阳区", "北京市辖区", "北京", 116.44, 39.92, 0, 0, 0, 0),
    ])
    c.execute("INSERT INTO pleiades VALUES ('1','Gaugamela',43.5,36.5,-750,300,'battle','precise','x')")
    c.execute("INSERT INTO pleiades_name VALUES ('1','gaugamela')")
    c.execute("INSERT INTO t2s VALUES ('會','会')")
    c.commit()
    c.close()
    monkeypatch.setattr(geo, "GEO_DB", p)
    monkeypatch.setattr(geo, "_t2s", None)
    return p


def test_year_gate_picks_right_kuaiji(fake_db):
    assert geo.hist_lookup(["会稽郡"], -100)["hit"]["pres"] == "今江苏苏州市区"
    assert geo.hist_lookup(["會稽郡"], 300)["hit"]["pres"] == "今浙江绍兴市"   # 繁体也认


def test_wrong_year_is_rejected_not_guessed(fake_db):
    h = geo.hist_lookup(["长安县"], 430)
    assert h["hit"]["pres"] == "今山西太原市柴村"  # 430 年只有太原那个（侨置）
    h = geo.hist_lookup(["长安县"], 600)            # 两个都不在年代内
    assert h["hit"] is None and h["other_years"]


def test_unknown_year_same_name_far_apart_is_ambiguous(fake_db):
    h = geo.hist_lookup(["会稽"], None)
    assert h["hit"] is None and len(h["ambiguous"]) >= 2


def test_resolve_ladder(fake_db):
    r = geo.resolve({"name": "会稽", "kind": "行政区", "lookup": ["会稽郡"]}, 300)
    assert r["level"] == 1 and r["pgn"] == "hvd_2" and r["uri"].endswith("hvd_2")
    # 政权不查 CHGIS；模型给了今地且今县库里找得到 → ②
    r = geo.resolve({"name": "函谷关", "kind": "关隘", "modern": "今河南灵宝", "modern_names": ["灵宝"],
                     "modern_prov": "河南"}, -300)
    assert r["level"] == 2 and abs(r["x"] - 110.77) < 0.01
    # 今地名找不到 → ③，不上图
    r = geo.resolve({"name": "槜李", "kind": "城邑", "modern": "今浙江嘉兴西南", "modern_names": ["嘉兴"]}, -496)
    assert r["level"] == 3 and "x" not in r
    # 只有方位 → ③；什么都没有 → ④
    assert geo.resolve({"name": "夫椒", "kind": "山", "direction": "太湖中"}, -494)["level"] == 3
    assert geo.resolve({"name": "某地", "kind": "其他"}, None)["level"] == 4


def test_kingdom_not_looked_up_in_chgis(fake_db):
    r = geo.resolve({"name": "会稽", "kind": "政权", "lookup": ["会稽郡"]}, 300)
    assert r["level"] == 3 and "x" not in r and "政权" in r["src"]


def test_modern_same_name_different_province_needs_prov(fake_db):
    assert geo.modern_lookup("朝阳") is None                     # 辽宁朝阳 vs 北京朝阳区，不猜
    assert geo.modern_lookup("朝阳", "辽宁省")["label"].startswith("辽宁")


def test_pleiades_year_gate(fake_db):
    assert geo.pleiades_lookup(["Gaugamela"], -331)["hit"]["title"] == "Gaugamela"
    assert geo.pleiades_lookup(["Gaugamela"], 900)["hit"] is None


def test_annotate_table_is_from_db_not_model(fake_db):
    calls = []

    def ask(messages, caller):
        calls.append(caller)
        if caller.endswith("extract"):
            # 模型试图塞进一个错的今地：库里命中时事实表必须用库里的考释
            return json.dumps({"year": 300, "year_basis": "测试", "places": [
                {"name": "会稽", "kind": "行政区", "lookup": ["会稽郡"], "modern": "今上海", "modern_names": ["上海"]},
                {"name": "函谷关", "kind": "关隘", "modern": "今河南灵宝", "modern_names": ["灵宝"]}]}), "m1", 1
        return "【推断】两地相距很远。", "m2", 1

    res = geo.annotate({"selection": "会稽……函谷关"}, "测试书", ask)
    assert calls == ["reader.geo.extract", "reader.geo.comment"]
    assert "今浙江绍兴市" in res["answer"] and "今上海" not in res["answer"]
    assert [p["level"] for p in res["places"]] == [1, 2]
    assert res["distances"] and res["distances"][0]["km"] > 500 and not res["distances"][0]["exact"]
    assert "粗略" in res["answer"]


def test_annotate_comment_failure_keeps_table(fake_db):
    def ask(messages, caller):
        if caller.endswith("extract"):
            return '{"year": 300, "places": [{"name": "会稽", "kind": "行政区", "lookup": ["会稽郡"]}]}', "m1", 1
        raise RuntimeError("boom")

    res = geo.annotate({"selection": "会稽"}, "测试书", ask)
    assert "今浙江绍兴市" in res["answer"] and "生成失败" in res["answer"]
