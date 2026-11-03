"""
产线布局投影面积估算 —— 网页版（FastAPI）。

    python run_web.py                                   # 本机：http://127.0.0.1:8100
    uvicorn --factory webapp.app:create_app             # 部署（只能 1 个 worker：任务状态在进程内）

前端只负责上传与展示；比例尺、单位、面积、工时、箱数、运费全部由后端（与命令行同一份代码）计算。
上传的 PDF / DXF 只保存在服务器私有目录（图纸库），任何接口都不返回其路径或原文件。

环境变量
    LAYOUT_DATA_DIR    数据目录（图纸库、缓存、任务），默认 ./data；部署时应指向持久磁盘
    ACCESS_PASSWORD    设置后整个网站需要密码（浏览器弹窗登录，用户名任意）；/api/health 除外
    LAYOUT_MAX_DXF_MB  单个 DXF 的上限，默认 2048 MB。测量时内存峰值约为文件大小的 7–25 倍
                       （README 中 4 张样例图实测），服务器内存小时应调低
"""
import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import secrets
import threading
from typing import List, Optional

import yaml
from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from webapp import dxfinfo
from webapp import jobs as J
from webapp.library import Library
from webapp.messages import msg
import estimate

MAX_DRAWINGS = 30
MAX_FILE_MB = 100            # 单个 PDF
MAX_DXF_MB = int(os.environ.get("LAYOUT_MAX_DXF_MB", 2048))
MAX_UPLOAD_MB = 4096         # 一次上传的总量：边收边写入磁盘（不占内存），上限只为保护磁盘
UPLOAD_CHUNK = 1024 * 1024
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WORKBOOK_NAME = "投影面积与装柜估算"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NO_CACHE = {"Cache-Control": "no-store"}
IMAGE_KINDS = {"overlay": ("results", "{id}_overlay.png"), "stations": ("results", "{id}_stations.png"),
               "dimension": ("dim", "{id}.png")}


class RevalidatedStatic(StaticFiles):
    """静态文件（脚本、样式）每次都向服务器确认是否有新版本：未改动时 304、不重传；
    程序更新后浏览器不会继续用缓存里的旧脚本去配新的接口。"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


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


def _dxf_unit(value, fname):
    """DXF 单位（每个图纸单位等于多少 mm）：空 -> None；否则必须是界面给出的选项之一"""
    if value is None or str(value).strip() == "":
        return None
    v = _positive(value)
    if v is None or v not in dxfinfo.UNIT_CHOICES_MM.values():
        raise ApiError(400, msg("bad_dxf_unit", file=fname))
    return v


async def _stage_upload(upload, lib, fname):
    """边收边写入图纸库目录并计算 SHA-256，不把文件读进内存；按文件开头的内容判断类型（不看扩展名），
    并按类型检查大小上限。返回 dict(path, sha256, size, kind)；出错时删除临时文件。"""
    tmp, h, size, kind, limit = lib.staging_path(), hashlib.sha256(), 0, None, None
    try:
        with open(tmp, "wb") as f:
            while True:
                chunk = await upload.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                if kind is None:
                    kind = dxfinfo.sniff(chunk)
                    if kind == dxfinfo.DWG:
                        raise ApiError(400, msg("dwg_not_supported", file=fname))
                    if kind is None:
                        raise ApiError(400, msg("unsupported_file", file=fname))
                    limit = MAX_DXF_MB if kind == dxfinfo.DXF else MAX_FILE_MB
                size += len(chunk)
                if size > limit * 1024 * 1024:
                    raise ApiError(413, msg("file_too_large", file=fname, max_mb=limit))
                h.update(chunk)
                f.write(chunk)
        if kind is None:                                   # 空文件
            raise ApiError(400, msg("unsupported_file", file=fname))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return dict(path=tmp, sha256=h.hexdigest(), size=size, kind=kind)


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
    keep = ("sha256", "kind", "name", "filenames", "size_bytes", "first_uploaded", "last_used", "reference_length_mm",
            "mm_per_pt", "reference_source", "dxf_unit_mm", "unit_source", "footprint_m2", "uses")
    return dict({k: m.get(k) for k in keep}, last_job=(m.get("jobs") or [None])[-1])


def _observed(jobs):
    """本服务器最近的实测：测量子进程的内存峰值与每张图用时（决定服务器规格）"""
    runs = [j["resources"]["measure"] for j in jobs
            if (j.get("resources") or {}).get("measure") and j["resources"]["measure"].get("peak_rss_mb")]
    if not runs:
        return None
    per_drawing = sorted(r["seconds"] / max(r["drawings"], 1) for r in runs)
    return dict(runs=len(runs), peak_rss_mb=max(r["peak_rss_mb"] for r in runs),
                seconds_per_drawing=per_drawing[len(per_drawing) // 2])


def _storage(data_dir):
    """数据目录各部分的实际占用 (MB)"""
    out = {}
    for part in ("library", "cache", "jobs"):
        total, files = 0, 0
        for root, _, names in os.walk(os.path.join(data_dir, part)):
            for n in names:
                total += os.path.getsize(os.path.join(root, n))
                files += n.endswith((".pdf", ".dxf")) and part == "library"
        out[part] = round(total / 1048576, 1)
        if part == "library":
            out["library_files"] = files
    return out


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


def create_app(data_dir=None, desktop=False):
    """desktop=True：桌面版（只监听本机，界面显示数据目录并把“服务器”称为“本机”）"""
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
                    labor_area_basis_source=cfg["labor"].get("area_basis_source", ""),
                    labor_area_bases=list(estimate.LABOR_AREA_BASES),
                    routes=[dict(name=k, usd_per_40ft=v) for k, v in fr["rates_usd_per_40ft"].items()],
                    rates_source=fr["rates_source"], container=cfg["packing"]["container"],
                    limits=dict(max_drawings=MAX_DRAWINGS, max_file_mb=MAX_FILE_MB, max_dxf_mb=MAX_DXF_MB),
                    dxf_units=dxfinfo.UNIT_CHOICES_MM)

    @app.get("/api/system")
    def system():
        """“工作流程与费用”：托管价格（参考）、本服务器实测资源、存储占用"""
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hosting.yaml"), encoding="utf-8") as f:
            hosting = yaml.safe_load(f)
        out = dict(hosting=hosting, observed=_observed(store.recent(200)), storage=_storage(data_dir),
                   ocr_available=J.ocr_available(), password_protected=bool(password), desktop=desktop)
        if desktop:                                       # 只在本机桌面版返回本机路径
            out["data_dir"] = os.path.abspath(data_dir)
        return out

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
        """rows：JSON 列表，每张图纸一项 {file: 上传文件序号 | sha256: 图纸库中的文件, name,
        reference_length_mm, mm_per_pt（PDF）, dxf_unit_mm（DXF，只在文件未声明单位时使用）}；
        标注总尺寸、单位可留空（沿用该文件上次的值，或在任务中请用户填写）。"""
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

        uploads, total = [], 0                            # 先全部收下并校验，再放入图纸库
        try:
            for i, f in enumerate(files):
                fname = os.path.basename(f.filename or f"drawing{i + 1}")[:200]
                uploads.append(dict(await _stage_upload(f, lib, fname), filename=fname))
                total += uploads[-1]["size"]
                if total > MAX_UPLOAD_MB * 1024 * 1024:
                    raise ApiError(413, msg("upload_too_large", max_mb=MAX_UPLOAD_MB))

            out = []
            for r in rows:
                fi = r.get("file")
                if fi is not None:
                    if not isinstance(fi, int) or isinstance(fi, bool) or not 0 <= fi < len(uploads):
                        raise ApiError(400, msg("bad_rows"))
                    up = uploads[fi]
                    meta, fname, size, kind = None, up["filename"], up["size"], up["kind"]
                else:
                    meta = lib.get(str(r.get("sha256") or ""))
                    if not meta:
                        raise ApiError(400, msg("library_missing"))
                    fname, size, kind = meta["filenames"][-1], meta["size_bytes"], meta["kind"]
                pdf = kind == dxfinfo.PDF
                ref = _optional_positive(r.get("reference_length_mm"), "bad_reference_length", fname, upper=1e7) if pdf else None
                mpp = _optional_positive(r.get("mm_per_pt"), "bad_mm_per_pt", fname) if pdf else None
                unit = None if pdf else _dxf_unit(r.get("dxf_unit_mm"), fname)
                out.append(dict(file=fi, meta=meta, filename=fname, size_bytes=size, kind=kind, ref=ref, mpp=mpp,
                                unit=unit, name=str(r.get("name") or "").strip()[:120]))
            job_rows = []
            for r in out:
                if r["file"] is not None:
                    up = uploads[r["file"]]
                    if not up.get("sha"):                  # 同一上传文件被多行引用时只放入一次
                        up["sha"], _ = lib.adopt(up["path"], up["sha256"], up["size"], up["kind"], up["filename"])
                    r["meta"] = lib.get(up["sha"])
                job_rows.append(dict(sha256=r["meta"]["sha256"], kind=r["kind"], filename=r["filename"],
                                     size_bytes=r["size_bytes"], name=r["name"] or r["meta"].get("name") or "",
                                     reference_length_mm=r["ref"], mm_per_pt=r["mpp"], dxf_unit_mm=r["unit"]))
        finally:
            for up in uploads:                             # 出错时、或没有被任何一行用到的上传文件
                if not up.get("sha") and os.path.exists(up["path"]):
                    os.remove(up["path"])
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

    app.mount("/static", RevalidatedStatic(directory=STATIC_DIR), name="static")
    return app
