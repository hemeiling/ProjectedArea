"""网页上传 DXF：类型按内容识别、单位来源（文件声明 / 用户选择 / 按哈希核对的记录 / 上次的值 / 停下来问）、
结果与工作簿、缓存复用、大小上限与临时文件清理。合成图纸，面积有解析解（每台设备 2 × 1 m）。"""
import io
import os

import pytest
from openpyxl import load_workbook

from conftest import submit_rows, wait_for
from test_dxf import make_doc
from webapp import app as webapp_app
from webapp import jobs as J
from webapp.library import sha256_bytes

MACHINES = 3


def dxf_bytes(tmp_path, units=4, name="301-line.dxf"):
    """三台 2 × 1 m 设备（同一图块插入三次，其中一台旋转 90°）"""
    doc = make_doc(units)
    msp = doc.modelspace()
    msp.add_blockref("MACHINE", (0, 0))
    msp.add_blockref("MACHINE", (5000, 0), dxfattribs=dict(rotation=90))
    msp.add_blockref("MACHINE", (10000, 0))
    msp.dxf.extmin, msp.dxf.extmax = (-1000, 0, 0), (12000, 2000, 0)     # 保存时写入文件头 $EXTMIN/$EXTMAX
    path = tmp_path / name
    doc.saveas(str(path))
    return path.read_bytes()


def upload(client, data, name="301-line.dxf", **row):
    return submit_rows(client, [dict(file=0, **row)], [(name, data)])


def staged_files(client):
    root = os.path.join(client.app_data_dir, "library")
    return [n for n in os.listdir(root) if n.startswith(".upload-")]


@pytest.fixture
def client_dir(client, tmp_path):
    client.app_data_dir = str(tmp_path / "data")
    return client


def test_dxf_with_declared_units_end_to_end(client_dir, tmp_path):
    client = client_dir
    data = dxf_bytes(tmp_path)
    r = upload(client, data, name="301-line.pdf")       # 扩展名不对也按内容识别为 DXF
    assert r.status_code == 202, r.text
    job = wait_for(client, r.json()["job_id"])
    assert job["status"] == "done", job

    d, res = job["drawings"][0], job["results"][0]
    assert d["kind"] == "dxf" and d["unit_source"] == "file" and d["dxf_unit_mm"] is None
    assert res["source"] == "dxf" and res["unit_mm"] == 1.0
    assert res["footprint_m2"] == pytest.approx(2.0 * MACHINES, rel=0.02)
    assert res["n_equipment"] == MACHINES and res["transform_check_failures"] == 0
    assert res["reference_length_mm"] is None and res["mm_per_pt"] is None
    codes = [w["code"] for w in job["warnings"]]
    assert "dxf_lanes" in codes and "dxf_no_stations" in codes and "no_stations" not in codes
    assert len(res["envelope"]["lanes"]) == 1 and res["envelope"]["gap_mm"] == 3000      # 一排设备 = 一条线体

    e = job["estimate"]["drawings"][0]
    assert e["fine_source"] == "footprint" and e["fine_m2"] == res["footprint_m2"]
    assert e["labor_area_basis"] == "baseline" and e["baseline_m2"] > e["fine_m2"]       # 整体外包含设备之间的空地

    assert client.get(res["overlay_url"]).content[:8] == b"\x89PNG\r\n\x1a\n"
    wb = load_workbook(io.BytesIO(client.get(job["workbook_url"]).content))
    assert wb["汇总"]["B6"].value == "DXF：1 单位 = 1 mm"
    assert wb["汇总"]["B9"].value.startswith("=IF(COUNTIF(")

    lib = client.get("/api/library").json()
    assert lib[0]["kind"] == "dxf" and lib[0]["sha256"] == sha256_bytes(data) and lib[0]["unit_source"] == "file"
    assert staged_files(client) == []

    # 按图块类型的包装方式：全部 MACHINE 可叠 2 层 → 包装后面积减半；Excel 多出『图块包装』页
    (g,) = e["block_groups"]
    assert (g["key"], g["count"]) == ("MACHINE", MACHINES) and g["area_m2"] == pytest.approx(res["footprint_m2"])
    r = client.post(f"/api/jobs/{job['id']}/estimate", json={"params": {"block_packing": {d["id"]: {"MACHINE": "stack2"}}}})
    assert r.status_code == 200, r.text
    e2 = r.json()["estimate"]["drawings"][0]
    assert e2["packing_source"] == "blocks" and e2["packed_m2"] == pytest.approx(e["packed_m2"] / 2)
    wb = load_workbook(io.BytesIO(client.get(job["workbook_url"]).content))
    assert "图块包装" in wb.sheetnames and wb["图块包装"]["B5"].value == "MACHINE" and wb["图块包装"]["E5"].value == "可叠 2 层"


def test_undeclared_units_ask_then_remember(client_dir, tmp_path):
    client = client_dir
    data = dxf_bytes(tmp_path, units=0)
    job = wait_for(client, upload(client, data).json()["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "dxf_units_needed"      # 不默认为 mm
    need = job["needs_units"][0]
    assert need["sha256"] == sha256_bytes(data) and need["extents"] == pytest.approx([13000, 2000])
    assert job["needs_dimension"] is None

    # 选择单位后，用图纸库中的同一文件继续（不再上传）
    r = submit_rows(client, [dict(sha256=need["sha256"], dxf_unit_mm="1")])
    job = wait_for(client, r.json()["job_id"])
    assert job["status"] == "done", job
    assert job["drawings"][0]["unit_source"] == "user"
    assert job["results"][0]["footprint_m2"] == pytest.approx(2.0 * MACHINES, rel=0.02)

    # 再次计算同一文件、不选单位：沿用上次的单位，并复用测量结果
    job = wait_for(client, submit_rows(client, [dict(sha256=need["sha256"])]).json()["job_id"])
    codes = [w["code"] for w in job["warnings"]]
    assert job["status"] == "done" and job["drawings"][0]["unit_source"] == "library"
    assert "dxf_unit_remembered" in codes and "reused_result" in codes


def test_reviewed_record_for_the_same_file_supplies_units_and_classification(client_dir, tmp_path, monkeypatch):
    client = client_dir
    data = dxf_bytes(tmp_path, units=0)
    base = J.load_base_config()
    base["drawings"].append(dict(id="301", dxf="drawings/301.dxf", sha256=sha256_bytes(data), dxf_unit_mm=1,
                                 dxf_unit_basis="测试：按设备尺寸核对", overrides={"MACHINE": {"class": "mobile", "reason": "测试"}}))
    monkeypatch.setattr(J, "load_base_config", lambda: base)
    job = wait_for(client, upload(client, data).json()["job_id"])
    assert job["status"] == "done", job
    d, res = job["drawings"][0], job["results"][0]
    assert d["unit_source"] == "reviewed" and "测试" in d["unit_basis"]
    assert res["footprint_m2"] == 0 and res["mobile_m2"] == pytest.approx(2.0 * MACHINES, rel=0.02)   # 人工分类生效
    codes = [w["code"] for w in job["warnings"]]
    assert "dxf_unit_reviewed" in codes and "dxf_set_aside" in codes and "dxf_no_footprint" in codes


def test_declared_units_win_over_a_chosen_unit(client_dir, tmp_path):
    job = wait_for(client_dir, upload(client_dir, dxf_bytes(tmp_path), dxf_unit_mm="1000").json()["job_id"])
    assert job["status"] == "done" and job["results"][0]["unit_mm"] == 1.0
    assert "dxf_unit_ignored" in [w["code"] for w in job["warnings"]]


@pytest.mark.parametrize("data, code", [
    (b"AC1032" + b"\x00" * 200, "dwg_not_supported"),
    (b"q'\x06\x98v\x14\x82I" * 100, "unsupported_file"),
    (b"", "unsupported_file"),
])
def test_rejects_dwg_and_unknown_files_without_leaving_files(client_dir, data, code):
    r = upload(client_dir, data, name="line.dxf")
    assert r.status_code == 400 and r.json()["error"]["code"] == code
    assert client_dir.get("/api/library").json() == [] and staged_files(client_dir) == []


def test_bad_unit_and_size_limit(client_dir, tmp_path, monkeypatch):
    data = dxf_bytes(tmp_path)
    r = upload(client_dir, data, dxf_unit_mm="3")
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_dxf_unit"
    monkeypatch.setattr(webapp_app, "MAX_DXF_MB", 0)
    r = upload(client_dir, data)
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"
    assert client_dir.get("/api/library").json() == [] and staged_files(client_dir) == []


def test_pdf_and_dxf_in_one_estimate(client_dir, tmp_path, layout_pdf):
    from conftest import REFERENCE_MM
    files = [("201-synthetic-line.pdf", open(layout_pdf, "rb").read()), ("301-line.dxf", dxf_bytes(tmp_path))]
    r = submit_rows(client_dir, [dict(file=0, reference_length_mm=str(REFERENCE_MM)), dict(file=1)], files)
    job = wait_for(client_dir, r.json()["job_id"])
    assert job["status"] == "done", job
    assert [d["source"] for d in job["results"]] == ["pdf", "dxf"]
    assert job["estimate"]["total"]["fine_m2"] == pytest.approx(sum(d["footprint_m2"] for d in job["results"]))
    assert job["resources"]["measure"]["drawings"] == 2
