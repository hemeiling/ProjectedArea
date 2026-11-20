"""
包装方式库：把“某类图块按什么方式包装”记下来，供以后的图纸沿用。

<data>/packing_library.json  {"entries": [...]}
    每条：id、name（标准名）、cad_names（已确认属于这一类的 CAD 图块名）、ref_area_m2（单台参考面积）、
    choice（"stack2" 或包装系数）、created_at / updated_at / source（任务与图纸）、history（每次改动，保留）、
    withdrawn_at（删除只做标记，记录保留）。

匹配（库里的设置只作为建议，用户点“采用”后才进入计算）：
    exact    图块名与库中某个 cad_name 完全相同；
    similar  去掉大小写、空格、标点和版本号后名字相同，且单台面积与参考面积相差 ≤ SIMILAR_AREA_TOL；
             只提示，用户确认后才把这个图块名加进该条的 cad_names。
名字不像、或名字像但面积对不上的，一律不匹配。
"""
import datetime as dt
import json
import os
import re
import secrets
import threading

SIMILAR_AREA_TOL = 0.02
STACK2 = "stack2"
MAX_NAME = 120


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


_VERSION = re.compile(r"v\s*\d+(?:[._-]\d+)*", re.I)


def normalize(name):
    """比较用：小写，去版本号（V1.3、v2）、空格和标点"""
    s = _VERSION.sub("", str(name or "").lower())
    return re.sub(r"[\W_]+", "", s)


def choice_ok(v, lo=0.05, hi=5.0):
    if v == STACK2:
        return True
    try:
        return lo <= float(v) <= hi
    except (TypeError, ValueError):
        return False


class PackLibrary:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "packing_library.json")
        self._lock = threading.Lock()

    def _load(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return json.load(f).get("entries", [])

    def _save(self, entries):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(entries=entries), f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def entries(self, with_withdrawn=False):
        return [e for e in self._load() if with_withdrawn or not e.get("withdrawn_at")]

    def match(self, block, area_m2, entries=None):
        """-> (条目, "exact" | "similar") 或 None"""
        entries = self.entries() if entries is None else entries
        for e in entries:
            if block in e["cad_names"]:
                return e, "exact"
        key = normalize(block)
        if not key or not area_m2:
            return None
        for e in entries:
            names = [e["name"], *e["cad_names"]]
            if any(normalize(n) == key for n in names) and abs(area_m2 - e["ref_area_m2"]) <= SIMILAR_AREA_TOL * e["ref_area_m2"]:
                return e, "similar"
        return None

    def remember(self, block, area_m2, choice, job_id, drawing_name):
        """设置包装方式后记入库：已有同名（exact）条目就更新，否则新建。返回条目。"""
        with self._lock:
            entries = self._load()
            hit = self.match(block, area_m2, [e for e in entries if not e.get("withdrawn_at")])
            now = _now()
            if hit and hit[1] == "exact":
                e = hit[0]
                if e["choice"] != choice:
                    e["history"].append(dict(at=now, choice=e["choice"], job=job_id, drawing=drawing_name))
                    e.update(choice=choice, updated_at=now, source=dict(job=job_id, drawing=drawing_name))
            else:
                e = dict(id=secrets.token_hex(4), name=block[:MAX_NAME], cad_names=[block], ref_area_m2=round(area_m2, 3),
                         choice=choice, created_at=now, updated_at=now, source=dict(job=job_id, drawing=drawing_name),
                         history=[])
                entries.append(e)
            self._save(entries)
            return e

    def _edit(self, eid, fn):
        with self._lock:
            entries = self._load()
            e = next((x for x in entries if x["id"] == eid and not x.get("withdrawn_at")), None)
            if e is None:
                return None
            fn(e)
            e["updated_at"] = _now()
            self._save(entries)
            return e

    def update(self, eid, name=None, choice=None):
        def fn(e):
            if name is not None:
                e["name"] = name[:MAX_NAME]
            if choice is not None and choice != e["choice"]:
                e["history"].append(dict(at=_now(), choice=e["choice"]))
                e["choice"] = choice
        return self._edit(eid, fn)

    def add_alias(self, eid, block):
        """用户确认“相似的图块是同一类”：把这个图块名加进条目"""
        return self._edit(eid, lambda e: e["cad_names"].append(block) if block not in e["cad_names"] else None)

    def withdraw(self, eid):
        return self._edit(eid, lambda e: e.update(withdrawn_at=_now())) is not None
