"""
公摊工时 / 装柜 / 运费估算 —— 与 build_workbook.py『汇总』页逐行对应的同一套公式，供网页显示。

工作簿里是 Excel 公式（改参数自动重算），这里是同样的计算，两处必须一致：
改一处公式时同时改另一处；tests/test_estimate.py 用 README 中 101/102 的结果核对。

业务参数（系数、利用率、运价……）都是人工输入，不由图纸推断；默认值来自 config.yaml。
"""
import copy
import math

# 网页可修改的参数：key -> (config.yaml 中的位置, 最小值, 最大值)；route 单独校验
PARAMS = {
    "fitter_hours_per_m2": (("labor", "fitter_hours_per_m2"), 0.0, 100.0),
    "electrician_hours_per_m2": (("labor", "electrician_hours_per_m2"), 0.0, 100.0),
    "packaging_factor": (("packing", "packaging_factor"), 0.5, 5.0),
    "floor_utilisation_low": (("packing", "floor_utilisation", "low"), 0.05, 1.0),
    "floor_utilisation_high": (("packing", "floor_utilisation", "high"), 0.05, 1.0),
    "footprint_uncertainty_low": (("packing", "footprint_uncertainty", "low"), 0.1, 2.0),
    "footprint_uncertainty_high": (("packing", "footprint_uncertainty", "high"), 0.1, 2.0),
    "oversize_share": (("packing", "oversize_share"), 0.0, 1.0),
    "flat_rack_multiplier": (("freight", "flat_rack_multiplier"), 0.0, 10.0),
    "local_charges_usd_per_box": (("freight", "local_charges_usd_per_box"), 0.0, 1e6),
    "insurance_pct_of_freight": (("freight", "insurance_pct_of_freight"), 0.0, 1.0),
}
# 公摊工时按哪种面积计算（业务决定，图纸不决定）：基准 = 产线总长×宽，精细 = 各工位投影面积之和
LABOR_AREA_BASES = ("baseline", "fine")


def _get(cfg, path):
    for k in path:
        cfg = cfg[k]
    return cfg


def _set(cfg, path, value):
    for k in path[:-1]:
        cfg = cfg[k]
    cfg[path[-1]] = value


def read_params(cfg):
    """config.yaml -> 网页参数（扁平字典）"""
    p = {k: _get(cfg, path) for k, (path, _, _) in PARAMS.items()}
    p["labor_area_basis"] = cfg["labor"]["area_basis"]
    p["route"] = cfg["freight"]["route"]
    return p


def validate_params(raw, cfg):
    """校验网页提交的参数；缺省项取 config.yaml 默认值。出错抛 ValueError（中英文说明）。"""
    p = read_params(cfg)
    for k, (_, lo, hi) in PARAMS.items():
        if raw.get(k) in (None, ""):
            continue
        try:
            v = float(raw[k])
        except (TypeError, ValueError):
            raise ValueError(f"参数 {k} 不是数字 / Parameter {k} is not a number")
        if not (lo <= v <= hi) or math.isnan(v):
            raise ValueError(f"参数 {k} 超出范围 {lo}–{hi} / Parameter {k} out of range {lo}–{hi}")
        p[k] = v
    if raw.get("labor_area_basis"):
        if raw["labor_area_basis"] not in LABOR_AREA_BASES:
            raise ValueError(f"未知面积口径 {raw['labor_area_basis']} / Unknown area basis {raw['labor_area_basis']}")
        p["labor_area_basis"] = raw["labor_area_basis"]
    if raw.get("route"):
        if raw["route"] not in cfg["freight"]["rates_usd_per_40ft"]:
            raise ValueError(f"未知航线 {raw['route']} / Unknown route {raw['route']}")
        p["route"] = raw["route"]
    if p["floor_utilisation_low"] > p["floor_utilisation_high"]:
        raise ValueError("保守利用率不能高于乐观利用率 / Conservative utilisation exceeds optimistic")
    if p["footprint_uncertainty_low"] > p["footprint_uncertainty_high"]:
        raise ValueError("面积误差下限系数不能高于上限 / Lower uncertainty factor exceeds upper")
    return p


def apply_params(cfg, p):
    """把参数写回一份 config 副本（供 build_workbook.py 使用，保证工作簿与网页同值）"""
    out = copy.deepcopy(cfg)
    for k, (path, _, _) in PARAMS.items():
        _set(out, path, p[k])
    out["labor"]["area_basis"] = p["labor_area_basis"]
    out["freight"]["route"] = p["route"]
    return out


def _roundup(x):
    """Excel ROUNDUP(x, 0)，x ≥ 0；先去掉浮点尾差"""
    return math.ceil(round(x, 9))


def _round(x):
    """Excel ROUND(x, 0)：四舍五入（Python round 是银行家舍入，不能直接用）"""
    return math.floor(x + 0.5)


def summarize(drawing, p, cfg):
    """一张图纸的『汇总』列。drawing = results.json 中的一项。"""
    lanes = drawing["envelope"]["lanes"]
    stations = drawing["stations"]
    # ① 基准：工作簿按 总长×总宽（各取 0.1 m）逐线体相乘再相加
    baseline = sum(l_["length_m"] * l_["width_m"] for l_ in lanes)
    # ② 精细：各工位面积之和；未识别到工位时取整图投影面积（同一口径）
    fine = sum(s["footprint_m2"] for s in stations) if stations else drawing["footprint_m2"]
    # ③ 公摊工时：工时表里钳工、电工各有一行“面积 × 系数”，按所选面积口径计算
    rf, re_ = p["fitter_hours_per_m2"], p["electrician_hours_per_m2"]
    labor_area = baseline if p["labor_area_basis"] == "baseline" else fine
    c = cfg["packing"]["container"]
    floor = c["inner_length_m"] * c["inner_width_m"]
    ulo, uhi = p["floor_utilisation_low"], p["floor_utilisation_high"]
    pack, os_ = p["packaging_factor"], p["oversize_share"]
    packed = fine * pack
    boxes_low = _roundup(fine * p["footprint_uncertainty_low"] * pack / (floor * uhi))
    boxes_mid = _roundup(packed / (floor * (ulo + uhi) / 2))
    boxes_high = _roundup(fine * p["footprint_uncertainty_high"] * pack / (floor * ulo))
    frame_mid = _round(boxes_mid * os_)
    rate = cfg["freight"]["rates_usd_per_40ft"][p["route"]]
    ins, loc = p["insurance_pct_of_freight"], p["local_charges_usd_per_box"]
    per_box = rate * (1 + ins) + loc
    per_frame = rate * p["flat_rack_multiplier"] * (1 + ins) + loc
    blended = (1 - os_) * per_box + os_ * per_frame
    return dict(
        station_count=len(stations),
        fine_source="stations" if stations else "footprint",
        baseline_m2=baseline,
        fine_m2=fine,
        fine_vs_baseline=fine / baseline if baseline else None,
        labor_area_basis=p["labor_area_basis"],
        labor_area_m2=labor_area,
        labor_hours_baseline=baseline * (rf + re_),
        labor_hours_fine=fine * (rf + re_),
        labor_hours_fitter=labor_area * rf,
        labor_hours_electrician=labor_area * re_,
        labor_hours=labor_area * (rf + re_),
        packed_m2=packed,
        boxes_low=boxes_low, boxes_mid=boxes_mid, boxes_high=boxes_high,
        frame_boxes_mid=frame_mid,
        rate_usd_per_40ft=rate,
        cost_per_box_usd=per_box, cost_per_frame_usd=per_frame,
        freight_low_usd=boxes_low * blended,
        freight_mid_usd=(boxes_mid - frame_mid) * per_box + frame_mid * per_frame,
        freight_high_usd=boxes_high * blended,
    )


SUMMED = ("station_count", "baseline_m2", "fine_m2", "labor_area_m2",
          "labor_hours_baseline", "labor_hours_fine", "labor_hours_fitter", "labor_hours_electrician", "labor_hours",
          "packed_m2",
          "boxes_low", "boxes_mid", "boxes_high", "frame_boxes_mid",
          "freight_low_usd", "freight_mid_usd", "freight_high_usd")


def estimate(drawings, p, cfg):
    """全部图纸 -> 每张一列 + 合计列（与『汇总』页的合计列相同）"""
    cols = [dict(id=d["id"], name=d["name"], **summarize(d, p, cfg)) for d in drawings]
    total = {k: sum(c_[k] for c_ in cols) for k in SUMMED}
    total["fine_vs_baseline"] = total["fine_m2"] / total["baseline_m2"] if total["baseline_m2"] else None
    return dict(drawings=cols, total=total, params=p,
                container=cfg["packing"]["container"]["name"], route=p["route"])
