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
    # README：101 基准 69.8×6.9 ≈ 482 m²，精细 216 m²；公摊工时（钳工+电工）501 → 225 h；箱数 14（10–18）；运费 ≈ US$81k
    e = estimate.summarize(drawing(215.9, [(69.8, 6.9)]), estimate.read_params(cfg), cfg)
    assert e["baseline_m2"] == pytest.approx(481.62)
    assert round(e["labor_hours_baseline"]) == 501 and round(e["labor_hours_fine"]) == 225
    # 默认按基准面积（产线总长×宽）：钳工、电工各 250 h
    assert e["labor_area_basis"] == "baseline" and e["labor_area_m2"] == e["baseline_m2"]
    assert round(e["labor_hours_fitter"]) == 250 and round(e["labor_hours_electrician"]) == 250
    assert e["labor_hours"] == pytest.approx(e["labor_hours_baseline"])
    assert (e["boxes_low"], e["boxes_mid"], e["boxes_high"], e["frame_boxes_mid"]) == (10, 14, 18, 1)
    assert e["freight_mid_usd"] == pytest.approx(13 * (3997 + 1500) + 1 * (3997 * 2 + 1500))


def test_102_matches_readme(cfg):
    # README：102 基准 103.3×20.2 + 107.2×20.4 ≈ 4,274 m²，精细 1,519 m²；公摊工时（钳工+电工）4,444 → 1,579 h；箱数 96（71–124）
    stations = [dict(footprint_m2=1000.0), dict(footprint_m2=518.6)]
    e = estimate.summarize(drawing(1518.5, [(103.3, 20.2), (107.2, 20.4)], stations), estimate.read_params(cfg), cfg)
    assert e["fine_m2"] == pytest.approx(1518.6) and e["fine_source"] == "stations"
    assert round(e["baseline_m2"]) == 4274 and round(e["labor_hours_baseline"]) == 4444 and round(e["labor_hours_fine"]) == 1579
    assert (e["boxes_low"], e["boxes_mid"], e["boxes_high"]) == (71, 96, 124)
    assert round(e["freight_mid_usd"], -3) == 568000


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
                {"floor_utilisation_low": 0.9, "floor_utilisation_high": 0.5}):
        with pytest.raises(ValueError):
            estimate.validate_params(bad, cfg)


def test_apply_params_does_not_touch_base_config(cfg):
    before = copy.deepcopy(cfg)
    p = estimate.validate_params({"fitter_hours_per_m2": 1.0, "labor_area_basis": "fine"}, cfg)
    out = estimate.apply_params(cfg, p)
    assert out["labor"]["fitter_hours_per_m2"] == 1.0 and out["labor"]["area_basis"] == "fine" and cfg == before
