"""斜线填充区（墙/通道带）只由同一颜色的斜线组成。

回归：101 图 OP-01 的两台提升机上画有“方框 + 对角线”符号（黑色，1.59 m）。旧做法把所有颜色的长斜线一起
闭合成片，这些对角线与旁边的灰色通道填充带连成一片，提升机的上半部被当作填充区排除（约 9 m²）。
CAD 的填充图案是一个对象、一种颜色，所以按颜色分别成片。符号与填充带同色时仍无法区分（局限，见 README）。
"""
import copy

import pymupdf
import pytest
import yaml

import estimate_footprint as ef
from webapp.jobs import ESTIMATOR_DIR

# 100 mm/pt：尺寸线 600 pt = 60000 mm；厂房 600 × 400 pt
GREY, BLACK, BLUE = (0.6, 0.6, 0.6), (0, 0, 0), (0, 0, 1)
MACHINE = (200, 150, 230, 200)              # 3 × 5 m = 15 m²，离填充带 2 m（小于 3 m 的闭合距离）
SYMBOLS = [(205, 155, 225, 175), (205, 177, 225, 197)]   # 上下两个 2 × 2 m 方框，各带一条 2.83 m 的 45° 对角线（同 101 的提升机）
MACHINE_M2 = 15.0


def make_pdf(path, symbol_colour):
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)

    def stroke(draw, colour):
        sh = page.new_shape(); draw(sh); sh.finish(color=colour, width=0.5); sh.commit()

    stroke(lambda s: s.draw_line((100, 60), (700, 60)), BLACK)                       # 总尺寸线
    for a, b in (((100, 100), (700, 100)), ((100, 500), (700, 500)), ((100, 100), (100, 500)), ((700, 100), (700, 500))):
        stroke(lambda s, a=a, b=b: s.draw_line(a, b), BLACK)                          # 厂房边界
    # 通道填充带：30 × 2 m，灰色 45° 斜线间距 2 m（单根 2.83 m），上下边线 30 m（> 14 m，按墙线排除）
    stroke(lambda s: s.draw_line((120, 110), (420, 110)), GREY)
    stroke(lambda s: s.draw_line((120, 130), (420, 130)), GREY)
    for x in range(120, 420, 20):
        stroke(lambda s, x=x: s.draw_line((x, 130), (x + 20, 110)), GREY)
    stroke(lambda s: s.draw_rect(pymupdf.Rect(*MACHINE)), BLUE)
    for box in SYMBOLS:
        stroke(lambda s, box=box: s.draw_rect(pymupdf.Rect(*box)), symbol_colour)
        stroke(lambda s, box=box: s.draw_line(box[:2], box[2:]), symbol_colour)
    doc.save(path)
    return path


@pytest.fixture
def cfg():
    with open(f"{ESTIMATOR_DIR}/config.yaml", encoding="utf-8") as f:
        c = copy.deepcopy(yaml.safe_load(f)["extraction"])
    c["stations"]["enabled"] = False
    return c


def test_box_diagonal_on_a_machine_is_not_a_corridor_hatch(tmp_path, cfg):
    pdf = make_pdf(str(tmp_path / "hatch.pdf"), BLACK)
    r = ef.measure(dict(id="h", name="h", pdf=pdf, reference_length_mm=60000), cfg, str(tmp_path))
    assert MACHINE_M2 <= r["footprint_m2"] <= MACHINE_M2 * 1.07         # 机器整台计入；填充带不计入


def test_corridor_hatch_is_still_excluded(tmp_path, cfg):
    """同色斜线成片（≥ 40 m²）仍是填充区：带内的东西不计入"""
    _, paths = ef.load_paths(make_pdf(str(tmp_path / "hatch.pdf"), BLACK))
    S, mm = cfg["render_px_per_pt"], 100.0
    hm = ef.hatch_mask(paths, 842 * S, 595 * S, S, mm, cfg["hatch"])
    assert hm[120 * S, 270 * S]                          # 填充带中部
    assert not hm[175 * S, 215 * S]                      # 机器中心


def test_drawing_with_no_equipment_reports_zero_instead_of_crashing(tmp_path, cfg):
    """全部被排除（或图上没有设备）时面积为 0，由界面提示，而不是除以 0 报错"""
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    for a, b in (((100, 60), (700, 60)), ((100, 100), (700, 100)), ((100, 500), (700, 500)),
                 ((100, 100), (100, 500)), ((700, 100), (700, 500))):
        sh = page.new_shape(); sh.draw_line(a, b); sh.finish(color=BLACK, width=0.5); sh.commit()
    doc.save(str(tmp_path / "empty.pdf"))
    r = ef.measure(dict(id="e", name="e", pdf=str(tmp_path / "empty.pdf"), reference_length_mm=60000), cfg, str(tmp_path))
    assert r["footprint_m2"] == 0 and r["footprint_vs_envelope"] is None
