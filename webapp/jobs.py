"""
网页任务：图纸库中的 PDF -> 运行 estimate_footprint.py 与 build_workbook.py -> 结果。

两个程序以子进程运行，与命令行完全相同（同一份代码、同一份 config 格式），网页只负责
准备输入和展示输出。一次只运行一个任务（单个后台线程）：一张大图测量时内存峰值可达数 GB。

同一文件（内容 SHA-256 相同）+ 同样的测量输入，直接复用图纸库中的测量缓存，不再重新测量；
未填写标注总尺寸时，沿用该文件上次的标注尺寸；从未填写过则停下来请用户填写（附提示图）。

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
import shutil
import subprocess
import sys
import threading
import uuid

import pymupdf
import yaml

from webapp.messages import msg
from webapp.library import code_version

ESTIMATOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ESTIMATOR_DIR)
import estimate  # noqa: E402  (estimator 目录在上面加入路径)
from webapp import dimension  # noqa: E402

log = logging.getLogger("layout_estimator.web")

MEASURE_TIMEOUT_S = 30 * 60        # 含逐个标签 600 dpi OCR；大图 + 多图时可能需要数分钟
WORKBOOK_TIMEOUT_S = 5 * 60
STDERR_TAIL = 2000                 # 失败时返回给界面的技术信息长度（字符）
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def ocr_available():
    """工位识别需要 Tesseract 程序（pytesseract 只是调用它）。每次任务前检查，装好后无需重启。"""
    return shutil.which("tesseract") is not None


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
        drawings.append(dict(id=did, name=r["name"] or os.path.splitext(r["filename"])[0], filename=r["filename"],
                             sha256=r["sha256"], size_bytes=r["size_bytes"],
                             reference_length_mm=r["reference_length_mm"], mm_per_pt=r["mm_per_pt"],
                             reference_source="user" if r["reference_length_mm"] else None, reused=False))
    job = dict(id=jid, created_at=_now(), updated_at=_now(), status="queued", stage="queued",
               progress=dict(done=0, total=len(drawings)), drawings=drawings, params=params,
               ocr=None, warnings=[], error=None, detail=None, needs_dimension=None, results=None, estimate=None)
    store.create(job)
    return job


# ------------------------------------------------------------------ 执行
class Failed(Exception):
    def __init__(self, message, detail=None, needs=None):
        super().__init__(message["en"])
        self.message, self.detail, self.needs = message, detail, needs


def _job_config(store, lib, jid, cfg, drawings):
    """本任务（或其中待测量部分）的 config.yaml：图纸列表 + 参数；其余沿用 config.yaml"""
    cfg = copy.deepcopy(cfg)
    cfg["drawings"] = []
    for d in drawings:
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
    """打开每个 PDF：能否读取、是否有矢量、页数。返回提示列表；不可用时抛 Failed。"""
    warnings = []
    for d in drawings:
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
    """标注总尺寸：用户填写 > 该文件上次的值；都没有则生成提示图并停下来请用户填写。"""
    warnings, needs = [], []
    for d in drawings:
        if d["reference_length_mm"]:
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
    if needs:
        raise Failed(msg("dimension_needed", names="、".join(n["name"] for n in needs)), needs=needs)
    return warnings


def _run(cmd, timeout_s, on_line=None):
    """运行估算程序；逐行回调 stdout。返回 (退出码, stderr 末尾)。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(cmd, cwd=ESTIMATOR_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
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
        proc.wait()
    finally:
        timer.cancel()
    t_err.join(timeout=5)
    if timed_out.is_set():
        raise Failed(msg("timeout", minutes=timeout_s // 60))
    return proc.returncode, "".join(err_chunks)[-STDERR_TAIL:]


def _measure(store, jid, todo, cfg_path, done_before, total):
    """只测量 todo 中的图纸；返回 results.json 的 drawings 列表"""
    done = [0]

    def on_line(line):
        if line.startswith("["):                     # estimate_footprint.py 每算完一张图打印一行 "[id] ..."
            done[0] += 1
            store.update(jid, progress=dict(done=done_before + done[0], total=total))

    out = os.path.join(store.dir(jid), "results")
    code, err = _run([sys.executable, os.path.join(ESTIMATOR_DIR, "estimate_footprint.py"), cfg_path, "--out", out],
                     MEASURE_TIMEOUT_S, on_line)
    if code != 0:
        d = todo[min(done[0], len(todo) - 1)]
        if "未找到尺寸线" in err:
            m = msg("scale_not_found", name=d["name"])
        elif "empty sequence" in err:
            m = msg("building_not_found", name=d["name"])
        else:
            m = msg("measure_failed", name=d["name"])
        raise Failed(m, err)
    with open(os.path.join(out, "results.json"), encoding="utf-8") as f:
        return json.load(f)["drawings"]


def build_workbook(store, jid, cfg_path):
    """(重新)生成工作簿。参数修改后只需重跑这一步，无需重新测量。"""
    jdir = store.dir(jid)
    code, err = _run([sys.executable, os.path.join(ESTIMATOR_DIR, "build_workbook.py"), cfg_path,
                      os.path.join(jdir, "results", "results.json"), os.path.join(jdir, "workbook.xlsx")],
                     WORKBOOK_TIMEOUT_S)
    if code != 0:
        raise Failed(msg("workbook_failed"), err)


def _result_warnings(results, ocr):
    out = []
    for d in results:
        if d["footprint_m2"] <= 0:
            out.append(msg("no_footprint", name=d["name"]))
        if d["stations"]:
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
    """results.json 中给界面用的部分（不含服务器路径、像素分组细节）"""
    keep = ("id", "name", "reference_length_mm", "mm_per_pt", "building_box_m", "agv_circles_removed",
            "footprint_m2", "n_groups", "envelope", "footprint_vs_envelope", "stations", "station_sum_m2")
    return [dict({k: d[k] for k in keep}, has_overlay=bool(d.get("overlay")),
                 has_station_overlay=bool(d.get("station_overlay"))) for d in results]


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
        warnings += _resolve_dimensions(store, lib, jid, drawings)
        cfg = _job_cfg(job, ocr)
        code_ver = code_version(ESTIMATOR_DIR)
        results_dir = os.path.join(store.dir(jid), "results")
        os.makedirs(results_dir, exist_ok=True)

        # 1. 同一文件 + 同样的测量输入：复用缓存
        entries, todo, keys = {}, [], {}
        for d in drawings:
            keys[d["id"]] = lib.cache_key(d["sha256"], d["reference_length_mm"], d.get("mm_per_pt"),
                                          cfg["extraction"], code_ver)
            hit = lib.cached(keys[d["id"]])
            if hit:
                entries[d["id"]] = lib.restore(hit[0], hit[1], d, results_dir)
                d.update(reused=True, reused_from=hit[0]["cached_at"])
                warnings.append(msg("reused_result", name=d["name"], when=hit[0]["cached_at"][:10]))
            else:
                todo.append(d)
        job = store.update(jid, stage="measuring", ocr=ocr, warnings=warnings, drawings=drawings,
                           progress=dict(done=len(drawings) - len(todo), total=len(drawings)))

        # 2. 其余的运行估算程序，并存入缓存
        if todo:
            cfg_path = _job_config(store, lib, jid, cfg, todo)
            for r in _measure(store, jid, todo, cfg_path, len(drawings) - len(todo), len(drawings)):
                entries[r["id"]] = r
                lib.store(keys[r["id"]], r, results_dir, jid)
        results = [entries[d["id"]] for d in drawings]
        with open(os.path.join(results_dir, "results.json"), "w", encoding="utf-8") as f:
            json.dump(dict(extraction=cfg["extraction"], drawings=results), f, ensure_ascii=False, indent=1)

        # 3. 工作簿与汇总（config.yaml 写全部图纸，便于命令行复现）
        store.update(jid, stage="workbook")
        cfg_path = _job_config(store, lib, jid, cfg, drawings)
        build_workbook(store, jid, cfg_path)
        warnings += _result_warnings(results, ocr)
        for d, r in zip(drawings, results):
            lib.remember(d["sha256"], jid, d["name"], d["reference_length_mm"], d.get("mm_per_pt"),
                         d["reference_source"], r["footprint_m2"])
        store.update(jid, status="done", stage="done", warnings=warnings, results=public_results(results),
                     estimate=estimate.estimate(results, job["params"], cfg))
        log.info("job %s done (%d reused, %d measured)", jid, len(drawings) - len(todo), len(todo))
    except Failed as e:
        log.warning("job %s failed: %s", jid, e.message["en"])
        store.update(jid, status="failed", stage="failed", error=e.message, detail=e.detail,
                     needs_dimension=e.needs, drawings=drawings)
    except Exception as e:  # noqa: BLE001 — 未预料的错误也要让用户看到，而不是一直“计算中”
        log.exception("job %s crashed", jid)
        store.update(jid, status="failed", stage="failed", error=msg("measure_failed", name="—"),
                     detail=f"{type(e).__name__}: {e}")


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
