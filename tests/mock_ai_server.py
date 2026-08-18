# -*- coding: utf-8 -*-
"""本地模拟 OpenAI 兼容接口，用于无真实 Key 时测试 AI 抓取流程。"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        user = ""
        for m in body.get("messages", []):
            if m["role"] == "user":
                user = m["content"]
        if "回复两个字母" in user:
            reply = "OK"
        else:
            reply = json.dumps({
                "产品名称": "智能恒温恒湿试验箱 HWS-250（AI 提取）",
                "用途": "用于电子、医药、食品等行业的环境模拟试验与可靠性测试。",
                "设备介绍": "AI 版：高精度环境模拟设备，采用 PID 温湿度控制技术。",
                "性能特点": "· 控温精度 ±0.5℃\n· 7 英寸触摸屏\n· 多重安全保护",
                "设备参数": "容积：250 L\n温度范围：-20℃ ~ +150℃\n湿度范围：20% ~ 98% RH",
                "参考图片": "http://127.0.0.1:8765/images/hws-250.jpg"
            }, ensure_ascii=False)
        resp = {"choices": [{"message": {"content": reply}}]}
        data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("mock AI server on 127.0.0.1:8766")
    HTTPServer(("127.0.0.1", 8766), Handler).serve_forever()
