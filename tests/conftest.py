"""测试夹具：合成的矢量 PDF（不依赖任何客户图纸）+ 指向临时目录的网页应用。"""
import json
import os
import sys
import time

import pymupdf
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 合成图纸：100 mm/pt。尺寸线 600 pt = 60000 mm；厂房边界 600 × 400 pt；三台设备（纯描边矩形）
SCALE_MM_PER_PT = 100.0
REFERENCE_MM = 60000
EQUIPMENT = [  # (x0, y0, x1, y1) pt，面积 = 宽 × 高 × 0.01 m²/pt²
    (200, 200, 250, 220),   # 5 × 2 m = 10 m²
    (400, 300, 430, 330),   # 3 × 3 m = 9 m²
    (550, 150, 590, 190),   # 4 × 4 m = 16 m²，内部另有一个 2 × 2 m 的细节框
]
EQUIPMENT_M2 = 35.0


def make_layout_pdf(path, with_dimension=True):
    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)

    def stroke(draw, colour=(0, 0, 0)):
        sh = page.new_shape(); draw(sh); sh.finish(color=colour, width=0.5); sh.commit()

    if with_dimension:
        stroke(lambda s: s.draw_line((100, 60), (700, 60)))                 # 总尺寸线
        for a, b in (((100, 100), (700, 100)), ((100, 500), (700, 500)),
                     ((100, 100), (100, 500)), ((700, 100), (700, 500))):
            stroke(lambda s, a=a, b=b: s.draw_line(a, b))                    # 厂房边界（各自独立的长线）
    for x0, y0, x1, y1 in EQUIPMENT:
        stroke(lambda s, r=(x0, y0, x1, y1): s.draw_rect(pymupdf.Rect(*r)), colour=(0, 0, 1))
    stroke(lambda s: s.draw_rect(pymupdf.Rect(560, 160, 580, 180)), colour=(0, 0, 1))
    doc.save(path)
    return path


@pytest.fixture
def layout_pdf(tmp_path):
    return make_layout_pdf(str(tmp_path / "201-synthetic-line.pdf"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from webapp import jobs
    from webapp.app import create_app
    monkeypatch.setattr(jobs, "ocr_available", lambda: False)   # 结果不依赖本机是否装了 Tesseract
    with TestClient(create_app(str(tmp_path / "data"))) as c:
        yield c


def wait_for(client, job_id, timeout_s=180):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.3)
    raise AssertionError(f"job {job_id} did not finish in {timeout_s} s")


def submit_rows(client, rows, files=(), params="{}"):
    """rows: [{file: 序号 | sha256, name, reference_length_mm, mm_per_pt}]；files: [(文件名, bytes)]"""
    parts = [("files", (name, data, "application/pdf")) for name, data in files]
    return client.post("/api/jobs", files=parts or None, data={"rows": json.dumps(rows), "params": params})


def submit(client, pdf_paths, refs, names=None, params="{}", mm_per_pt=None, filenames=None):
    rows, files = [], []
    for i, p in enumerate(pdf_paths):
        rows.append(dict(file=i, name=(names or [""] * len(pdf_paths))[i],
                         reference_length_mm="" if refs[i] is None else str(refs[i]),
                         mm_per_pt=(mm_per_pt or [""] * len(pdf_paths))[i]))
        files.append(((filenames or [None] * len(pdf_paths))[i] or os.path.basename(p), open(p, "rb").read()))
    return submit_rows(client, rows, files, params)
