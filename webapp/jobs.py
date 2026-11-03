"""
网页任务：图纸库中的 PDF / DXF -> 运行 estimate_footprint.py（PDF）、dxf_footprint.py（DXF）
与 build_workbook.py -> 结果。

这些程序以子进程运行，与命令行完全相同（同一份代码、同一份 config 格式），网页只负责
准备输入和展示输出。一次只运行一个任务（单个后台线程）：一张大图测量时内存峰值可达数 GB。

同一文件（内容 SHA-256 相同）+ 同样的测量输入，直接复用图纸库中的测量缓存，不再重新测量；
PDF 未填写标注总尺寸时，沿用该文件上次的标注尺寸；从未填写过则停下来请用户填写（附提示图）。
DXF 单位：文件声明（$INSUNITS）> 用户上传时选择 > config.yaml 中按文件哈希核对过的记录 > 该文件上次的单位；
都没有则停下来请用户选择，绝不默认为 mm。按文件哈希核对过的人工分类（overrides）只用于同一文件。

每个任务一个私有目录 <data>/jobs/<id>/：
    config.yaml       本任务的配置（图纸 + 参数），可直接用于命令行复现
    results/          results.json、叠加图
    dim/              缺少标注尺寸时的提示图
    workbook.xlsx     估算工作簿
    job.json          任务状态（服务重启后仍可查看）
"""
import copy
import datetime as dt
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

import pymupdf
import yaml

from webapp import dxfinfo
from webapp.messages import msg
from webapp.library import DXF_CODE, code_version
from webapp.ocr import child_env, find_tesseract

ESTIMATOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ESTIMATOR_DIR not in sys.path:      # 打包后已在路径中；重复加到最前面会让 OpenCV 的加载器递归导入自身
    sys.path.insert(0, ESTIMATOR_DIR)
import estimate  # noqa: E402  (estimator 目录在上面加入路径)
from webapp import dimension  # noqa: E402

log = logging.getLogger("layout_estimator.web")

MEASURE_TIMEOUT_S = 30 * 60        # 含逐个标签 600 dpi OCR；大图 + 多图时可能需要数分钟
DXF_MEASURE_TIMEOUT_S = 60 * 60    # 几百 MB 的 DXF 每张 1–7 分钟（README 实测），一次多张时更久
DXF_ENGINE_WARNINGS_SHOWN = 3      # 每张图最多列出的测量程序提示（其余只计数）
_DXF_DONE = re.compile(r"^\[[^\]]+\] DXF ")    # dxf_footprint.py 每算完一张图打印一行 "[id] DXF ..."
WORKBOOK_TIMEOUT_S = 5 * 60
STDERR_TAIL = 2000                 # 失败时返回给界面的技术信息长度（字符）
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def ocr_available():
    """工位识别需要 Tesseract 程序（pytesseract 只是调用它）。每次任务前检查，装好后无需重启。"""
    return find_tesseract(ESTIMATOR_DIR) is not None


def script_cmd(name):
    """运行估算程序的命令。桌面版（PyInstaller 打包）里没有单独的 python，由程序自身带 --run-script 运行同一份脚本。"""
    path = os.path.join(ESTIMATOR_DIR, name)
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-script", path]
    return [sys.executable, path]


_children = set()          # 正在运行的估算子进程：桌面版关闭窗口时一并结束


def stop_children():
    for proc in list(_children):
        proc.kill()


def load_base_config():
    with open(os.path.join(ESTIMATOR_DIR, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def drawing_id(filename, taken, n):
    """图纸编号：取文件名开头的编号（如 101-xxx.pdf -> 101），否则 D1、D2…；只含字母数字，可安全用作文件名。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = re.match(r"([A-Za-z0-9]{1,12})(?=[-_ .]|$)", stem)
    cand = m.group(1) if m else f"D{n}"
    if cand in taken:
        cand = f"D{n}"
    k = 2
    while cand in taken:
        cand = f"D{n}x{k}"; k += 1
    return cand


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class JobStore:
    """任务状态：内存 + job.json。所有修改经由 update()，线程安全。"""

    def __init__(self, data_dir):
        self.root = os.path.join(data_dir, "jobs")
        os.makedirs(self.root, exist_ok=True)
        self._jobs, self._lock = {}, threading.Lock()
        for jid in os.listdir(self.root):
            p = os.path.join(self.root, jid, "job.json")
            if JOB_ID_RE.match(jid) and os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    job = json.load(f)
                if job["status"] in ("queued", "running"):        # 上次运行中途服务重启
                    job.update(status="failed", stage="failed", error=msg("interrupted"))
                    self._write(job)
                self._jobs[jid] = job

    def dir(self, jid):
        return os.path.join(self.root, jid)

    def _write(self, job):
        p = os.path.join(self.dir(job["id"]), "job.json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)

    def create(self, job):
        with self._lock:
            os.makedirs(self.dir(job["id"]), exist_ok=True)
            self._jobs[job["id"]] = job
            self._write(job)

    def get(self, jid):
        with self._lock:
            job = self._jobs.get(jid) if JOB_ID_RE.match(jid or "") else None
            return copy.deepcopy(job) if job else None

    def update(self, jid, **fields):
        with self._lock:
            job = self._jobs[jid]
            job.update(fields, updated_at=_now())
            self._write(job)
            return copy.deepcopy(job)

    def recent(self, n=20):
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)[:n]
            return copy.deepcopy(jobs)


def new_job(store, rows, params):
    """rows: [dict(sha256, filename, size_bytes, name, reference_length_mm 或 None, mm_per_pt 或 None)]，
    文件均已在图纸库中，数值均已校验。"""
    jid = uuid.uuid4().hex
    drawings, taken = [], set()
    for n, r in enumerate(rows, 1):
        did = drawing_id(r["filename"], taken, n); taken.add(did)
        drawings.append(dict(id=did, kind=r.get("kind", "pdf"), name=r["name"] or os.path.splitext(r["filename"])[0],
                             filename=r["filename"], sha256=r["sha256"], size_bytes=r["size_bytes"],
                             reference_length_mm=r["reference_length_mm"], mm_per_pt=r["mm_per_pt"],
                             reference_source="user" if r["reference_length_mm"] else None,
                             dxf_unit_mm=r.get("dxf_unit_mm"), unit_source="user" if r.get("dxf_unit_mm") else None,
                             reused=False))
    job = dict(id=jid, created_at=_now(), updated_at=_now(), status="queued", stage="queued",
               progress=dict(done=0, total=len(drawings)), drawings=drawings, params=params,
               ocr=None, warnings=[], error=None, detail=None, needs_dimension=None, needs_units=None,
               results=None, estimate=None)
    store.create(job)
    return job


# ------------------------------------------------------------------ 执行
class Failed(Exception):
    def __init__(self, message, detail=None, needs=None, needs_units=None):
        super().__init__(message["en"])
        self.message, self.detail, self.needs, self.needs_units = message, detail, needs, needs_units


def _is_dxf(d):
    return d.get("kind") == dxfinfo.DXF


def _job_config(store, lib, jid, cfg, drawings):
    """本任务（或其中待测量部分）的 config.yaml：图纸列表 + 参数；其余沿用 config.yaml"""
    cfg = copy.deepcopy(cfg)
    cfg["drawings"] = []
    for d in drawings:
        if _is_dxf(d):
            entry = dict(id=d["id"], name=d["name"], dxf=lib.file_path(d["sha256"], dxfinfo.DXF), sha256=d["sha256"])
            if d.get("dxf_unit_mm"):                       # 文件已声明单位时不写（以文件为准）
                entry.update(dxf_unit_mm=d["dxf_unit_mm"], dxf_unit_basis=d.get("unit_basis") or "")
            if d.get("overrides"):
                entry["overrides"] = d["overrides"]
        else:
            entry = dict(id=d["id"], name=d["name"], pdf=lib.pdf_path(d["sha256"]),
                         reference_length_mm=d["reference_length_mm"])
            if d.get("mm_per_pt"):
                entry["mm_per_pt"] = d["mm_per_pt"]
        cfg["drawings"].append(entry)
    path = os.path.join(store.dir(jid), "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return path


def _precheck(drawings, lib):
    """打开每个 PDF：能否读取、是否有矢量、页数；DXF 只读文件头（单位、范围）。返回提示列表；不可用时抛 Failed。"""
    warnings = []
    for d in drawings:
        if _is_dxf(d):
            try:
                d["header"] = dxfinfo.header_info(lib.file_path(d["sha256"], dxfinfo.DXF))
            except Exception as e:  # noqa: BLE001 — 任何读取失败都要原样告知用户
                raise Failed(msg("dxf_unreadable", file=d["filename"], detail=str(e)[:200]))
            continue
        try:
            with pymupdf.open(lib.pdf_path(d["sha256"])) as doc:
                n_paths, n_pages = len(doc[0].get_drawings()), doc.page_count
        except Exception as e:  # noqa: BLE001 — 任何打开失败都要原样告知用户
            raise Failed(msg("pdf_unreadable", file=d["filename"], detail=str(e)[:200]))
        if n_paths == 0:
            raise Failed(msg("no_vectors", file=d["filename"]))
        if n_pages > 1:
            warnings.append(msg("multi_page", file=d["filename"], pages=n_pages))
    return warnings


def _resolve_dimensions(store, lib, jid, drawings):
    """PDF 标注总尺寸：用户填写 > 该文件上次的值；都没有则生成提示图，返回待填写列表。"""
    warnings, needs = [], []
    for d in drawings:
        if _is_dxf(d) or d["reference_length_mm"]:
            continue
        meta = lib.get(d["sha256"]) or {}
        if meta.get("reference_length_mm"):
            d.update(reference_length_mm=meta["reference_length_mm"], reference_source="library",
                     mm_per_pt=d["mm_per_pt"] or meta.get("mm_per_pt"))
            warnings.append(msg("dimension_remembered", name=d["name"], value=f"{meta['reference_length_mm']:,.0f}",
                                when=(meta.get("last_used") or "")[:10]))
            continue
        os.makedirs(os.path.join(store.dir(jid), "dim"), exist_ok=True)
        info = dimension.preview(lib.pdf_path(d["sha256"]), os.path.join(store.dir(jid), "dim", f"{d['id']}.png"))
        needs.append(dict(id=d["id"], name=d["name"], filename=d["filename"], sha256=d["sha256"],
                          mm_per_pt=d["mm_per_pt"], orientation=info["orientation"]))
    return warnings, needs


def _unit_text(unit_mm):
    return f"1 = {unit_mm:g} mm"


def _resolve_units(lib, drawings, cfg):
    """DXF 单位与人工分类。单位：文件声明 > 用户选择 > 按文件哈希核对过的记录 > 该文件上次的值；
    都没有则返回待选择列表（附文件头中的图形范围作参考）。人工分类只取哈希相同的核对记录。"""
    warnings, needs = [], []
    reviewed = {e["sha256"]: e for e in cfg.get("drawings") or [] if e.get("dxf") and e.get("sha256")}
    for d in drawings:
        if not _is_dxf(d):
            continue
        h, rv = d["header"], reviewed.get(d["sha256"], {})
        d["overrides"] = rv.get("overrides") or None
        if h["units_mm"]:
            if d.get("dxf_unit_mm") and d["dxf_unit_mm"] != h["units_mm"]:
                warnings.append(msg("dxf_unit_ignored", name=d["name"], declared=h["units_name"]))
            d.update(dxf_unit_mm=None, unit_source="file", unit_basis=f"$INSUNITS={h['insunits']}（{h['units_name']}）")
        elif d.get("dxf_unit_mm"):
            d.update(unit_source="user", unit_basis="用户在网页上选择")
        elif rv.get("dxf_unit_mm"):
            d.update(dxf_unit_mm=float(rv["dxf_unit_mm"]), unit_source="reviewed", unit_basis=rv.get("dxf_unit_basis") or "")
            warnings.append(msg("dxf_unit_reviewed", name=d["name"], unit=_unit_text(d["dxf_unit_mm"]),
                                basis=d["unit_basis"] or "—"))
        else:
            meta = lib.get(d["sha256"]) or {}
            if meta.get("dxf_unit_mm"):
                d.update(dxf_unit_mm=meta["dxf_unit_mm"], unit_source="library", unit_basis=meta.get("unit_basis") or "")
                warnings.append(msg("dxf_unit_remembered", name=d["name"], unit=_unit_text(d["dxf_unit_mm"]),
                                    when=(meta.get("last_used") or "")[:10]))
            else:
                needs.append(dict(id=d["id"], name=d["name"], filename=d["filename"], sha256=d["sha256"],
                                  extents=h["extents"], release=h["release"]))
    return warnings, needs


def _resolve_inputs(store, lib, jid, drawings, cfg):
    """测量前必须确定的输入（PDF 标注总尺寸、DXF 单位）；缺少时一次列出全部，停下来请用户填写。"""
    dim_warn, dim_needs = _resolve_dimensions(store, lib, jid, drawings)
    unit_warn, unit_needs = _resolve_units(lib, drawings, cfg)
    if dim_needs or unit_needs:
        names = "、".join(n["name"] for n in dim_needs + unit_needs)
        code = "inputs_needed" if dim_needs and unit_needs else "dimension_needed" if dim_needs else "dxf_units_needed"
        raise Failed(msg(code, names=names), needs=dim_needs or None, needs_units=unit_needs or None)
    return dim_warn + unit_warn


def _reap(proc):
    """等待子进程结束；能取到时同时返回它的内存峰值 (MB)。Windows 没有 wait4，只记用时。"""
    if not hasattr(os, "wait4"):
        proc.wait()
        return None
    _, status, usage = os.wait4(proc.pid, 0)
    proc.returncode = os.waitstatus_to_exitcode(status)
    # ru_maxrss：macOS 单位是字节，Linux 是 KB
    return usage.ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)


def _run(cmd, timeout_s, on_line=None):
    """运行估算程序；逐行回调 stdout。返回 (退出码, stderr 末尾, 实测资源 {seconds, peak_rss_mb})。"""
    env = child_env(dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PYTHONUTF8="1"), ESTIMATOR_DIR)
    started = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=ESTIMATOR_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _children.add(proc)
    err_chunks, timed_out = [], threading.Event()
    t_err = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    t_err.start()

    def kill():
        timed_out.set(); proc.kill()

    timer = threading.Timer(timeout_s, kill); timer.start()
    try:
        for line in proc.stdout:
            log.info("%s", line.rstrip())
            if on_line:
                on_line(line)
        peak_mb = _reap(proc)
    finally:
        timer.cancel()
        _children.discard(proc)
    t_err.join(timeout=5)
    if timed_out.is_set():
        raise Failed(msg("timeout", minutes=timeout_s // 60))
    usage = dict(seconds=round(time.monotonic() - started, 1), peak_rss_mb=peak_mb and round(peak_mb))
    return proc.returncode, "".join(err_chunks)[-STDERR_TAIL:], usage


def _measure(store, jid, todo, cfg_path, done_before, total):
    """只测量 todo 中的图纸（同一类型）；返回 (results.json 的 drawings 列表, 实测资源)"""
    done, dxf = [0], _is_dxf(todo[0])

    def on_line(line):
        # estimate_footprint.py 每算完一张图打印一行 "[id] ..."；dxf_footprint.py 打印 "[id] DXF ..."（警告行不算）
        if (_DXF_DONE.match(line) if dxf else line.startswith("[")):
            done[0] += 1
            store.update(jid, progress=dict(done=done_before + done[0], total=total))

    out = os.path.join(store.dir(jid), "results")
    script, timeout = ("dxf_footprint.py", DXF_MEASURE_TIMEOUT_S) if dxf else ("estimate_footprint.py", MEASURE_TIMEOUT_S)
    code, err, usage = _run(script_cmd(script) + [cfg_path, "--out", out], timeout, on_line)
    if code != 0:
        d = todo[min(done[0], len(todo) - 1)]
        if dxf and (code < 0 or "MemoryError" in err):   # 被系统结束（多为内存不足）
            m = msg("dxf_killed", name=d["name"], size_mb=round(d["size_bytes"] / 1048576))
        elif dxf:
            m = msg("measure_failed", name=d["name"])
        elif "未找到尺寸线" in err:
            m = msg("scale_not_found", name=d["name"])
        elif "empty sequence" in err:
            m = msg("building_not_found", name=d["name"])
        else:
            m = msg("measure_failed", name=d["name"])
        raise Failed(m, err)
    with open(os.path.join(out, "results.json"), encoding="utf-8") as f:
        return json.load(f)["drawings"], dict(usage, drawings=len(todo))


def build_workbook(store, jid, cfg_path):
    """(重新)生成工作簿，返回实测资源。参数修改后只需重跑这一步，无需重新测量。"""
    jdir = store.dir(jid)
    code, err, usage = _run(script_cmd("build_workbook.py") + [cfg_path,
                      os.path.join(jdir, "results", "results.json"), os.path.join(jdir, "workbook.xlsx")],
                     WORKBOOK_TIMEOUT_S)
    if code != 0:
        raise Failed(msg("workbook_failed"), err)
    return usage


def _dxf_warnings(d):
    """DXF 结果中需要人工注意的地方"""
    env = d.get("envelope") or {}
    out = [msg("dxf_lanes", name=d["name"], n=len(env.get("lanes") or []), gap=f"{(env.get('gap_mm') or 0) / 1000:g}")]
    if d.get("mobile_m2") or d.get("unresolved_m2"):
        out.append(msg("dxf_set_aside", name=d["name"], mobile=d.get("mobile_m2") or 0,
                       unresolved=d.get("unresolved_m2") or 0))
    if d.get("transform_check_failures"):
        out.append(msg("dxf_transform_check", name=d["name"], count=len(d["transform_check_failures"])))
    engine = list(dict.fromkeys(d.get("warnings") or []))
    for w in engine[:DXF_ENGINE_WARNINGS_SHOWN]:
        out.append(msg("dxf_engine_note", name=d["name"], text=w))
    if len(engine) > DXF_ENGINE_WARNINGS_SHOWN:
        out.append(msg("dxf_engine_more", name=d["name"], count=len(engine) - DXF_ENGINE_WARNINGS_SHOWN))
    out.append(msg("dxf_no_stations", name=d["name"]))
    return out


def _result_warnings(results, ocr):
    out = []
    for d in results:
        dxf = d.get("source") == dxfinfo.DXF
        if d["footprint_m2"] <= 0:
            out.append(msg("dxf_no_footprint" if dxf else "no_footprint", name=d["name"]))
        if dxf:
            out += _dxf_warnings(d)
        elif d["stations"]:
            if abs(d["station_sum_m2"] - d["footprint_m2"]) > 0.01 * max(d["footprint_m2"], 1e-9):
                out.append(msg("station_sum_mismatch", name=d["name"], stations=d["station_sum_m2"],
                               total=d["footprint_m2"]))
            n_inf = sum(1 for s in d["stations"] if s["op_inferred"])
            if n_inf:
                out.append(msg("inferred_ops", name=d["name"], count=n_inf))
        elif ocr:
            out.append(msg("no_stations", name=d["name"]))
    return out


def public_results(results):
    """results.json 中给界面用的部分（不含服务器路径、像素分组、逐个插入的明细）"""
    keep = ("id", "name", "reference_length_mm", "mm_per_pt", "building_box_m", "agv_circles_removed",
            "footprint_m2", "n_groups", "envelope", "footprint_vs_envelope", "stations", "station_sum_m2")
    dxf_keep = ("unit_mm", "dxf_version", "mobile_m2", "unresolved_m2", "overlap_m2")
    out = []
    for d in results:
        v = dict({k: d.get(k) for k in keep}, source=d.get("source") or dxfinfo.PDF,
                 has_overlay=bool(d.get("overlay")), has_station_overlay=bool(d.get("station_overlay")))
        if v["source"] == dxfinfo.DXF:
            rows = d.get("inserts") or []
            v.update({k: d.get(k) for k in dxf_keep}, n_unresolved=len(d.get("unresolved") or []),
                     transform_check_failures=len(d.get("transform_check_failures") or []),
                     line_region_basis=(d.get("line_region") or {}).get("basis"),
                     n_equipment=sum(r["cls"] == "equipment" for r in rows),
                     n_excluded=sum(r["cls"] == "excluded" for r in rows))
        out.append(v)
    return out


def _job_cfg(job, ocr):
    cfg = estimate.apply_params(load_base_config(), job["params"])
    cfg["extraction"]["stations"]["enabled"] = bool(ocr and cfg["extraction"]["stations"]["enabled"])
    return cfg


def run_job(store, lib, jid):
    job = store.update(jid, status="running", stage="checking")
    drawings = job["drawings"]
    try:
        warnings = _precheck(drawings, lib)
        ocr = ocr_available()
        if not ocr:
            warnings.insert(0, msg("ocr_unavailable"))
        cfg = _job_cfg(job, ocr)
        warnings += _resolve_inputs(store, lib, jid, drawings, cfg)
        code_ver = dict(pdf=code_version(ESTIMATOR_DIR), dxf=code_version(ESTIMATOR_DIR, DXF_CODE))
        results_dir = os.path.join(store.dir(jid), "results")
        os.makedirs(results_dir, exist_ok=True)

        # 1. 同一文件 + 同样的测量输入：复用缓存
        entries, todo, keys = {}, [], {}
        for d in drawings:
            if _is_dxf(d):
                keys[d["id"]] = lib.dxf_cache_key(d["sha256"], d.get("dxf_unit_mm"), d.get("overrides"),
                                                  cfg["extraction"], cfg["dxf"], code_ver["dxf"])
            else:
                keys[d["id"]] = lib.cache_key(d["sha256"], d["reference_length_mm"], d.get("mm_per_pt"),
                                              cfg["extraction"], code_ver["pdf"])
            hit = lib.cached(keys[d["id"]])
            if hit:
                entries[d["id"]] = lib.restore(hit[0], hit[1], d, results_dir)
                d.update(reused=True, reused_from=hit[0]["cached_at"])
                warnings.append(msg("reused_result", name=d["name"], when=hit[0]["cached_at"][:10]))
            else:
                todo.append(d)
        job = store.update(jid, stage="measuring", ocr=ocr, warnings=warnings, drawings=drawings,
                           progress=dict(done=len(drawings) - len(todo), total=len(drawings)))

        # 2. 其余的运行估算程序，并存入缓存；记录实测用时与内存峰值（决定服务器规格）
        resources = dict(reused=len(drawings) - len(todo), measure=None, workbook=None)
        done = len(drawings) - len(todo)
        for group in ([d for d in todo if not _is_dxf(d)], [d for d in todo if _is_dxf(d)]):
            if not group:
                continue
            cfg_path = _job_config(store, lib, jid, cfg, group)
            measured, usage = _measure(store, jid, group, cfg_path, done, len(drawings))
            done += len(group)
            resources["measure"] = _combine_usage(resources["measure"], usage)
            for r in measured:
                entries[r["id"]] = r
                lib.store(keys[r["id"]], r, results_dir, jid)
        results = [entries[d["id"]] for d in drawings]
        with open(os.path.join(results_dir, "results.json"), "w", encoding="utf-8") as f:
            json.dump(dict(extraction=cfg["extraction"], drawings=results), f, ensure_ascii=False, indent=1)

        # 3. 工作簿与汇总（config.yaml 写全部图纸，便于命令行复现）
        store.update(jid, stage="workbook")
        cfg_path = _job_config(store, lib, jid, cfg, drawings)
        resources["workbook"] = build_workbook(store, jid, cfg_path)
        warnings += _result_warnings(results, ocr)
        for d, r in zip(drawings, results):
            if _is_dxf(d):
                lib.remember(d["sha256"], jid, d["name"], r["footprint_m2"], dxf_unit_mm=d.get("dxf_unit_mm"),
                             unit_source=d.get("unit_source"), unit_basis=d.get("unit_basis"))
            else:
                lib.remember(d["sha256"], jid, d["name"], r["footprint_m2"], reference_length_mm=d["reference_length_mm"],
                             mm_per_pt=d.get("mm_per_pt"), reference_source=d["reference_source"])
        store.update(jid, status="done", stage="done", warnings=warnings, results=public_results(results),
                     estimate=estimate.estimate(results, job["params"], cfg), resources=resources)
        log.info("job %s done (%d reused, %d measured)", jid, len(drawings) - len(todo), len(todo))
    except Failed as e:
        log.warning("job %s failed: %s", jid, e.message["en"])
        store.update(jid, status="failed", stage="failed", error=e.message, detail=e.detail,
                     needs_dimension=e.needs, needs_units=e.needs_units, drawings=drawings)
    except Exception as e:  # noqa: BLE001 — 未预料的错误也要让用户看到，而不是一直“计算中”
        log.exception("job %s crashed", jid)
        store.update(jid, status="failed", stage="failed", error=msg("measure_failed", name="—"),
                     detail=f"{type(e).__name__}: {e}")


def _combine_usage(a, b):
    """PDF 与 DXF 分两次运行时合并实测资源：用时相加，内存取峰值"""
    if not a:
        return b
    peak = max(a["peak_rss_mb"] or 0, b["peak_rss_mb"] or 0) or None
    return dict(seconds=round(a["seconds"] + b["seconds"], 1), peak_rss_mb=peak, drawings=a["drawings"] + b["drawings"])


def recalculate(store, lib, jid, params):
    """只改业务参数：重写 config、重建工作簿、重算汇总；不重新测量面积。"""
    job = store.get(jid)
    job["params"] = params
    cfg = _job_cfg(job, job["ocr"])
    with open(os.path.join(store.dir(jid), "results", "results.json"), encoding="utf-8") as f:
        results = json.load(f)["drawings"]
    build_workbook(store, jid, _job_config(store, lib, jid, cfg, job["drawings"]))
    return store.update(jid, params=params, estimate=estimate.estimate(results, params, cfg))


class Worker:
    """单线程任务队列"""

    def __init__(self, store, lib):
        self.store, self.lib, self.q = store, lib, queue.Queue()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="estimator-worker")
        self.thread.start()

    def submit(self, jid):
        self.q.put(jid)

    def _loop(self):
        while True:
            run_job(self.store, self.lib, self.q.get())
