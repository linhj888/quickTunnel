#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QuickTunnel 演示 Web 服务 — 独立运行于 9999 端口，用于测试内网穿透"""
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9999
START_TIME = time.time()


class DemoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")

    def _page(self):
        uptime = int(time.time() - START_TIME)
        return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Demo · QuickTunnel</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;background:#0B0E14;color:#E6EDF7;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.card{{background:#111621;border:1px solid #1F2A3D;border-radius:14px;padding:40px 48px;max-width:460px;text-align:center}}
h1{{margin:0 0 6px;font-size:22px}}
.ok{{color:#34D399;font-size:14px;margin:0 0 24px}}
.meta{{color:#8892A6;font-size:13px;line-height:1.9;margin:0}}
.port{{font-family:Menlo,monospace;color:#22D3EE}}
</style>
</head>
<body>
<div class="card">
<h1>⇅ Demo 服务运行中</h1>
<p class="ok">✓ 页面渲染正常，穿透链路可用</p>
<p class="meta">监听端口: <span class="port">:{PORT}</span></p>
<p class="meta">已运行: {uptime // 60} 分 {uptime % 60} 秒</p>
<p class="meta">来自: {self.address_string()}</p>
</div>
</body>
</html>"""

    def do_GET(self):
        if self.path == "/health":
            payload = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = self._page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), DemoHandler)
    except OSError:
        raise SystemExit(f"[错误] 端口 {PORT} 已被占用")
    print(f"[*] Demo 服务已启动: http://localhost:{PORT}  (Ctrl+C 停止)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 已停止")
