"""server/geo_build.py 的纯函数：Douglas-Peucker 简化（含闭合环）、外包框、去行政类型后缀。

geo_build 顶层 import shapefile（pyshp，只在建库时装），这里注入一个假的 shapefile 模块再导入。
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest


@pytest.fixture(scope="module")
def gb():
    mp = pytest.MonkeyPatch()
    mp.setitem(sys.modules, "shapefile", types.ModuleType("shapefile"))
    mp.delitem(sys.modules, "geo_build", raising=False)
    mod = importlib.import_module("geo_build")
    yield mod
    mp.undo()
    sys.modules.pop("geo_build", None)


def test_simplify_short_line_untouched(gb):
    """少于 5 个点原样返回（不取整）。"""
    pts = [[0.00001, 0], [1, 1], [2, 0]]
    assert gb._simplify(pts, 0.1) is pts


def test_simplify_open_drops_collinear_points(gb):
    """共线的中间点被去掉，只剩端点；坐标取 4 位小数。"""
    pts = [[0, 0], [1, 0.00001], [2, 0], [3, 0], [4.000049, 0]]
    assert gb._simplify(pts, 0.01) == [[0, 0], [4.0, 0]]


def test_simplify_open_keeps_far_point(gb):
    """偏离超过容差的点保留，它两侧共线的点照样去掉。"""
    pts = [[0, 0], [1, 1], [2, 2], [3, 3], [4, 0]]
    assert gb._simplify(pts, 0.01) == [[0, 0], [3, 3], [4, 0]]


def test_simplify_closed_ring_stays_closed(gb):
    """闭合环（首尾同点）从最远点切两段分别简化，结果仍闭合且不塌成一条线。"""
    ring = [[0, 0], [1, 0], [2, 0], [2, 1], [2, 2], [1, 2], [0, 2], [0, 1], [0, 0]]
    out = gb._simplify(ring, 0.01)
    assert out[0] == out[-1] == [0, 0]
    assert out == [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]


def test_simplify_geom_polygon_and_point(gb, monkeypatch):
    monkeypatch.setattr(gb, "TOL", 0.01)
    ring = [[0, 0], [1, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
    poly = gb._simplify_geom({"type": "Polygon", "coordinates": [ring]})
    assert poly["coordinates"] == [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]
    multi = gb._simplify_geom({"type": "MultiPolygon", "coordinates": [[ring]]})
    assert multi["coordinates"] == [poly["coordinates"]]
    pt = {"type": "Point", "coordinates": [1, 2]}
    assert gb._simplify_geom(pt) is pt


def test_bbox_point_and_nested(gb):
    assert gb._bbox({"type": "Point", "coordinates": [3, 4]}) == (3, 4, 3, 4)
    g = {"type": "MultiPolygon", "coordinates": [[[[0, 5], [2, -1]]], [[[-3, 1], [1, 7]]]]}
    assert gb._bbox(g) == (-3, -1, 2, 7)


def test_strip_type(gb):
    """名字以类型字结尾且不止这一个字时去掉后缀。"""
    assert gb._strip_type("杭州府", "府") == "杭州"
    assert gb._strip_type("府", "府") == "府"          # 只剩类型字不去
    assert gb._strip_type("杭州", "府") == "杭州"      # 不以它结尾
    assert gb._strip_type("杭州府", "") == "杭州府"    # 无类型字
