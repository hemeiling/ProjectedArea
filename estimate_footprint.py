#!/usr/bin/env python3
"""
从 CAD 导出的矢量布局 PDF 估算设备投影面积（占地面积）。

用法:
    python estimate_footprint.py config.yaml [--out results]

所有阈值都在 config.yaml 中，代码中不针对具体图纸写死任何坐标或颜色。
每张图纸只需提供: PDF 路径 + 图上标注的一个总尺寸（mm）。
"""
import argparse, json, math, os, sys
from array import array
from collections import namedtuple
import numpy as np
import yaml
import pymupdf
import cv2
import stations as stn
from scipy import ndimage as ndi
from PIL import Image, ImageDraw


# ---------------------------------------------------------------- 1. 读取矢量
def each_drawing(page, fn):
    """逐条把页面矢量路径（page.get_cdrawings() 的原始字典）交给 fn，不在内存中保留整页列表。
    A3 大图有 60 多万条路径，page.get_drawings() 一次性生成的 Point/Rect 对象要占近 2 GB。
    列表模式下 PyMuPDF 把“填充 f + 紧随其后、折线相同的描边 s”合并为一条 fs（文字字形），
    回调模式不合并；这里按同样的规则合并，逐条结果与 page.get_drawings() 一致。"""
    prev = None

    def take(d):
        nonlocal prev
        if prev is not None and d["type"] == "s" and prev["type"] == "f" and prev["items"] == d["items"]:
            for k, v in d.items():
                prev.setdefault(k, v)
            prev["type"] = "fs"
            fn(prev); prev = None
            return
        if prev is not None:
            fn(prev)
        prev = d

    page.get_cdrawings(callback=take)
    if prev is not None:
        fn(prev)


class Box(namedtuple("Box", "x0 y0 x1 y1")):
    """路径外框；width / height 的定义与 pymupdf.Rect 相同"""
    __slots__ = ()
    width = property(lambda b: max(0, b.x1 - b.x0))
    height = property(lambda b: max(0, b.y1 - b.y0))


class Paths:
    """页面矢量路径的紧凑存储：坐标放在数组里，迭代时才逐条生成 (颜色, 外框 Box, 折线列表)，
    折线为 [(x, y), ...]。另存文字字形 glyphs = [(Rect, 填充色)] 供工位识别使用。"""

    def __init__(self):
        self.cols, self.box = [], array("d")     # 每条路径：颜色；外框 x0 y0 x1 y1
        self.xy = array("d")                     # 所有折线顶点 x y
        self.pl_end = array("q")                 # 每条折线末顶点之后的序号
        self.path_end = array("q")               # 每条路径末折线之后的序号
        self.glyphs = []
        self._col = {}

    def add(self, col, rect, polylines):
        self.cols.append(self._col.setdefault(col, col))
        self.box.extend(rect)
        for pl in polylines:
            for p in pl:
                self.xy.extend(p)
            self.pl_end.append(len(self.xy) // 2)
        self.path_end.append(len(self.pl_end))

    def boxes(self):
        b = self.box
        return (Box(b[i], b[i + 1], b[i + 2], b[i + 3]) for i in range(0, len(b), 4))

    def __iter__(self):
        xy, pe = self.xy, self.pl_end
        j = v = 0
        for col, r, k in zip(self.cols, self.boxes(), self.path_end):
            polys = []
            for e in pe[j:k]:
                polys.append(list(zip(xy[2 * v:2 * e:2], xy[2 * v + 1:2 * e:2]))); v = e
            j = k
            yield col, r, polys


def load_paths(pdf_path):
    """读取 PDF 第一页所有矢量路径 -> Paths（逐条为 (颜色, 外框, 折线列表)）"""
    page = pymupdf.open(pdf_path)[0]
    paths = Paths()

    def add(d):
        if d["type"] == "fs":          # 文字字形（填充+描边）不是设备，单独处理
            paths.glyphs.append((pymupdf.Rect(d["rect"]), tuple(round(c, 2) for c in (d.get("fill") or (0, 0, 0)))))
            return
        col = d.get("color") or d.get("fill") or (0, 0, 0)
        col = tuple(round(c, 2) for c in col)
        polylines = []
        for it in d["items"]:
            k = it[0]
            if k == "l":
                polylines.append((it[1], it[2]))
            elif k == "c":                       # 贝塞尔曲线: 用起止点近似
                polylines.append((it[1], it[4]))
            elif k == "re":
                r = pymupdf.Rect(it[1]).normalize(); polylines.append((r.tl, r.tr, r.br, r.bl, r.tl))
            elif k == "qu":
                q = pymupdf.Quad(it[1]); polylines.append((q.ul, q.ur, q.lr, q.ll, q.ul))
        paths.add(col, d["rect"], polylines)

    each_drawing(page, add)
    return page, paths


def dist(a, b):
    """两点距离，算法与 abs(pymupdf.Point - Point) 相同"""
    dx, dy = a[0] - b[0], a[1] - b[1]
    return math.sqrt(dx * dx + dy * dy)


# ---------------------------------------------------------------- 2. 比例尺
def longest_line(paths, line_ratio=0.03):
    """图上最长的“线状”路径 -> (长度 pt, 外框)；没有则 (0, None)"""
    best, rect = 0.0, None
    for r in paths.boxes():
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
            pts = [(x * S, y * S) for x, y in pl]
            d.line(pts, fill=255, width=width)
    return np.array(im) > 0


def building_box(paths, ref_pt, min_frac):
    """长线状路径（墙/柱网/边界，长度 ≥ min_frac×总尺寸）的外包框 = 厂房范围"""
    xs0, ys0, xs1, ys1 = [], [], [], []
    for r in paths.boxes():
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
                ang = abs(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))) % 180
                if dist(b, a) < min_len:
                    continue
                if cfg["angle_min"] <= ang <= cfg["angle_max"] or \
                   180 - cfg["angle_max"] <= ang <= 180 - cfg["angle_min"]:
                    d.line([(a[0] * S, a[1] * S), (b[0] * S, b[1] * S)], fill=255, width=1)
    m = np.array(diag) > 0
    k = max(1, int(cfg["close_mm"] / mm * S))
    m = cv2.morphologyEx(m.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((k, k), np.uint8)) > 0
    lab, _ = ndi.label(m)
    objs = ndi.find_objects(lab)
    px_m2 = (mm / S) ** 2 / 1e6
    sizes = label_sizes(lab, objs) * px_m2
    return select_labels(lab, objs, np.where(sizes >= cfg["min_area_m2"])[0] + 1)


def label_sizes(lab, objs):
    """各连通区像素数（= ndi.sum(mask, lab, range(1, n + 1))）。逐个外框切片计数，
    不生成整图大小的 int64 / float64 临时数组。"""
    return np.array([np.count_nonzero(lab[sl] == i) for i, sl in enumerate(objs, 1)], dtype=np.int64)


def select_labels(lab, objs, ids):
    """属于 ids 中连通区的像素（= np.isin(lab, ids)），同样按外框切片计算"""
    out = np.zeros(lab.shape, bool)
    for i in ids:
        sl = objs[i - 1]
        out[sl] |= lab[sl] == i
    return out


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

    def kept():                                          # 每次重新生成，不在内存中保留整份列表
        for col, r, polys in paths:
            if tuple(col) in excl or min(col) >= cfg["near_white"]:
                continue                                 # 安全围栏/近白色注释
            if max(r.width, r.height) > max_ext:
                continue                                 # 墙、轴线、尺寸线
            cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
            if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
                continue                                 # 厂房外：标题、尺寸标注
            if hm[min(H - 1, int(cy * S)), min(W - 1, int(cx * S))]:
                continue                                 # 斜线填充区内：工位标签、墙
            if is_neutral_grey(col, gl, gh):             # 灰色: 去掉长的通道/路线线段
                polys = [pl for pl in polys if len(pl) == 5 or dist(pl[0], pl[-1]) < route_max]
            yield col, r, polys

    m = rasterize(kept(), W, H, S, lambda c, r: True)

    # 4.4 AGV 转弯圆（灰色细线圆）—— OpenCV 霍夫圆检测后擦除
    n_circles = 0
    ac = cfg["agv_circles"]
    if ac["enabled"]:
        grey = rasterize(kept(), W, H, S, lambda c, r: is_neutral_grey(c, gl, gh)).astype(np.uint8) * 255
        rmin, rmax = int(ac["min_radius_mm"] / px_mm), int(ac["max_radius_mm"] / px_mm)
        # 多线程时每个线程各有一份整图大小的累加器（每线程约 64 MB）；单线程只占一份，结果相同
        nt = cv2.getNumThreads(); cv2.setNumThreads(0)
        try:
            cs = cv2.HoughCircles(cv2.GaussianBlur(grey, (3, 3), 0), cv2.HOUGH_GRADIENT, dp=1,
                                  minDist=rmin * 2, param1=50, param2=ac["vote_threshold"],
                                  minRadius=rmin, maxRadius=rmax)
        finally:
            cv2.setNumThreads(nt)
        if cs is not None:
            er = np.zeros_like(grey)
            for x, y, rr in cs[0]:
                cv2.circle(er, (int(x), int(y)), int(rr), 255, 3); n_circles += 1
            m &= ~(er > 0)
            del er
        del grey

    # 4.5 闭合小缺口 -> 填充设备轮廓内部（只填小于阈值的封闭区，避免把通道围合区算进去）
    g = max(1, int(round(cfg["gap_close_mm"] / px_mm)))
    closed = ndi.binary_closing(ndi.binary_dilation(m), structure=np.ones((g, g)))
    holes = ndi.binary_fill_holes(closed) & ~closed
    del m
    hl, _ = ndi.label(holes)
    del holes
    hobjs = ndi.find_objects(hl)
    hs = label_sizes(hl, hobjs) * px_m2
    filled = closed | select_labels(hl, hobjs, np.where(hs < cfg["max_hole_fill_m2"])[0] + 1)
    del closed, hl, hobjs
    # 去掉细线结构（墙线、尺寸线、通道虚线、箭头）：宽度 < min_feature_mm 的部分开运算去除
    k = max(1, int(round(cfg["min_feature_mm"] / px_mm)))
    filled = cv2.morphologyEx(filled.astype(np.uint8), cv2.MORPH_OPEN, np.ones((k, k), np.uint8)) > 0

    # 4.6 连通分组并统计
    lab, n = ndi.label(filled)
    objs = ndi.find_objects(lab)
    px_n = label_sizes(lab, objs)
    groups = []
    for i, sl in enumerate(objs):
        area = px_n[i] * px_m2
        if area < cfg["min_group_m2"]:
            continue
        h, w = (sl[0].stop - sl[0].start) * px_mm, (sl[1].stop - sl[1].start) * px_mm
        groups.append(dict(length_mm=round(max(h, w)), width_mm=round(min(h, w)),
                           bbox_m2=round(h * w / 1e6, 2), footprint_m2=round(area, 2),
                           x0_pt=round(sl[1].start / S, 1), y0_pt=round(sl[0].start / S, 1)))
    groups.sort(key=lambda g_: -g_["footprint_m2"])
    small = [i + 1 for i in range(n) if px_n[i] * px_m2 < cfg["min_group_m2"]]
    filled[select_labels(lab, objs, small)] = False          # 碎片同时从工位分摊中去掉，保证合计一致
    total = round(sum(g_["footprint_m2"] for g_ in groups), 1)

    # 4.7 基准：产线外包矩形（总长 × 总宽），保留原粗算口径作对比
    # 线体 = 厂房内被斜线填充带隔开的区域；每条线体单独取外包矩形，再相加
    big = [i + 1 for i in range(n) if px_n[i] * px_m2 >= cfg["envelope_min_group_m2"]]
    bigmask = select_labels(lab, objs, big)
    del lab, objs
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
        st = stn.detect_stations(page, paths.glyphs, mm, cfg["stations"])
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
    band = 256                                   # 逐像素混色按行带分块，避免整图大小的 float64 临时数组
    for y in range(0, a.shape[0], band):
        ab, fb = a[y:y + band], f[y:y + band]
        ab[fb] = (0.5 * ab[fb] + [127, 0, 0]).astype(np.uint8)
    ov = Image.fromarray(a); ov.thumbnail((1400, 2000))
    ov_path = os.path.join(out_dir, f"{drawing['id']}_overlay.png"); ov.save(ov_path)
    st_path = None
    if labels is not None:                       # 按工位着色的叠加图
        rng = np.random.default_rng(7)
        pal = rng.integers(60, 230, size=(len(st), 3))
        b2 = np.frombuffer(bg.samples, np.uint8).reshape(bg.h, bg.w, bg.n)[:, :, :3].copy()[:H, :W]
        L2 = labels[:b2.shape[0], :b2.shape[1]]
        for y in range(0, b2.shape[0], band):
            bb, Lb = b2[y:y + band], L2[y:y + band]
            msk = Lb >= 0
            bb[msk] = (0.35 * bb[msk] + 0.65 * pal[Lb[msk]]).astype(np.uint8)
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
