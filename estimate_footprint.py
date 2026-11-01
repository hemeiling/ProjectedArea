#!/usr/bin/env python3
"""
从 CAD 导出的矢量布局 PDF 估算设备投影面积（占地面积）。

用法:
    python estimate_footprint.py config.yaml [--out results]

所有阈值都在 config.yaml 中，代码中不针对具体图纸写死任何坐标或颜色。
每张图纸只需提供: PDF 路径 + 图上标注的一个总尺寸（mm）。
"""
import argparse, json, math, os, sys
import numpy as np
import yaml
import pymupdf
import cv2
import stations as stn
from scipy import ndimage as ndi
from PIL import Image, ImageDraw


# ---------------------------------------------------------------- 1. 读取矢量
def load_paths(pdf_path):
    """读取 PDF 第一页所有矢量路径 -> [(颜色, 外框, 折线列表)]"""
    page = pymupdf.open(pdf_path)[0]
    paths = []
    for d in page.get_drawings():
        col = d.get("color") or d.get("fill") or (0, 0, 0)
        col = tuple(round(c, 2) for c in col)
        polylines = []
        for it in d["items"]:
            k = it[0]
            if k == "l":
                polylines.append([it[1], it[2]])
            elif k == "c":                       # 贝塞尔曲线: 用起止点近似
                polylines.append([it[1], it[4]])
            elif k == "re":
                r = it[1]; polylines.append([r.tl, r.tr, r.br, r.bl, r.tl])
            elif k == "qu":
                q = it[1]; polylines.append([q.ul, q.ur, q.lr, q.ll, q.ul])
        if d["type"] == "fs":          # 文字字形（填充+描边）不是设备，单独处理
            continue
        paths.append((col, d["rect"], polylines))
    return page, paths


# ---------------------------------------------------------------- 2. 比例尺
def longest_line(paths, line_ratio=0.03):
    """图上最长的“线状”路径 -> (长度 pt, 外框)；没有则 (0, None)"""
    best, rect = 0.0, None
    for _, r, _ in paths:
        L, s = max(r.width, r.height), min(r.width, r.height)
        if L > 0 and s / L <= line_ratio and L > best:
            best, rect = L, r
    return best, rect


def detect_scale(paths, reference_length_mm, line_ratio=0.03):
    """图上最长的“线状”路径 = 标注的总尺寸线。返回 mm/pt 及该线长度(pt)。"""
    best, _ = longest_line(paths, line_ratio)
    if best == 0:
        raise ValueError("未找到尺寸线，请在配置中直接给出 mm_per_pt")
    return reference_length_mm / best, best


# ---------------------------------------------------------------- 3. 区域识别
def is_neutral_grey(col, lo, hi):
    return max(col) - min(col) < 0.02 and lo <= col[0] <= hi


def rasterize(paths, W, H, S, keep, width=1):
    im = Image.new("L", (W, H), 0); d = ImageDraw.Draw(im)
    for col, r, polys in paths:
        if not keep(col, r):
            continue
        for pl in polys:
            pts = [(p.x * S, p.y * S) for p in pl]
            d.line(pts, fill=255, width=width)
    return np.array(im) > 0


def building_box(paths, ref_pt, min_frac):
    """长线状路径（墙/柱网/边界，长度 ≥ min_frac×总尺寸）的外包框 = 厂房范围"""
    xs0, ys0, xs1, ys1 = [], [], [], []
    for _, r, _ in paths:
        if max(r.width, r.height) >= min_frac * ref_pt:
            xs0.append(r.x0); ys0.append(r.y0); xs1.append(r.x1); ys1.append(r.y1)
    return min(xs0), min(ys0), max(xs1), max(ys1)


def hatch_mask(paths, W, H, S, mm, cfg):
    """填充区 = 长斜线(约45°, 单根 ≥ min_segment_mm)成片的区域 = 墙体/非设备区。
    设备内部的斜线（弧线近似、斜撑）都很短，不会被误判。"""
    min_len = cfg["min_segment_mm"] / mm
    diag = Image.new("L", (W, H), 0); d = ImageDraw.Draw(diag)
    for _, _, polys in paths:
        for pl in polys:
            for a, b in zip(pl, pl[1:]):
                ang = abs(math.degrees(math.atan2(b.y - a.y, b.x - a.x))) % 180
                if abs(b - a) < min_len:
                    continue
                if cfg["angle_min"] <= ang <= cfg["angle_max"] or \
                   180 - cfg["angle_max"] <= ang <= 180 - cfg["angle_min"]:
                    d.line([(a.x * S, a.y * S), (b.x * S, b.y * S)], fill=255, width=1)
    m = np.array(diag) > 0
    k = max(1, int(cfg["close_mm"] / mm * S))
    m = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((k, k), np.uint8)) > 0
    lab, n = ndi.label(m)
    px_m2 = (mm / S) ** 2 / 1e6
    sizes = ndi.sum(m, lab, range(1, n + 1)) * px_m2
    return np.isin(lab, np.where(sizes >= cfg["min_area_m2"])[0] + 1)


# ---------------------------------------------------------------- 4. 主流程
def measure(drawing, cfg, out_dir):
    S = cfg["render_px_per_pt"]
    page, paths = load_paths(drawing["pdf"])
    W, H = int(page.rect.width * S), int(page.rect.height * S)

    # 4.1 比例尺
    if drawing.get("mm_per_pt"):
        mm, ref_pt = drawing["mm_per_pt"], drawing["reference_length_mm"] / drawing["mm_per_pt"]
    else:
        mm, ref_pt = detect_scale(paths, drawing["reference_length_mm"])
    px_mm = mm / S                       # 每像素毫米数
    px_m2 = px_mm ** 2 / 1e6

    # 4.2 厂房范围 + 斜线填充区
    bx0, by0, bx1, by1 = building_box(paths, ref_pt, cfg["building"]["min_line_frac"])
    hm = hatch_mask(paths, W, H, S, mm, cfg["hatch"]) if cfg["hatch"]["enabled"] else np.zeros((H, W), bool)

    # 4.3 过滤非设备线条
    gl, gh = cfg["route_grey_range"]
    excl = {tuple(c) for c in cfg["exclude_colors"]}
    max_ext = cfg["max_path_extent_mm"] / mm
    route_max = cfg["route_max_segment_mm"] / mm

    kept = []
    for col, r, polys in paths:
        if tuple(col) in excl or min(col) >= cfg["near_white"]:
            continue                                     # 安全围栏/近白色注释
        if max(r.width, r.height) > max_ext:
            continue                                     # 墙、轴线、尺寸线
        cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
        if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
            continue                                     # 厂房外：标题、尺寸标注
        if hm[min(H - 1, int(cy * S)), min(W - 1, int(cx * S))]:
            continue                                     # 斜线填充区内：工位标签、墙
        if is_neutral_grey(col, gl, gh):                 # 灰色: 去掉长的通道/路线线段
            polys = [pl for pl in polys if len(pl) == 5 or abs(pl[0] - pl[-1]) < route_max]
        kept.append((col, r, polys))

    m = rasterize(kept, W, H, S, lambda c, r: True)

    # 4.4 AGV 转弯圆（灰色细线圆）—— OpenCV 霍夫圆检测后擦除
    n_circles = 0
    ac = cfg["agv_circles"]
    if ac["enabled"]:
        grey = rasterize(kept, W, H, S, lambda c, r: is_neutral_grey(c, gl, gh)).astype(np.uint8) * 255
        rmin, rmax = int(ac["min_radius_mm"] / px_mm), int(ac["max_radius_mm"] / px_mm)
        cs = cv2.HoughCircles(cv2.GaussianBlur(grey, (3, 3), 0), cv2.HOUGH_GRADIENT, dp=1,
                              minDist=rmin * 2, param1=50, param2=ac["vote_threshold"],
                              minRadius=rmin, maxRadius=rmax)
        if cs is not None:
            er = np.zeros_like(grey)
            for x, y, rr in cs[0]:
                cv2.circle(er, (int(x), int(y)), int(rr), 255, 3); n_circles += 1
            m &= ~(er > 0)

    # 4.5 闭合小缺口 -> 填充设备轮廓内部（只填小于阈值的封闭区，避免把通道围合区算进去）
    g = max(1, int(round(cfg["gap_close_mm"] / px_mm)))
    closed = ndi.binary_closing(ndi.binary_dilation(m), structure=np.ones((g, g)))
    holes = ndi.binary_fill_holes(closed) & ~closed
    hl, hn = ndi.label(holes)
    hs = ndi.sum(holes, hl, range(1, hn + 1)) * px_m2
    filled = closed | np.isin(hl, np.where(hs < cfg["max_hole_fill_m2"])[0] + 1)
    # 去掉细线结构（墙线、尺寸线、通道虚线、箭头）：宽度 < min_feature_mm 的部分开运算去除
    k = max(1, int(round(cfg["min_feature_mm"] / px_mm)))
    filled = cv2.morphologyEx(filled.astype(np.uint8), cv2.MORPH_OPEN, np.ones((k, k), np.uint8)) > 0

    # 4.6 连通分组并统计
    lab, n = ndi.label(filled)
    groups = []
    for i, sl in enumerate(ndi.find_objects(lab)):
        area = (lab[sl] == i + 1).sum() * px_m2
        if area < cfg["min_group_m2"]:
            continue
        h, w = (sl[0].stop - sl[0].start) * px_mm, (sl[1].stop - sl[1].start) * px_mm
        groups.append(dict(length_mm=round(max(h, w)), width_mm=round(min(h, w)),
                           bbox_m2=round(h * w / 1e6, 2), footprint_m2=round(area, 2),
                           x0_pt=round(sl[1].start / S, 1), y0_pt=round(sl[0].start / S, 1)))
    groups.sort(key=lambda g_: -g_["footprint_m2"])
    small = [i + 1 for i, sl in enumerate(ndi.find_objects(lab)) if (lab[sl] == i + 1).sum() * px_m2 < cfg["min_group_m2"]]
    filled[np.isin(lab, small)] = False          # 碎片同时从工位分摊中去掉，保证合计一致
    total = round(sum(g_["footprint_m2"] for g_ in groups), 1)

    # 4.7 基准：产线外包矩形（总长 × 总宽），保留原粗算口径作对比
    # 线体 = 厂房内被斜线填充带隔开的区域；每条线体单独取外包矩形，再相加
    big = [i + 1 for i, sl in enumerate(ndi.find_objects(lab)) if (lab[sl] == i + 1).sum() * px_m2 >= cfg["envelope_min_group_m2"]]
    bigmask = np.isin(lab, big)
    along = "y" if (by1 - by0) >= (bx1 - bx0) else "x"
    y0i, y1i, x0i, x1i = int(by0 * S), int(by1 * S), int(bx0 * S), int(bx1 * S)
    sub = hm[y0i:y1i, x0i:x1i]
    frac = sub.mean(axis=0) if along == "y" else sub.mean(axis=1)        # 每个横截位置上，填充带占产线长度的比例
    sep = frac >= cfg["lane_separator_frac"]
    runs, start = [], None
    for i, s_ in enumerate(list(sep) + [True]):
        if not s_ and start is None: start = i
        if s_ and start is not None: runs.append((start, i)); start = None
    lanes_lab = np.zeros(filled.shape, np.int32)
    for li, (a0, a1) in enumerate(runs, 1):
        if along == "y": lanes_lab[y0i:y1i, x0i + a0:x0i + a1] = li
        else:            lanes_lab[y0i + a0:y0i + a1, x0i:x1i] = li
    n_l = len(runs)
    lanes = []
    for li in range(1, n_l + 1):
        mm_ = bigmask & (lanes_lab == li)
        if mm_.sum() * px_m2 < cfg["envelope_min_group_m2"]:
            continue
        ys_, xs_ = np.nonzero(mm_)
        a_ = (ys_.max() - ys_.min()) * px_mm / 1000; b_ = (xs_.max() - xs_.min()) * px_mm / 1000
        Lm, Wm = (a_, b_) if along == "y" else (b_, a_)
        lanes.append(dict(lane=len(lanes) + 1, length_m=round(Lm, 1), width_m=round(Wm, 1), area_m2=round(Lm * Wm, 1),
                          footprint_m2=round(float((filled & (lanes_lab == li)).sum() * px_m2), 1)))
    envelope = dict(lanes=lanes, area_m2=round(sum(l_["area_m2"] for l_ in lanes), 1))

    # 4.8 工位识别 + 面积按工位分摊
    station_rows = []
    labels = None
    if cfg["stations"]["enabled"]:
        st = stn.detect_stations(page, mm, cfg["stations"])
        ovs = drawing.get("station_overrides", [])
        if st:
            area, labels, cols = stn.allocate(filled, S, st, mm, cfg["stations"], along)
            for k, s_ in enumerate(st):
                m_ = labels == k
                if m_.any():
                    yy, xx = np.nonzero(m_)
                    h_, w_ = (yy.max() - yy.min()) * px_mm / 1000, (xx.max() - xx.min()) * px_mm / 1000
                else:
                    h_ = w_ = 0
                L_, W_ = (max(h_, w_), min(h_, w_))
                name = s_["name"]
                for o in ovs:                    # 人工修正：按编号（可选再按 OCR 原文片段）匹配
                    if o["op"] == s_["op"] and o.get("match", "") in s_["raw"]:
                        name = o["name"]; break
                station_rows.append(dict(op=s_["op"], name=name, ocr_text=s_["raw"], op_inferred=s_["inferred"],
                                         label_column=s_["column"], footprint_m2=round(float(area[k]), 2),
                                         bbox_length_m=round(L_, 1), bbox_width_m=round(W_, 1),
                                         bbox_m2=round(L_ * W_, 1), label_x_pt=round(s_["x"], 1), label_y_pt=round(s_["y"], 1)))
            station_rows.sort(key=lambda r: (r["op"], r["label_column"]))

    # 4.9 叠加图（红色 = 计入面积的部分），便于人工核对
    bg = page.get_pixmap(matrix=pymupdf.Matrix(S, S))
    a = np.frombuffer(bg.samples, np.uint8).reshape(bg.h, bg.w, bg.n)[:, :, :3].copy()
    a = a[:H, :W]; f = filled[:a.shape[0], :a.shape[1]]
    a[f] = (0.5 * a[f] + [127, 0, 0]).astype(np.uint8)
    ov = Image.fromarray(a); ov.thumbnail((1400, 2000))
    ov_path = os.path.join(out_dir, f"{drawing['id']}_overlay.png"); ov.save(ov_path)
    st_path = None
    if labels is not None:                       # 按工位着色的叠加图
        rng = np.random.default_rng(7)
        pal = rng.integers(60, 230, size=(len(st), 3))
        b2 = np.frombuffer(bg.samples, np.uint8).reshape(bg.h, bg.w, bg.n)[:, :, :3].copy()[:H, :W]
        L2 = labels[:b2.shape[0], :b2.shape[1]]
        msk = L2 >= 0
        b2[msk] = (0.35 * b2[msk] + 0.65 * pal[L2[msk]]).astype(np.uint8)
        im2 = Image.fromarray(b2); d2 = ImageDraw.Draw(im2)
        for k, s_ in enumerate(st):
            col_ = tuple(int(v) for v in pal[k])
            d2.rectangle([s_["x"] * S - 6, s_["y"] * S - 6, s_["x"] * S + 6, s_["y"] * S + 6], fill=col_, outline=(0, 0, 0))
        im2.thumbnail((1400, 2000))
        st_path = os.path.join(out_dir, f"{drawing['id']}_stations.png"); im2.save(st_path)

    return dict(id=drawing["id"], name=drawing.get("name", drawing["id"]), pdf=os.path.basename(drawing["pdf"]),
                reference_length_mm=drawing["reference_length_mm"], mm_per_pt=round(mm, 4),
                building_box_m=[round((bx1 - bx0) * mm / 1000, 1), round((by1 - by0) * mm / 1000, 1)],
                agv_circles_removed=n_circles, footprint_m2=total, n_groups=len(groups),
                envelope=envelope, footprint_vs_envelope=round(total / envelope["area_m2"], 3),
                stations=station_rows, station_sum_m2=round(sum(r["footprint_m2"] for r in station_rows), 1),
                overlay=os.path.basename(ov_path), station_overlay=os.path.basename(st_path) if st_path else None,
                groups=groups)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config"); ap.add_argument("--out", default="results")
    a = ap.parse_args()
    conf = yaml.safe_load(open(a.config, encoding="utf-8"))
    os.makedirs(a.out, exist_ok=True)
    base = os.path.dirname(os.path.abspath(a.config))
    res = []
    for d in conf["drawings"]:
        d = dict(d); d["pdf"] = os.path.join(base, d["pdf"]) if not os.path.isabs(d["pdf"]) else d["pdf"]
        r = measure(d, conf["extraction"], a.out); res.append(r)
        print(f"[{r['id']}] 比例 {r['mm_per_pt']} mm/pt | 厂房 {r['building_box_m']} m | "
              f"去除AGV圆 {r['agv_circles_removed']} | 投影面积 {r['footprint_m2']} m² | "
              f"基准 总长×宽 " + " + ".join(f"{l['length_m']}×{l['width_m']}" for l in r['envelope']['lanes']) + f" = {r['envelope']['area_m2']} m² | "
              f"识别工位 {len(r['stations'])} 个，合计 {r['station_sum_m2']} m²")
    json.dump(dict(extraction=conf["extraction"], drawings=res), open(os.path.join(a.out, "results.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
