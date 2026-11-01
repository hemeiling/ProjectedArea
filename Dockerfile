# 产线布局投影面积估算 —— 网页版镜像（Render 或任意 Docker 主机）
#
# Tesseract OCR 在镜像中安装，工位识别开箱可用；libgomp1 供 OpenCV / SciPy 的 OpenMP 使用。
# 数据（图纸库、测量缓存、任务）写到 LAYOUT_DATA_DIR，部署时挂载持久磁盘到该目录。
#
#   docker build -t layout-estimator .
#   docker run -p 8100:8100 -e PORT=8100 -v estimator-data:/var/data layout-estimator

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LAYOUT_DATA_DIR=/var/data

RUN apt-get update && apt-get install --no-install-recommends -y \
        libgomp1 \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && tesseract --version

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN mkdir -p /var/data

# 平台注入 PORT 时 run_web.py 监听 0.0.0.0:$PORT；只能单进程（任务状态在进程内）
EXPOSE 8100
CMD ["python", "run_web.py"]
