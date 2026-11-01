#!/usr/bin/env python3
"""
启动网页版（本机）：

    .venv/bin/python run_web.py              # http://127.0.0.1:8100
    .venv/bin/python run_web.py --port 9000

设置了 PORT 环境变量（部署平台）时监听 0.0.0.0:$PORT。
"""
import argparse
import logging
import os
import sys

import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8100)))
    ap.add_argument("--host", default="0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.chdir(HERE)
    print(f"\n  产线布局投影面积估算 / Layout Footprint Estimator → http://{a.host}:{a.port}\n", flush=True)
    uvicorn.run("webapp.app:create_app", factory=True, host=a.host, port=a.port)
