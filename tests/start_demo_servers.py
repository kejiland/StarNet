# -*- coding: utf-8 -*-
"""启动本地示例网站(8765) 与模拟 AI 服务(8766)"""
import os
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.normpath(os.path.join(HERE, "..", "samples"))


def serve_files():
    os.chdir(SAMPLES)
    print("示例网站: http://127.0.0.1:8765")
    HTTPServer(("127.0.0.1", 8765), SimpleHTTPRequestHandler).serve_forever()


def serve_ai():
    sys.path.insert(0, HERE)
    from mock_ai_server import Handler
    print("模拟 AI 接口: http://127.0.0.1:8766")
    HTTPServer(("127.0.0.1", 8766), Handler).serve_forever()


if __name__ == "__main__":
    print("本地演示服务已启动")
    for fn in (serve_files, serve_ai):
        threading.Thread(target=fn, daemon=True).start()
    try:
        while True:
            pass
    except KeyboardInterrupt:
        print("已停止服务")
