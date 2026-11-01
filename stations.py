"""
工位标签识别 + 面积按工位分摊。

原理
1. CAD 打印成 PDF 时，文字被转成“填充+描边”(fs) 路径，设备是纯描边路径 —— 据此把文字和设备分开。
2. 黑色文字按距离聚成文字块；去掉特大字号（图名）。每个文字块单独渲染、转正、OCR，
   解析出 “OP-xx” 编号和工位名称。含 OP 编号的文字块 = 一个工位标签。
3. 标签所在位置把产线切成工位区段：
   - 横向：每块设备面积归属到横向距离最近的那一列标签（产线两侧各有一列标签）；
   - 纵向：同一列标签按沿线位置排序，相邻标签的中点为工位分界。
"""
import re
import numpy as np
import cv2
import pymupdf
import pytesseract
from PIL import Image
from scipy import ndimage as ndi

OP_RE = re.compile(r"[O0DQo]?\s*P\s*[\-—–_.~=]*\s*(\d{1,2})")


def text_paths(page):
    """返回 [(rect, colour)]：所有 fs 文字字形"""
    out = []
    for d in page.get_drawings():
        if d["type"] == "fs":
            col = tuple(round(c, 2) for c in (d.get("fill") or (0, 0, 0)))
            out.append((d["rect"], col))
    return out


def find_label_blocks(page, glyphs, mm, cfg):
    """黑色文字字形 -> 文字块外框(pt)"""
    black = [r for r, c in glyphs if max(c) <= cfg["label_max_lightness"]]
    if not black:
        return []
    sizes = np.array([min(r.width, r.height) for r in black])
    med = float(np.median(sizes))
    black = [r for r in black if min(r.width, r.height) <= cfg["title_size_factor"] * med]
    S = 4
    W, H = int(page.rect.width * S), int(page.rect.height * S)
    m = np.zeros((H, W), np.uint8)
    for r in black:
        cv2.rectangle(m, (int(r.x0 * S), int(r.y0 * S)), (int(r.x1 * S), int(r.y1 * S)), 255, -1)
    k = max(1, int(cfg["cluster_gap_mm"] / mm * S))
    m = cv2.dilate(m, np.ones((k, k), np.uint8))
    lab, n = ndi.label(m > 0)
    blocks = []
    for sl in ndi.find_objects(lab):
        x0, y0 = sl[1].start / S, sl[0].start / S
        x1, y1 = sl[1].stop / S, sl[0].stop / S
        blocks.append(pymupdf.Rect(x0, y0, x1, y1))
    return blocks


def ocr_block(page, rect, dpi):
    pix = page.get_pixmap(dpi=dpi, clip=rect)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3]
    g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    b = (g < 90).astype(np.uint8) * 255                       # 只保留黑色文字
    b = cv2.dilate(b, np.ones((2, 2), np.uint8))
    im = Image.fromarray(255 - b)
    im = Image.fromarray(np.pad(np.array(im), 30, constant_values=255))
    best = ""
    rots = (90, 270) if rect.height > rect.width else (0,)    # 竖排文字先转正
    for rot in rots:
        t = pytesseract.image_to_string(im.rotate(rot, expand=True), config="--psm 6")
        t = " ".join(t.split())
        if len(OP_RE.findall(t)) > len(OP_RE.findall(best)) or (not best and t):
            best = t
    return best


def parse_label(text):
    m = OP_RE.search(text)
    if not m:
        return None, text
    name = (text[:m.start()] + " " + text[m.end():]).strip(" -—|.,")
    name = re.sub(r"[^A-Za-z0-9 &\-/一-鿿]", "", name).strip()
    return int(m.group(1)), name


def detect_stations(page, mm, cfg):
    glyphs = text_paths(page)
    blocks = find_label_blocks(page, glyphs, mm, cfg)
    stations = []
    for r in blocks:
        txt = ocr_block(page, r + (-2, -2, 2, 2), cfg["ocr_dpi"])
        op, name = parse_label(txt)
        letters = len(re.findall(r"[A-Za-z\u4e00-\u9fff]", name))
        if op is None and letters < cfg["min_name_letters"]:
            continue                                   # 既无编号也无像样的名称：不是工位标签
        stations.append(dict(op_num=op, name=name, raw=txt, has_op=op is not None,
                             x=(r.x0 + r.x1) / 2, y=(r.y0 + r.y1) / 2,
                             rect=[round(v, 1) for v in r]))
    # 只保留落在“标签列”里的无编号文字块（列由带编号的标签确定）
    tol = cfg["column_tolerance_mm"] / mm
    xs = [s["x"] for s in stations if s["has_op"]]
    stations = [s for s in stations if s["has_op"] or any(abs(s["x"] - x) < tol / 4 for x in xs)]
    return stations


def fix_numbers(cols):
    """同一列内编号应连续：夹在 n-1 与 n+1 之间却不是 n 的，改为 n（标记为推断）"""
    for c in cols:
        for i, s in enumerate(c):
            s["inferred"] = False
        for i in range(1, len(c) - 1):
            a, b = c[i - 1]["op_num"], c[i + 1]["op_num"]
            if a is None or b is None:
                continue
            step = 1 if b > a else -1
            if abs(b - a) == 2 and c[i]["op_num"] != a + step:
                c[i]["op_num"] = a + step; c[i]["inferred"] = True
        # 列首/列尾或孤立的无编号标签：按相邻两个标签的步长外推
        for i, s in enumerate(c):
            if s["op_num"] is not None:
                continue
            if i >= 2 and None not in (c[i-1]["op_num"], c[i-2]["op_num"]):
                s["op_num"] = 2 * c[i-1]["op_num"] - c[i-2]["op_num"]; s["inferred"] = True
            elif i + 2 < len(c) and None not in (c[i+1]["op_num"], c[i+2]["op_num"]):
                s["op_num"] = 2 * c[i+1]["op_num"] - c[i+2]["op_num"]; s["inferred"] = True
        for s in c:
            s["op"] = f"OP-{s['op_num']:02d}" if s["op_num"] is not None else "OP-??"


def group_columns(stations, mm, tol_mm):
    """把标签按横向位置分成若干“标签列”"""
    st = sorted(stations, key=lambda s: s["x"])
    cols = []
    for s in st:
        if cols and abs(s["x"] - np.mean([c["x"] for c in cols[-1]])) * mm < tol_mm:
            cols[-1].append(s)
        else:
            cols.append([s])
    for i, c in enumerate(cols):
        c.sort(key=lambda s: s["y"])
        for s in c:
            s["column"] = i
    return cols


def allocate(filled, S, stations, mm, cfg, along_axis="y"):
    """把投影面积像素分配到工位，返回 每个工位面积(m²)、工位标签图、标签列。
    along_axis: 产线方向。'x' 时内部转置处理。"""
    if along_axis == "x":
        T = [dict(s, x=s["y"], y=s["x"]) for s in stations]
        area, labels, cols = allocate(filled.T, S, T, mm, cfg, "y")
        for s, t in zip(stations, T):
            s.update(op=t["op"], op_num=t["op_num"], inferred=t["inferred"], column=t["column"])
        return area, labels.T, cols
    H, W = filled.shape
    px_m2 = (mm / S) ** 2 / 1e6
    cols = group_columns(stations, mm, cfg["column_tolerance_mm"])
    fix_numbers(cols)
    col_x = np.array([np.mean([s["x"] for s in c]) for c in cols]) * S
    ys, xs = np.nonzero(filled)
    # 横向：最近的标签列
    ci = np.argmin(np.abs(xs[:, None] - col_x[None, :]), axis=1)
    sid = np.full(len(xs), -1)
    idx_of = {id(s): k for k, s in enumerate(stations)}
    for c_i, c in enumerate(cols):
        sel = ci == c_i
        cy = np.array([s["y"] for s in c]) * S
        bounds = (cy[1:] + cy[:-1]) / 2                         # 相邻标签中点 = 工位分界
        band = np.searchsorted(bounds, ys[sel])
        sid[sel] = np.array([idx_of[id(c[b])] for b in band]) if len(c) else -1
    area = np.bincount(sid[sid >= 0], minlength=len(stations)) * px_m2
    labels = np.full(filled.shape, -1, np.int32)
    labels[ys, xs] = sid
    return area, labels, cols
