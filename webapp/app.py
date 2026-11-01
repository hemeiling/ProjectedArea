"""
产线布局投影面积估算 —— 网页版（FastAPI）。

    python run_web.py                                   # 本机：http://127.0.0.1:8100
    uvicorn --factory webapp.app:create_app             # 部署（只能 1 个 worker：任务状态在进程内）

前端只负责上传与展示；比例尺、面积、工时、箱数、运费全部由后端（与命令行同一份代码）计算。
上传的 PDF 只保存在服务器私有目录（图纸库），任何接口都不返回其路径或原文件。

环境变量
    LAYOUT_DATA_DIR   数据目录（图纸库、缓存、任务），默认 ./data；部署时应指向持久磁盘
    ACCESS_PASSWORD   设置后整个网站需要密码（浏览器弹窗登录，用户名任意）；/api/health 除外
"""
import base64
import binascii
import datetime as dt
import json
import os
import secrets
import threading
from typing import List, Optional

from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from webapp import jobs as J
from webapp.library import Library
from webapp.messages import msg
import estimate

MAX_DRAWINGS = 10
MAX_FILE_MB = 100
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WORKBOOK_NAME = "投影面积与装柜估算"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NO_CACHE = {"Cache-Control": "no-store"}
IMAGE_KINDS = {"overlay": ("results", "{id}_overlay.png"), "stations": ("results", "{id}_stations.png"),
               "dimension": ("dim", "{id}.png")}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message["en"])
        self.status, self.message = status, message


def _positive(text):
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 and v == v and v != float("inf") else None


def _optional_positive(value, code, fname, upper=None):
    """空 -> None；否则必须是正数（且不超过 upper）"""
    if value is None or str(value).strip() == "":
        return None
    v = _positive(value)
    if v is None or (upper and v > upper):
        raise ApiError(400, msg(code, file=fname))
    return v


async def _read_limited(upload, max_mb):
    """读上传文件；超过上限返回 None（不把超大文件整个读进内存）"""
    chunks, size, limit = [], 0, max_mb * 1024 * 1024
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)


def _job_view(job):
    """任务 -> 界面数据，附上图片与工作簿的下载地址"""
    base = f"/api/jobs/{job['id']}"
    view = dict(job)
    if job["status"] == "done":
        view["workbook_url"] = f"{base}/workbook"
        for d in view["results"] or []:
            d["overlay_url"] = f"{base}/images/{d['id']}/overlay" if d["has_overlay"] else None
            d["station_overlay_url"] = f"{base}/images/{d['id']}/stations" if d["has_station_overlay"] else None
    for n in view.get("needs_dimension") or []:
        n["image_url"] = f"{base}/images/{n['id']}/dimension"
    return view


def _library_view(m):
    keep = ("sha256", "name", "filenames", "size_bytes", "first_uploaded", "last_used", "reference_length_mm",
            "mm_per_pt", "reference_source", "footprint_m2", "uses")
    return dict({k: m.get(k) for k in keep}, last_job=(m.get("jobs") or [None])[-1])


def _authorized(request, password):
    """HTTP Basic：只校验密码（用户名任意），常量时间比较"""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    try:
        _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(given.encode("utf-8"), password.encode("utf-8"))


def create_app(data_dir=None):
    data_dir = data_dir or os.environ.get("LAYOUT_DATA_DIR") or os.path.join(J.ESTIMATOR_DIR, "data")
    store, lib = J.JobStore(data_dir), Library(data_dir)
    worker = J.Worker(store, lib)
    recalc_lock = threading.Lock()
    password = os.environ.get("ACCESS_PASSWORD", "")
    app = FastAPI(title="产线布局投影面积估算 / Layout Footprint Estimator", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def _require_password(request: Request, call_next):
        if password and request.url.path != "/api/health" and not _authorized(request, password):
            return JSONResponse(status_code=401, content=dict(error=msg("unauthorized")),
                                headers={"WWW-Authenticate": 'Basic realm="Layout Estimator", charset="UTF-8"'})
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def _api_error(_, e):
        return JSONResponse(status_code=e.status, content=dict(error=e.message))

    def _job_or_404(jid):
        job = store.get(jid)
        if not job:
            raise ApiError(404, msg("job_not_found"))
        return job

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"), headers=NO_CACHE)

    @app.get("/api/health")
    def health():
        return dict(ok=True, ocr_available=J.ocr_available())

    @app.get("/api/meta")
    def meta():
        cfg = J.load_base_config()
        fr = cfg["freight"]
        return dict(ocr_available=J.ocr_available(), params=estimate.read_params(cfg),
                    labor_source=cfg["labor"].get("source", ""),
                    routes=[dict(name=k, usd_per_40ft=v) for k, v in fr["rates_usd_per_40ft"].items()],
                    rates_source=fr["rates_source"], container=cfg["packing"]["container"],
                    limits=dict(max_drawings=MAX_DRAWINGS, max_file_mb=MAX_FILE_MB))

    # ------------------------------------------------------------ 图纸库
    @app.get("/api/library")
    def library():
        return [_library_view(m) for m in lib.entries()]

    @app.get("/api/library/{sha}")
    def library_entry(sha: str):
        m = lib.get(sha)
        if not m:
            raise ApiError(404, msg("library_missing"))
        return _library_view(m)

    # ------------------------------------------------------------ 估算任务
    @app.post("/api/jobs", status_code=202)
    async def create_job(rows: str = Form(""), files: Optional[List[UploadFile]] = File(None),
                         params: str = Form("{}")):
        """rows：JSON 列表，每张图纸一项 {file: 上传文件序号 | sha256: 图纸库中的文件, name, reference_length_mm, mm_per_pt}；
        标注总尺寸可留空（沿用该文件上次的值，或在任务中请用户填写）。"""
        try:
            rows = json.loads(rows or "[]")
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ValueError
        except ValueError:
            raise ApiError(400, msg("bad_rows"))
        files = files or []
        if not rows:
            raise ApiError(400, msg("no_files"))
        if len(rows) > MAX_DRAWINGS:
            raise ApiError(400, msg("too_many_files", max=MAX_DRAWINGS))
        try:
            p = estimate.validate_params(json.loads(params or "{}"), J.load_base_config())
        except (ValueError, TypeError, AttributeError) as e:
            raise ApiError(400, msg("bad_params", detail=str(e)))

        uploads = []                                      # 先全部校验，再存入图纸库
        for i, f in enumerate(files):
            fname = os.path.basename(f.filename or f"drawing{i + 1}.pdf")[:200]
            data = await _read_limited(f, MAX_FILE_MB)
            if data is None:
                raise ApiError(413, msg("file_too_large", file=fname, max_mb=MAX_FILE_MB))
            if b"%PDF-" not in data[:1024]:
                raise ApiError(400, msg("not_pdf", file=fname))
            uploads.append((fname, data))

        out = []
        for r in rows:
            fi = r.get("file")
            if fi is not None:
                if not isinstance(fi, int) or isinstance(fi, bool) or not 0 <= fi < len(uploads):
                    raise ApiError(400, msg("bad_rows"))
                fname, data = uploads[fi]
                meta, size = None, len(data)
            else:
                meta = lib.get(str(r.get("sha256") or ""))
                if not meta:
                    raise ApiError(400, msg("library_missing"))
                fname, size = meta["filenames"][-1], meta["size_bytes"]
            ref = _optional_positive(r.get("reference_length_mm"), "bad_reference_length", fname, upper=1e7)
            mpp = _optional_positive(r.get("mm_per_pt"), "bad_mm_per_pt", fname)
            out.append(dict(file=fi, meta=meta, filename=fname, size_bytes=size, ref=ref, mpp=mpp,
                            name=str(r.get("name") or "").strip()[:120]))
        job_rows = []
        for r in out:
            if r["file"] is not None:
                sha, _ = lib.add(uploads[r["file"]][1], r["filename"])
                r["meta"] = lib.get(sha)
            sha = r["meta"]["sha256"]
            job_rows.append(dict(sha256=sha, filename=r["filename"], size_bytes=r["size_bytes"],
                                 name=r["name"] or r["meta"].get("name") or "",
                                 reference_length_mm=r["ref"], mm_per_pt=r["mpp"]))
        job = J.new_job(store, job_rows, p)
        worker.submit(job["id"])
        return dict(job_id=job["id"])

    @app.get("/api/jobs")
    def list_jobs():
        out = []
        for j in store.recent():
            fine = j["estimate"]["total"]["fine_m2"] if j.get("estimate") else None
            out.append(dict(id=j["id"], created_at=j["created_at"], status=j["status"],
                            drawings=[d["name"] for d in j["drawings"]], fine_m2=fine))
        return out

    @app.get("/api/jobs/{jid}")
    def get_job(jid: str):
        return JSONResponse(_job_view(_job_or_404(jid)), headers=NO_CACHE)

    @app.get("/api/jobs/{jid}/images/{did}/{kind}")
    def image(jid: str, did: str, kind: str):
        job = _job_or_404(jid)
        if kind not in IMAGE_KINDS or did not in {d["id"] for d in job["drawings"]}:
            raise ApiError(404, msg("job_not_found"))
        sub, pattern = IMAGE_KINDS[kind]
        path = os.path.join(store.dir(jid), sub, pattern.format(id=did))
        if not os.path.exists(path):
            raise ApiError(404, msg("job_not_ready"))
        return FileResponse(path, media_type="image/png")

    @app.get("/api/jobs/{jid}/workbook")
    def workbook(jid: str):
        job = _job_or_404(jid)
        path = os.path.join(store.dir(jid), "workbook.xlsx")
        if job["status"] != "done" or not os.path.exists(path):
            raise ApiError(409, msg("job_not_ready"))
        day = dt.datetime.fromisoformat(job["created_at"]).strftime("%Y%m%d")
        return FileResponse(path, media_type=XLSX, filename=f"{WORKBOOK_NAME}_{day}.xlsx", headers=NO_CACHE)

    @app.post("/api/jobs/{jid}/estimate")
    def recalculate(jid: str, payload: dict = Body(...)):
        job = _job_or_404(jid)
        if job["status"] != "done":
            raise ApiError(409, msg("job_not_ready"))
        try:
            p = estimate.validate_params(payload.get("params") or {}, J.load_base_config())
        except (ValueError, TypeError, AttributeError) as e:
            raise ApiError(400, msg("bad_params", detail=str(e)))
        with recalc_lock:
            try:
                job = J.recalculate(store, lib, jid, p)
            except J.Failed as e:
                raise ApiError(500, e.message)
        return JSONResponse(_job_view(job), headers=NO_CACHE)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
