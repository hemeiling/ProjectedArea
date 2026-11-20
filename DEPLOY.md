# 部署说明 / Deployment guide

产线布局投影面积估算（Layout Footprint Estimator）：FastAPI 网页服务，单进程。
本文件列出部署所需的全部条件。更多细节见 `README.md`。

## 1. 先选部署方式

| 方式 | 适合 | 说明 |
|---|---|---|
| 桌面版安装包 | 个人使用，大图 | 在 GitHub Releases 下载 `LayoutEstimator-<版本>-Windows-Setup.exe`，不需要装 Python，数据都在本机 |
| 本机网页版 | 一台电脑，几个人偶尔用 | 下面的第 3 节，浏览器打开 `http://127.0.0.1:8100` |
| Docker（服务器） | 多人同时用 | 第 4 节，镜像里已含 Tesseract |

## 2. 硬件要求

内存是关键：DXF 要整份读入内存，**峰值约为文件大小的 7–25 倍**（实测：114 MB → 1.4 GB，119 MB → 1.1 GB，374 MB → 9.1 GB，862 MB → 6.3 GB）。

| | 最低（只算 PDF 和 ≤ 100 MB 的 DXF） | 建议（含 400 MB 以内的 DXF） | 大图（800 MB 以上的 DXF） |
|---|---|---|---|
| 内存 | 4 GB（实例至少 2 GB 可用） | 16 GB | 32 GB |
| CPU | 2 核 | 4 核以上 | 4 核以上（单张图基本单线程，核多不会更快） |
| 磁盘 | 2 GB + 图纸 | 见下 | 见下 |
| 网络 | 本机版不需要 | 内网访问即可 | |

**磁盘**：增长的主要部分是上传的 DXF 原文件，平均约 350 MB/张（你们现有 4 张共 1.4 GB）。
测量缓存每张几百 KB，每个估算任务约 3–12 MB，包装方式库、设备改判、画的区域都是几 KB。
按 100 张 DXF 约 35 GB、500 张约 175 GB 规划，再加 20% 余量；用持久磁盘并定期备份 `LAYOUT_DATA_DIR`。

计算速度（实测）：一张 DXF 1–7 分钟，一张 A3 的 PDF 约十几秒到 1 分钟。**一次只算一个任务**，多人同时提交会排队。

> 内存与用时是 2026-11-02 版本的实测，新版本在测量时多存了每台设备的轮廓，内存会略多，尚未重新测量。

## 3. 本机 / 服务器直接运行（不用 Docker）

软件：
- Python 3.9 以上（开发环境 3.9.6，Docker 镜像用 3.12）
- Tesseract OCR（含英文语言包）——只有 PDF 的工位识别需要；DXF 不需要。没装时网页自动跳过并提示
  - macOS：`brew install tesseract`
  - Ubuntu / Debian：`sudo apt-get install tesseract-ocr tesseract-ocr-eng libgomp1`
  - Windows：安装 UB Mannheim 版 Tesseract，并加入 PATH
- Python 依赖在 `requirements.txt`（pymupdf、opencv-python-headless、scipy、numpy、pillow、pyyaml、pytesseract、openpyxl、ezdxf、shapely、fastapi、uvicorn、python-multipart）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run_web.py --port 8100        # 只监听 127.0.0.1
```

要让同一网络的同事访问：设置环境变量 `PORT`（此时监听 `0.0.0.0:$PORT`），并务必设置 `ACCESS_PASSWORD`。

## 4. Docker

```bash
docker build -t layout-estimator .
docker run -d -p 8100:8100 -e PORT=8100 -e ACCESS_PASSWORD=改成你的密码 \
  -e LAYOUT_MAX_DXF_MB=2000 -v estimator-data:/var/data layout-estimator
```

`render.yaml` 是 Render 的配置（1c-2g 实例只够 ≤ 100 MB 的 DXF，所以里面把 `LAYOUT_MAX_DXF_MB` 设为 100）；自建服务器请按第 2 节的内存选规格。

## 5. 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `PORT` | 未设置 | 设置后监听 `0.0.0.0:$PORT`；不设则只监听 `127.0.0.1:8100` |
| `LAYOUT_DATA_DIR` | `./data` | 数据目录：图纸库、测量缓存、任务、包装方式库。**部署时指向持久磁盘** |
| `ACCESS_PASSWORD` | 空（不设密码） | 设置后整站要密码（用户名任意），`/api/health` 除外。放在内网以外一定要设 |
| `LAYOUT_MAX_DXF_MB` | 2048 | 单个 DXF 的大小上限；按服务器内存调（内存的 1/25 左右） |

## 6. 必须知道的限制

- **只能单进程、单实例**：任务状态在进程内，不要用多个 worker 或多个副本。
- 上传的图纸可能含客户机密：不要把 `data/` 放到公开位置，不要提交进版本库（已在 `.gitignore` 里）。
- 只支持矢量 PDF 和 DXF，不支持 DWG（需要先在 AutoCAD 里另存为 DXF），不支持扫描件。
- 点设备改判、画区域只支持 DXF，且旧测量结果要重新测量一次才能用（新建一次计算）。
- 包装系数、公摊工时系数、运价是业务输入，在页面“估算参数”里改；默认值来自 `config.yaml`，不是从 CAD 里算出来的。

## 7. 部署后自检

```bash
curl http://<服务器>:8100/api/health        # 应返回 {"ok":true,"ocr_available":true}
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q    # 全部通过
```

`ocr_available` 为 false 表示没装 Tesseract：DXF 不受影响，PDF 的工位识别会被跳过。
然后上传一张小的 DXF 或 PDF 走一遍：能出面积、叠加图和 Excel 即部署成功。

## 8. 备份与升级

- 备份：整个 `LAYOUT_DATA_DIR`（`library/` 是原图和人工修正，`jobs/` 是历史结果，`packing_library.json` 是包装方式库）。
- 升级：替换代码后重启即可。测量代码变了，旧的测量缓存会自动失效，同一文件下次计算会重新测量；人工修正和包装方式库不受影响。
