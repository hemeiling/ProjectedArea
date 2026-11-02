#!/usr/bin/env python3
"""
桌面版入口（也是 PyInstaller 打包的入口，见 desktop.spec）：

    .venv/bin/python desktop.py          # 从源代码运行：打开一个窗口，计算在本机进行
    LayoutEstimator.app / .exe           # 打包后双击运行

打包后程序自身也用来运行估算脚本：网页服务以 “程序 --run-script <脚本> 参数…” 启动子进程。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if not getattr(sys, "frozen", False):  # 打包后程序目录已在路径中，不能重复添加（见 webapp/jobs.py）
    sys.path.insert(0, HERE)

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-script":
        from webapp.desktop import run_script
        run_script(sys.argv[2], sys.argv[3:])
    elif len(sys.argv) >= 2 and sys.argv[1] == "--smoke-test":
        from webapp.smoke import main as smoke
        sys.exit(smoke())
    else:
        from webapp.desktop import run
        run()
