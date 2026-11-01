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
    # README：101 基准 69.8×6.9 ≈ 482 m²，精细 216 m²；工时 250 → 112 h；箱数 14（10–18）；运费 ≈ US$81k
    e = estimate.summarize(drawing(215.9, [(69.8, 6.9)]), estimate.read_params(cfg), cfg)
    assert e["baseline_m2"] == pytest.approx(481.62)
    assert round(e["hours_baseline"]) == 250 and round(e["hours_fine"]) == 112
    assert (e["boxes_low"], e["boxes_mid"], e["boxes_high"], e["frame_boxes_mid"]) == (10, 14, 18, 1)
    assert e["freight_mid_usd"] == pytest.approx(13 * (3997 + 1500) + 1 * (3997 * 2 + 1500))


def test_102_matches_readme(cfg):
    # README：102 基准 103.3×20.2 + 107.2×20.4 ≈ 4,274 m²，精细 1,519 m²；工时 2,222 → 790 h；箱数 96（71–124）
    stations = [dict(footprint_m2=1000.0), dict(footprint_m2=518.6)]
    e = estimate.summarize(drawing(1518.5, [(103.3, 20.2), (107.2, 20.4)], stations), estimate.read_params(cfg), cfg)
    assert e["fine_m2"] == pytest.approx(1518.6) and e["fine_source"] == "stations"
    assert round(e["baseline_m2"]) == 4274 and round(e["hours_baseline"]) == 2222 and round(e["hours_fine"]) == 790
    assert (e["boxes_low"], e["boxes_mid"], e["boxes_high"]) == (71, 96, 124)
    assert round(e["freight_mid_usd"], -3) == 568000


def test_fine_area_falls_back_to_footprint_without_stations(cfg):
    e = estimate.summarize(drawing(100.0, [(20, 10)]), estimate.read_params(cfg), cfg)
    assert e["fine_m2"] == 100.0 and e["fine_source"] == "footprint" and e["station_count"] == 0


def test_totals_sum_columns(cfg):
    p = estimate.read_params(cfg)
    e = estimate.estimate([drawing(215.9, [(69.8, 6.9)]), drawing(100.0, [(20, 10)])], p, cfg)
    assert e["total"]["fine_m2"] == pytest.approx(315.9)
    assert e["total"]["boxes_mid"] == sum(d["boxes_mid"] for d in e["drawings"])
    assert e["total"]["fine_vs_baseline"] == pytest.approx(315.9 / (69.8 * 6.9 + 200))


def test_excel_rounding():
    # Excel ROUND 四舍五入；Python round(2.5) == 2（银行家舍入）不能直接用
    assert [estimate._round(x) for x in (0.4, 0.5, 1.5, 2.5)] == [0, 1, 2, 3]
    assert estimate._roundup(13.0000000000001) == 13 and estimate._roundup(13.01) == 14


def test_validate_params(cfg):
    p = estimate.validate_params({"shared_hours_per_m2": "0.6", "route": "上海 → 纽约"}, cfg)
    assert p["shared_hours_per_m2"] == 0.6 and p["route"] == "上海 → 纽约"
    assert p["packaging_factor"] == cfg["packing"]["packaging_factor"]          # 未填的取默认
    for bad in ({"shared_hours_per_m2": "abc"}, {"packaging_factor": -1}, {"route": "火星"},
                {"floor_utilisation_low": 0.9, "floor_utilisation_high": 0.5}):
        with pytest.raises(ValueError):
            estimate.validate_params(bad, cfg)


def test_apply_params_does_not_touch_base_config(cfg):
    before = copy.deepcopy(cfg)
    p = estimate.validate_params({"shared_hours_per_m2": 1.0}, cfg)
    out = estimate.apply_params(cfg, p)
    assert out["labor"]["shared_hours_per_m2"] == 1.0 and cfg == before
