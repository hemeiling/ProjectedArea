"""
图纸库：历史上传的 PDF / DXF 按内容 SHA-256 保存一次，并记住每张图纸上次的名称、标注总尺寸（PDF）
或单位（DXF）和结果；
测量缓存：同一文件 + 同样的测量输入 + 同一版本的测量代码 -> 直接复用上次的测量结果。

<data>/library/<sha256>.pdf|.dxf   原文件（私有；任何接口都不返回文件本身或路径）
<data>/library/<sha256>.json       元数据（kind = pdf / dxf；旧记录没有 kind，即 pdf）
<data>/cache/<key>/                一次测量的结果（result.json + 叠加图 + DXF 计入轮廓）

识别“同一文件”只看内容哈希，不看文件名：改名的同一文件会被识别，内容有任何改动（新版本）都不会。
测量缓存的键包含提取参数和测量代码（PDF：estimate_footprint.py、stations.py；DXF：dxf_footprint.py）的哈希，
代码或参数一变，旧结果自动失效。
"""
import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import threading

SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


PDF_CODE = ("estimate_footprint.py", "stations.py")
DXF_CODE = ("dxf_footprint.py",)
# 缓存中保存的结果文件：results.json 中的字段 -> (缓存里的固定文件名, 任务里的文件名后缀)
CACHED_FILES = (("overlay", "overlay.png", "overlay.png"), ("station_overlay", "stations.png", "stations.png"),
                ("footprint_wkt", "footprint.wkt", "footprint.wkt"))


def code_version(estimator_dir, names=PDF_CODE):
    """测量代码的哈希：算法一改，旧缓存就不再命中"""
    h = hashlib.sha256()
    for name in names:
        with open(os.path.join(estimator_dir, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Library:
    def __init__(self, data_dir):
        self.root = os.path.join(data_dir, "library")
        self.cache_root = os.path.join(data_dir, "cache")
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.cache_root, exist_ok=True)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 文件
    def file_path(self, sha, kind="pdf"):
        return os.path.join(self.root, f"{sha}.{kind}")

    def pdf_path(self, sha):
        return self.file_path(sha, "pdf")

    def _meta_path(self, sha):
        return os.path.join(self.root, f"{sha}.json")

    def get(self, sha):
        if not SHA_RE.match(sha or ""):
            return None
        p = self._meta_path(sha)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            meta = json.load(f)
        meta.setdefault("kind", "pdf")
        return meta if os.path.exists(self.file_path(sha, meta["kind"])) else None

    def staging_path(self):
        """上传中的临时文件（与图纸库同一磁盘，完成后直接改名放入）"""
        return os.path.join(self.root, f".upload-{os.urandom(8).hex()}.tmp")

    def adopt(self, tmp_path, sha, size, kind, filename):
        """把已校验、已算好哈希的上传文件放入图纸库（同内容只存一份）。返回 (sha256, 是否为已知文件)。"""
        with self._lock:
            meta = self.get(sha)
            known = meta is not None
            if known:
                os.remove(tmp_path)
            else:
                os.replace(tmp_path, self.file_path(sha, kind))
                meta = dict(sha256=sha, kind=kind, size_bytes=size, filenames=[], first_uploaded=_now(),
                            last_used=None, name=None, reference_length_mm=None, mm_per_pt=None,
                            reference_source=None, dxf_unit_mm=None, unit_source=None, unit_basis=None,
                            footprint_m2=None, uses=0, jobs=[])
            if filename not in meta["filenames"]:
                meta["filenames"].append(filename)
            _write_json(self._meta_path(sha), meta)
        return sha, known

    def add(self, data, filename, kind="pdf"):
        """保存内存中的文件（测试与命令行用）。返回 (sha256, 是否为已知文件)。"""
        tmp = self.staging_path()
        with open(tmp, "wb") as f:
            f.write(data)
        return self.adopt(tmp, sha256_bytes(data), len(data), kind, filename)

    def remember(self, sha, job_id, name, footprint_m2, **measured_with):
        """一次成功的估算之后：记住这张图纸的名称、面积，以及测量输入
        （PDF：reference_length_mm、mm_per_pt、reference_source；DXF：dxf_unit_mm、unit_source、unit_basis）"""
        with self._lock:
            meta = self.get(sha)
            if meta is None:
                return
            meta.update(measured_with)
            meta.update(name=name, footprint_m2=footprint_m2, last_used=_now(),
                        uses=meta["uses"] + 1, jobs=(meta["jobs"] + [job_id])[-50:])
            _write_json(self._meta_path(sha), meta)

    def entries(self):
        out = []
        for fn in os.listdir(self.root):
            if fn.endswith(".json"):
                m = self.get(fn[:-5])
                if m:
                    out.append(m)
        return sorted(out, key=lambda m: m["last_used"] or m["first_uploaded"], reverse=True)

    # ------------------------------------------------------------ 测量缓存
    @staticmethod
    def cache_key(sha, reference_length_mm, mm_per_pt, extraction_cfg, code_ver):
        blob = json.dumps(dict(sha=sha, ref=float(reference_length_mm), mpp=mm_per_pt and float(mm_per_pt),
                               extraction=extraction_cfg, code=code_ver), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def dxf_cache_key(sha, unit_mm, overrides, extraction_cfg, dxf_cfg, code_ver):
        """DXF：单位（文件声明时为 None）、人工分类、提取与 DXF 参数、测量代码"""
        blob = json.dumps(dict(sha=sha, kind="dxf", unit=unit_mm and float(unit_mm), overrides=overrides,
                               extraction=extraction_cfg, dxf=dxf_cfg, code=code_ver),
                          sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def cached(self, key):
        """-> (result.json 中这张图的条目, 缓存目录) 或 None"""
        d = os.path.join(self.cache_root, key)
        p = os.path.join(d, "result.json")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f), d

    def store(self, key, result, results_dir, job_id):
        """把刚测完的一张图存入缓存（叠加图等结果文件按固定文件名保存）"""
        d = os.path.join(self.cache_root, key)
        tmp = d + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        for field, fixed, _ in CACHED_FILES:
            if result.get(field):
                shutil.copyfile(os.path.join(results_dir, result[field]), os.path.join(tmp, fixed))
        _write_json(os.path.join(tmp, "result.json"), dict(result, cached_at=_now(), cached_from_job=job_id))
        with self._lock:
            if os.path.exists(d):
                shutil.rmtree(tmp)          # 另一任务刚存过同一结果
            else:
                os.replace(tmp, d)

    @staticmethod
    def restore(cached, cache_dir, drawing, results_dir):
        """把缓存的结果放进本任务：改成本任务的图纸编号/名称，复制结果文件。返回 results.json 条目。"""
        r = copy.deepcopy(cached)
        r.update(id=drawing["id"], name=drawing["name"])
        r[r.get("source") or "pdf"] = f"{drawing['id']}.{r.get('source') or 'pdf'}"
        for field, fixed, suffix in CACHED_FILES:
            if r.get(field) and os.path.exists(os.path.join(cache_dir, fixed)):
                r[field] = f"{drawing['id']}_{suffix}"
                shutil.copyfile(os.path.join(cache_dir, fixed), os.path.join(results_dir, r[field]))
        return r
