# PyInstaller 打包桌面版（在要发布的系统上各打一次：Windows 打出 .exe，macOS 打出 .app）
#
#     .venv/bin/pip install -r requirements-desktop.txt
#     .venv/bin/python -m PyInstaller --noconfirm desktop.spec
#
# 输出：dist/LayoutEstimator/LayoutEstimator.exe（Windows，整个文件夹一起分发）
#       dist/LayoutEstimator.app（macOS）
# 用“文件夹”模式而不是单文件：估算以子进程运行程序自身，单文件模式每次都要先解压几百 MB。
# 如有 vendor/tesseract/（CI 在 Windows 上放入 Tesseract），一并打包，工位识别无需另装。
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(SPECPATH)
NAME = "LayoutEstimator"

datas = [(os.path.join(ROOT, f), ".") for f in
         ("estimate_footprint.py", "stations.py", "dxf_footprint.py", "build_workbook.py", "estimate.py", "config.yaml")]
datas += [(os.path.join(ROOT, "webapp", "static"), os.path.join("webapp", "static")),
          (os.path.join(ROOT, "webapp", "hosting.yaml"), "webapp")]
tesseract = os.path.join(ROOT, "vendor", "tesseract")
if os.path.isdir(tesseract):
    datas.append((tesseract, "tesseract"))

# 估算脚本由 runpy 运行，PyInstaller 看不到它们的 import，需显式列出
hiddenimports = collect_submodules("uvicorn") + ["openpyxl", "pytesseract", "estimate_footprint", "stations",
                                                 "dxf_footprint", "PIL.ImageDraw"]

a = Analysis([os.path.join(ROOT, "desktop.py")], pathex=[ROOT], datas=datas, hiddenimports=hiddenimports,
             excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME)

if sys.platform == "darwin":
    app = BUNDLE(coll, name=f"{NAME}.app", bundle_identifier="local.layout-estimator",
                 info_plist={"CFBundleDisplayName": "产线投影面积估算", "CFBundleShortVersionString": "1.0",
                             "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0"})
