"""
合成图纸 + 桌面版自检。

    python desktop.py --smoke-test          # 从源代码
    LayoutEstimator --smoke-test            # 打包后的程序（CI 用来检查安装包能真正算出面积）

在临时目录中：生成已知面积的合成 PDF 和 DXF 图纸 → 启动本机服务 → 一次上传两张并估算 → 检查面积、Excel、页面。
测量和生成工作簿都走子进程，所以也检查了打包后 “程序 --run-script” 的调用方式。
不打开窗口，但会加载窗口所需的库（macOS：Cocoa/WebKit；Windows：WinForms + WebView2），确认已打包。
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

import ezdxf
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

# 合成 DXF（文件声明 mm）：同一 2 × 1 m 设备图块插入三次，其中一台旋转 90°
DXF_MACHINE_MM = (2000.0, 1000.0)
DXF_EQUIPMENT_M2 = 6.0
DXF_TOLERANCE = 0.02        # 相对误差：轮廓按 DXF 方法栅格化后的边缘误差


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


def make_layout_dxf(path):
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4                                          # 毫米
    w, h = DXF_MACHINE_MM
    blk = doc.blocks.new("MACHINE")
    blk.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)
    blk.add_circle((w / 2, h / 2), 200)
    for k in range(1, 6):                                                # 内部细节线（只有外框的图形按待定处理）
        blk.add_line((w * k / 6, h * 0.2), (w * k / 6, h * 0.8))
    msp = doc.modelspace()
    for x, rotation in ((0, 0), (5000, 90), (10000, 0)):
        msp.add_blockref("MACHINE", (x, 0), dxfattribs=dict(rotation=rotation))
    msp.dxf.extmin, msp.dxf.extmax = (-1000, 0, 0), (12000, 2000, 0)     # 保存时写入文件头 $EXTMIN/$EXTMAX
    doc.saveas(path)
    return path


def _post_job(url, uploads):
    """uploads: [(文件名, 路径, 内容类型, 该行参数)]，一个任务提交多张图纸"""
    boundary = uuid.uuid4().hex
    rows = json.dumps([dict(file=i, **row) for i, (_, _, _, row) in enumerate(uploads)])
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="rows"\r\n\r\n{rows}\r\n'.encode(),
             f'--{boundary}\r\nContent-Disposition: form-data; name="params"\r\n\r\n{{}}\r\n'.encode()]
    for name, path, content_type, _ in uploads:
        with open(path, "rb") as f:
            parts += [f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                      f'Content-Type: {content_type}\r\n\r\n'.encode(), f.read(), b"\r\n"]
    body = b"".join(parts + [f"--{boundary}--\r\n".encode()])
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


def _window_toolkit():
    """加载原生窗口所用的库（不打开窗口）：返回渲染引擎名，失败时返回 'error: …'"""
    try:
        if sys.platform == "darwin":
            from webview.platforms import cocoa as toolkit
        elif sys.platform == "win32":
            from webview.platforms import winforms as toolkit          # 同时加载 WebView2 程序集
        else:
            return "skipped"
    except Exception as e:  # noqa: BLE001 — 任何导入失败都说明安装包缺文件
        return f"error: {type(e).__name__}: {e}"
    return getattr(toolkit, "renderer", None) or "ok"


def main(timeout_s=600):
    from webapp import jobs as J
    from webapp.desktop import start_server

    tmp = tempfile.mkdtemp(prefix="layout-smoke-")
    report = dict(frozen=bool(getattr(sys, "frozen", False)), ocr_available=J.ocr_available(),
                  tesseract=_tesseract_version(J.ESTIMATOR_DIR), window_toolkit=_window_toolkit())
    try:
        server, url = start_server(os.path.join(tmp, "data"))
        report["page_ok"] = b"<html" in _get(url + "/").lower()
        jid = _post_job(url, [
            ("smoke.pdf", make_layout_pdf(os.path.join(tmp, "smoke.pdf")), "application/pdf",
             dict(name="smoke", reference_length_mm=str(REFERENCE_MM), mm_per_pt="")),
            ("smoke.dxf", make_layout_dxf(os.path.join(tmp, "smoke.dxf")), "application/octet-stream",
             dict(name="smoke-dxf")),
        ])
        deadline = time.monotonic() + timeout_s
        while True:
            job = json.loads(_get(f"{url}/api/jobs/{jid}"))
            if job["status"] in ("done", "failed") or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        report["status"] = job["status"]
        if job["status"] == "done":
            by_name = {r["name"]: r for r in job["results"]}
            area, dxf_area = by_name["smoke"]["footprint_m2"], by_name["smoke-dxf"]["footprint_m2"]
            report.update(footprint_m2=area, expected_m2=EQUIPMENT_M2,
                          area_ok=EQUIPMENT_M2 <= area <= EQUIPMENT_M2 * TOLERANCE,
                          dxf_footprint_m2=dxf_area, dxf_expected_m2=DXF_EQUIPMENT_M2,
                          dxf_area_ok=abs(dxf_area / DXF_EQUIPMENT_M2 - 1) <= DXF_TOLERANCE,
                          workbook_ok=_get(url + job["workbook_url"])[:2] == b"PK")
        else:
            report["error"] = job.get("error")
        server.should_exit = True
    finally:
        J.stop_children()
        shutil.rmtree(tmp, ignore_errors=True)
    ok = (report.get("page_ok") and report.get("area_ok") and report.get("dxf_area_ok") and report.get("workbook_ok")
          and not report["window_toolkit"].startswith("error"))
    if os.environ.get("SMOKE_EXPECT_OCR"):              # 附带了 Tesseract 的安装包：必须能找到并运行
        ok = ok and bool(report["tesseract"])
    report["ok"] = bool(ok)
    print(json.dumps(report, ensure_ascii=False, indent=1), flush=True)
    return 0 if ok else 1
