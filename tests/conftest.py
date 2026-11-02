"""测试夹具：合成的矢量 PDF（不依赖任何客户图纸）+ 指向临时目录的网页应用。"""
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 合成图纸（与桌面版自检共用）：100 mm/pt，三台设备合计 35 m²
from webapp.smoke import EQUIPMENT_M2, REFERENCE_MM, SCALE_MM_PER_PT, make_layout_pdf  # noqa: E402,F401


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
