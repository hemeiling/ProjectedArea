"""
查找 Tesseract 程序（工位识别用；pytesseract 只是调用它）。

桌面版从“访达 / 开始菜单”启动时没有终端里的 PATH（例如 Homebrew 的 /opt/homebrew/bin），
所以除了 PATH 还查：环境变量 TESSERACT_CMD、桌面版自带的 tesseract/ 目录、各系统的常见安装位置。
找到后估算子进程的 PATH 前面加上它所在目录，estimate_footprint.py / stations.py 不需要任何改动。
"""
import glob
import os
import shutil
import sys

EXE = "tesseract.exe" if sys.platform == "win32" else "tesseract"


def _candidates(base_dir):
    yield os.environ.get("TESSERACT_CMD")
    bundled = os.path.join(base_dir, "tesseract")              # 桌面版安装包自带
    yield os.path.join(bundled, EXE)                             # Windows（UB Mannheim 目录结构）
    yield os.path.join(bundled, "bin", EXE)                      # conda 目录结构
    yield shutil.which("tesseract")
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if root:
                yield os.path.join(root, "Tesseract-OCR", EXE)
    else:
        yield from ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/opt/local/bin/tesseract",
                    "/usr/bin/tesseract")
        for pattern in ("micromamba/envs/*/bin/tesseract", "miniforge3/bin/tesseract", "miniforge3/envs/*/bin/tesseract",
                        "miniconda3/bin/tesseract", "miniconda3/envs/*/bin/tesseract", ".local/bin/tesseract"):
            yield from sorted(glob.glob(os.path.join(home, pattern)))


def find_tesseract(base_dir):
    """Tesseract 可执行文件的路径；没有安装时返回 None"""
    for c in _candidates(base_dir):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.abspath(c)
    return None


def child_env(env, base_dir):
    """估算子进程的环境变量：找得到 Tesseract 时把它的目录放到 PATH 最前面，并在需要时给出语言包目录"""
    cmd = find_tesseract(base_dir)
    if not cmd:
        return env
    env = dict(env)
    bindir = os.path.dirname(cmd)
    env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    if "TESSDATA_PREFIX" not in env:
        # 复制到别处的 conda / 安装包版本找不到编译时写死的语言包目录，需要明确指定
        for tessdata in (os.path.join(bindir, "tessdata"), os.path.join(bindir, os.pardir, "share", "tessdata")):
            if os.path.isfile(os.path.join(tessdata, "eng.traineddata")):
                env["TESSDATA_PREFIX"] = os.path.abspath(tessdata)
                break
    return env
