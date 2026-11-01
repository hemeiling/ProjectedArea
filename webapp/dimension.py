"""
标注总尺寸未知时的提示图：标出程序用来确定比例尺的那条线（图上最长的线状路径），
请用户读出图上这条线方向的总尺寸。比例尺只来自这个数字，程序不猜测。
"""
import pymupdf
from PIL import Image, ImageDraw

import estimate_footprint as ef

HIGHLIGHT = (230, 0, 120)


def preview(pdf_path, out_png, max_px=1600):
    """生成提示图；返回 {length_pt, orientation}，找不到线状路径时 length_pt = 0"""
    page, paths = ef.load_paths(pdf_path)
    length, rect = ef.longest_line(paths)
    zoom = max_px / max(page.rect.width, page.rect.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if rect is not None:
        d = ImageDraw.Draw(im)
        pad = 8
        d.rectangle([rect.x0 * zoom - pad, rect.y0 * zoom - pad, rect.x1 * zoom + pad, rect.y1 * zoom + pad],
                    outline=HIGHLIGHT, width=6)
    im.save(out_png)
    orientation = None if rect is None else ("vertical" if rect.height > rect.width else "horizontal")
    return dict(length_pt=round(length, 1), orientation=orientation)
