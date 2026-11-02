"""
桌面版：在本机启动网页服务（只监听 127.0.0.1），并在一个原生窗口中打开同一个界面。

计算、内存、图纸库都在这台电脑上，不需要云服务器。界面、接口、估算代码与网页版完全相同；
区别只有：数据目录在用户目录下、文件“下载”改为本机保存对话框、关闭窗口时结束正在运行的计算。

数据目录（可用环境变量 LAYOUT_DATA_DIR 改）：
    macOS    ~/Library/Application Support/LayoutEstimator
    Windows  %LOCALAPPDATA%\\LayoutEstimator
"""
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

APP_NAME = "LayoutEstimator"
TITLE = "产线投影面积估算 · Layout Footprint Estimator"
log = logging.getLogger("layout_estimator.desktop")


def default_data_dir():
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, APP_NAME)


# ------------------------------------------------------------ 只运行一个实例
# 两个实例共用一个数据目录时，后启动的会把前一个正在运行的任务标记为“已中断”。
def acquire_lock(data_dir):
    """拿到锁返回打开的锁文件（程序退出前保持打开）；已有实例在运行时返回 None"""
    f = open(os.path.join(data_dir, "desktop.lock"), "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _port_file(data_dir):
    return os.path.join(data_dir, "desktop.port")


def running_url(data_dir):
    """已在运行的实例的地址（能连上才算）"""
    try:
        with open(_port_file(data_dir), encoding="utf-8") as f:
            url = f"http://127.0.0.1:{int(f.read().strip())}"
        with urllib.request.urlopen(url + "/api/health", timeout=2):
            return url
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------ 本机网页服务
def start_server(data_dir):
    """在后台线程启动服务，返回 (uvicorn.Server, 地址)。端口由系统分配，只接受本机连接。"""
    import uvicorn
    from webapp.app import create_app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(create_app(data_dir, desktop=True), log_level="warning",
                            loop="asyncio", http="h11", ws="none", lifespan="off")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, kwargs=dict(sockets=[sock]), daemon=True, name="web").start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("本机服务 30 秒内未能启动 / the local service did not start within 30 s")
        time.sleep(0.05)
    with open(_port_file(data_dir), "w", encoding="utf-8") as f:
        f.write(str(port))
    return server, f"http://127.0.0.1:{port}"


# ------------------------------------------------------------ 窗口里的“下载”
def _filename_from(headers, fallback):
    cd = headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    if m:
        return urllib.parse.unquote(m.group(1))
    m = re.search(r'filename="?([^";]+)"?', cd)
    return m.group(1) if m else fallback


def _safe_name(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "download"


class DesktopApi:
    """界面通过 window.pywebview.api 调用：下载改为本机保存对话框（窗口内没有浏览器的下载栏）"""

    def __init__(self, base_url, data_dir):
        self._base, self._data_dir, self._window = base_url, data_dir, None

    def _ask_path(self, filename):
        import webview
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        r = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=downloads,
                                            save_filename=_safe_name(filename))
        if isinstance(r, (list, tuple)):
            r = r[0] if r else None
        return r or None

    def save_url(self, path):
        """保存本机服务上的文件（Excel 工作簿）。只允许本服务的 /api/ 地址。"""
        if not str(path).startswith("/api/"):
            return dict(ok=False, error="bad path")
        with urllib.request.urlopen(self._base + path, timeout=60) as resp:
            data, name = resp.read(), _filename_from(resp.headers, "download.xlsx")
        target = self._ask_path(name)
        if not target:
            return dict(ok=False, cancelled=True)
        with open(target, "wb") as f:
            f.write(data)
        return dict(ok=True, path=target)

    def save_text(self, filename, text):
        """保存界面生成的文件（发给同事核对的 HTML 报告）"""
        target = self._ask_path(filename)
        if not target:
            return dict(ok=False, cancelled=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
        return dict(ok=True, path=target)

    def open_data_dir(self):
        """在访达 / 资源管理器中打开数据目录（备份或迁移图纸库用）"""
        if sys.platform == "win32":
            os.startfile(self._data_dir)  # noqa: S606（只打开本程序自己的数据目录）
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", self._data_dir])
        return dict(ok=True)


def _busy(base_url):
    """是否有任务在排队或计算中"""
    import json
    try:
        with urllib.request.urlopen(base_url + "/api/jobs", timeout=2) as resp:
            jobs = json.load(resp)
    except OSError:
        return False
    return any(j.get("status") in ("queued", "running") for j in jobs)


def run(data_dir=None):
    """打开桌面版；窗口关闭后返回"""
    import webview
    from webapp import jobs as J

    data_dir = data_dir or os.environ.get("LAYOUT_DATA_DIR") or default_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(os.path.join(data_dir, "desktop.log"), encoding="utf-8")])

    lock = acquire_lock(data_dir)
    if lock is None:                       # 已经开着：再开一个窗口连到同一个服务
        url = running_url(data_dir)
        if url:
            webview.create_window(TITLE, url, width=1280, height=900, text_select=True)
            webview.start()
            return
        raise SystemExit("另一个实例正在启动或未正常退出，请稍后再试 / another instance is starting; try again shortly")

    server, url = start_server(data_dir)
    log.info("data dir %s, serving %s", data_dir, url)
    api = DesktopApi(url, data_dir)
    window = webview.create_window(TITLE, url, js_api=api, width=1280, height=900, min_size=(900, 600),
                                   text_select=True)
    api._window = window

    def on_closing():
        if _busy(url) and not window.create_confirmation_dialog(
                "仍在计算 / Still calculating",
                "关闭窗口会中断本次计算，下次打开时显示为“已中断”。确定关闭？\n"
                "Closing stops the current calculation; it will show as interrupted next time. Close anyway?"):
            return False
        return True

    window.events.closing += on_closing
    try:
        webview.start()
    finally:
        J.stop_children()
        server.should_exit = True
        try:
            os.remove(_port_file(data_dir))
        except OSError:
            pass
        lock.close()


def run_script(path, args):
    """打包后的 --run-script：以 __main__ 运行估算脚本（网页服务用子进程调用，与命令行相同）"""
    import runpy
    for stream in ("stdout", "stderr"):
        s = getattr(sys, stream)
        if s is None:                                    # Windows 无控制台程序没有标准输出
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8"))
        elif hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    sys.argv = [path] + list(args)
    runpy.run_path(path, run_name="__main__")
