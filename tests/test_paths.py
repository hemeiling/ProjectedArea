"""省内存的矢量读取与连通区统计必须与原实现（page.get_drawings() / ndi.sum / np.isin）逐值一致。"""
import numpy as np
import pymupdf
import pytest
from scipy import ndimage as ndi

import estimate_footprint as ef


def reference_paths(page):
    """原 load_paths：基于 page.get_drawings() 的 Point/Rect 对象"""
    paths, glyphs = [], []
    for d in page.get_drawings():
        if d["type"] == "fs":
            glyphs.append((tuple(d["rect"]), tuple(round(c, 2) for c in (d.get("fill") or (0, 0, 0)))))
            continue
        col = tuple(round(c, 2) for c in (d.get("color") or d.get("fill") or (0, 0, 0)))
        polylines = []
        for it in d["items"]:
            k = it[0]
            if k == "l":
                polylines.append([it[1], it[2]])
            elif k == "c":
                polylines.append([it[1], it[4]])
            elif k == "re":
                r = it[1]; polylines.append([r.tl, r.tr, r.br, r.bl, r.tl])
            elif k == "qu":
                q = it[1]; polylines.append([q.ul, q.ur, q.lr, q.ll, q.ul])
        paths.append((col, tuple(d["rect"]), [[(p.x, p.y) for p in pl] for pl in polylines]))
    return paths, glyphs


@pytest.fixture
def mixed_pdf(tmp_path):
    """描边线、填充+描边（fs）、仅填充、矩形、四边形、曲线，以及“填充后接不同折线的描边”（不应合并）"""
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)

    def shape(draw, **finish):
        sh = page.new_shape(); draw(sh); sh.finish(**finish); sh.commit()

    shape(lambda s: s.draw_line((50, 50), (400, 60)), color=(0, 0, 0), width=0.5)
    shape(lambda s: s.draw_polyline([(100, 100), (160, 100), (130, 150), (100, 100)]), color=(0, 0, 0), fill=(0, 0, 0))
    shape(lambda s: s.draw_rect(pymupdf.Rect(300, 300, 340, 280)), color=(0, 0, 1))
    shape(lambda s: s.draw_rect(pymupdf.Rect(200, 200, 260, 240)), color=None, fill=(0.5, 0.5, 0.5))
    shape(lambda s: s.draw_line((200, 260), (260, 300)), color=(0.5, 0.5, 0.5))
    shape(lambda s: s.draw_quad(pymupdf.Quad((500, 100), (560, 110), (490, 160), (570, 170))), color=(1, 0, 0))
    shape(lambda s: s.draw_circle((600, 400), 30), color=(0, 0.6, 0))
    shape(lambda s: s.draw_bezier((50, 500), (100, 450), (150, 550), (200, 500)), color=(0, 0, 0), fill=(1, 1, 0))
    path = tmp_path / "mixed.pdf"
    doc.save(path)
    return str(path)


def test_streamed_paths_match_get_drawings(mixed_pdf):
    page, paths = ef.load_paths(mixed_pdf)
    ref, ref_glyphs = reference_paths(page)
    got = [(c, tuple(r), polys) for c, r, polys in paths]
    assert got == ref
    assert [(tuple(r), c) for r, c in paths.glyphs] == ref_glyphs
    drawings = page.get_drawings()
    assert [d["type"] for d in drawings].count("fs") == len(ref_glyphs) == 2 and "f" in [d["type"] for d in drawings]
    assert {it[0] for d in drawings for it in d["items"]} >= {"l", "c", "re", "qu"}
    assert all((b.width, b.height) == (pymupdf.Rect(b).width, pymupdf.Rect(b).height) for b in paths.boxes())


def test_dist_matches_point_abs():
    a, b = (3.1, -4.7), (-0.2, 9.9)
    assert ef.dist(a, b) == abs(pymupdf.Point(a) - pymupdf.Point(b)) == abs(pymupdf.Point(b) - pymupdf.Point(a))


def test_label_helpers_match_scipy():
    rng = np.random.default_rng(3)
    m = ndi.binary_opening(rng.random((300, 400)) > 0.55)
    lab, n = ndi.label(m)
    objs = ndi.find_objects(lab)
    sizes = ef.label_sizes(lab, objs)
    assert n > 50 and np.array_equal(sizes * 0.37, ndi.sum(m, lab, range(1, n + 1)) * 0.37)
    ids = np.where(sizes >= 5)[0] + 1
    assert np.array_equal(ef.select_labels(lab, objs, ids), np.isin(lab, ids))
    assert not ef.select_labels(lab, objs, []).any()
