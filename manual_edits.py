"""
人工修正投影面积（只用于 DXF：计入轮廓以矢量保存在 footprint.wkt，每个设备的精确轮廓在 instances.json）：
  · 设备改判：在叠加图上点一个设备，“不计入”（这一台，或这一类图块的全部），或把排除 / 待定 / 移动设备“计入”（这一台）；
  · 画多边形：“加入”自动识别漏掉的区域，或“剪掉”多包进来的区域。

修正绑定图纸文件的 SHA-256（文件改版后不会自动套用）：区域以图纸坐标保存；设备以 DXF 句柄保存，另记图块名和面积，
套用时核对，对不上的不套用并报告。测量面积保持不变，修正单独记录：

    设备改判后的轮廓 B = (测量轮廓 − 不计入设备的轮廓) ∪ 计入设备的轮廓
        （不计入设备与仍计入的设备重叠的部分不扣：投影面积是并集）
    修正后面积 = 测量面积 − 设备不计入 + 设备计入 + 加入 − 剪掉
        设备不计入 = 测量轮廓 − B，设备计入 = B − 测量轮廓
        加入 = 加入区域并集中 B 没有的部分
        剪掉 = 剪掉区域并集与（B ∪ 加入区域）的交集；同一处既加又剪时以剪掉为准

修正后的面积再分成两部分，供按图块类型分摊包装系数：
    图块部分   = B − 剪掉区域            （改判过的设备按新类别分组；被剪到的设备按剩下的比例分摊）
    人工加入部分 = (加入区域 − B) − 剪掉区域 （单独一组“人工加入区域”）
两部分之和 = 修正后面积。
"""
import json

import shapely
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.validation import make_valid

ADD, CUT = "add", "cut"
OPS = (ADD, CUT)                       # 画区域
EXCLUDE, INCLUDE = "exclude", "include"
DEVICE_OPS = (EXCLUDE, INCLUDE)        # 点设备改判
ONE, BLOCK = "one", "block"            # 这一台 / 这一类图块的全部（只用于不计入）
EQUIPMENT = "equipment"
MIN_POINTS = 3
MAX_POINTS = 500
MAX_NOTE = 200
# 打散的设备线条没有 DXF 句柄，编号随识别顺序：套用时还要核对面积（相对误差），对不上就不套用
LOOSE_AREA_MATCH = 0.01


def to_world(points_px, canvas):
    """叠加图像素 -> 图纸坐标（与 dxf_footprint.overlay_canvas 互逆）"""
    s, x0, y1 = canvas["scale"], canvas["x0"], canvas["y1"]
    return [[x0 + float(px) / s, y1 - float(py) / s] for px, py in points_px]


def to_px(points, canvas):
    s, x0, y1 = canvas["scale"], canvas["x0"], canvas["y1"]
    return [[round((x - x0) * s, 1), round((y1 - y) * s, 1)] for x, y in points]


def polygon(points):
    """多边形顶点 -> 有效的面（自相交的折线按 make_valid 修复，只保留面）；无面积时返回 None"""
    if not MIN_POINTS <= len(points) <= MAX_POINTS:
        return None
    g = make_valid(Polygon(points))
    parts = [p for p in getattr(g, "geoms", [g]) if isinstance(p, (Polygon, MultiPolygon)) and p.area > 0]
    return unary_union(parts) if parts else None


def outline_world(row, canvas):
    """一个设备的外边界（outline_px，按 1 像素简化）换回图纸坐标；没有精确轮廓时用来估计它被剪掉了多少"""
    polys = [polygon(to_world(ring, canvas)) for ring in row.get("outline_px") or []]
    polys = [p for p in polys if p is not None]
    return unary_union(polys) if polys else None


def load_instances(path):
    """dxf_footprint.write_instances 的文件 -> {句柄: 世界坐标中的设备轮廓}（与测量时完全相同的变换）"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    blocks = [shapely.from_wkt(w) for w in data["blocks"]]
    return {h: shapely.from_wkt(it["wkt"]) if "wkt" in it else affinity.affine_transform(blocks[it["block"]], it["affine"])
            for h, it in data["items"].items()}


def is_region(e):
    return e["op"] in OPS


def device_targets(e, rows):
    """一条设备改判对应的设备行；核对不上（句柄不在、图块名不同、散线设备面积变了）时返回 None"""
    if e.get("scope") == BLOCK:
        hit = [r for r in rows if e.get("block") and r.get("block") == e["block"]
               and r["cls"] == EQUIPMENT and r.get("outline_px")]
        return hit or None
    r = next((r for r in rows if r["handle"] == e.get("handle")), None)
    if r is None or not r.get("outline_px") or r.get("block") != e.get("block"):
        return None
    if r.get("block") is None and abs(r["area_m2"] - (e.get("area_m2") or 0)) > LOOSE_AREA_MATCH * max(r["area_m2"], 1e-9):
        return None
    return [r]


def device_classes(rows, edits):
    """设备改判后的类别。先套“这一类全部”，再套“这一台”（后者优先），同一级按时间先后。
    返回 ({句柄: [新类别, 修正 id]}（只列出类别真正变了的）, {修正 id: 对应的句柄}, [核对不上的修正 id])"""
    measured = {r["handle"]: r["cls"] for r in rows}
    final, targets, unmatched = {}, {}, []
    for e in sorted(edits, key=lambda e: e.get("scope") != BLOCK):      # sorted 稳定：同一级保持原顺序
        hit = device_targets(e, rows)
        if hit is None:
            unmatched.append(e["id"])
            continue
        targets[e["id"]] = [r["handle"] for r in hit]
        for r in hit:
            final[r["handle"]] = [EQUIPMENT if e["op"] == INCLUDE else "excluded", e["id"]]
    changed = {h: v for h, v in final.items() if (v[0] == EQUIPMENT) != (measured[h] == EQUIPMENT)}
    return changed, targets, unmatched


def effective_class(row, classes):
    return (classes.get(row["handle"]) or [row["cls"]])[0]


def apply(footprint, edits, unit_mm, rows=(), canvas=None, geoms=None):
    """footprint：测量轮廓（图纸坐标）；edits：生效的修正（区域 [{id, op, points}]，设备 [{id, op, scope, handle, block}]）；
    geoms：{句柄: 设备精确轮廓}（load_instances；没有时设备改判不套用）。

    返回各项面积（m²，不取整）、每条修正单独的效果、改判后的类别 classes {句柄: [类别, 修正 id]}，
    以及被剪到的设备剩下的比例 kept {句柄: 0–1}。
    """
    to_m2 = (unit_mm / 1000) ** 2
    # 在以测量轮廓左下角为原点的局部坐标中计算：结果与图纸的绝对坐标无关（大坐标不损失精度）
    ox, oy = footprint.bounds[:2] if not footprint.is_empty else (0.0, 0.0)
    local = lambda g: None if g is None else affinity.translate(g, -ox, -oy)
    measured = local(footprint)
    regions = [e for e in edits if is_region(e)]
    devices = [e for e in edits if e["op"] in DEVICE_OPS]

    # 1. 设备改判
    if geoms is not None:
        classes, targets, unmatched = device_classes(rows, devices)
        classes = {h: v for h, v in classes.items() if h in geoms}
    else:
        classes, targets, unmatched = {}, {}, []
    dev = {h: local(geoms[h]) for h in classes} if classes else {}
    base = measured
    if classes:
        removed = unary_union([dev[h] for h, (c, _) in classes.items() if c != EQUIPMENT])
        if not removed.is_empty:
            still = [local(geoms[r["handle"]]) for r in rows
                     if r["handle"] not in classes and r["cls"] == EQUIPMENT and r["handle"] in geoms]
            removed = removed.difference(unary_union([g for g in still if g.intersects(removed)]))
        base = measured.difference(removed).union(unary_union([dev[h] for h, (c, _) in classes.items() if c == EQUIPMENT]))
    gone, gained = measured.difference(base), base.difference(measured)

    # 2. 画的区域（在改判后的轮廓上）
    shapes = [(e, local(polygon(e["points"]))) for e in regions]
    adds = unary_union([g for e, g in shapes if g is not None and e["op"] == ADD])
    cuts = unary_union([g for e, g in shapes if g is not None and e["op"] == CUT])
    covered = base.union(adds)
    adjusted = covered.difference(cuts)
    new = adds.difference(base)
    out = dict(measured_m2=measured.area * to_m2,
               device_removed_m2=gone.area * to_m2,
               device_added_m2=gained.area * to_m2,
               added_m2=new.area * to_m2,
               cut_m2=cuts.intersection(covered).area * to_m2,
               adjusted_m2=adjusted.area * to_m2,
               blocks_m2=base.difference(cuts).area * to_m2,
               manual_add_m2=new.difference(cuts).area * to_m2,
               n_add=sum(e["op"] == ADD for e, _ in shapes), n_cut=sum(e["op"] == CUT for e, _ in shapes),
               n_exclude=sum(e["op"] == EXCLUDE for e in devices), n_include=sum(e["op"] == INCLUDE for e in devices),
               edits=[], classes=classes, kept={})
    for e, g in shapes:
        effect = 0.0 if g is None else (g.difference(base).difference(cuts) if e["op"] == ADD
                                        else g.intersection(covered)).area * to_m2
        out["edits"].append(dict({k: v for k, v in e.items() if k != "points"},
                                 area_m2=(g.area * to_m2) if g is not None else 0.0, effect_m2=effect))
    for e in devices:
        own = [h for h, (_, eid) in classes.items() if eid == e["id"]]
        changed = unary_union([dev[h] for h in own])
        effect = changed.intersection(gone if e["op"] == EXCLUDE else gained).area * to_m2 if own else 0.0
        status = "no_geometry" if geoms is None else "unmatched" if e["id"] in unmatched else "applied"
        out["edits"].append(dict(e, n_devices=len(own), effect_m2=effect, status=status,
                                 n_targets=len(targets.get(e["id"], []))))

    # 3. 被剪到的设备剩下的比例（按改判后的类别；有精确轮廓用精确轮廓）
    if not cuts.is_empty:
        for r in rows:
            if effective_class(r, classes) != EQUIPMENT:
                continue
            if geoms is not None and r["handle"] in geoms:
                g = local(geoms[r["handle"]])
            elif canvas and r.get("outline_px"):
                g = local(outline_world(r, canvas))
            else:
                continue
            if g is None or g.area == 0 or not g.intersects(cuts):
                continue
            out["kept"][r["handle"]] = g.difference(cuts).area / g.area
    return out
