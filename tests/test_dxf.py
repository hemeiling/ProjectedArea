"""DXF 输入（dxf_footprint.py）：合成图纸，面积有解析解。

覆盖：图块多次插入（旋转、镜像、拉伸方向 (0,0,-1)、非等比缩放）、重叠只算一次、单位（声明 / 未声明 / 米）、
多段线凸度（圆弧）、线外图形、打散的设备线条、长墙线、绑定外部参照图层、黄色、平移不变、人工分类与文件哈希。
"""
import copy
import math
import os

import ezdxf
import pytest
import yaml
from shapely.geometry import box

import dxf_footprint as D
from webapp.jobs import ESTIMATOR_DIR

MACHINE_W, MACHINE_H = 2000.0, 1000.0           # mm：外框 2 × 1 m，内部有细节线


@pytest.fixture
def conf():
    with open(os.path.join(ESTIMATOR_DIR, "config.yaml"), encoding="utf-8") as f:
        return copy.deepcopy(yaml.safe_load(f))


def detail(layout, x0, y0, w, h):
    """设备内部细节线（真实设备图有成百上千条；只有外框的图形按待定处理）"""
    for k in range(1, 6):
        layout.add_line((x0 + w * k / 6, y0 + h * 0.2), (x0 + w * k / 6, y0 + h * 0.8))


def make_doc(units=4):
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = units
    blk = doc.blocks.new("MACHINE")
    blk.add_lwpolyline([(0, 0), (MACHINE_W, 0), (MACHINE_W, MACHINE_H), (0, MACHINE_H)], close=True)
    blk.add_line((300, 200), (1700, 200))
    blk.add_circle((1000, 500), 200)
    blk.add_text("M1", dxfattribs=dict(height=100)).set_placement((100, 100))
    detail(blk, 0, 0, MACHINE_W, MACHINE_H)
    return doc


def run(doc, tmp_path, conf, drawing=None, name="t.dxf"):
    path = str(tmp_path / name)
    doc.saveas(path)
    d = dict(id="t", name="t", dxf=path, **(drawing or {}))
    return D.measure(d, conf, str(tmp_path / "out"))


def test_rotated_mirrored_and_flipped_instances_have_the_block_area(tmp_path, conf):
    doc = make_doc()
    msp = doc.modelspace()
    msp.add_blockref("MACHINE", (0, 0))
    msp.add_blockref("MACHINE", (5000, 0), dxfattribs=dict(rotation=90))
    msp.add_blockref("MACHINE", (10000, 0), dxfattribs=dict(xscale=-1))
    msp.add_blockref("MACHINE", (15000, 0), dxfattribs=dict(extrusion=(0, 0, -1), rotation=30))
    msp.add_blockref("MACHINE", (20000, 0), dxfattribs=dict(xscale=1.5, yscale=0.5))   # 非等比缩放：3 × 0.5 m
    r = run(doc, tmp_path, conf)
    assert r["transform_check_failures"] == []
    areas = sorted(x["area_m2"] for x in r["inserts"])
    assert areas[0] == pytest.approx(1.5, rel=0.02)                     # 非等比缩放的那一个
    assert areas[1:] == pytest.approx([2.0] * 4, rel=0.02)
    assert r["footprint_m2"] == pytest.approx(9.5, rel=0.02)


def test_overlapping_instances_are_counted_once(tmp_path, conf):
    doc = make_doc()
    doc.modelspace().add_blockref("MACHINE", (0, 0))
    doc.modelspace().add_blockref("MACHINE", (1000, 0))                 # 与第一个重叠一半
    r = run(doc, tmp_path, conf)
    assert r["footprint_m2"] == pytest.approx(3.0, rel=0.02)
    assert r["overlap_m2"] == pytest.approx(1.0, rel=0.05)


def test_undeclared_units_are_refused_unless_stated_with_a_basis(tmp_path, conf):
    doc = make_doc(units=0)
    doc.modelspace().add_blockref("MACHINE", (0, 0))
    with pytest.raises(D.UnitsUnknown):
        run(doc, tmp_path, conf)
    r = run(doc, tmp_path, conf, dict(dxf_unit_mm=1, dxf_unit_basis="test"), name="t2.dxf")
    assert r["footprint_m2"] == pytest.approx(2.0, rel=0.02) and "test" in r["unit_basis"]


def test_metre_drawing_scales_to_the_same_area(tmp_path, conf):
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 6                                          # 米
    blk = doc.blocks.new("M")
    blk.add_lwpolyline([(0, 0), (2, 0), (2, 1), (0, 1)], close=True)
    detail(blk, 0, 0, 2, 1)
    doc.modelspace().add_blockref("M", (0, 0))
    r = run(doc, tmp_path, conf)
    assert r["unit_mm"] == 1000 and r["footprint_m2"] == pytest.approx(2.0, rel=0.02)


def test_bulged_polyline_follows_the_arc(tmp_path, conf):
    """跑道形：2 m 直段 + 两端半径 0.5 m 的半圆 = 2 × 1 + π × 0.5² = 2.785 m²"""
    doc = make_doc()
    blk = doc.blocks.new("STADIUM")
    blk.add_lwpolyline([(0, 0, 0, 0, 0), (2000, 0, 0, 0, 1), (2000, 1000, 0, 0, 0), (0, 1000, 0, 0, 1)],
                       format="xyseb", close=True)
    detail(blk, 0, 0, 2000, 1000)
    doc.modelspace().add_blockref("STADIUM", (0, 0))
    r = run(doc, tmp_path, conf)
    assert r["footprint_m2"] == pytest.approx(2.0 + math.pi * 0.25, rel=0.02)


def test_far_away_copy_and_people_are_not_counted(tmp_path, conf):
    doc = make_doc()
    msp = doc.modelspace()
    for x in (0, 3000, 6000):
        msp.add_blockref("MACHINE", (x, 0))
    msp.add_blockref("MACHINE", (500000, 0))                            # 500 m 外的副本
    person = doc.blocks.new("REN")
    person.add_circle((0, 0), 300)
    detail(person, -300, -300, 600, 600)                                # 人员符号也有若干条线
    msp.add_blockref("REN", (1000, 2000))
    path = str(tmp_path / "t.dxf")
    doc.saveas(path)
    sha = D.sha256(path)
    r = D.measure(dict(id="t", name="t", dxf=path, sha256=sha,
                       overrides={"REN": {"class": "excluded", "reason": "人员符号"}}), conf, str(tmp_path / "o"))
    assert r["footprint_m2"] == pytest.approx(6.0, rel=0.02)
    reasons = {x["reason"] for x in r["inserts"] if x["cls"] == "excluded"}
    assert any("产线范围" in x for x in reasons) and "人员符号" in reasons
    # 文件改动后，人工分类不再生效（人员符号重新计入）
    r2 = D.measure(dict(id="t", name="t", dxf=path, sha256="0" * 64,
                        overrides={"REN": {"class": "excluded", "reason": "人员符号"}}), conf, str(tmp_path / "o"))
    assert r2["footprint_m2"] > r["footprint_m2"]


def test_loose_lines_count_but_walls_xrefs_and_yellow_do_not(tmp_path, conf):
    doc = make_doc()
    msp = doc.modelspace()
    msp.add_blockref("MACHINE", (0, 0))
    for a, b in (((4000, 0), (7000, 0)), ((7000, 0), (7000, 2000)), ((7000, 2000), (4000, 2000)), ((4000, 2000), (4000, 0))):
        msp.add_line(a, b)                                              # 打散的设备：3 × 2 m
    detail(msp, 4000, 0, 3000, 2000)
    msp.add_line((-5000, 4000), (25000, 4000))                          # 30 m 长墙线
    doc.layers.add("ARCH$0$A-WALL")
    msp.add_lwpolyline([(0, 6000), (3000, 6000), (3000, 9000), (0, 9000)], close=True, dxfattribs=dict(layer="ARCH$0$A-WALL"))
    msp.add_lwpolyline([(9000, 0), (12000, 0), (12000, 3000), (9000, 3000)], close=True, dxfattribs=dict(color=2))
    r = run(doc, tmp_path, conf)
    assert r["footprint_m2"] == pytest.approx(2.0 + 6.0, rel=0.02)
    assert r["loose"]["entities"]["long line (wall/grid/dimension)"] == 1
    assert r["loose"]["entities"]["bound xref (building base plan)"] == 1 and r["loose"]["entities"]["yellow"] == 1


def test_translation_does_not_change_the_area(tmp_path, conf):
    def drawing(dx, dy):
        doc = make_doc()
        for x in (0, 1500, 5000):
            doc.modelspace().add_blockref("MACHINE", (x + dx, dy), dxfattribs=dict(rotation=15))
        return doc
    a = run(drawing(0, 0), tmp_path, conf, name="a.dxf")["footprint_m2"]
    b = run(drawing(3.2e6, -6.0e5), tmp_path, conf, name="b.dxf")["footprint_m2"]
    assert a == pytest.approx(b, abs=0.05)


def test_bare_outline_is_unresolved_not_counted(tmp_path, conf):
    """只有外框、没有内部细节的矩形（区域框、预留位）不计入主结果，单列为待定"""
    doc = make_doc()
    msp = doc.modelspace()
    msp.add_blockref("MACHINE", (0, 0))
    msp.add_lwpolyline([(5000, 0), (8000, 0), (8000, 3000), (5000, 3000)], close=True)   # 3 × 3 m 空框
    r = run(doc, tmp_path, conf)
    assert r["footprint_m2"] == pytest.approx(2.0, rel=0.02)
    assert r["unresolved_m2"] == pytest.approx(9.0, rel=0.02)


# ---------------------------------------------------------------- 线体基准（总长 × 总宽，按线体分开）
def rects(spec, unit=1.0):
    """[(x, y, 宽, 高) mm] -> 图纸单位的矩形"""
    return [box(x / unit, y / unit, (x + w) / unit, (y + h) / unit) for x, y, w, h in spec]


# 两条沿 x 的线体，各 30 m × 4 m，中间 5 m 走道；下面一条由相隔 2 m 的两段组成（沿线的空隙不分开）
TWO_LINES = [(0, 0, 10000, 4000), (12000, 0, 18000, 4000), (0, 9000, 30000, 4000)]


def test_lines_separated_by_a_full_length_aisle_are_measured_separately():
    parts = rects(TWO_LINES)
    e = D.line_envelope(parts, 1.0, 2, 3000)
    assert e["along"] == "x" and [l_["lane"] for l_ in e["lanes"]] == [1, 2]
    top, bottom = e["lanes"]
    assert top["box"][1] == 9000                                      # 编号从上到下
    assert (top["length_m"], top["width_m"], bottom["length_m"], bottom["width_m"]) == (30.0, 4.0, 30.0, 4.0)
    assert e["area_m2"] == 240.0                                      # 5 m 走道不计入（整体外包是 30 × 13 = 390）
    assert top["footprint_m2"] + bottom["footprint_m2"] == pytest.approx(sum(p.area for p in parts) / 1e6)


@pytest.mark.parametrize("aisle_mm, n_lines", [(2900, 1), (3000, 2)])
def test_only_aisles_at_least_the_gap_split_lines(aisle_mm, n_lines):
    """更窄的贯通空隙（线体内的操作通道）不分开线体"""
    e = D.line_envelope(rects([(0, 0, 30000, 3000), (0, 3000 + aisle_mm, 30000, 3000)]), 1.0, 2, 3000)
    assert len(e["lanes"]) == n_lines
    if n_lines == 1:
        assert e["lanes"][0]["width_m"] == pytest.approx((6000 + aisle_mm) / 1000, abs=0.05)


def test_lines_along_y_are_numbered_left_to_right():
    e = D.line_envelope(rects([(y, x, h, w) for x, y, w, h in TWO_LINES]), 1.0, 2, 3000)
    assert e["along"] == "y" and e["lanes"][0]["box"][0] == 0 and e["area_m2"] == 240.0


def test_line_split_ignores_position_and_follows_units():
    shifted = D.line_envelope(rects([(x + 3.2e9, y - 6.0e8, w, h) for x, y, w, h in TWO_LINES]), 1.0, 2, 3000)
    metres = D.line_envelope(rects(TWO_LINES, unit=1000.0), 1000.0, 2, 3000)      # 图纸单位为 m
    base = D.line_envelope(rects(TWO_LINES), 1.0, 2, 3000)
    dims = lambda e: [(l_["length_m"], l_["width_m"], l_["footprint_m2"]) for l_ in e["lanes"]]
    assert dims(shifted) == dims(base) == dims(metres)


def test_small_part_in_the_aisle_neither_joins_lines_nor_is_lost():
    parts = rects(TWO_LINES + [(5000, 6000, 1000, 1000)])              # 1 m² 小件在走道中间偏下
    e = D.line_envelope(parts, 1.0, 2, 3000)
    assert len(e["lanes"]) == 2 and e["area_m2"] == 240.0
    assert sum(l_["footprint_m2"] for l_ in e["lanes"]) == pytest.approx(232.0 + 1.0)     # 设备 40 + 72 + 120


def test_measure_splits_two_rows_of_machines_into_two_lines(tmp_path, conf):
    doc = make_doc()
    msp = doc.modelspace()
    for y in (0, 6000):                                                 # 两排设备（每台 4 × 1 m），相距 5 m
        for x in (0, 5000, 10000):
            msp.add_blockref("MACHINE", (x, y), dxfattribs=dict(xscale=2))
    r = run(doc, tmp_path, conf)
    lanes = r["envelope"]["lanes"]
    assert [(l_["length_m"], l_["width_m"]) for l_ in lanes] == [(14.0, 1.0), (14.0, 1.0)]
    assert r["envelope"]["area_m2"] == pytest.approx(28.0, abs=0.5)
    assert sum(l_["footprint_m2"] for l_ in lanes) == pytest.approx(r["footprint_m2"], abs=0.2)


def test_installer_self_test_dxf_has_its_known_area(tmp_path, conf):
    """安装包自检（webapp/smoke.py）用的合成 DXF：面积必须是已知值，自检才有意义"""
    from webapp.smoke import DXF_EQUIPMENT_M2, DXF_TOLERANCE, make_layout_dxf
    path = make_layout_dxf(str(tmp_path / "smoke.dxf"))
    r = D.measure(dict(id="s", name="s", dxf=path), conf, str(tmp_path / "out"))
    assert r["footprint_m2"] == pytest.approx(DXF_EQUIPMENT_M2, rel=DXF_TOLERANCE)
    assert sum(x["cls"] == "equipment" for x in r["inserts"]) == 3 and r["transform_check_failures"] == []
