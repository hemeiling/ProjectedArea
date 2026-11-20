"""人工修正区域：加入 / 剪掉的面积口径、重叠、平移不变、自相交折线、按插入剩下的比例"""
import pytest
from shapely.geometry import MultiPolygon, box

import manual_edits as M

UNIT_MM = 1.0                     # 图纸单位 = mm，1 m² = 1e6
M2 = 1e6


def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def edit(op, pts, i="e"):
    return dict(id=i, op=op, points=pts)


# 两台设备：2 × 1 m 和 1 × 1 m，中间 2 m 空地
FOOTPRINT = MultiPolygon([box(0, 0, 2000, 1000), box(4000, 0, 5000, 1000)])


def test_no_edits_keeps_measured_area():
    r = M.apply(FOOTPRINT, [], UNIT_MM)
    assert r["measured_m2"] == pytest.approx(3.0) and r["adjusted_m2"] == pytest.approx(3.0)
    assert r["added_m2"] == r["cut_m2"] == r["manual_add_m2"] == 0 and r["blocks_m2"] == pytest.approx(3.0)


def test_add_counts_only_area_not_already_measured():
    # 加入区域 1000–3000：其中 1000–2000 已经计入，只多出 1 m²
    r = M.apply(FOOTPRINT, [edit("add", rect(1000, 0, 3000, 1000))], UNIT_MM)
    assert r["added_m2"] == pytest.approx(1.0) and r["adjusted_m2"] == pytest.approx(4.0)
    assert r["edits"][0]["area_m2"] == pytest.approx(2.0) and r["edits"][0]["effect_m2"] == pytest.approx(1.0)
    assert r["blocks_m2"] + r["manual_add_m2"] == pytest.approx(r["adjusted_m2"])


def test_cut_counts_only_area_that_was_counted_and_wins_over_add():
    edits = [edit("add", rect(2000, 0, 4000, 1000), "a"),        # 空地全部加入：+2 m²
             edit("cut", rect(1500, 0, 2500, 1000), "c")]        # 剪掉 0.5 设备 + 0.5 刚加入的
    r = M.apply(FOOTPRINT, edits, UNIT_MM)
    assert r["added_m2"] == pytest.approx(2.0) and r["cut_m2"] == pytest.approx(1.0)
    assert r["adjusted_m2"] == pytest.approx(3.0 + 2.0 - 1.0)
    assert r["blocks_m2"] == pytest.approx(2.5) and r["manual_add_m2"] == pytest.approx(1.5)
    assert r["n_add"] == 1 and r["n_cut"] == 1


def test_cut_on_empty_floor_changes_nothing():
    r = M.apply(FOOTPRINT, [edit("cut", rect(2200, 0, 3800, 1000))], UNIT_MM)
    assert r["cut_m2"] == pytest.approx(0) and r["adjusted_m2"] == pytest.approx(3.0)


def test_result_does_not_depend_on_absolute_position():
    edits = [edit("add", rect(2000, 0, 4000, 500)), edit("cut", rect(0, 0, 500, 1000))]
    base = M.apply(FOOTPRINT, edits, UNIT_MM)
    dx, dy = 3.7e7, -2.1e7                                        # 远离原点的大坐标（厂区坐标系常见）
    from shapely import affinity
    moved = M.apply(affinity.translate(FOOTPRINT, dx, dy),
                    [dict(e, points=[[x + dx, y + dy] for x, y in e["points"]]) for e in edits], UNIT_MM)
    for k in ("measured_m2", "added_m2", "cut_m2", "adjusted_m2", "blocks_m2", "manual_add_m2"):
        assert moved[k] == pytest.approx(base[k], abs=1e-6), k


def test_self_intersecting_outline_is_repaired_not_dropped():
    # “8”字形折线：两个 1 × 1 m 的三角形，共 1 m²
    bowtie = [[2000, 0], [4000, 1000], [4000, 0], [2000, 1000]]
    r = M.apply(FOOTPRINT, [edit("add", bowtie)], UNIT_MM)
    assert r["added_m2"] == pytest.approx(1.0)


def test_degenerate_polygon_is_rejected():
    assert M.polygon([[0, 0], [1000, 0], [2000, 0]]) is None      # 共线
    assert M.polygon([[0, 0], [1000, 0]]) is None                 # 点数不足


def test_pixel_round_trip_and_kept_share_of_cut_inserts():
    canvas = dict(x0=-100.0, y1=1100.0, scale=0.5, width=2601, height=601)
    pts = rect(0, 0, 2000, 1000)
    back = M.to_world(M.to_px(pts, canvas), canvas)
    assert [v for xy in back for v in xy] == pytest.approx([v for xy in pts for v in xy])
    rows = [dict(handle="A", cls="equipment", outline_px=[M.to_px(rect(0, 0, 2000, 1000), canvas)]),
            dict(handle="B", cls="equipment", outline_px=[M.to_px(rect(4000, 0, 5000, 1000), canvas)])]
    r = M.apply(FOOTPRINT, [edit("cut", rect(0, 0, 500, 1000))], UNIT_MM, rows, canvas)
    assert r["kept"] == {"A": pytest.approx(0.75)}                # B 没有被剪到，不列出


def _device_rows():
    # A、B 在测量轮廓里（A 与 C 重叠 0.5 m²）；C 是被排除的设备（与 A 相邻，不在测量轮廓里）；D 是散线设备
    a, b, c = box(0, 0, 2000, 1000), box(4000, 0, 5000, 1000), box(1500, 0, 2500, 1000)
    rows = [dict(handle="A", block="M", cls="equipment", area_m2=2.0, outline_px=[[0, 0]]),
            dict(handle="B", block="M", cls="equipment", area_m2=1.0, outline_px=[[0, 0]]),
            dict(handle="C", block="X", cls="excluded", area_m2=1.0, outline_px=[[0, 0]])]
    return rows, dict(A=a, B=b, C=c)


def test_device_exclude_removes_only_the_exclusive_part_and_include_adds_it_back():
    rows, geoms = _device_rows()
    fp = geoms["A"].union(geoms["B"])
    ex = dict(id="x", op="exclude", scope="one", handle="B", block="M")
    r = M.apply(fp, [ex], UNIT_MM, rows, None, geoms)
    assert r["device_removed_m2"] == pytest.approx(1.0) and r["adjusted_m2"] == pytest.approx(2.0)
    assert r["classes"] == {"B": ["excluded", "x"]} and r["edits"][0]["status"] == "applied"
    inc = dict(id="y", op="include", scope="one", handle="C", block="X")
    r = M.apply(fp, [inc], UNIT_MM, rows, None, geoms)
    assert r["device_added_m2"] == pytest.approx(0.5) and r["adjusted_m2"] == pytest.approx(3.5)   # C 与 A 重叠的 0.5 已计入
    # A 不计入，但与 C 重叠的部分因 C 计入而保留
    both = [dict(id="x", op="exclude", scope="one", handle="A", block="M"), inc]
    r = M.apply(fp, both, UNIT_MM, rows, None, geoms)
    assert r["adjusted_m2"] == pytest.approx(1.0 + 1.0)            # B + C
    assert r["blocks_m2"] + r["manual_add_m2"] == pytest.approx(r["adjusted_m2"])


def test_exclude_whole_block_then_the_single_device_wins_and_mismatches_are_reported():
    rows, geoms = _device_rows()
    fp = geoms["A"].union(geoms["B"])
    edits = [dict(id="one", op="include", scope="one", handle="B", block="M"),
             dict(id="all", op="exclude", scope="block", handle="A", block="M")]
    r = M.apply(fp, edits, UNIT_MM, rows, None, geoms)
    assert r["classes"] == {"A": ["excluded", "all"]}              # B 被“这一台计入”保住，没变化
    assert r["adjusted_m2"] == pytest.approx(1.0)
    stale = dict(id="s", op="exclude", scope="one", handle="B", block="OTHER")   # 图块名对不上（改版的图纸）：不套用
    r = M.apply(fp, [stale], UNIT_MM, rows, None, geoms)
    assert r["adjusted_m2"] == pytest.approx(3.0) and r["edits"][0]["status"] == "unmatched"
    r = M.apply(fp, [dict(stale, block="M")], UNIT_MM, rows, None, None)          # 没有逐台轮廓：不套用
    assert r["adjusted_m2"] == pytest.approx(3.0) and r["edits"][0]["status"] == "no_geometry"


def test_device_edits_are_translation_invariant_and_cuts_follow_the_new_class():
    rows, geoms = _device_rows()
    fp = geoms["A"].union(geoms["B"])
    edits = [dict(id="x", op="exclude", scope="one", handle="B", block="M"),
             dict(id="c", op="cut", points=rect(0, 0, 500, 1000))]
    base = M.apply(fp, edits, UNIT_MM, rows, None, geoms)
    assert base["kept"] == {"A": pytest.approx(0.75)}              # 不计入的 B 不再参与“被剪到”
    from shapely import affinity
    dx, dy = 3.7e7, -2.1e7
    moved = M.apply(affinity.translate(fp, dx, dy), [edits[0], dict(edits[1], points=[[x + dx, y + dy] for x, y in rect(0, 0, 500, 1000)])],
                    UNIT_MM, rows, None, {h: affinity.translate(g, dx, dy) for h, g in geoms.items()})
    for k in ("measured_m2", "device_removed_m2", "cut_m2", "adjusted_m2", "blocks_m2"):
        assert moved[k] == pytest.approx(base[k], abs=1e-6), k
