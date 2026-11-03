#!/usr/bin/env python3
"""
DXF 产线布局 -> 设备投影面积（按图块计算）。

与 PDF 方法（estimate_footprint.py）同一面积口径、同一组形态学参数，但直接读 CAD：
  1. 单位：$INSUNITS；未声明（0）时必须在配置中给出 dxf_unit_mm 及依据，否则不出面积。
  2. 每个模型空间的图块插入（INSERT）逐个分类：设备 / 排除（图层、空图块、背景框、线外）/ 待定。
  3. 每个设备图块在自身坐标里求一次外轮廓：曲线离散 -> 栅格化 -> 闭合 < gap_close_mm 的缺口
     -> 填充 < max_hole_fill_m2 的内部 -> 开运算去掉 < min_feature_mm 的细线 -> 轮廓多边形。
     同一图块的多次插入共用结果，再按各自的变换（平移、旋转、镜像、缩放、拉伸方向）放到图上。
  4. 所有设备外轮廓取并集（重叠只算一次；设备之间围出的空地不填），去掉 < min_group_m2 的碎片 = 投影面积。
  5. 基准（总长 × 总宽）按线体计算：与产线长边平行、贯穿整张图、宽 ≥ lane_gap_mm 的空走道把设备分成几条线体，
     每条线体取 ≥ envelope_min_group_m2 的设备块的外包矩形，再相加（不含线体之间的走道）。
  6. 输出与 estimate_footprint.py 相同格式的结果（面积、线体外包尺寸、叠加图），另附每个插入的明细与原因。

用法：
    python dxf_footprint.py config.yaml --out results            # 配置中 drawings 带 dxf: 的图纸
"""
import argparse
import hashlib
import json
import math
import os
import time

import cv2
import ezdxf
import numpy as np
from ezdxf import bbox as ezbbox
import yaml
from ezdxf import disassemble, path as ezpath, units as ezunits
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage as ndi
from shapely import affinity
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union

# 不参与轮廓的实体：文字、标注、引线、图像等注释信息；无限长的构造线
ANNOTATION_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "ARC_DIMENSION", "LARGE_RADIAL_DIMENSION",
                    "LEADER", "MULTILEADER", "MLEADER", "TOLERANCE", "ACAD_TABLE", "POINT", "IMAGE", "WIPEOUT",
                    "OLE2FRAME", "VIEWPORT", "XLINE", "RAY"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 1. 单位
class UnitsUnknown(ValueError):
    pass


def unit_mm(doc, drawing):
    """每个 DXF 单位等于多少 mm，以及依据"""
    code = doc.header.get("$INSUNITS", 0)
    if code:
        return ezunits.conversion_factor(code, ezunits.MM), f"$INSUNITS={code}（{ezunits.decode(code)}）"
    if drawing.get("dxf_unit_mm"):
        return float(drawing["dxf_unit_mm"]), "配置 dxf_unit_mm：" + drawing.get("dxf_unit_basis", "未写依据")
    raise UnitsUnknown("DXF 未声明单位（$INSUNITS=0）：请在配置中给出 dxf_unit_mm 及依据（例如与图上总尺寸核对）")


# ---------------------------------------------------------------- 2. 图块几何
class Layers:
    """图层可见性与颜色（AutoCAD 不打印：关闭、冻结、Defpoints、不打印图层）"""

    def __init__(self, doc, exclude):
        self.colour, self.hidden = {}, set(exclude)
        for layer in doc.layers:
            name = layer.dxf.name
            self.colour[name] = abs(layer.dxf.color)
            if layer.is_off() or layer.is_frozen() or not layer.dxf.get("plot", 1) or name.lower() == "defpoints":
                self.hidden.add(name)

    def effective_colour(self, e, parent_colour):
        c = e.dxf.get("color", 256)
        if c == 256:
            return self.colour.get(e.dxf.layer, 7)
        if c == 0:
            return parent_colour
        return c


def flatten_block(doc, name, layers, cfg, insert_colour=256, insert_layer="0"):
    """图块（含嵌套）在图块坐标中的折线列表 [np.ndarray (n, 2)]，以及跳过的实体统计"""
    tol = cfg["curve_tolerance_mm"] / cfg["_unit_mm"]
    exclude_colours = set(cfg["exclude_colours"])
    parent_colour = layers.colour.get(insert_layer, 7) if insert_colour == 256 else insert_colour
    polys, skipped = [], {}
    for e in disassemble.recursive_decompose(doc.blocks.get(name)):
        t = e.dxftype()
        layer = e.dxf.get("layer", "0")
        if layer == "0":                    # 图块内 0 层的实体随插入所在图层
            layer = insert_layer
        why = ("annotation" if t in ANNOTATION_TYPES else "layer" if layer in layers.hidden
               else "colour" if layers.effective_colour(e, parent_colour) in exclude_colours else None)
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        try:
            ps = ezpath.from_hatch(e) if t == "HATCH" else [ezpath.make_path(e)]
        except (TypeError, ValueError, AttributeError):
            skipped["unsupported:" + t] = skipped.get("unsupported:" + t, 0) + 1
            continue
        for p in ps:
            for sp in p.sub_paths():
                pts = np.array([(v.x, v.y) for v in sp.flattening(distance=tol)], dtype=np.float64)
                if len(pts) >= 2:
                    polys.append(pts)
    return polys, skipped


def footprint_polygon(polys, unit, cfg, ex):
    """折线 -> 图块外轮廓（图块坐标）。与 estimate_footprint.measure() 第 5 步同一组参数。
    跨度超过 max_object_mm 的（图块里混有远处的零散图形）先按邻近分组，各组分别求轮廓再合并。"""
    if not polys:
        return None, {}
    allp = np.concatenate(polys)
    x0, y0 = allp.min(0)
    x1, y1 = allp.max(0)
    if max(x1 - x0, y1 - y0) * unit > cfg["max_object_mm"] and len(polys) > 1:
        groups = spatial_groups(polys, cfg["cluster_mm"] / unit)
        if len(groups) > 1:
            parts = [footprint_polygon(gr, unit, cfg, ex) for gr in groups]
            geoms = [g for g, _ in parts if g is not None]
            warn = [w for _, info in parts for w in info.get("warnings", [])]
            return (unary_union(geoms) if geoms else None), dict(split_groups=len(groups), warnings=warn)
    r = cfg["resolution_mm"] / unit                       # 每像素多少图纸单位
    warnings = []
    px = (x1 - x0) / r * (y1 - y0) / r
    if px > cfg["max_raster_px"]:                         # 防止超大栅格：降低分辨率并记录
        r *= math.sqrt(px / cfg["max_raster_px"])
        warnings.append(f"对象 {round((x1 - x0) * unit / 1000)}×{round((y1 - y0) * unit / 1000)} m 过大，"
                        f"分辨率降为 {round(r * unit)} mm/像素")
    pad = int(math.ceil(ex["gap_close_mm"] / (r * unit))) + 2
    W, H = int((x1 - x0) / r) + 2 * pad + 1, int((y1 - y0) / r) + 2 * pad + 1
    m = np.zeros((H, W), np.uint8)
    shift = 4                                             # 亚像素精度 1/16
    for p in polys:
        q = np.round(((p - (x0, y0)) / r + pad) * (1 << shift)).astype(np.int32)
        cv2.polylines(m, [q.reshape(-1, 1, 2)], False, 1, 1, cv2.LINE_8, shift)
    mm_px = r * unit
    g = max(1, int(round(ex["gap_close_mm"] / mm_px)))
    closed = ndi.binary_closing(ndi.binary_dilation(m > 0), structure=np.ones((g, g)))
    holes = ndi.binary_fill_holes(closed) & ~closed
    hl, n = ndi.label(holes)
    if n:
        sizes = ndi.sum(holes, hl, range(1, n + 1)) * (mm_px ** 2) / 1e6
        small = np.zeros(n + 1, bool)
        small[1:] = sizes < ex["max_hole_fill_m2"]
        closed |= small[hl]
    k = max(1, int(round(ex["min_feature_mm"] / mm_px)))
    filled = cv2.morphologyEx(closed.astype(np.uint8), cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    # PDF 方法开始时把线条加粗了 1 像素（连通斜线），这里收回，使轮廓落在 CAD 线上而不是线外 1 像素
    filled = ndi.binary_erosion(filled, border_value=0).astype(np.uint8)
    contours, hier = cv2.findContours(filled, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    parts = []
    for i, c in enumerate(contours):
        if hier[0][i][3] != -1 or len(c) < 3:
            continue
        holes_ = [contours[j][:, 0, :] for j in range(len(contours)) if hier[0][j][3] == i and len(contours[j]) >= 3]
        poly = Polygon(c[:, 0, :], holes_).buffer(0)
        if not poly.is_empty:
            parts.append(poly)
    if not parts:
        return None, dict(raster_px=[W, H], warnings=warnings)
    geom = unary_union(parts)
    # 像素坐标 -> 图块坐标（像素中心；y 轴两者同向，因为栅格行号随 y 增大）
    geom = affinity.affine_transform(geom, [r, 0, 0, r, x0 - pad * r, y0 - pad * r])
    return geom, dict(raster_px=[W, H], warnings=warnings)


def insert_affine(ins):
    """INSERT 的平面仿射变换（图块坐标 -> 世界坐标 XY）：含基点、旋转、镜像、非等比缩放、拉伸方向"""
    m = ins.matrix44()
    o, ux, uy = m.transform((0, 0, 0)), m.transform_direction((1, 0, 0)), m.transform_direction((0, 1, 0))
    return [ux.x, uy.x, ux.y, uy.y, o.x, o.y]


# ---------------------------------------------------------------- 3. 分类
BARE_REASON = "只有外框、没有内部细节的图形（区域、预留位或简化画法？），待确认"


def classify(inserts, boxes, extents, cfg, overrides, n_polylines):
    """每个插入 -> (类别, 原因)。类别：equipment（固定设备，计入）/ mobile（移动设备，单列）/
    unresolved（待定，单列）/ excluded（不计）。人工分类（overrides）优先。
    boxes：设备轮廓的外框（无轮廓为 None）；extents：图块全部 CAD 内容（含文字、黄线）的外框，用于找背景框与线外图块"""
    out = {}
    live = [i for i in range(len(inserts)) if extents[i] is not None]
    centres = {i: ((extents[i][0] + extents[i][2]) / 2, (extents[i][1] + extents[i][3]) / 2) for i in live}
    # 背景框 = 线体/厂房背景，不是设备：外框包含一半以上（且至少 3 个）其他图块的中心，
    # 而且框内基本是空的（自身设备轮廓 ≤ 外框面积的 20%）
    frame = None
    for i in live:
        x0, y0, x1, y1 = extents[i]
        own = (boxes[i][2] - boxes[i][0]) * (boxes[i][3] - boxes[i][1]) if boxes[i] is not None else 0.0
        if own > 0.2 * (x1 - x0) * (y1 - y0):
            continue
        inside = sum(1 for j in live if j != i and x0 <= centres[j][0] <= x1 and y0 <= centres[j][1] <= y1)
        if inside >= max(3, 0.5 * (len(live) - 1)) and (frame is None or inside > frame[1]):
            frame = (i, inside)
    for i, ins in enumerate(inserts):
        name, layer = ins.dxf.name, ins.dxf.layer
        key = next((k for k in (ins.dxf.handle, name) if k in overrides), None)
        if key is not None:
            out[i] = (overrides[key]["class"], overrides[key]["reason"])
        elif extents[i] is None:
            out[i] = ("excluded", "空图块")
        elif layer in cfg["_hidden_layers"]:
            out[i] = ("excluded", f"图层「{layer}」不计入（配置 exclude_layers 或不打印）")
        elif frame and i == frame[0]:
            out[i] = ("excluded", "背景框：外框包含大部分其他图块（线体/厂房范围）")
        elif boxes[i] is None:
            out[i] = ("excluded", "没有设备轮廓（只有注释、黄线或细线）")
        elif n_polylines[i] <= cfg["bare_outline_max_polylines"]:
            out[i] = ("unresolved", BARE_REASON)
        else:
            out[i] = ("equipment", "设备图块")
    return out, (frame[0] if frame else None)


def line_region(items, frame_box, gap):
    """产线范围：有背景框用背景框；否则从最大的设备开始，把相距 < gap 的设备逐个并入（线外的孤立图形不计）。
    items：[(外框, 面积)]。返回 (x0, y0, x1, y1) 与依据。"""
    if frame_box is not None:
        return frame_box, "背景框"
    if not items:
        return None, "无设备"
    order = sorted(range(len(items)), key=lambda i: -items[i][1])
    x0, y0, x1, y1 = items[order[0]][0]
    pending, grown = set(order[1:]), True
    while grown:
        grown = False
        for i in list(pending):
            b = items[i][0]
            if b[0] <= x1 + gap and b[2] >= x0 - gap and b[1] <= y1 + gap and b[3] >= y0 - gap:
                x0, y0, x1, y1 = min(x0, b[0]), min(y0, b[1]), max(x1, b[2]), max(y1, b[3])
                pending.discard(i)
                grown = True
    return (x0, y0, x1, y1), f"从最大设备起相距 < {gap:g} 单位的设备连成的范围"


def inside(box, region):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return region[0] <= cx <= region[2] and region[1] <= cy <= region[3]


def spatial_groups(polys, g):
    """折线按中心落入边长 g 的网格，8 邻接的格子连成一组"""
    cells = {}
    for i, p in enumerate(polys):
        c = p.mean(0)
        cells.setdefault((int(c[0] // g), int(c[1] // g)), []).append(i)
    comp, n = {}, 0
    for start in cells:
        if start in comp:
            continue
        stack = [start]; comp[start] = n
        while stack:
            cx, cy = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb = (cx + dx, cy + dy)
                    if nb in cells and nb not in comp:
                        comp[nb] = n; stack.append(nb)
        n += 1
    groups = [[] for _ in range(n)]
    for cell, idx in cells.items():
        groups[comp[cell]].extend(polys[i] for i in idx)
    return groups


# ---------------------------------------------------------------- 3b. 打散在模型空间里的设备线条
def loose_items(msp, layers, cfg, ex, unit):
    """不在图块中的线条（打散的设备）。排除注释、不打印/排除图层、黄色、绑定外部参照图层（建筑底图，名称含 $0$）、
    单根长于 max_path_extent_mm 的线（墙、轴网、尺寸线）；其余按邻近聚成若干团，每团与图块同样求外轮廓。"""
    tol = cfg["curve_tolerance_mm"] / unit
    max_len = ex["max_path_extent_mm"] / unit
    exclude_colours = set(cfg["exclude_colours"])
    polys, counts = [], {}

    def count(k):
        counts[k] = counts.get(k, 0) + 1

    for e in msp:
        t = e.dxftype()
        if t == "INSERT":
            continue
        layer = e.dxf.get("layer", "0")
        kind = ("annotation" if t in ANNOTATION_TYPES else "hidden layer" if layer in layers.hidden
                else "bound xref (building base plan)" if cfg["exclude_bound_xref_layers"] and "$0$" in layer
                else "yellow" if layers.effective_colour(e, 7) in exclude_colours else None)
        if kind:
            count(kind); continue
        try:
            ps = ezpath.from_hatch(e) if t == "HATCH" else [ezpath.make_path(e)]
        except (TypeError, ValueError, AttributeError):
            count("unsupported:" + t); continue
        for p in ps:
            for sp in p.sub_paths():
                pts = np.array([(v.x, v.y) for v in sp.flattening(distance=tol)], dtype=np.float64)
                if len(pts) < 2:
                    continue
                if max(np.ptp(pts[:, 0]), np.ptp(pts[:, 1])) > max_len:
                    count("long line (wall/grid/dimension)"); continue
                polys.append(pts)
                count("equipment lines")
    if not polys:
        return [], counts
    groups = spatial_groups(polys, cfg["cluster_mm"] / unit)
    items = []
    for k, grp in enumerate(groups):
        geom, info = footprint_polygon(grp, unit, cfg, ex)
        if geom is not None and not geom.is_empty:
            items.append(dict(id=f"loose-{k + 1}", geom=geom, polylines=grp, info=info))
    return items, counts


# ---------------------------------------------------------------- 4. 主流程
def measure(drawing, conf, out_dir):
    ex, cfg = conf["extraction"], dict(conf["dxf"])
    t0 = time.time()
    file_sha = sha256(drawing["dxf"])
    doc = ezdxf.readfile(drawing["dxf"])
    load_s = time.time() - t0
    unit, unit_basis = unit_mm(doc, drawing)
    cfg["_unit_mm"] = unit
    layers = Layers(doc, cfg["exclude_layers"])
    cfg["_hidden_layers"] = layers.hidden
    overrides = {}
    if drawing.get("overrides"):
        if drawing.get("sha256") == file_sha:            # 人工分类只对核对过的同一文件有效
            overrides = drawing["overrides"]
        else:
            print(f"[{drawing['id']}] 警告：文件内容与配置中的 sha256 不同，人工分类未使用")
    msp = doc.modelspace()
    inserts = list(msp.query("INSERT"))

    # 图块外轮廓：每个（图块, 颜色继承, 图层继承）只算一次
    cache, block_info = {}, {}
    local, boxes = [], []
    for ins in inserts:
        key = (ins.dxf.name, ins.dxf.get("color", 256), ins.dxf.layer)
        if key not in cache:
            polys, skipped = flatten_block(doc, ins.dxf.name, layers, cfg, key[1], key[2])
            geom, info = footprint_polygon(polys, unit, cfg, ex)
            cache[key] = (geom, polys)
            block_info[ins.dxf.name] = dict(entities=len(doc.blocks.get(ins.dxf.name)), polylines=len(polys),
                                            skipped=skipped, **info)
        geom, _ = cache[key]
        g = affinity.affine_transform(geom, insert_affine(ins)) if geom is not None else None
        local.append(g)
        boxes.append(g.bounds if g is not None else None)
    bcache = ezbbox.Cache()
    extents = []
    for ins in inserts:
        e = ezbbox.extents([ins], cache=bcache)
        extents.append((e.extmin.x, e.extmin.y, e.extmax.x, e.extmax.y) if e.has_data else None)
    n_polylines = [len(cache[(ins.dxf.name, ins.dxf.get("color", 256), ins.dxf.layer)][1]) for ins in inserts]
    classes, frame = classify(inserts, boxes, extents, cfg, overrides, n_polylines)
    loose_list, loose_counts = loose_items(msp, layers, cfg, ex, unit)
    cand = [(boxes[i], local[i].area) for i in range(len(inserts)) if classes[i][0] != "excluded" and boxes[i] is not None]
    cand += [(it["geom"].bounds, it["geom"].area) for it in loose_list]
    region, region_basis = line_region(cand, extents[frame] if frame is not None else None, cfg["offline_gap_mm"] / unit)
    for i in range(len(inserts)):
        if region is not None and classes[i][0] != "excluded" and i != frame and extents[i] is not None \
                and not inside(extents[i], region):
            classes[i] = ("excluded", f"在产线范围（{region_basis}）之外")
    for it in loose_list:
        it["cls"], it["reason"] = (("excluded", f"在产线范围（{region_basis}）之外")
                                   if region is not None and not inside(it["geom"].bounds, region)
                                   else ("unresolved", BARE_REASON) if len(it["polylines"]) <= cfg["bare_outline_max_polylines"]
                                   else ("equipment", "打散在模型空间的设备线条"))
    # 自检：按本程序变换放到图上的轮廓，必须落在 ezdxf 独立算出的图块范围内（检查旋转、镜像、拉伸方向）
    tol = 2 * cfg["resolution_mm"] / unit
    transform_failures = [inserts[i].dxf.handle for i in range(len(inserts))
                          if boxes[i] is not None and extents[i] is not None and not (
                              boxes[i][0] >= extents[i][0] - tol and boxes[i][1] >= extents[i][1] - tol
                              and boxes[i][2] <= extents[i][2] + tol and boxes[i][3] <= extents[i][3] + tol)]

    to_m2 = (unit / 1000) ** 2
    rows, equip = [], []
    for i, ins in enumerate(inserts):
        cls, why = classes[i]
        g = local[i]
        rows.append(dict(handle=ins.dxf.handle, block=ins.dxf.name, layer=ins.dxf.layer, cls=cls, reason=why,
                         insert=[round(v, 1) for v in ins.dxf.insert.xyz[:2]], rotation=round(ins.dxf.rotation, 3),
                         scale=[round(ins.dxf.xscale, 4), round(ins.dxf.yscale, 4)],
                         extrusion=[round(v, 3) for v in ins.dxf.extrusion.xyz],
                         area_m2=round(g.area * to_m2, 3) if g is not None else 0.0))
        if cls == "equipment" and g is not None:
            equip.append(g)
    for it in loose_list:
        rows.append(dict(handle=it["id"], block=None, layer=None, cls=it["cls"], reason=it["reason"],
                         insert=None, rotation=None, scale=None, extrusion=None,
                         area_m2=round(it["geom"].area * to_m2, 3), polylines=len(it["polylines"])))
        if it["cls"] == "equipment":
            equip.append(it["geom"])
    union = unary_union(equip) if equip else Polygon()
    parts = list(union.geoms) if isinstance(union, MultiPolygon) else ([union] if not union.is_empty else [])
    kept = [p for p in parts if p.area * to_m2 >= ex["min_group_m2"]]
    dropped = [p for p in parts if p.area * to_m2 < ex["min_group_m2"]]
    footprint = unary_union(kept) if kept else Polygon()
    total = round(footprint.area * to_m2, 1)
    unresolved = [r for r in rows if r["cls"] == "unresolved"]

    def outside_fixed(cls):
        """移动设备、待定项不并入主结果：只报告它们落在固定设备轮廓之外的面积"""
        g = unary_union([local[i] for i, r in enumerate(rows[:len(inserts)]) if r["cls"] == cls and local[i] is not None]
                        + [it["geom"] for it in loose_list if it["cls"] == cls])
        return round(g.difference(footprint).area * to_m2, 2) if not g.is_empty else 0.0

    unresolved_m2, mobile_m2 = outside_fixed("unresolved"), outside_fixed("mobile")

    # 基准：按线体分别取设备外包矩形（总长 × 总宽）再相加
    envelope = line_envelope(kept, unit, ex["envelope_min_group_m2"], cfg["lane_gap_mm"])
    duplicated = sum(r["area_m2"] for r in rows if r["cls"] == "equipment") - round(union.area * to_m2, 3)

    os.makedirs(out_dir, exist_ok=True)
    ov_path = os.path.join(out_dir, f"{drawing['id']}_overlay.png")
    fp_path = os.path.join(out_dir, f"{drawing['id']}_footprint.wkt")   # 计入的设备轮廓（图纸单位，世界坐标），供核对
    with open(fp_path, "w", encoding="utf-8") as f:
        f.write(footprint.wkt)
    shown = [b for b, r in zip(boxes, rows) if b is not None and r["cls"] != "excluded"]
    shown += [it["geom"].bounds for it in loose_list if it["cls"] != "excluded"]
    view = ((min(b[0] for b in shown), min(b[1] for b in shown), max(b[2] for b in shown), max(b[3] for b in shown))
            if shown else region)
    draw_overlay(ov_path, inserts, rows, cache, footprint, frame, [b or e for b, e in zip(boxes, extents)], loose_list,
                 region, view, [l_["box"] for l_ in envelope["lanes"]])
    groups = sorted((dict(footprint_m2=round(p.area * to_m2, 2),
                          length_mm=round(max(p.bounds[2] - p.bounds[0], p.bounds[3] - p.bounds[1]) * unit),
                          width_mm=round(min(p.bounds[2] - p.bounds[0], p.bounds[3] - p.bounds[1]) * unit))
                     for p in kept), key=lambda g_: -g_["footprint_m2"])
    if transform_failures:
        print(f"[{drawing['id']}] 警告：{len(transform_failures)} 个图块的变换自检未通过：{transform_failures[:10]}")
    print(f"[{drawing['id']}] DXF {unit_basis}；设备插入 {sum(r['cls'] == 'equipment' for r in rows)} 个，"
          f"投影面积 {total} m²（读取 {load_s:.0f} s，共 {time.time() - t0:.0f} s）", flush=True)
    return dict(id=drawing["id"], name=drawing.get("name", drawing["id"]), source="dxf",
                dxf=os.path.basename(drawing["dxf"]), sha256=file_sha, dxf_version=doc.dxfversion,
                unit_mm=unit, unit_basis=unit_basis, reference_length_mm=None, mm_per_pt=None,
                footprint_m2=total, n_groups=len(kept), groups=groups, envelope=envelope,
                footprint_vs_envelope=round(total / envelope["area_m2"], 3) if envelope["area_m2"] else None,
                overlap_m2=round(duplicated, 2), fragments_dropped_m2=round(sum(p.area for p in dropped) * to_m2, 3),
                perimeter_m=round(footprint.length * unit / 1000, 1), transform_check_failures=transform_failures,
                line_region=dict(box=[round(v, 1) for v in region] if region else None, basis=region_basis),
                warnings=[w for b in block_info.values() for w in b.get("warnings", [])]
                + [w for it in loose_list for w in it["info"].get("warnings", [])],
                loose=dict(entities=loose_counts, clusters=len(loose_list),
                           equipment_clusters=sum(it["cls"] == "equipment" for it in loose_list),
                           equipment_m2=round(unary_union([it["geom"] for it in loose_list if it["cls"] == "equipment"]).area * to_m2, 2)
                           if loose_list else 0.0),
                mobile_m2=mobile_m2, unresolved_m2=unresolved_m2, unresolved=[dict(block=r["block"], handle=r["handle"], reason=r["reason"])
                                                          for r in unresolved],
                stations=[], station_sum_m2=0.0, inserts=rows, blocks=block_info,
                overlay=os.path.basename(ov_path), station_overlay=None, footprint_wkt=os.path.basename(fp_path),
                seconds=dict(load=round(load_s, 1), total=round(time.time() - t0, 1)))


# ---------------------------------------------------------------- 4b. 线体基准
def line_envelope(parts, unit, min_box_m2, gap_mm):
    """基准“总长 × 总宽”，按线体分开。parts：计入的设备块（图纸单位、世界坐标），unit：每单位 mm。

    线体之间的分隔 = 与产线长边平行、贯穿整张图、宽 ≥ gap_mm 的空走道（PDF 方法中的填充分隔带同理）：
    把 ≥ min_box_m2 的设备块投影到横向（与长边垂直）上，相邻投影区间相距 ≥ gap_mm 处分开。只比较坐标差，
    与图纸在世界坐标中的位置无关。更小的设备块按横向中心归入所在（或最近的）线体，只计入面积，不扩大外包。
    返回 dict(lanes, area_m2, along, gap_mm)；每条线体带 box（图纸单位）供叠加图与核对。"""
    to_m2 = (unit / 1000) ** 2
    if not parts:
        return dict(lanes=[], area_m2=0.0, along=None, gap_mm=gap_mm)
    big = [p for p in parts if p.area * to_m2 >= min_box_m2] or parts
    x0, y0 = min(p.bounds[0] for p in big), min(p.bounds[1] for p in big)
    x1, y1 = max(p.bounds[2] for p in big), max(p.bounds[3] for p in big)
    along = "x" if (x1 - x0) >= (y1 - y0) else "y"
    lo_k, hi_k = (1, 3) if along == "x" else (0, 2)            # 横向坐标在 bounds 中的位置
    gap = gap_mm / unit
    groups, reach = [], None
    for p in sorted(big, key=lambda p_: p_.bounds[lo_k]):
        if groups and p.bounds[lo_k] - reach < gap:
            groups[-1].append(p)
            reach = max(reach, p.bounds[hi_k])
        else:
            groups.append([p])
            reach = p.bounds[hi_k]
    bands = [(min(p.bounds[lo_k] for p in g), max(p.bounds[hi_k] for p in g)) for g in groups]
    area = [0.0] * len(groups)
    for p in parts:                                           # 全部计入的设备块（含小块）按横向中心归入线体
        c = (p.bounds[lo_k] + p.bounds[hi_k]) / 2
        k = min(range(len(bands)), key=lambda i: 0 if bands[i][0] <= c <= bands[i][1]
                else min(abs(c - bands[i][0]), abs(c - bands[i][1])))
        area[k] += p.area * to_m2
    lanes = []
    # 线体编号：沿 x 的产线从上到下，沿 y 的产线从左到右（与叠加图中看到的顺序一致）
    order = sorted(range(len(groups)), key=lambda i: -bands[i][1] if along == "x" else bands[i][0])
    for n, i in enumerate(order, 1):
        g = groups[i]
        bx = (min(p.bounds[0] for p in g), min(p.bounds[1] for p in g), max(p.bounds[2] for p in g), max(p.bounds[3] for p in g))
        a_, b_ = (bx[2] - bx[0]) * unit / 1000, (bx[3] - bx[1]) * unit / 1000
        L, Wd = (a_, b_) if along == "x" else (b_, a_)
        lanes.append(dict(lane=n, length_m=round(L, 1), width_m=round(Wd, 1), area_m2=round(L * Wd, 1),
                          footprint_m2=round(area[i], 1), box=[round(v, 1) for v in bx]))
    return dict(lanes=lanes, area_m2=round(sum(l_["area_m2"] for l_ in lanes), 1), along=along, gap_mm=gap_mm)


# ---------------------------------------------------------------- 5. 叠加图
CLASS_COLOURS = {"equipment": (40, 160, 70), "excluded": (214, 39, 40), "unresolved": (230, 140, 0),
                 "mobile": (148, 103, 189)}
LANE_COLOUR = (0, 150, 170)


def draw_overlay(path, inserts, rows, cache, footprint, frame, boxes, loose_list, region, view, lane_boxes=(),
                 max_px=4000):
    """灰 = CAD 线条；绿 = 计入的设备轮廓；红框 = 排除（按图层排除的不画）；橙框 = 待定；紫框 = 移动设备；
    蓝粗框 = 产线范围；青色框 + 编号 = 各线体的外包矩形（基准“总长 × 总宽”）。画面范围 = 计入、移动、待定的图形"""
    if view is None:
        Image.new("RGB", (400, 200), "white").save(path); return
    m_ = 0.03 * max(view[2] - view[0], view[3] - view[1])
    X0, Y0, X1, Y1 = view[0] - m_, view[1] - m_, view[2] + m_, view[3] + m_
    margin = 0.02 * max(X1 - X0, Y1 - Y0)
    X0, Y0, X1, Y1 = X0 - margin, Y0 - margin, X1 + margin, Y1 + margin
    s = max_px / max(X1 - X0, Y1 - Y0)
    W, H = int((X1 - X0) * s) + 1, int((Y1 - Y0) * s) + 1
    tx = lambda pts: np.column_stack(((pts[:, 0] - X0) * s, (Y1 - pts[:, 1]) * s))
    im = Image.new("RGB", (W, H), "white")
    fill = Image.new("L", (W, H), 0)
    fd = ImageDraw.Draw(fill)
    for poly in (footprint.geoms if isinstance(footprint, MultiPolygon) else [footprint]):
        if poly.is_empty:
            continue
        fd.polygon([tuple(p) for p in tx(np.asarray(poly.exterior.coords))], fill=255)
        for hole in poly.interiors:
            fd.polygon([tuple(p) for p in tx(np.asarray(hole.coords))], fill=0)
    im.paste(Image.new("RGB", (W, H), (150, 220, 160)), (0, 0), fill)
    arr = np.asarray(im).copy()
    for i, ins in enumerate(inserts):
        key = (ins.dxf.name, ins.dxf.get("color", 256), ins.dxf.layer)
        polys = cache[key][1]
        if not polys or boxes[i] is None:
            continue
        b = boxes[i]
        if b[2] < X0 or b[0] > X1 or b[3] < Y0 or b[1] > Y1:
            continue
        a, bb, d, e, xo, yo = insert_affine(ins)
        for p in polys:
            w = np.column_stack((a * p[:, 0] + bb * p[:, 1] + xo, d * p[:, 0] + e * p[:, 1] + yo))
            cv2.polylines(arr, [np.round(tx(w)).astype(np.int32).reshape(-1, 1, 2)], False, (120, 120, 120), 1)
    for it in loose_list:
        b = it["geom"].bounds
        if b[2] < X0 or b[0] > X1 or b[3] < Y0 or b[1] > Y1:
            continue
        for p in it["polylines"]:
            cv2.polylines(arr, [np.round(tx(p)).astype(np.int32).reshape(-1, 1, 2)], False, (120, 120, 120), 1)
    im = Image.fromarray(arr)
    d = ImageDraw.Draw(im)
    p0, p1 = tx(np.array([[region[0], region[3]], [region[2], region[1]]]))
    d.rectangle([*p0, *p1], outline=(31, 119, 180), width=3)
    for i, r in enumerate(rows[:len(inserts)]):
        if boxes[i] is None:
            continue
        b = boxes[i]
        if b[2] < X0 or b[0] > X1 or b[3] < Y0 or b[1] > Y1:
            continue
        p0, p1 = tx(np.array([[b[0], b[3]], [b[2], b[1]]]))
        if r["cls"] != "equipment" and i != frame and not r["reason"].startswith("图层"):
            d.rectangle([*p0, *p1], outline=CLASS_COLOURS[r["cls"]], width=2)
    font = ImageFont.load_default(size=max(14, max_px // 80))
    for n, b in enumerate(lane_boxes, 1):
        p0, p1 = tx(np.array([[b[0], b[3]], [b[2], b[1]]]))
        d.rectangle([*p0, *p1], outline=LANE_COLOUR, width=3)
        d.text((p0[0] + 6, p0[1] + 4), f"L{n}", fill=LANE_COLOUR, font=font)
    im.save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--out", default="results")
    a = ap.parse_args()
    with open(a.config, encoding="utf-8") as f:
        conf = yaml.safe_load(f)
    base = os.path.dirname(os.path.abspath(a.config))
    res = []
    for d in (d for d in conf["drawings"] if d.get("dxf")):
        d = dict(d, dxf=d["dxf"] if os.path.isabs(d["dxf"]) else os.path.join(base, d["dxf"]))
        res.append(measure(d, conf, a.out))
    with open(os.path.join(a.out, "results.json"), "w", encoding="utf-8") as f:
        json.dump(dict(extraction=conf["extraction"], dxf=conf["dxf"], drawings=res), f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
