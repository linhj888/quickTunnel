#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" QuickTunnel — 零配置、一键式内网穿透（桌面版）
基于 Cloudflare Tunnel (cloudflared) 官方内核二次封装，内置反向代理支持多端口 + 自定义路径。
用法:
    python3 quicktunnel.py          # 启动桌面 GUI
    python3 quicktunnel.py 3000     # CLI 直连模式：穿透单个端口
    python3 quicktunnel.py http://localhost:8080/app
环境变量:
    QUICKTUNNEL_CF_PATH  指定本地 cloudflared 内核路径（跳过自动下载）
    QUICKTUNNEL_MIRROR   自定义 GitHub 镜像前缀，如 https://gh-proxy.com/%7Burl%7D
"""
import argparse
import http.client
import json
import lzma
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- 常量 ----------
BIN_DIR = os.path.join(os.path.expanduser("~"), ".quicktunnel", "bin")
CF_LOCAL = os.path.join(BIN_DIR, "cloudflared.exe" if os.name == "nt" else "cloudflared")
DOWNLOAD_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download"
MIRRORS = [
    "https://gh-proxy.com/%7Burl%7D",
    "https://ghfast.top/%7Burl%7D",
]
TARGETS = {
    ("darwin", "arm64"): "cloudflared-darwin-arm64.tgz",
    ("darwin", "x86_64"): "cloudflared-darwin-amd64.tgz",
    ("linux", "x86_64"): "cloudflared-linux-amd64",
    ("linux", "arm64"): "cloudflared-linux-arm64",
    ("windows", "amd64"): "cloudflared-windows-amd64.exe",
}
URL_RE = re.compile(r"https://[-a-zA-Z0-9]+.trycloudflare.com")
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
PROXY_PORT = 9417

# ---------- 终端颜色 ----------
def _supports_color():
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"

def c(text, code):
    return f"\033[{code}m{text}\033[0m" if _supports_color() else text

def bold(t): return c(t, "1")
def green(t): return c(t, "32")
def yellow(t): return c(t, "33")
def red(t): return c(t, "31")
def cyan(t): return c(t, "36")

# ---------- 全局状态 ----------
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = []
        self.running = False
        self.want_running = False
        self.url = None
        self.cf_proc = None
        self.logs = deque(maxlen=400)
        self.on_update = None
        self.installed = False
        self.installing = False
        self.install_progress = 0
        self.install_status = "未安装"
        self.ever_connected = False

    def log(self, msg):
        entry = {"t": time.strftime("%H:%M:%S"), "msg": msg}
        with self.lock:
            self.logs.append(entry)
        print(msg)
        if self.on_update:
            self.on_update()

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "url": self.url,
                "entries": [dict(e) for e in self.entries],
                "logs": list(self.logs),
                "installed": self.installed,
                "installing": self.installing,
                "install_progress": self.install_progress,
                "install_status": self.install_status,
            }

STATE = State()

# ---------- 配置持久化 ----------
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".quicktunnel", "config.json")

def save_config():
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"entries": STATE.snapshot()["entries"]}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        STATE.log(f"[!] 配置保存失败: {e}")

def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        STATE.log(f"[!] 配置读取失败: {e}")
        return
    for e in data.get("entries", []):
        p, path = e.get("port"), normalize_public_path(e.get("path"))
        if not (isinstance(p, int) and 1 <= p <= 65535 and path is not False):
            continue
        if path is None:
            path = f"/p{p}"
        with STATE.lock:
            if any(x["port"] == p for x in STATE.entries):
                continue
            if any(x["path"] == path for x in STATE.entries):
                continue
            STATE.entries.append({"port": p, "path": path})
    if STATE.entries:
        STATE.log(f"[*] 已恢复上次配置: {len(STATE.entries)} 个端口映射")

def normalize_public_path(raw):
    if raw is None or str(raw).strip() == "":
        return None
    p = str(raw).strip()
    if not p.startswith("/"):
        p = "/" + p
    p = re.sub(r"/{2,}", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    if p in ("", "/"):
        return "/"
    if not re.fullmatch(r"/[A-Za-z0-9-_.!]+", p):
        return False
    return p

# ---------- 内核管理 ----------
def find_cloudflared():
    env_path = os.environ.get("QUICKTUNNEL_CF_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path, "env"
    which = shutil.which("cloudflared")
    if which:
        return which, "system"
    if os.path.isfile(CF_LOCAL):
        return CF_LOCAL, "cache"
    return None, "none"

def _fetch(url, timeout=120, progress_cb=None):
    candidates = [url]
    custom = os.environ.get("QUICKTUNNEL_MIRROR")
    mirror_templates = ([custom] if custom else []) + MIRRORS
    candidates += [tpl.format(url=url) for tpl in mirror_templates]
    last_err = None
    for i, u in enumerate(candidates):
        try:
            STATE.log(f"[*] 正在下载: {u}")
            req = urllib.request.Request(u, headers={"User-Agent": "QuickTunnel/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp, tempfile.NamedTemporaryFile(delete=False, suffix=os.path.basename(url)) as tmp:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)
                return tmp.name
        except Exception as e:
            last_err = e
            STATE.log(f"[!] 下载失败 ({e})" + ("，尝试备用源…" if i < len(candidates) - 1 else ""))
    raise RuntimeError(f"内核下载失败: {last_err}\n可手动下载 {url} 后设置 QUICKTUNNEL_CF_PATH 指向该文件。")

def _install_local_pkg(filename):
    pkg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pkg", f"{os.path.splitext(filename)[0]}.xz")
    if not os.path.isfile(pkg_path):
        return False
    STATE.install_status = "解压中"
    if STATE.on_update:
        STATE.on_update()
    try:
        with open(pkg_path, "rb") as f:
            data = lzma.decompress(f.read())
        with open(CF_LOCAL, "wb") as f:
            f.write(data)
        os.chmod(CF_LOCAL, 0o755)
        return True
    except Exception as e:
        STATE.log(f"[!] 内置安装包不可用 ({e})，改用在线下载")
        return False

def download_cloudflared():
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        machine = "amd64"
    key = (system, machine)
    if key not in TARGETS:
        raise RuntimeError(f"暂不支持的平台: {system}/{machine}，请手动下载 cloudflared 后设置 QUICKTUNNEL_CF_PATH。")
    filename = TARGETS[key]
    url = f"{DOWNLOAD_BASE}/{filename}"
    os.makedirs(BIN_DIR, exist_ok=True)
    STATE.log(f"[*] 首次运行，正在准备 cloudflared 内核 ({system}/{machine}) ...")

    if _install_local_pkg(filename):
        STATE.installed = True
        STATE.installing = False
        STATE.install_status = "已安装"
        STATE.log(f"[✓] 内核已安装到 {CF_LOCAL}")
        return CF_LOCAL

    def progress_cb(downloaded, total):
        STATE.install_progress = downloaded
        if total > 0:
            STATE.install_status = f"下载中 {int(downloaded / total * 100)}%"
        else:
            STATE.install_status = f"下载中 {downloaded // 1024} KB"
        if STATE.on_update:
            STATE.on_update()

    tmp_path = _fetch(url, progress_cb=progress_cb)
    try:
        STATE.install_status = "解压中"
        if STATE.on_update:
            STATE.on_update()
        if filename.endswith(".tgz"):
            with tarfile.open(tmp_path) as tf:
                member = next(m for m in tf.getmembers() if m.name.endswith("cloudflared"))
                member.name = os.path.basename(CF_LOCAL)
                tf.extract(member, BIN_DIR, filter="data")
        else:
            shutil.move(tmp_path, CF_LOCAL)
        os.chmod(CF_LOCAL, 0o755)
        STATE.installed = True
        STATE.installing = False
        STATE.install_status = "已安装"
        STATE.log(f"[✓] 内核已安装到 {CF_LOCAL}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return CF_LOCAL

def ensure_cloudflared():
    cf, source = find_cloudflared()
    if cf:
        src_name = {"env": "环境变量指定", "system": "系统 PATH", "cache": "本地缓存"}[source]
        STATE.log(f"[*] 使用 cloudflared 内核 ({src_name}): {cf}")
        return cf
    return download_cloudflared()

def install_cloudflared_async():
    if STATE.installing:
        return
    STATE.installing = True
    STATE.install_progress = 0
    STATE.install_status = "准备中"
    if STATE.on_update:
        STATE.on_update()

    def worker():
        try:
            download_cloudflared()
        except Exception as e:
            STATE.installing = False
            STATE.install_status = "安装失败"
            STATE.log(f"[!] 内核安装失败: {e}")
            if STATE.on_update:
                STATE.on_update()

    threading.Thread(target=worker, daemon=True, name="cf-install").start()

# ---------- 公共页面 ----------
def _page_shell(title, body):
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;background:#0B0E14;color:#E6EDF7;margin:0;min-height:100vh;display:flex;flex-direction:column}}
header{{padding:18px 28px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #1F2A3D}}
.logo{{font-weight:800;letter-spacing:1px}}
.badge{{font-size:11px;color:#8892A6;border:1px solid #2A3650;padding:3px 10px;border-radius:99px}}
main{{flex:1;max-width:720px;width:100%;margin:0 auto;padding:40px 20px}}
h1{{font-size:24px;margin:0 0 8px}}
.sub{{color:#8892A6;margin:0 0 28px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}}
.card{{display:block;text-decoration:none;background:#111621;border:1px solid #1F2A3D;border-radius:14px;padding:20px}}
.card .p{{font-family:monospace;color:#22D3EE;font-size:15px}}
.card .d{{color:#8892A6;font-size:12px;margin-top:8px}}
.err{{background:#151B28;border:1px solid #1D3A2A;border-radius:14px;padding:24px;text-align:center}}
.err .big{{font-size:18px;margin:0 0 10px}}
.err .detail{{color:#8892A6;font-size:13px}}
footer{{padding:18px 28px;color:#5B6778;font-size:12px;border-top:1px solid #1F2A3D}}
</style>
</head>
<body>
<header><span class="logo">⇅ QUICKTUNNEL</span><span class="badge">反向代理</span></header>
{body}
<footer>由 QuickTunnel 提供穿透服务 · 仅开放你授权的端口</footer>
</body>
</html>"""

def landing_page():
    with STATE.lock:
        entries = [dict(e) for e in STATE.entries]
    if not entries:
        body = """
<main><h1>服务目录</h1>
<p class="sub">当前没有开放任何端口</p>
<p class="sub">请在 QuickTunnel 桌面端中添加需要暴露的服务端口</p>
</main>"""
    else:
        cards = "".join(
            f'<a class="card" href="{e["path"]}"><div class="p">:{e["port"]} → {e["path"]}</div><div class="d">点击进入对应服务</div></a>'
            for e in entries
        )
        body = f"""
<main><h1>服务目录</h1>
<p class="sub">共 {len(entries)} 个本地服务已开放公网访问，点击卡片进入对应服务</p>
<div class="cards">{cards}</div>
</main>"""
    return _page_shell("服务目录 · QuickTunnel", body)

def error_page(port, detail=""):
    body = f"""
<main><div class="err">
<p class="big" style="color:#34D399">✓ 通道正常，端口 :{port} 已开放</p>
<p class="detail">公网请求已成功到达你的电脑，QuickTunnel 工作正常。</p>
<p class="detail">但本地端口 :{port} 上没有检测到正在运行的服务，暂时无法返回内容。</p>
<p class="detail" style="margin-top:14px;color:#E6EDF7">请启动该端口对应的本地服务，然后刷新本页即可正常访问。</p>
</div></main>"""
    return _page_shell("端口已开放 · 本地服务未响应", body)

# ---------- 反向代理 ----------
class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _route(self, path):
        with STATE.lock:
            entries = sorted(STATE.entries, key=lambda e: -len(e["path"]))
        for e in entries:
            ep = e["path"]
            if ep == "/":
                return e["port"], path, "/"
            if path == ep or path.startswith(ep + "/"):
                rest = path[len(ep):]
                return e["port"], (rest or "/"), ep
        return None

    def _handle(self):
        routed = self._route(self.path.split("?")[0])
        if routed is None:
            self._send_html(landing_page())
            return
        port, new_path, prefix = routed
        if "?" in self.path:
            new_path += "?" + self.path.split("?", 1)[1]
        if "websocket" in (self.headers.get("Upgrade") or "").lower():
            self._relay_ws(port, new_path)
            return
        try:
            self._forward(port, new_path, prefix)
        except Exception:
            try:
                self._send_html(error_page(port), status=503)
            except Exception:
                pass

    def _relay_ws(self, port, path):
        import socket as _s
        hdr = [f"{self.command} {path} {self.request_version}"]
        for k, v in self.headers.items():
            if k.lower() == "host":
                continue
            hdr.append(f"{k}: {v}")
        hdr.append(f"Host: 127.0.0.1:{port}")
        raw = ("\r\n".join(hdr) + "\r\n\r\n").encode("latin-1", "replace")
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            raw += self.rfile.read(length)
        try:
            up = _s.create_connection(("127.0.0.1", port), timeout=15)
        except Exception:
            self._send_html(error_page(port), status=503)
            return
        up.settimeout(None)
        up.sendall(raw)
        self.connection.settimeout(None)
        self.close_connection = True

        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.shutdown(_s.SHUT_RDWR)
                    except Exception:
                        pass

        t = threading.Thread(target=pump, args=(self.connection, up), daemon=True)
        t.start()
        pump(up, self.connection)
        t.join(timeout=2)

    def _forward(self, port, path, prefix="/"):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {}
        for k, v in self.headers.items():
            if k.lower() not in HOP_HEADERS:
                headers[k] = v
        headers["X-Forwarded-Host"] = self.headers.get("Host", "")
        headers["X-Forwarded-Proto"] = "https"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in HOP_HEADERS:
                    continue
                if lk == "location" and v.startswith("/") and prefix != "/":
                    v = prefix + v
                self.send_header(k, v)
            self.send_header("Content-Length", str(0 if self.command == "HEAD" else len(data)))
            self.end_headers()
            if self.command != "HEAD" and data:
                self.wfile.write(data)
        finally:
            conn.close()

    def _send_html(self, html, status=200):
        payload = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

def start_proxy_server(port=PROXY_PORT):
    server = ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="reverse-proxy").start()
    return server

# ---------- 隧道控制 ----------
RECONNECT_DELAYS = [1, 2, 5, 10, 20, 30]

def _read_cf_output(proc):
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        STATE.log(f"[cloudflared] {line}")
        m = URL_RE.search(line)
        if m and not STATE.url:
            STATE.url = m.group(0)
            STATE.running = True
            if STATE.ever_connected:
                STATE.log("[!] 公网地址已变更，旧分享链接已失效，请重新复制！")
            else:
                STATE.ever_connected = True
                STATE.log(f"[✓] 隧道已建立! 公网访问地址: {STATE.url}")
    was_crash = STATE.cf_proc is proc
    if was_crash:
        STATE.cf_proc = None
        STATE.running = False
        STATE.url = None
        if STATE.want_running:
            STATE.log("[!] 隧道意外断开，正在自动重连...")
            threading.Thread(target=_reconnect_loop, daemon=True, name="cf-reconnect").start()
        else:
            STATE.log("[*] 隧道已关闭")

def _reconnect_loop():
    for delay in RECONNECT_DELAYS:
        if not STATE.want_running:
            return
        time.sleep(delay)
        if not STATE.want_running:
            return
        r = start_tunnel()
        if r.get("ok") and STATE.cf_proc is not None:
            STATE.log("[✓] 已自动重连，新地址建立后将更新显示")
            return
    STATE.log("[!] 自动重连失败，请手动点击「启动隧道」重试")
    STATE.want_running = False

def start_tunnel():
    if STATE.running or STATE.cf_proc is not None:
        return {"ok": True, "msg": "隧道已在运行或正在启动"}
    if not STATE.entries:
        return {"ok": False, "msg": "请先添加至少一个本地端口"}
    if not STATE.installed:
        return {"ok": False, "msg": "cloudflared 内核未安装，请先一键安装"}
    try:
        cf, _ = find_cloudflared()
        if not cf:
            return {"ok": False, "msg": "未找到 cloudflared 内核，请先一键安装"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    try:
        proc = subprocess.Popen(
            [cf, "tunnel", "--url", f"http://127.0.0.1:{PROXY_PORT}", "--no-autoupdate"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    except FileNotFoundError:
        return {"ok": False, "msg": "cloudflared 启动失败，请检查内核路径"}
    STATE.cf_proc = proc
    STATE.want_running = True
    STATE.ever_connected = STATE.ever_connected
    STATE.log("[*] 正在建立隧道（约需数秒）...")
    threading.Thread(target=_read_cf_output, args=(proc,), daemon=True, name="cf-reader").start()
    return {"ok": True, "msg": "隧道启动中"}

def stop_tunnel():
    proc = STATE.cf_proc
    STATE.want_running = False
    if proc is None:
        return {"ok": True, "msg": "隧道未在运行"}
    STATE.log("[*] 正在停止隧道...")
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    STATE.cf_proc = None
    STATE.running = False
    STATE.url = None
    STATE.log("[✓] 隧道已停止")
    return {"ok": True, "msg": "隧道已停止"}

def add_port_entry(port, pub):
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return False, "端口必须为 1-65535 的整数"
    if pub is False:
        return False, "路径仅支持字母/数字/-/_ 等安全字符"
    with STATE.lock:
        if any(e["port"] == port for e in STATE.entries):
            return False, f"端口 {port} 已存在"
        if pub is None:
            pub = "/" if not STATE.entries else f"/p{port}"
        if any(e["path"] == pub for e in STATE.entries):
            if pub == "/":
                return False, "根路径已被其他端口占用"
            return False, f"路径 {pub} 已被占用"
        STATE.entries = STATE.entries + [{"port": port, "path": pub}]
    STATE.log(f"[+] 已开放端口 {port} → {pub}")
    save_config()
    return True, f"已开放端口 {port} → {pub}"

def remove_port_entry(port):
    with STATE.lock:
        STATE.entries = [e for e in STATE.entries if e["port"] != port]
    STATE.log(f"[-] 已移除端口 {port}")
    save_config()

# ---------- 桌面 GUI ----------
def run_gui():
    try:
        import customtkinter as ctk
    except ImportError:
        sys.exit("[!] 桌面 GUI 需要 customtkinter 库，请先执行: pip install customtkinter")
    import tkinter as tk

    load_config()
    cf_path, cf_src = find_cloudflared()
    if cf_path:
        STATE.installed = True
        STATE.install_status = "已安装"
        STATE.log(f"[*] 检测到 cloudflared 内核 ({cf_src})")
    else:
        STATE.installed = False
        STATE.install_status = "未安装"
        STATE.log("[*] 未检测到 cloudflared 内核，正在自动安装…")
        install_cloudflared_async()

    BG, CARD, BORDER = "#0B0E14", "#111621", "#1F2A3D"
    INPUT, TEXT, MUTED, DIM = "#0D1220", "#E6EDF7", "#8892A6", "#5B6778"
    ACCENT, ACCENT_HOVER, ACCENT_FG = "#22D3EE", "#0FB5D6", "#06222B"
    GREEN, RED, RED_BG, RED_HOVER = "#34D399", "#FF6B6B", "#3A1D1D", "#4A2424"
    BTN, BTN_HOVER = "#1C2740", "#243352"
    MONO = "Menlo" if os.name != "nt" else "Consolas"

    def _font(size, weight="normal"):
        return ctk.CTkFont(size=size, weight=weight)

    def _mono(size):
        return ctk.CTkFont(family=MONO, size=size)

    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.title("QuickTunnel")
    root.geometry("920x620")
    root.minsize(820, 560)
    root.configure(fg_color=BG)

    # ---------- 左侧栏 ----------
    sidebar = ctk.CTkFrame(root, width=216, corner_radius=0, fg_color=CARD)
    sidebar.pack(side="left", fill="y")
    sidebar.pack_propagate(False)

    brand = ctk.CTkFrame(sidebar, fg_color="transparent")
    brand.pack(fill="x", padx=22, pady=(28, 0))
    ctk.CTkLabel(brand, text="⇅", font=_font(24, "bold"), text_color=ACCENT).pack(side="left")
    ctk.CTkLabel(brand, text=" QuickTunnel", font=_font(18, "bold"), text_color=TEXT).pack(side="left")
    ctk.CTkLabel(sidebar, text="零配置 · 一键内网穿透", font=_font(12), text_color=MUTED).pack(anchor="w", padx=22, pady=(2, 0))

    status_wrap = ctk.CTkFrame(sidebar, fg_color="transparent")
    status_wrap.pack(anchor="w", padx=22, pady=(40, 10))
    status_dot = ctk.CTkLabel(status_wrap, text="●", font=_font(13), text_color=MUTED)
    status_dot.pack(side="left")
    status_lbl = ctk.CTkLabel(status_wrap, text="已停止", font=_font(15, "bold"), text_color=TEXT)
    status_lbl.pack(side="left", padx=8)

    toggle_btn = ctk.CTkButton(sidebar, text="▶  启动隧道", height=44, corner_radius=12,
                               font=_font(14, "bold"), fg_color=ACCENT, hover_color=ACCENT_HOVER,
                               text_color=ACCENT_FG, command=lambda: do_toggle())
    toggle_btn.pack(fill="x", padx=22)

    ctk.CTkFrame(sidebar, height=1, corner_radius=0, fg_color=BORDER).pack(fill="x", padx=22, pady=26)

    ctk.CTkLabel(sidebar, text="CLOUDFLARE 内核", font=_font(11), text_color=DIM).pack(anchor="w", padx=22)
    install_btn = ctk.CTkButton(sidebar, text="一键安装", height=34, corner_radius=10,
                                font=_font(13), fg_color=BTN, hover_color=BTN_HOVER, text_color=TEXT,
                                command=lambda: install_cloudflared_async())
    install_btn.pack(fill="x", padx=22, pady=(6, 0))

    ctk.CTkLabel(sidebar, text="仅开放你授权的端口", font=_font(11), text_color=DIM).pack(side="bottom", pady=18)

    # ---------- 主区域 ----------
    main = ctk.CTkFrame(root, fg_color="transparent")
    main.pack(side="left", fill="both", expand=True)
    main.grid_columnconfigure(0, weight=1)
    main.grid_rowconfigure(2, weight=1)

    url_card = ctk.CTkFrame(main, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
    url_card.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 12))
    ctk.CTkLabel(url_card, text="公网地址", font=_font(11), text_color=MUTED).pack(anchor="w", padx=18, pady=(14, 2))
    url_row = ctk.CTkFrame(url_card, fg_color="transparent")
    url_row.pack(fill="x", padx=18, pady=(0, 14))
    url_lbl = ctk.CTkLabel(url_row, text="— 等待隧道建立", font=_mono(14), text_color=MUTED,
                           anchor="w", wraplength=480)
    url_lbl.pack(side="left", fill="x", expand=True)

    def open_url():
        if STATE.url:
            import webbrowser
            webbrowser.open(STATE.url)

    def copy_url():
        if STATE.url:
            root.clipboard_clear()
            root.clipboard_append(STATE.url)
            flash("公网地址已复制")

    ctk.CTkButton(url_row, text="复制", width=64, height=30, corner_radius=8, font=_font(12),
                  fg_color=BTN, hover_color=BTN_HOVER, text_color=TEXT,
                  command=copy_url).pack(side="left", padx=(8, 0))
    ctk.CTkButton(url_row, text="打开", width=64, height=30, corner_radius=8, font=_font(12),
                  fg_color=BTN, hover_color=BTN_HOVER, text_color=TEXT,
                  command=open_url).pack(side="left", padx=(8, 0))

    port_card = ctk.CTkFrame(main, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
    port_card.grid(row=1, column=0, sticky="ew", padx=18, pady=0)
    ctk.CTkLabel(port_card, text="端口映射", font=_font(11), text_color=MUTED).pack(anchor="w", padx=18, pady=(14, 4))
    add_row = ctk.CTkFrame(port_card, fg_color="transparent")
    add_row.pack(fill="x", padx=18)
    port_entry = ctk.CTkEntry(add_row, width=110, height=32, corner_radius=8, font=_mono(13),
                              placeholder_text="本地端口", fg_color=INPUT, border_color=BORDER, text_color=TEXT)
    port_entry.pack(side="left")
    ctk.CTkLabel(add_row, text="→", font=_font(13), text_color=MUTED).pack(side="left", padx=8)
    path_entry = ctk.CTkEntry(add_row, height=32, corner_radius=8, font=_mono(13),
                              placeholder_text="公网路径，可留空自动生成", fg_color=INPUT, border_color=BORDER, text_color=TEXT)
    path_entry.pack(side="left", fill="x", expand=True)

    def do_add():
        raw = port_entry.get().strip()
        if not re.fullmatch(r"\d{1,5}", raw or ""):
            flash("请输入 1-65535 之间的端口号", err=True)
            return
        ok, msg = add_port_entry(int(raw), normalize_public_path(path_entry.get()))
        if ok:
            port_entry.delete(0, "end")
            path_entry.delete(0, "end")
        flash(msg, err=not ok)
        refresh()

    for ent in (port_entry, path_entry):
        ent.bind("<Return>", lambda e: do_add())
    ctk.CTkButton(add_row, text="添加", width=72, height=32, corner_radius=8, font=_font(13, "bold"),
                  fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ACCENT_FG,
                  command=do_add).pack(side="left", padx=(8, 0))

    port_list = ctk.CTkScrollableFrame(port_card, height=116, corner_radius=10, fg_color=INPUT,
                                       scrollbar_button_color=BORDER, scrollbar_button_hover_color=BTN_HOVER)
    port_list.pack(fill="x", padx=18, pady=(10, 14))

    log_card = ctk.CTkFrame(main, fg_color=CARD, corner_radius=14, border_width=1, border_color=BORDER)
    log_card.grid(row=2, column=0, sticky="nsew", padx=18, pady=(12, 18))
    ctk.CTkLabel(log_card, text="运行日志", font=_font(11), text_color=MUTED).pack(anchor="w", padx=18, pady=(14, 4))
    log_box = tk.Text(log_card, height=8, wrap="word", relief="flat", bd=0, highlightthickness=0,
                      font=(MONO, 11), bg=INPUT, fg=MUTED, insertbackground=TEXT,
                      selectbackground=BTN)
    log_box.pack(fill="both", expand=True, padx=18, pady=(0, 14))
    log_box.tag_configure("err", foreground=RED)
    log_box.tag_configure("ok", foreground=GREEN)
    log_box.tag_configure("t", foreground=DIM)

    toast_lbl = ctk.CTkLabel(root, text="", fg_color=BTN, corner_radius=10, height=36, font=_font(12), text_color=TEXT)

    def flash(msg, err=False):
        toast_lbl.configure(text=f"  {msg}  ", fg_color=RED_BG if err else BTN,
                            text_color=RED if err else TEXT)
        toast_lbl.place(relx=0.5, rely=0.93, anchor="center")
        root.after(2200, toast_lbl.place_forget)

    def do_toggle():
        if STATE.running or STATE.cf_proc is not None:
            stop_tunnel()
        else:
            r = start_tunnel()
            if not r["ok"]:
                flash(r["msg"], err=True)
        refresh()

    port_sig = [""]

    def _rebuild_ports(entries):
        for w in port_list.winfo_children():
            w.destroy()
        if not entries:
            ctk.CTkLabel(port_list, text="暂无端口映射，请添加需要暴露的本地服务", font=_font(12), text_color=DIM).pack(anchor="w", padx=12, pady=8)
            return
        for e in entries:
            row = ctk.CTkFrame(port_list, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=2)
            ctk.CTkLabel(row, text=f":{e['port']}", font=_mono(13), text_color=ACCENT).pack(side="left")
            ctk.CTkLabel(row, text="→", font=_font(12), text_color=DIM).pack(side="left", padx=10)
            ctk.CTkLabel(row, text="/" if e["path"] == "/" else e["path"], font=_mono(13), text_color=TEXT).pack(side="left")

            def _visit(p=e["path"]):
                if STATE.url:
                    import webbrowser
                    webbrowser.open(STATE.url.rstrip("/") + p)
                else:
                    flash("隧道尚未建立，无法访问", err=True)

            def _remove(p=e["port"]):
                remove_port_entry(p)
                refresh()

            ctk.CTkButton(row, text="访问", width=44, height=24, corner_radius=6,
                          fg_color="transparent", hover_color=BTN_HOVER, text_color=ACCENT,
                          command=_visit).pack(side="right", padx=(0, 6))
            ctk.CTkButton(row, text="✕", width=28, height=24, corner_radius=6,
                          fg_color="transparent", hover_color=RED_BG, text_color=MUTED,
                          command=_remove).pack(side="right")

    # ---------- 状态刷新 ----------
    def refresh():
        try:
            if not root.winfo_exists():
                return
        except Exception:
            return
        s = STATE.snapshot()
        running = s["running"]
        starting = STATE.cf_proc is not None and not running
        status_dot.configure(text_color=GREEN if running else (ACCENT if starting else MUTED))
        status_lbl.configure(text="运行中" if running else ("启动中" if starting else "已停止"),
                             text_color=GREEN if running else (ACCENT if starting else TEXT))
        toggle_btn.configure(text="■  停止隧道" if running or starting else "▶  启动隧道",
                             fg_color=RED_BG if running or starting else ACCENT,
                             hover_color=RED_HOVER if running or starting else ACCENT_HOVER,
                             text_color=RED if running or starting else ACCENT_FG,
                             state="disabled" if not STATE.installed and not STATE.installing else "normal")
        url_lbl.configure(text=s["url"] or ("正在建立隧道，请稍候…" if starting else "— 等待隧道建立"),
                          text_color=ACCENT if s["url"] else MUTED)
        if STATE.installing:
            install_btn.configure(text=s["install_status"] or "安装中...", state="disabled", text_color=MUTED)
        elif STATE.installed:
            install_btn.configure(text="✓ 内核已安装", state="disabled", text_color=GREEN)
        else:
            install_btn.configure(text="一键安装", state="normal", text_color=TEXT)
        sig = ";".join(f"{e['port']}{e['path']}" for e in s["entries"])
        if sig != port_sig[0]:
            port_sig[0] = sig
            _rebuild_ports(s["entries"])
        log_box.configure(state="normal")
        log_box.delete("1.0", "end")
        for l in s["logs"][-100:]:
            tag = "err" if ("[!]" in l["msg"] or "错误" in l["msg"] or "退出" in l["msg"]) else \
                ("ok" if ("[✓]" in l["msg"] or "已建立" in l["msg"]) else "")
            log_box.insert("end", l["t"] + "  ", "t")
            log_box.insert("end", l["msg"] + "\n", tag)
        log_box.configure(state="disabled")
        log_box.see("end")

    STATE.on_update = lambda: root.after_idle(refresh)

    def _loop():
        try:
            if not root.winfo_exists():
                return
            refresh()
            root.after(1800, _loop)
        except Exception:
            pass

    root.after(600, refresh)
    root.after(1800, _loop)

    try:
        start_proxy_server()
    except OSError:
        flash(f"反向代理端口 {PROXY_PORT} 被占用，可能是已有 QuickTunnel 实例在运行", err=True)

    def on_close():
        stop_tunnel()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()

# ---------- CLI 直连模式 ----------
def normalize_target(raw):
    raw = raw.strip()
    if re.fullmatch(r"\d{1,5}", raw):
        port = int(raw)
        if not (1 <= port <= 65535):
            sys.exit(red("[错误] 端口必须在 1-65535 之间"))
        return f"http://localhost:{port}"
    if re.match(r"^https?://", raw, re.I):
        return raw
    sys.exit(red(f"[错误] 无法识别的目标: {raw}\n"
                 f"  请输入端口号 (如 3000) 或完整 URL (如 http://localhost:3000)"))

def run_tunnel_cli(cf_path, target, quiet):
    cmd = [cf_path, "tunnel", "--url", target, "--no-autoupdate"]
    print()
    print(bold(" ╭──────────────────────────────────────────────╮"))
    print(bold(" │") + bold(" QuickTunnel 内网穿透启动 ") + bold("│"))
    print(bold(" ╰──────────────────────────────────────────────╯"))
    print(f"  本地服务: {cyan(target)}")
    print(yellow(" ⚠ 安全提示: 公网 URL 任何人都可访问，请勿暴露敏感服务；"))
    print(yellow("  使用完毕请按 Ctrl+C 及时关闭隧道。"))
    print()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
    except FileNotFoundError:
        sys.exit(red("[错误] cloudflared 启动失败，请检查内核路径。"))
    found = False
    for line in proc.stdout:
        line = line.rstrip()
        m = URL_RE.search(line)
        if m and not found:
            found = True
            print(green(bold(" ✓ 隧道已建立! 公网访问地址:")))
            print()
            print(bold(f" ➜ {m.group(0)}"))
            print()
            print(cyan(" [*] 隧道运行中，按 Ctrl+C 停止…"))
        if not found and line and ("error" in line.lower() or "fail" in line.lower()):
            print(red(f" [cloudflared] {line}"))
        elif not found and line and ("connect" in line.lower() or "register" in line.lower()):
            print(f" [cloudflared] {line}")
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    print()
    print(cyan("[*] 隧道已关闭，再见。"))

# ---------- 入口 ----------
def main():
    global PROXY_PORT
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        prog="quicktunnel",
        description="零配置、一键式内网穿透 (基于 Cloudflare Tunnel 官方内核)")
    parser.add_argument("target", nargs="?", help="本地端口号/URL（CLI 直连模式）；省略启动桌面 GUI")
    parser.add_argument("--proxy-port", type=int, default=PROXY_PORT,
                        help=f"内置反向代理端口 (默认 {PROXY_PORT})")
    parser.add_argument("-q", "--quiet", action="store_true", help="CLI 模式下静默内核日志")
    args = parser.parse_args()
    PROXY_PORT = args.proxy_port
    if args.target is None:
        run_gui()
        return
    target = normalize_target(args.target)
    cf_path = ensure_cloudflared()
    try:
        run_tunnel_cli(cf_path, target, args.quiet)
    except KeyboardInterrupt:
        print("\n" + cyan("[*] 已中断，隧道正在关闭…"))

if __name__ == "__main__":
    main()
