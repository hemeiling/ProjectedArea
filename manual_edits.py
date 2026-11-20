"""
人工修正投影面积：在叠加图上画多边形，“加入”自动识别漏掉的设备区域，或“剪掉”多包进来的区域。

只用于 DXF（计入轮廓以矢量保存在 footprint.wkt）。修正区域以图纸坐标保存，绑定图纸文件的 SHA-256
（文件改版后不会自动套用）；测量面积保持不变，修正单独记录：

    修正后面积 = 测量面积 + 加入 − 剪掉
    加入 = 加入区域并集中原来没有计入的部分
    剪掉 = 剪掉区域并集与（测量轮廓 ∪ 加入区域）的交集；同一处既加又剪时以剪掉为准

修正后的面积再分成两部分，供按图块类型分摊包装系数：
    图块部分   = 测量轮廓 − 剪掉区域            （按各图块被剪后剩下的比例分摊给图块）
    人工加入部分 = (加入区域 − 测量轮廓) − 剪掉区域 （单独一组“人工加入区域”）
两部分之和 = 修正后面积。
"""
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.validation import make_valid

ADD, CUT = "add", "cut"
OPS = (ADD, CUT)
MIN_POINTS = 3
MAX_POINTS = 500
MAX_NOTE = 200


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
    """一个计入插入的外边界（outline_px）换回图纸坐标；只用于判断它被剪掉了多少"""
    polys = [polygon(to_world(ring, canvas)) for ring in row.get("outline_px") or []]
    polys = [p for p in polys if p is not None]
    return unary_union(polys) if polys else None


def apply(footprint, edits, unit_mm, rows=(), canvas=None):
    """footprint：测量轮廓（图纸坐标）；edits：生效的修正 [{id, op, points, ...}]（图纸坐标）。

    返回各项面积（m²，不取整）、每条修正单独的效果，以及被剪到的插入剩下的比例 kept {handle: 0–1}。
    """
    to_m2 = (unit_mm / 1000) ** 2
    # 在以测量轮廓左下角为原点的局部坐标中计算：结果与图纸的绝对坐标无关（大坐标不损失精度）
    ox, oy = footprint.bounds[:2] if not footprint.is_empty else (0.0, 0.0)
    local = lambda g: None if g is None else affinity.translate(g, -ox, -oy)
    footprint = local(footprint)
    shapes = [(e, local(polygon(e["points"]))) for e in edits]
    adds = unary_union([g for e, g in shapes if g is not None and e["op"] == ADD])
    cuts = unary_union([g for e, g in shapes if g is not None and e["op"] == CUT])
    covered = footprint.union(adds)
    adjusted = covered.difference(cuts)
    new = adds.difference(footprint)
    out = dict(measured_m2=footprint.area * to_m2,
               added_m2=new.area * to_m2,
               cut_m2=cuts.intersection(covered).area * to_m2,
               adjusted_m2=adjusted.area * to_m2,
               blocks_m2=footprint.difference(cuts).area * to_m2,
               manual_add_m2=new.difference(cuts).area * to_m2,
               n_add=sum(e["op"] == ADD for e, _ in shapes), n_cut=sum(e["op"] == CUT for e, _ in shapes),
               edits=[], kept={})
    for e, g in shapes:
        effect = 0.0 if g is None else (g.difference(footprint).difference(cuts) if e["op"] == ADD
                                        else g.intersection(covered)).area * to_m2
        out["edits"].append(dict({k: v for k, v in e.items() if k != "points"},
                                 area_m2=(g.area * to_m2) if g is not None else 0.0, effect_m2=effect))
    if not cuts.is_empty and canvas:
        for r in rows:
            if r.get("cls") != "equipment" or not r.get("outline_px"):
                continue
            g = local(outline_world(r, canvas))
            if g is None or not g.intersects(cuts):
                continue
            out["kept"][r["handle"]] = g.difference(cuts).area / g.area
    return out
