"""网页版：上传 -> 运行估算程序 -> 结果 / 叠加图 / Excel。用合成矢量 PDF 走完整流程。"""
import base64
import io
import json
import os
import re
import string

import pytest
from openpyxl import load_workbook

from conftest import (EQUIPMENT_M2, REFERENCE_MM, ROOT, SCALE_MM_PER_PT, make_layout_pdf, submit, submit_rows,
                      wait_for)

PNG = b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------------------ 上传校验
def test_page_and_meta(client):
    assert "产线布局投影面积估算" in client.get("/").text
    meta = client.get("/api/meta").json()
    assert meta["ocr_available"] is False
    assert meta["params"]["shared_hours_per_m2"] == 0.52
    assert any(r["name"] == meta["params"]["route"] for r in meta["routes"])


def test_rejects_non_pdf_with_bilingual_message(client):
    # 加密软件处理过的文件：开头是随机字节，没有 %PDF
    r = submit_rows(client, [dict(file=0, reference_length_mm="104000")], [("103-V5.1.pdf", b"q'\x06\x98v\x14\x82I" * 100)])
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "not_pdf" and "103-V5.1.pdf" in err["zh"] and "103-V5.1.pdf" in err["en"]
    assert client.get("/api/library").json() == []                       # 无效文件不进图纸库


@pytest.mark.parametrize("row, code", [
    (dict(file=0, reference_length_mm="0"), "bad_reference_length"),
    (dict(file=0, reference_length_mm="abc"), "bad_reference_length"),
    (dict(file=0, reference_length_mm="60000", mm_per_pt="-3"), "bad_mm_per_pt"),
    (dict(file=3, reference_length_mm="60000"), "bad_rows"),
    (dict(file=True, reference_length_mm="60000"), "bad_rows"),
    (dict(sha256="0" * 64), "library_missing"),
    (dict(sha256="../../etc/passwd"), "library_missing"),
])
def test_rejects_bad_rows(client, layout_pdf, row, code):
    r = submit_rows(client, [row], [("a.pdf", open(layout_pdf, "rb").read())])
    assert r.status_code == 400 and r.json()["error"]["code"] == code


@pytest.mark.parametrize("data, code", [
    ({"rows": "[]"}, "no_files"),
    ({"rows": "not json"}, "bad_rows"),
    ({"rows": '{"file": 0}'}, "bad_rows"),
    ({"rows": '[{"file": 0}]', "params": '{"route": "火星"}'}, "bad_params"),
    ({"rows": '[{"file": 0}]', "params": "not json"}, "bad_params"),
])
def test_rejects_bad_form(client, layout_pdf, data, code):
    r = client.post("/api/jobs", files=[("files", ("a.pdf", open(layout_pdf, "rb").read(), "application/pdf"))], data=data)
    assert r.status_code == 400 and r.json()["error"]["code"] == code


# ------------------------------------------------------------------ 完整流程
def test_end_to_end(client, layout_pdf, tmp_path):
    r = submit(client, [layout_pdf], [REFERENCE_MM], names=["合成产线"])
    assert r.status_code == 202
    job = wait_for(client, r.json()["job_id"])
    assert job["status"] == "done", job

    d = job["results"][0]
    assert d["id"] == "201" and d["name"] == "合成产线"
    assert d["mm_per_pt"] == pytest.approx(SCALE_MM_PER_PT)
    # 栅格化：每条 1 px 描边 + 1 px 闭合膨胀，小设备每边多算约 25–37 mm（约 +4–6%）
    assert EQUIPMENT_M2 <= d["footprint_m2"] <= EQUIPMENT_M2 * 1.07
    assert d["n_groups"] == 3
    assert [w["code"] for w in job["warnings"]] == ["ocr_unavailable"]
    assert job["drawings"][0]["reference_source"] == "user" and job["drawings"][0]["reused"] is False

    e = job["estimate"]["drawings"][0]
    assert e["fine_source"] == "footprint" and e["fine_m2"] == d["footprint_m2"]
    assert e["hours_fine"] == pytest.approx(d["footprint_m2"] * 0.52)
    assert str(tmp_path) not in json.dumps(job, ensure_ascii=False)                       # 不暴露服务器路径

    img = client.get(d["overlay_url"])
    assert img.status_code == 200 and img.content[:8] == PNG

    wb_resp = client.get(job["workbook_url"])
    assert wb_resp.status_code == 200
    assert "filename*=utf-8''" in wb_resp.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(wb_resp.content))
    assert wb.sheetnames == ["汇总", "工位明细", "线体基准（总长×宽）", "参数", "方法说明"]
    fine_formula = wb["汇总"]["B9"].value
    assert fine_formula.startswith("=IF(COUNTIF(") and str(d["footprint_m2"]) in fine_formula   # 无工位时取整图面积
    assert wb["参数"]["B4"].value == 0.52                                                   # 工时系数来自 config

    listed = client.get("/api/jobs").json()
    assert listed[0]["id"] == job["id"] and listed[0]["fine_m2"] == pytest.approx(d["footprint_m2"])


def test_recalculate_rebuilds_workbook_without_remeasuring(client, layout_pdf):
    job = wait_for(client, submit(client, [layout_pdf], [REFERENCE_MM]).json()["job_id"])
    area = job["results"][0]["footprint_m2"]
    r = client.post(f"/api/jobs/{job['id']}/estimate", json={"params": {"shared_hours_per_m2": 1.0, "route": "上海 → 纽约"}})
    assert r.status_code == 200
    e = r.json()["estimate"]["drawings"][0]
    assert e["hours_fine"] == pytest.approx(area) and e["rate_usd_per_40ft"] == 9726
    assert r.json()["results"][0]["footprint_m2"] == area
    wb = load_workbook(io.BytesIO(client.get(job["workbook_url"]).content))
    assert wb["参数"]["B4"].value == 1.0 and wb["参数"]["B15"].value == "上海 → 纽约"

    bad = client.post(f"/api/jobs/{job['id']}/estimate", json={"params": {"oversize_share": 5}})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "bad_params"


def test_two_drawings_get_distinct_ids(client, tmp_path, layout_pdf):
    other = make_layout_pdf(str(tmp_path / "201-copy.pdf"))
    job = wait_for(client, submit(client, [layout_pdf, other], [REFERENCE_MM, REFERENCE_MM]).json()["job_id"])
    assert job["status"] == "done"
    assert [d["id"] for d in job["drawings"]] == ["201", "D2"]
    tot = job["estimate"]["total"]
    assert tot["fine_m2"] == pytest.approx(2 * job["results"][0]["footprint_m2"])


def test_missing_dimension_line_is_explained(client, tmp_path):
    pdf = make_layout_pdf(str(tmp_path / "no-dim.pdf"), with_dimension=False)
    job = wait_for(client, submit(client, [pdf], [REFERENCE_MM]).json()["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "scale_not_found"


def test_scanned_pdf_is_rejected_with_reason(client, tmp_path):
    import pymupdf
    doc = pymupdf.open(); doc.new_page().insert_text((72, 72), "scan")       # 只有文字，没有矢量线条
    path = str(tmp_path / "scan.pdf"); doc.save(path)
    job = wait_for(client, submit(client, [path], [REFERENCE_MM]).json()["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "no_vectors"


# ------------------------------------------------------------------ 图纸库：识别同一文件并复用结果
def test_same_file_is_recognised_and_reused(client, layout_pdf):
    first = wait_for(client, submit(client, [layout_pdf], [REFERENCE_MM], names=["合成产线"]).json()["job_id"])
    area = first["results"][0]["footprint_m2"]
    lib = client.get("/api/library").json()
    assert len(lib) == 1 and lib[0]["reference_length_mm"] == REFERENCE_MM and lib[0]["footprint_m2"] == area
    sha = lib[0]["sha256"]
    assert client.get(f"/api/library/{sha}").json()["name"] == "合成产线"

    # 同一内容、换个文件名、不填标注尺寸：识别为同一文件，沿用尺寸与名称，直接复用测量结果
    again = wait_for(client, submit(client, [layout_pdf], [None], filenames=["改名后的图纸.pdf"]).json()["job_id"])
    assert again["status"] == "done", again
    d = again["drawings"][0]
    assert d["sha256"] == sha and d["reused"] is True and d["reference_source"] == "library" and d["name"] == "合成产线"
    assert {"dimension_remembered", "reused_result"} <= {w["code"] for w in again["warnings"]}
    assert again["results"][0]["footprint_m2"] == area
    assert client.get(again["results"][0]["overlay_url"]).content[:8] == PNG
    entry = client.get(f"/api/library/{sha}").json()
    assert entry["uses"] == 2 and set(entry["filenames"]) == {"201-synthetic-line.pdf", "改名后的图纸.pdf"}

    # 只按哈希提交，不上传文件
    by_hash = wait_for(client, submit_rows(client, [dict(sha256=sha)]).json()["job_id"])
    assert by_hash["status"] == "done" and by_hash["drawings"][0]["reused"] is True


def test_changed_measurement_inputs_are_measured_again(client, layout_pdf):
    wait_for(client, submit(client, [layout_pdf], [REFERENCE_MM]).json()["job_id"])
    half = wait_for(client, submit(client, [layout_pdf], [REFERENCE_MM / 2]).json()["job_id"])
    assert half["drawings"][0]["reused"] is False
    assert half["results"][0]["mm_per_pt"] == pytest.approx(SCALE_MM_PER_PT / 2)
    assert client.get("/api/library").json()[0]["reference_length_mm"] == REFERENCE_MM / 2   # 记住最新输入


def test_cache_key_depends_on_inputs_and_code():
    from webapp.library import Library
    base = dict(sha="a" * 64, reference_length_mm=60000, mm_per_pt=None, extraction_cfg={"x": 1}, code_ver="v1")
    k = Library.cache_key(**base)
    assert k == Library.cache_key(**dict(base, reference_length_mm=60000.0))
    for change in (dict(sha="b" * 64), dict(reference_length_mm=60001), dict(mm_per_pt=50),
                   dict(extraction_cfg={"x": 2}), dict(code_ver="v2")):
        assert Library.cache_key(**dict(base, **change)) != k, change


def test_unknown_dimension_asks_then_continues(client, layout_pdf):
    job = wait_for(client, submit(client, [layout_pdf], [None]).json()["job_id"])
    assert job["status"] == "failed" and job["error"]["code"] == "dimension_needed"
    need = job["needs_dimension"][0]
    assert need["orientation"] == "horizontal"
    assert client.get(need["image_url"]).content[:8] == PNG
    cont = wait_for(client, submit_rows(client, [dict(sha256=need["sha256"], reference_length_mm=REFERENCE_MM)]).json()["job_id"])
    assert cont["status"] == "done" and cont["results"][0]["mm_per_pt"] == pytest.approx(SCALE_MM_PER_PT)


# ------------------------------------------------------------------ 接口安全
def test_unknown_or_malformed_ids_are_404(client, layout_pdf):
    assert client.get("/api/jobs/" + "0" * 32).status_code == 404
    assert client.get("/api/jobs/not-a-job").status_code == 404
    assert client.get("/api/library/" + "0" * 64).status_code == 404
    job = wait_for(client, submit(client, [layout_pdf], [REFERENCE_MM]).json()["job_id"])
    base = f"/api/jobs/{job['id']}/images"
    assert client.get(f"{base}/201/input").status_code == 404
    assert client.get(f"{base}/..%2F..%2Flibrary/overlay").status_code == 404
    assert client.get(f"{base}/D9/overlay").status_code == 404


def test_access_password(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from webapp.app import create_app
    monkeypatch.setenv("ACCESS_PASSWORD", "s3cret")
    with TestClient(create_app(str(tmp_path / "data"))) as c:
        assert c.get("/api/health").status_code == 200                     # 平台健康检查不需要密码
        denied = c.get("/")
        assert denied.status_code == 401 and denied.headers["www-authenticate"].startswith("Basic")
        assert c.get("/api/library", headers={"Authorization": "Basic " + base64.b64encode(b"x:wrong").decode()}).status_code == 401
        ok = {"Authorization": "Basic " + base64.b64encode("任意:s3cret".encode()).decode()}
        assert c.get("/", headers=ok).status_code == 200 and c.get("/api/library", headers=ok).status_code == 200


def test_interrupted_jobs_are_marked_failed_on_restart(tmp_path):
    from webapp import jobs
    store = jobs.JobStore(str(tmp_path))
    row = dict(sha256="a" * 64, filename="a.pdf", size_bytes=8, name="a", reference_length_mm=1000.0, mm_per_pt=None)
    job = jobs.new_job(store, [row], {})
    store.update(job["id"], status="running")
    again = jobs.JobStore(str(tmp_path))
    assert again.get(job["id"])["status"] == "failed" and again.get(job["id"])["error"]["code"] == "interrupted"


def test_drawing_ids():
    from webapp.jobs import drawing_id
    assert drawing_id("101-SSY1070.pdf", set(), 1) == "101"
    assert drawing_id("101-other.pdf", {"101"}, 2) == "D2"
    assert drawing_id("产线图.pdf", set(), 3) == "D3"
    assert drawing_id("x.pdf", {"x", "D4"}, 4) == "D4x2"


# ------------------------------------------------------------------ 双语完整性
def _placeholders(s):
    return {f for _, f, _, _ in string.Formatter().parse(s) if f}


def test_messages_are_bilingual_with_same_placeholders():
    from webapp.messages import MESSAGES
    for code, (zh, en) in MESSAGES.items():
        assert zh and en, code
        assert _placeholders(zh) == _placeholders(en), code


def test_ui_strings_exist_in_both_languages():
    src = open(os.path.join(ROOT, "webapp", "static", "i18n.js"), encoding="utf-8").read()
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("//"))
    i18n = json.loads(body[body.index("{"): body.rindex("}") + 1])
    assert set(i18n["zh"]) == set(i18n["en"])
    for k in i18n["zh"]:
        assert set(re.findall(r"{\w+}", i18n["zh"][k])) == set(re.findall(r"{\w+}", i18n["en"][k])), k
    # 页面与脚本里直接引用的每个键都必须存在
    js = open(os.path.join(ROOT, "webapp", "static", "app.js"), encoding="utf-8").read()
    used = set(re.findall(r'data-i18n(?:-aria)?="([\w.]+)"', open(os.path.join(ROOT, "webapp", "static", "index.html"), encoding="utf-8").read()))
    used |= set(re.findall(r'\bt\("([\w.]+)"', js))
    # 流程图的步骤、阶段、运行位置是拼出来的键
    flow = js[js.index("const FLOW"): js.index("];", js.index("const FLOW"))]
    for k in re.findall(r'k: "(\w+)"', flow):
        used |= {f"flow.{k}.t", f"flow.{k}.d"}
    for s in re.findall(r'stage: "(\w+)"', flow):
        used |= {f"stage.{s}", f"stage.{s}.d"}
    used |= {f"where.{w}" for w in re.findall(r'where: "(\w+)"', flow)}
    checks = re.search(r"\[([\d, ]+)\]\.map\(\(n\) => `<li>\$\{esc\(t\(`report\.check", js)
    assert checks, "report checklist not found"
    used |= {f"report.check{n.strip()}" for n in checks.group(1).split(",")}
    assert used <= set(i18n["zh"]), used - set(i18n["zh"])


def test_system_endpoint_reports_hosting_usage_and_measured_resources(client, layout_pdf):
    before = client.get("/api/system").json()
    assert before["observed"] is None and before["password_protected"] is False
    assert {o["key"] for o in before["hosting"]["options"]} >= {"local", "render-free", "render-2c-4g"}
    job = wait_for(client, submit(client, [layout_pdf], [REFERENCE_MM]).json()["job_id"])
    m = job["resources"]["measure"]
    assert m["drawings"] == 1 and m["seconds"] > 0 and job["resources"]["workbook"]["seconds"] > 0
    after = client.get("/api/system").json()
    if m["peak_rss_mb"] is not None:                                     # 有 wait4 的系统（macOS / Linux）
        assert after["observed"]["runs"] == 1 and after["observed"]["peak_rss_mb"] == m["peak_rss_mb"] > 50
    assert after["storage"]["library_files"] == 1 and after["storage"]["library"] >= 0     # 1.5 KB 合成文件按 MB 取整为 0
    # 再次计算同一文件：复用缓存，不测量
    again = wait_for(client, submit(client, [layout_pdf], [REFERENCE_MM]).json()["job_id"])
    assert again["resources"]["measure"] is None and again["resources"]["reused"] == 1


def test_upload_limits_are_explained(client, layout_pdf, monkeypatch):
    from webapp import app as A
    r = submit_rows(client, [dict(file=0)] * (A.MAX_DRAWINGS + 1))
    assert r.status_code == 400 and r.json()["error"]["code"] == "too_many_files"
    monkeypatch.setattr(A, "MAX_UPLOAD_MB", 0.001)                       # 合成图纸约 2 KB
    r = submit(client, [layout_pdf], [REFERENCE_MM])
    assert r.status_code == 413 and r.json()["error"]["code"] == "upload_too_large"
