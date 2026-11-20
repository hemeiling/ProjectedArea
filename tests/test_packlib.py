"""包装方式库：名字归一、exact / similar 匹配、记忆与更新、删除只做标记"""
import pytest

from webapp.packlib import PackLibrary, normalize


def test_normalize_ignores_case_spaces_punctuation_and_version():
    assert normalize("标准人工工位V1.3") == normalize("标准人工工位 v1.4") == normalize("标准人工工位")
    assert normalize("CCS-焊接 工位") == normalize("ccs焊接工位")
    assert normalize("托盘") != normalize("托架")


def test_exact_similar_and_no_match(tmp_path):
    lib = PackLibrary(str(tmp_path))
    e = lib.remember("标准人工工位V1.3", 4.72, "stack2", "job1", "103")
    assert lib.match("标准人工工位V1.3", 4.72)[1] == "exact"
    hit = lib.match("标准人工工位 v1.4", 4.75)                    # 名字相似、面积差 < 2%
    assert hit and hit[1] == "similar" and hit[0]["id"] == e["id"]
    assert lib.match("标准人工工位 v1.4", 9.4) is None              # 面积差一倍：不是同一类
    assert lib.match("完全不同的设备", 4.72) is None
    lib.add_alias(e["id"], "标准人工工位 v1.4")                       # 用户确认同类后，以后按完全相同匹配
    assert lib.match("标准人工工位 v1.4", 9.4)[1] == "exact"


def test_remember_updates_exact_entry_keeps_history_and_withdraw_keeps_record(tmp_path):
    lib = PackLibrary(str(tmp_path))
    a = lib.remember("托盘", 3.3, "stack2", "j1", "103")
    b = lib.remember("托盘", 3.3, 0.9, "j2", "104")
    assert a["id"] == b["id"] and len(lib.entries()) == 1
    assert lib.entries()[0]["choice"] == 0.9 and lib.entries()[0]["history"][0]["choice"] == "stack2"
    assert lib.update(a["id"], name="托盘（标准）")["name"] == "托盘（标准）"
    assert lib.withdraw(a["id"]) and lib.entries() == [] and len(lib.entries(with_withdrawn=True)) == 1
    assert not lib.withdraw(a["id"])
    assert PackLibrary(str(tmp_path)).entries(with_withdrawn=True)[0]["withdrawn_at"]       # 重新读取仍在
