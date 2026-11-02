"""桌面版：Tesseract 查找、打包后的子进程命令、数据目录、单实例锁、本机保存的文件名、desktop 模式接口。"""
import os
import stat
import sys

import pytest

from webapp import desktop, jobs, ocr


def _fake_exe(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def no_system_tesseract(monkeypatch):
    """只看测试给出的位置：清掉环境变量、PATH、用户目录与系统常见安装位置"""
    for k in ("TESSERACT_CMD", "TESSDATA_PREFIX", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ocr.shutil, "which", lambda _: None)
    monkeypatch.setattr(ocr.os.path, "expanduser", lambda p: "/nonexistent-home")
    real = os.path.isfile
    monkeypatch.setattr(ocr.os.path, "isfile", lambda p: not str(p).startswith(("/opt/", "/usr/")) and real(p))


def test_bundled_tesseract_is_found_and_gets_its_language_data(tmp_path, no_system_tesseract):
    base = str(tmp_path)
    assert ocr.find_tesseract(base) is None
    exe = _fake_exe(os.path.join(base, "tesseract", "bin", ocr.EXE))          # conda 目录结构
    data = tmp_path / "tesseract" / "share" / "tessdata"
    data.mkdir(parents=True)
    (data / "eng.traineddata").write_bytes(b"x")
    assert ocr.find_tesseract(base) == exe
    env = ocr.child_env({"PATH": "/usr/bin"}, base)
    assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(exe)
    assert os.path.samefile(env["TESSDATA_PREFIX"], str(data))


def test_tesseract_cmd_variable_wins_and_missing_tesseract_leaves_env_alone(tmp_path, monkeypatch, no_system_tesseract):
    assert ocr.child_env({"PATH": "/usr/bin"}, str(tmp_path)) == {"PATH": "/usr/bin"}
    exe = _fake_exe(str(tmp_path / "custom" / ocr.EXE))
    monkeypatch.setenv("TESSERACT_CMD", exe)
    assert ocr.find_tesseract(str(tmp_path)) == exe
    assert "TESSDATA_PREFIX" not in ocr.child_env({"PATH": ""}, str(tmp_path))   # 没有语言包目录时不乱设


def test_script_command_source_vs_packaged(monkeypatch):
    path = os.path.join(jobs.ESTIMATOR_DIR, "estimate_footprint.py")
    assert jobs.script_cmd("estimate_footprint.py") == [sys.executable, path]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert jobs.script_cmd("estimate_footprint.py") == [sys.executable, "--run-script", path]


@pytest.mark.parametrize("platform, env, expected", [
    ("darwin", {}, os.path.join("~", "Library", "Application Support", "LayoutEstimator")),
    ("win32", {"LOCALAPPDATA": "C:\\Users\\张三\\AppData\\Local"}, os.path.join("C:\\Users\\张三\\AppData\\Local", "LayoutEstimator")),
    ("linux", {"XDG_DATA_HOME": ""}, os.path.join("~", ".local", "share", "LayoutEstimator")),
])
def test_default_data_dir(monkeypatch, platform, env, expected):
    monkeypatch.setattr(desktop.sys, "platform", platform)
    monkeypatch.setattr(desktop.os.path, "expanduser", lambda p: p)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert desktop.default_data_dir() == expected


@pytest.mark.skipif(sys.platform == "win32", reason="flock")
def test_only_one_instance_per_data_dir(tmp_path):
    first = desktop.acquire_lock(str(tmp_path))
    assert first is not None
    assert desktop.acquire_lock(str(tmp_path)) is None
    first.close()
    again = desktop.acquire_lock(str(tmp_path))
    assert again is not None
    again.close()


def test_saved_filename_comes_from_the_server_response():
    cd = {"Content-Disposition": "attachment; filename*=UTF-8''%E6%8A%95%E5%BD%B1%E9%9D%A2%E7%A7%AF_20261101.xlsx"}
    assert desktop._filename_from(cd, "x.xlsx") == "投影面积_20261101.xlsx"
    assert desktop._filename_from({"Content-Disposition": 'attachment; filename="a.xlsx"'}, "x") == "a.xlsx"
    assert desktop._filename_from({}, "x.xlsx") == "x.xlsx"
    assert desktop._safe_name('报告: 101/102?.html') == "报告_ 101_102_.html"


def test_desktop_mode_reports_its_data_dir_and_web_mode_does_not(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from webapp.app import create_app
    monkeypatch.setattr(jobs, "ocr_available", lambda: False)
    with TestClient(create_app(str(tmp_path / "d"), desktop=True)) as c:
        s = c.get("/api/system").json()
        assert s["desktop"] is True and s["data_dir"] == os.path.abspath(str(tmp_path / "d"))
        assert "desktop" in [o["key"] for o in s["hosting"]["options"]]
    with TestClient(create_app(str(tmp_path / "w"))) as c:
        s = c.get("/api/system").json()
        assert s["desktop"] is False and "data_dir" not in s
