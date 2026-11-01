"""
图纸库：历史上传的 PDF 按内容 SHA-256 保存一次，并记住每张图纸上次的名称、标注总尺寸和结果；
测量缓存：同一文件 + 同样的测量输入 + 同一版本的测量代码 -> 直接复用上次的测量结果。

<data>/library/<sha256>.pdf    原文件（私有；任何接口都不返回文件本身或路径）
<data>/library/<sha256>.json   元数据
<data>/cache/<key>/            一次测量的结果（result.json + 叠加图）

识别“同一文件”只看内容哈希，不看文件名：改名的同一文件会被识别，内容有任何改动（新版本）都不会。
测量缓存的键包含提取参数和测量代码（estimate_footprint.py、stations.py）的哈希，代码或参数一变，旧结果自动失效。
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


def code_version(estimator_dir):
    """测量代码的哈希：算法一改，旧缓存就不再命中"""
    h = hashlib.sha256()
    for name in ("estimate_footprint.py", "stations.py"):
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
    def pdf_path(self, sha):
        return os.path.join(self.root, f"{sha}.pdf")

    def _meta_path(self, sha):
        return os.path.join(self.root, f"{sha}.json")

    def get(self, sha):
        if not SHA_RE.match(sha or ""):
            return None
        p = self._meta_path(sha)
        if not os.path.exists(p) or not os.path.exists(self.pdf_path(sha)):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def add(self, data, filename):
        """保存上传的文件（同内容只存一份）。返回 (sha256, 是否为已知文件)。"""
        sha = sha256_bytes(data)
        with self._lock:
            meta = self.get(sha)
            known = meta is not None
            if not known:
                with open(self.pdf_path(sha) + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(self.pdf_path(sha) + ".tmp", self.pdf_path(sha))
                meta = dict(sha256=sha, size_bytes=len(data), filenames=[], first_uploaded=_now(),
                            last_used=None, name=None, reference_length_mm=None, mm_per_pt=None,
                            reference_source=None, footprint_m2=None, uses=0, jobs=[])
            if filename not in meta["filenames"]:
                meta["filenames"].append(filename)
            _write_json(self._meta_path(sha), meta)
        return sha, known

    def remember(self, sha, job_id, name, reference_length_mm, mm_per_pt, reference_source, footprint_m2):
        """一次成功的估算之后：记住这张图纸的名称、标注尺寸（及来源）和面积"""
        with self._lock:
            meta = self.get(sha)
            if meta is None:
                return
            meta.update(name=name, reference_length_mm=reference_length_mm, mm_per_pt=mm_per_pt,
                        reference_source=reference_source, footprint_m2=footprint_m2, last_used=_now(),
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

    def cached(self, key):
        """-> (result.json 中这张图的条目, 缓存目录) 或 None"""
        d = os.path.join(self.cache_root, key)
        p = os.path.join(d, "result.json")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f), d

    def store(self, key, result, results_dir, job_id):
        """把刚测完的一张图存入缓存（叠加图按固定文件名保存）"""
        d = os.path.join(self.cache_root, key)
        tmp = d + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        for field, fixed in (("overlay", "overlay.png"), ("station_overlay", "stations.png")):
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
        """把缓存的结果放进本任务：改成本任务的图纸编号/名称，复制叠加图。返回 results.json 条目。"""
        r = copy.deepcopy(cached)
        r.update(id=drawing["id"], name=drawing["name"], pdf=f"{drawing['id']}.pdf")
        for field, fixed, suffix in (("overlay", "overlay.png", "overlay"), ("station_overlay", "stations.png", "stations")):
            if r.get(field):
                r[field] = f"{drawing['id']}_{suffix}.png"
                shutil.copyfile(os.path.join(cache_dir, fixed), os.path.join(results_dir, r[field]))
        return r
