"""estimate.py（网页上的汇总）必须与 Excel『汇总』页的公式一致。

核对值取自 README『本例结果』（当时由工作簿公式算出）。
"""
import copy

import pytest
import yaml

import estimate
from conftest import ROOT


@pytest.fixture
def cfg():
    with open(f"{ROOT}/config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def drawing(footprint, lanes, stations=()):
    return dict(id="X", name="X", footprint_m2=footprint, stations=list(stations),
                envelope=dict(lanes=[dict(length_m=L, width_m=W) for L, W in lanes]))


def test_101_matches_readme(cfg):
    # README：101 基准 69.8×6.9 ≈ 482 m²，精细 216 m²；公摊工时（钳工+电工）501 → 225 h；箱数 11（10–12）；运费 ≈ US$60k
    e = estimate.summarize(drawing(215.9, [(69.8, 6.9)]), estimate.read_params(cfg), cfg)
    assert e["baseline_m2"] == pytest.approx(481.62)
    assert round(e["labor_hours_baseline"]) == 501 and round(e["labor_hours_fine"]) == 225
    # 默认按基准面积（产线总长×宽）：钳工、电工各 250 h
    assert e["labor_area_basis"] == "baseline" and e["labor_area_m2"] == e["baseline_m2"]
    assert round(e["labor_hours_fitter"]) == 250 and round(e["labor_hours_electrician"]) == 250
    assert e["labor_hours"] == pytest.approx(e["labor_hours_baseline"])
    # 低 / 高只换地板利用率（乐观 75% / 保守 55%），不再乘面积误差系数
    # 超限比例默认 0：是否走框架箱由人工按线填写，系统不自动安排
    # 地板利用率默认 0.75 / 0.88（唐总：保留两个值，取较高区间）
    assert (e["boxes_low"], e["boxes_mid"], e["boxes_high"], e["frame_boxes_mid"]) == (10, 11, 12, 0)
    assert e["freight_mid_usd"] == pytest.approx(11 * (3997 + 1500))


def test_102_matches_readme(cfg):
    # README：102 基准 103.3×20.2 + 107.2×20.4 ≈ 4,274 m²，精细 1,519 m²；公摊工时（钳工+电工）4,444 → 1,579 h；箱数 76（71–83）
    stations = [dict(footprint_m2=1000.0), dict(footprint_m2=518.6)]
    e = estimate.summarize(drawing(1518.5, [(103.3, 20.2), (107.2, 20.4)], stations), estimate.read_params(cfg), cfg)
    assert e["fine_m2"] == pytest.approx(1518.6) and e["fine_source"] == "stations"
    assert round(e["baseline_m2"]) == 4274 and round(e["labor_hours_baseline"]) == 4444 and round(e["labor_hours_fine"]) == 1579
    assert (e["boxes_low"], e["boxes_mid"], e["boxes_high"]) == (71, 76, 83)
    assert round(e["freight_mid_usd"], -3) == 418000


def test_fine_area_falls_back_to_footprint_without_stations(cfg):
    e = estimate.summarize(drawing(100.0, [(20, 10)]), estimate.read_params(cfg), cfg)
    assert e["fine_m2"] == 100.0 and e["fine_source"] == "footprint" and e["station_count"] == 0


def test_labor_hours_per_trade_and_area_basis(cfg):
    # 工时表：钳工、电工各算一次“面积 × 系数”；口径由业务选择，不改变面积本身
    d = drawing(100.0, [(20, 10)])
    p = estimate.validate_params({"fitter_hours_per_m2": 0.5, "electrician_hours_per_m2": 0.25}, cfg)
    e = estimate.summarize(d, p, cfg)
    assert (e["labor_hours_fitter"], e["labor_hours_electrician"], e["labor_hours"]) == pytest.approx((100, 50, 150))
    p = estimate.validate_params({"fitter_hours_per_m2": 0.5, "electrician_hours_per_m2": 0.25, "labor_area_basis": "fine"}, cfg)
    f = estimate.summarize(d, p, cfg)
    assert f["labor_area_m2"] == 100.0 and (f["labor_hours_fitter"], f["labor_hours_electrician"]) == pytest.approx((50, 25))
    assert (f["baseline_m2"], f["fine_m2"], f["boxes_mid"]) == (e["baseline_m2"], e["fine_m2"], e["boxes_mid"])
    assert f["labor_hours_baseline"] == e["labor_hours_baseline"] and f["labor_hours_fine"] == e["labor_hours_fine"]


def test_totals_sum_columns(cfg):
    p = estimate.read_params(cfg)
    e = estimate.estimate([drawing(215.9, [(69.8, 6.9)]), drawing(100.0, [(20, 10)])], p, cfg)
    assert e["total"]["fine_m2"] == pytest.approx(315.9)
    assert e["total"]["boxes_mid"] == sum(d["boxes_mid"] for d in e["drawings"])
    assert e["total"]["labor_hours"] == pytest.approx(sum(d["labor_hours"] for d in e["drawings"]))
    assert e["total"]["fine_vs_baseline"] == pytest.approx(315.9 / (69.8 * 6.9 + 200))


def test_excel_rounding():
    # Excel ROUND 四舍五入；Python round(2.5) == 2（银行家舍入）不能直接用
    assert [estimate._round(x) for x in (0.4, 0.5, 1.5, 2.5)] == [0, 1, 2, 3]
    assert estimate._roundup(13.0000000000001) == 13 and estimate._roundup(13.01) == 14


def test_validate_params(cfg):
    p = estimate.validate_params({"fitter_hours_per_m2": "0.6", "route": "上海 → 纽约"}, cfg)
    assert p["fitter_hours_per_m2"] == 0.6 and p["route"] == "上海 → 纽约"
    assert p["packaging_factor"] == cfg["packing"]["packaging_factor"]          # 未填的取默认
    assert p["electrician_hours_per_m2"] == cfg["labor"]["electrician_hours_per_m2"]
    assert p["labor_area_basis"] == cfg["labor"]["area_basis"]
    for bad in ({"fitter_hours_per_m2": "abc"}, {"electrician_hours_per_m2": -1}, {"labor_area_basis": "envelope"},
                {"packaging_factor": -1}, {"route": "火星"},
                {"floor_utilisation_low": 0.9, "floor_utilisation_high": 0.5},
                {"flat_rack_rate_usd": "abc"}, {"flat_rack_rate_usd": -1}):
        with pytest.raises(ValueError):
            estimate.validate_params(bad, cfg)


def test_apply_params_does_not_touch_base_config(cfg):
    before = copy.deepcopy(cfg)
    p = estimate.validate_params({"fitter_hours_per_m2": 1.0, "labor_area_basis": "fine"}, cfg)
    out = estimate.apply_params(cfg, p)
    assert out["labor"]["fitter_hours_per_m2"] == 1.0 and out["labor"]["area_basis"] == "fine" and cfg == before


def test_flat_rack_rate_is_entered_directly(cfg):
    # 框架箱运价直接输入（US$/柜），不再是 40HQ 运价 × 倍数；未填时取所选航线的默认值
    rates = cfg["freight"]["flat_rack_rates_usd_per_40ft"]
    assert estimate.validate_params({}, cfg)["flat_rack_rate_usd"] == rates[cfg["freight"]["route"]]
    assert estimate.validate_params({"route": "上海 → 纽约"}, cfg)["flat_rack_rate_usd"] == rates["上海 → 纽约"]
    p = estimate.validate_params({"flat_rack_rate_usd": "12000", "oversize_share": 1.0, "route": "上海 → 纽约"}, cfg)
    assert p["flat_rack_rate_usd"] == 12000.0
    e = estimate.summarize(drawing(100.0, [(20, 10)]), p, cfg)
    assert e["cost_per_frame_usd"] == pytest.approx(12000 + cfg["freight"]["local_charges_usd_per_box"])
    assert e["cost_per_box_usd"] == pytest.approx(9726 + cfg["freight"]["local_charges_usd_per_box"])   # 40HQ 不受影响
    assert e["freight_mid_usd"] == pytest.approx(e["boxes_mid"] * e["cost_per_frame_usd"])
    before = copy.deepcopy(cfg)
    out = estimate.apply_params(cfg, p)            # 写回 config 副本，供工作簿使用
    assert out["freight"]["flat_rack_rates_usd_per_40ft"]["上海 → 纽约"] == 12000.0 and cfg == before
    assert "footprint_uncertainty_low" not in p and "flat_rack_multiplier" not in p


def test_frame_boxes_entered_per_line(cfg):
    # 按线直接填写框架箱柜数：该线不再按超限比例，低 / 中 / 高都用所填柜数；其他线不受影响
    lines = [dict(drawing(215.9, [(69.8, 6.9)]), id="101"), dict(drawing(215.9, [(69.8, 6.9)]), id="102")]
    p = estimate.validate_params({"frame_boxes": {"101": "3", "102": ""}, "oversize_share": 0.1}, cfg)
    assert p["frame_boxes"] == {"101": 3}
    a, b = estimate.estimate(lines, p, cfg)["drawings"]
    per_box, per_frame = a["cost_per_box_usd"], a["cost_per_frame_usd"]
    assert (a["frame_source"], a["frame_boxes_mid"], a["frame_boxes_by_share"]) == ("count", 3, 1)
    assert (a["boxes_low"], a["boxes_mid"], a["boxes_high"]) == (10, 11, 12)          # 柜数不变，只是其中 3 个是框架箱
    assert a["freight_low_usd"] == pytest.approx(7 * per_box + 3 * per_frame)
    assert a["freight_mid_usd"] == pytest.approx(8 * per_box + 3 * per_frame)
    assert a["freight_high_usd"] == pytest.approx(9 * per_box + 3 * per_frame)
    assert (b["frame_source"], b["frame_boxes_mid"]) == ("share", 1)                     # 空着：按比例
    assert b["freight_low_usd"] == pytest.approx(10 * (0.9 * per_box + 0.1 * per_frame))


def test_frame_boxes_above_area_count_raise_the_total(cfg):
    # 填的框架箱柜数比按面积算出的柜数还多：总柜数取所填柜数，全部按框架箱计费
    p = estimate.validate_params({"frame_boxes": {"X": 20}}, cfg)
    e = estimate.summarize(drawing(215.9, [(69.8, 6.9)]), p, cfg)
    assert (e["boxes_low"], e["boxes_mid"], e["boxes_high"], e["frame_boxes_mid"]) == (20, 20, 20, 20)
    assert e["freight_mid_usd"] == pytest.approx(20 * e["cost_per_frame_usd"])
    zero = estimate.summarize(drawing(215.9, [(69.8, 6.9)]), estimate.validate_params({"frame_boxes": {"X": 0}}, cfg), cfg)
    assert zero["frame_boxes_mid"] == 0 and zero["freight_mid_usd"] == pytest.approx(11 * zero["cost_per_box_usd"])


def test_frame_boxes_validation(cfg):
    for bad in ({"frame_boxes": {"101": -1}}, {"frame_boxes": {"101": 1.5}}, {"frame_boxes": {"101": "abc"}},
                {"frame_boxes": {"101": 10001}}, {"frame_boxes": [3]}):
        with pytest.raises(ValueError):
            estimate.validate_params(bad, cfg)
    out = estimate.apply_params(cfg, estimate.validate_params({"frame_boxes": {"101": 2}}, cfg))
    assert out["packing"]["frame_boxes"] == {"101": 2} and "frame_boxes" not in cfg["packing"]


def dxf_drawing(footprint, inserts):
    return dict(drawing(footprint, [(20, 10)]), inserts=[
        dict(handle=h, block=b, cls=c, area_m2=a) for h, b, c, a in inserts])


def test_block_groups_split_overlap_by_gross_area():
    # 同一图块的插入合成一组；打散的设备线条每团一组；重叠按毛面积比例分摊，各组之和 = 投影面积
    d = dxf_drawing(90.0, [("1", "A", "equipment", 40.0), ("2", "A", "equipment", 20.0), ("3", "B", "equipment", 30.0),
                           ("loose-1", None, "equipment", 10.0), ("4", "C", "mobile", 50.0), ("5", "D", "excluded", 5.0)])
    g = {x["key"]: x for x in estimate.block_groups(d)}
    assert set(g) == {"A", "B", "loose:loose-1"}                      # 移动设备、排除的图块不参与
    assert (g["A"]["count"], g["A"]["gross_m2"]) == (2, 60.0)
    assert g["A"]["area_m2"] == pytest.approx(54.0) and g["B"]["area_m2"] == pytest.approx(27.0)
    assert sum(x["area_m2"] for x in g.values()) == pytest.approx(90.0)
    assert estimate.block_groups(dict(d, stations=[dict(footprint_m2=90.0)])) == []   # PDF 按工位分摊时不分图块


def test_block_packing_stack2_and_custom_factor(cfg):
    d = dict(dxf_drawing(90.0, [("1", "A", "equipment", 60.0), ("2", "B", "equipment", 40.0)]), id="103")
    base = estimate.summarize(d, estimate.read_params(cfg), cfg)
    pack = cfg["packing"]["packaging_factor"]
    assert base["packed_m2"] == pytest.approx(90.0 * pack) and base["packing_source"] == "uniform"
    p = estimate.validate_params({"block_packing": {"103": {"A": "stack2", "B": "0.4"}}}, cfg)
    e = estimate.summarize(d, p, cfg)
    # A 分摊 54 m²，可叠 2 层 → 系数减半；B 分摊 36 m²，系数 0.4
    assert e["packed_m2"] == pytest.approx(54.0 * pack / 2 + 36.0 * 0.4) and e["packing_source"] == "blocks"
    assert {g["key"]: g["choice"] for g in e["block_groups"]} == {"A": "stack2", "B": "custom"}
    other = estimate.summarize(dict(d, id="104"), p, cfg)           # 别的图纸不受影响
    assert other["packed_m2"] == pytest.approx(90.0 * pack)
    for bad in ({"block_packing": {"103": {"A": "0.01"}}}, {"block_packing": {"103": {"A": "abc"}}},
                {"block_packing": {"103": "stack2"}}, {"block_packing": ["A"]}):
        with pytest.raises(ValueError):
            estimate.validate_params(bad, cfg)
