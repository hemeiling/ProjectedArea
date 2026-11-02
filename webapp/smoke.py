"""
合成图纸 + 桌面版自检。

    python desktop.py --smoke-test          # 从源代码
    LayoutEstimator --smoke-test            # 打包后的程序（CI 用来检查安装包能真正算出面积）

在临时目录中：生成一张已知面积的合成矢量图纸 → 启动本机服务 → 上传并估算 → 检查面积、Excel、页面。
测量和生成工作簿都走子进程，所以也检查了打包后 “程序 --run-script” 的调用方式。不需要窗口。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

import pymupdf

# 合成图纸：100 mm/pt。尺寸线 600 pt = 60000 mm；厂房边界 600 × 400 pt；三台设备（纯描边矩形）
SCALE_MM_PER_PT = 100.0
REFERENCE_MM = 60000
EQUIPMENT = [  # (x0, y0, x1, y1) pt，面积 = 宽 × 高 × 0.01 m²/pt²
    (200, 200, 250, 220),   # 5 × 2 m = 10 m²
    (400, 300, 430, 330),   # 3 × 3 m = 9 m²
    (550, 150, 590, 190),   # 4 × 4 m = 16 m²，内部另有一个 2 × 2 m 的细节框
]
EQUIPMENT_M2 = 35.0
TOLERANCE = 1.07            # 栅格边缘约半个像素：小设备面积偏大几个百分点


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


def _post_job(url, pdf_path):
    boundary = uuid.uuid4().hex
    rows = json.dumps([dict(file=0, name="smoke", reference_length_mm=str(REFERENCE_MM), mm_per_pt="")])
    with open(pdf_path, "rb") as f:
        pdf = f.read()
    body = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="rows"\r\n\r\n{rows}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="params"\r\n\r\n{{}}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="smoke.pdf"\r\n'
        f'Content-Type: application/pdf\r\n\r\n'.encode(), pdf, f"\r\n--{boundary}--\r\n".encode()])
    req = urllib.request.Request(url + "/api/jobs", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["job_id"]


def _get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _tesseract_version(base_dir):
    """找得到 Tesseract 时，用估算子进程同样的环境运行一次 --version"""
    from webapp.ocr import child_env, find_tesseract
    cmd = find_tesseract(base_dir)
    if not cmd:
        return None
    try:
        r = subprocess.run([cmd, "--version"], env=child_env(dict(os.environ), base_dir), capture_output=True,
                           text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        return None
    out = (r.stdout or r.stderr).strip().splitlines()
    return out[0] if r.returncode == 0 and out else None


def main(timeout_s=600):
    from webapp import jobs as J
    from webapp.desktop import start_server

    tmp = tempfile.mkdtemp(prefix="layout-smoke-")
    report = dict(frozen=bool(getattr(sys, "frozen", False)), ocr_available=J.ocr_available(),
                  tesseract=_tesseract_version(J.ESTIMATOR_DIR))
    try:
        server, url = start_server(os.path.join(tmp, "data"))
        report["page_ok"] = b"<html" in _get(url + "/").lower()
        jid = _post_job(url, make_layout_pdf(os.path.join(tmp, "smoke.pdf")))
        deadline = time.monotonic() + timeout_s
        while True:
            job = json.loads(_get(f"{url}/api/jobs/{jid}"))
            if job["status"] in ("done", "failed") or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        report["status"] = job["status"]
        if job["status"] == "done":
            area = job["results"][0]["footprint_m2"]
            report.update(footprint_m2=area, expected_m2=EQUIPMENT_M2,
                          area_ok=EQUIPMENT_M2 <= area <= EQUIPMENT_M2 * TOLERANCE,
                          workbook_ok=_get(url + job["workbook_url"])[:2] == b"PK")
        else:
            report["error"] = job.get("error")
        server.should_exit = True
    finally:
        J.stop_children()
        shutil.rmtree(tmp, ignore_errors=True)
    ok = report.get("page_ok") and report.get("area_ok") and report.get("workbook_ok")
    if os.environ.get("SMOKE_EXPECT_OCR"):              # 附带了 Tesseract 的安装包：必须能找到并运行
        ok = ok and bool(report["tesseract"])
    report["ok"] = bool(ok)
    print(json.dumps(report, ensure_ascii=False, indent=1), flush=True)
    return 0 if ok else 1
