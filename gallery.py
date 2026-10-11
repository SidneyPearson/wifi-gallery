#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wi-Fi 相册 / 媒体库（仅局域网、只读）

- 媒体目录通过 config.json 管理，可用 add/remove 命令增删，无需改代码
- 所有文件只读；路径与软链接均限制在已添加目录内
- 登录会话 / HTTP Basic Auth 访问保护；HTTP Range 支持视频拖动
支持 macOS / Windows；媒体目录在每台电脑上独立配置。
"""

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import posixpath
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

APP_DIR = os.path.dirname(os.path.abspath(__file__))

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif",
    ".tif", ".tiff",
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".webm", ".mkv", ".avi"}

mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("video/mp4", ".m4v")
mimetypes.add_type("video/quicktime", ".mov")

SCAN_CACHE_SECONDS = 60
THUMB_SIZE = 500


# ---------------------------------------------------------------- 配置

def paths(app_dir):
    # web 资源跟随代码（脚本目录）；配置/缓存跟随 app_dir
    return {
        "config": os.path.join(app_dir, "config.json"),
        "pid": os.path.join(app_dir, "gallery.pid"),
        "log": os.path.join(app_dir, "gallery.log"),
        "web": os.path.join(APP_DIR, "web"),
        "cache": os.path.join(app_dir, "cache", "thumb"),
    }


DEFAULT_CONFIG = {
    "port": 9265,
    "username": "gallery",
    "password": "",
    "secret": "",         # 会话 Cookie 签名密钥，首次自动生成
    "roots": [],          # [{"name": "...", "path": "/..."}]
}

SESSION_COOKIE = "wgsession"
SESSION_DAYS = 90


def load_config(app_dir):
    p = paths(app_dir)["config"]
    if not os.path.exists(p):
        cfg = dict(DEFAULT_CONFIG)
        cfg["password"] = secrets.token_urlsafe(9)
        save_config(app_dir, cfg)
    with open(p, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    changed = False
    if not cfg.get("password"):
        cfg["password"] = secrets.token_urlsafe(9)
        changed = True
    if not cfg.get("secret"):
        cfg["secret"] = secrets.token_hex(32)
        changed = True
    if changed:
        save_config(app_dir, cfg)
    return cfg


# ---------------------------------------------------------------- 会话令牌

def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_session_token(cfg):
    """令牌签名混入密码哈希：改密码后旧令牌全部失效。"""
    payload = json.dumps(
        {"u": cfg.get("username", ""), "iat": int(time.time())},
        separators=(",", ":")).encode()
    pb = _b64u(payload)
    pw_hash = hashlib.sha256(cfg.get("password", "").encode()).digest()
    mac = hmac.new(cfg["secret"].encode(), pb.encode() + b"." + pw_hash,
                   hashlib.sha256).digest()
    return pb + "." + _b64u(mac)


def check_session_token(cfg, token):
    try:
        pb, sb = token.split(".", 1)
        payload = json.loads(_b64u_dec(pb))
        pw_hash = hashlib.sha256(cfg.get("password", "").encode()).digest()
        expect = hmac.new(cfg["secret"].encode(), pb.encode() + b"." + pw_hash,
                          hashlib.sha256).digest()
        if not hmac.compare_digest(_b64u(expect), sb):
            return False
        if int(time.time()) - int(payload["iat"]) > SESSION_DAYS * 86400:
            return False
        return hmac.compare_digest(payload.get("u", ""), cfg.get("username", ""))
    except Exception:
        return False


def save_config(app_dir, cfg):
    p = paths(app_dir)["config"]
    os.makedirs(app_dir, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def root_entry(cfg, idx):
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return None
    roots = cfg.get("roots", [])
    if 0 <= idx < len(roots):
        return roots[idx]
    return None


# ---------------------------------------------------------------- 路径安全

class UnsafePath(Exception):
    pass


def is_within(root_real, target_real):
    """兼容 Windows 盘符、大小写及磁盘根目录的边界判断。"""
    try:
        common = os.path.commonpath((root_real, target_real))
        return os.path.normcase(common) == os.path.normcase(root_real)
    except ValueError:
        return False


def safe_resolve(root_real, rel):
    """把相对路径解析到 root_real 内，拦截 ../ 与逃逸软链接。"""
    if rel is None:
        rel = ""
    if "\x00" in rel:
        raise UnsafePath("非法字符")
    if (rel.startswith("/") or rel.startswith("\\") or
            os.path.isabs(rel) or os.path.splitdrive(rel)[0]):
        raise UnsafePath("不允许绝对路径")
    for part in rel.replace("\\", "/").split("/"):
        if part == "..":
            raise UnsafePath("不允许访问上级目录")
    cand = os.path.normpath(os.path.join(root_real, rel)) if rel else root_real
    real = os.path.realpath(cand)
    if not is_within(root_real, real):
        raise UnsafePath("路径超出已授权目录")
    return real


def media_kind(fname):
    ext = os.path.splitext(fname)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


# ---------------------------------------------------------------- 扫描缓存

class ScanCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}          # key: (root_real) -> (ts, items)

    def root_items(self, root_real, force=False):
        now = time.time()
        with self._lock:
            hit = self._data.get(root_real)
            if hit and not force and now - hit[0] < SCAN_CACHE_SECONDS:
                return hit[1]
        items = list(walk_media(root_real))
        items.sort(key=lambda it: it["mtime"], reverse=True)
        with self._lock:
            self._data[root_real] = (now, items)
        return items


def walk_media(root_real):
    for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            kind = media_kind(fn)
            if not kind:
                continue
            full = os.path.join(dirpath, fn)
            try:
                real = os.path.realpath(full)
                if not is_within(root_real, real):
                    continue
                st = os.stat(real)
            except OSError:
                continue
            rel = os.path.relpath(real, root_real)
            yield {
                "name": fn,
                "path": rel.replace(os.sep, "/"),
                "dir": os.path.dirname(rel).replace(os.sep, "/"),
                "type": kind,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            }


SCAN = ScanCache()
THUMB_LOCKS = {}
THUMB_LOCKS_GUARD = threading.Lock()


def thumb_lock(key):
    with THUMB_LOCKS_GUARD:
        lk = THUMB_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            THUMB_LOCKS[key] = lk
        return lk


# ---------------------------------------------------------------- 缩略图

def thumb_cache_path(cache_dir, real, st):
    raw = f"{real}\n{st.st_mtime_ns}\n{st.st_size}".encode("utf-8")
    key = hashlib.sha1(raw).hexdigest()
    return os.path.join(cache_dir, key[:2], key + ".jpg")


def run(cmd, timeout=30):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=timeout, check=True, **options)


def ffmpeg_executable():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        raise FileNotFoundError("请先安装 requirements.txt 中的缩略图依赖")


def generate_image_thumb(src, dst):
    tmp = dst + ".tmp.jpg"
    # Mac 保留原生缩略图方式；其他平台使用 Pillow（含 HEIC 插件）。
    if sys.platform == "darwin":
        try:
            run(["sips", "-Z", str(THUMB_SIZE), "-s", "format", "jpeg",
                 "-s", "formatOptions", "65", "--out", tmp, src])
            os.replace(tmp, dst)
            return True
        except Exception:
            pass
    try:
        from PIL import Image, ImageOps
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
        with Image.open(src) as original:
            image = ImageOps.exif_transpose(original)
            image.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image.save(tmp, "JPEG", quality=65)
        os.replace(tmp, dst)
        return True
    except Exception:
        for p in (tmp,):
            try:
                os.unlink(p)
            except OSError:
                pass
        return False


def generate_video_thumb(src, dst):
    tmp = dst + ".tmp.jpg"
    # 优先 ffmpeg
    try:
        run([ffmpeg_executable(), "-y", "-ss", "0", "-i", src, "-frames:v", "1",
             "-vf", f"scale='min({THUMB_SIZE},iw)':-2", tmp], timeout=40)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, dst)
            return True
    except Exception:
        pass
    if sys.platform != "darwin":
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    # Mac 退回 Quick Look
    try:
        qldir = dst + ".ql"
        os.makedirs(qldir, exist_ok=True)
        run(["qlmanage", "-t", "-s", str(THUMB_SIZE), "-o", qldir, src], timeout=30)
        produced = None
        for name in os.listdir(qldir):
            if name.endswith(".png.png") or name.endswith(".png"):
                produced = os.path.join(qldir, name)
                break
        if produced:
            run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "65",
                 "--out", tmp, produced])
            os.replace(tmp, dst)
            return True
    except Exception:
        pass
    finally:
        import shutil
        qldir = dst + ".ql"
        if os.path.isdir(qldir):
            shutil.rmtree(qldir, ignore_errors=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return False


# ---------------------------------------------------------------- HTTP 处理

# PWA 静态资源白名单：URL 路径 -> (web 目录内文件名, Content-Type)
STATIC_FILES = {
    "/manifest.webmanifest": ("manifest.webmanifest",
                              "application/manifest+json; charset=utf-8"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/icon-maskable-512.png": ("icon-maskable-512.png", "image/png"),
    "/favicon.ico": ("apple-touch-icon.png", "image/png"),
}


class GalleryHandler(BaseHTTPRequestHandler):
    server_version = "WifiGallery/1.0"
    protocol_version = "HTTP/1.1"

    app_dir = APP_DIR
    cfg = None

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---- 基础响应 ----
    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        h = {"Cache-Control": "no-store"}
        if extra:
            h.update(extra)
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8", h)

    def _error(self, code, msg):
        if self.path.startswith("/api/"):
            self._json({"error": msg}, code)
        else:
            self._send(code, msg, "text/plain; charset=utf-8")

    # ---- 访问保护 ----
    def _cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return None

    def _basic_ok(self, cfg):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
            user, pw = decoded.split(":", 1)
        except Exception:
            return False
        return (hmac.compare_digest(user, cfg.get("username", ""))
                and hmac.compare_digest(pw, cfg.get("password", "")))

    def _authenticated(self):
        cfg = load_config(self.app_dir)
        token = self._cookie(SESSION_COOKIE)
        if token and check_session_token(cfg, token):
            return True
        return self._basic_ok(cfg)

    def _wants_html(self):
        return "text/html" in self.headers.get("Accept", "")

    def _redirect(self, location, code=303, cookie=None):
        self.send_response(code)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _host_ok(self):
        host = self.headers.get("Host")
        if not host:
            return True
        host = host.split(":")[0].rstrip(".")
        if host in ("localhost",) or host.endswith(".localhost"):
            return True
        try:
            import ipaddress
            ipaddress.ip_address(host)
            return True
        except ValueError:
            return False

    def _require(self):
        if not self._host_ok():
            self._error(403, "Forbidden")
            return False
        if self._authenticated():
            return True
        # 浏览器页面访问 -> 跳登录页；接口/图片请求 -> 401 JSON
        if self._wants_html():
            self._redirect("/login", code=302)
        else:
            self._json({"error": "未登录或登录已过期"}, 401)
        return False

    # ---- 入口 ----
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = posixpath.normpath(unquote(parsed.path))
            qs = parse_qs(parsed.query)
            if path == "/login":
                return self.handle_login(qs)
            if path == "/logout":
                return self.handle_logout()
            if not self._require():
                return
            if path in ("/", "/index.html"):
                return self.handle_index()
            if path == "/api/roots":
                return self.handle_roots()
            if path == "/api/list":
                return self.handle_list(qs)
            if path == "/api/all":
                return self.handle_all(qs)
            if path == "/api/refresh":
                return self.handle_refresh(qs)
            if path == "/thumb":
                return self.handle_thumb(qs)
            if path == "/raw":
                return self.handle_raw(qs)
            if path == "/static/video.svg":
                return self.handle_video_svg()
            if path in STATIC_FILES:
                return self.handle_static(path)
            self._error(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._error(500, "服务器错误: %s" % e)
            except Exception:
                pass

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = posixpath.normpath(unquote(parsed.path))
            if path == "/login":
                return self.handle_login_post()
            self._error(404, "Not Found")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._error(500, "服务器错误: %s" % e)
            except Exception:
                pass

    # ---- 登录页 ----
    LOGIN_ERROR_HTML = (
        '<div class="form-error">用户名或密码不正确，请重试</div>')

    def handle_login(self, qs):
        if self._authenticated():
            return self._redirect("/")
        fp = os.path.join(paths(self.app_dir)["web"], "login.html")
        with open(fp, "rb") as f:
            body = f.read()
        error = self.LOGIN_ERROR_HTML if qs.get("error") else ""
        body = body.replace(b"<!--ERROR-->", error.encode("utf-8"))
        self._send(200, body, "text/html; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def handle_login_post(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            return self._redirect("/login?error=1")
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        user = form.get("username", [""])[0].strip()
        pw = form.get("password", [""])[0]
        cfg = load_config(self.app_dir)
        ok = (hmac.compare_digest(user, cfg.get("username", ""))
              and hmac.compare_digest(pw, cfg.get("password", "")))
        if not ok:
            time.sleep(0.8)  # 稍微拖慢暴力尝试
            return self._redirect("/login?error=1")
        token = make_session_token(cfg)
        cookie = ("%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                  % (SESSION_COOKIE, token, SESSION_DAYS * 86400))
        self._redirect("/", cookie=cookie)

    def handle_logout(self):
        cookie = ("%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
                  % SESSION_COOKIE)
        self._redirect("/login", cookie=cookie)

    # ---- 静态首页 ----
    def handle_index(self):
        fp = os.path.join(paths(self.app_dir)["web"], "index.html")
        with open(fp, "rb") as f:
            body = f.read()
        self._send(200, body, "text/html; charset=utf-8",
                   {"Cache-Control": "no-cache"})

    def handle_video_svg(self):
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
               b'<rect width="100%" height="100%" fill="#1c1c1e"/>'
               b'<circle cx="200" cy="150" r="52" fill="none" stroke="#8e8e93" stroke-width="6"/>'
               b'<path d="M184 122 L184 178 L228 150 Z" fill="#8e8e93"/></svg>')
        self._send(200, svg, "image/svg+xml",
                   {"Cache-Control": "public, max-age=86400"})

    def handle_static(self, url_path):
        fname, ctype = STATIC_FILES[url_path]
        # 文件名来自白名单常量，不拼接任何用户输入
        fp = os.path.join(paths(self.app_dir)["web"], fname)
        try:
            with open(fp, "rb") as f:
                body = f.read()
        except OSError:
            self._error(404, "Not Found")
            return
        self._send(200, body, ctype, {"Cache-Control": "public, max-age=86400"})

    # ---- API ----
    def handle_roots(self):
        cfg = load_config(self.app_dir)
        out = []
        for i, r in enumerate(cfg["roots"]):
            real = os.path.realpath(r["path"])
            missing = not os.path.isdir(real)
            count = None if missing else len(SCAN.root_items(real))
            out.append({
                "id": i,
                "name": r.get("name") or os.path.basename(real),
                "path": r["path"],
                "missing": missing,
                "count": count,
            })
        self._json({"roots": out})

    def _resolve_target(self, qs):
        cfg = load_config(self.app_dir)
        entry = root_entry(cfg, qs.get("root", [""])[0])
        if entry is None:
            self._error(400, "未知的目录")
            return None, None
        root_real = os.path.realpath(entry["path"])
        if not os.path.isdir(root_real):
            self._error(404, "目录不存在: %s" % entry["path"])
            return None, None
        try:
            target = safe_resolve(root_real, qs.get("path", [""])[0])
        except UnsafePath as e:
            self._error(403, str(e))
            return None, None
        return root_real, target

    def handle_list(self, qs):
        root_real, target = self._resolve_target(qs)
        if target is None:
            return
        if not os.path.isdir(target):
            self._error(404, "文件夹不存在")
            return
        cfg = load_config(self.app_dir)
        entry = root_entry(cfg, qs.get("root", [""])[0])

        folders, items = [], []
        try:
            with os.scandir(target) as it:
                entries = list(it)
        except OSError as e:
            self._error(403, "无法读取: %s" % e)
            return

        for de in entries:
            if de.name.startswith("."):
                continue
            try:
                real = os.path.realpath(de.path)
                if not is_within(root_real, real):
                    continue
                if de.is_dir(follow_symlinks=False) or (
                        de.is_symlink() and os.path.isdir(real)):
                    if not os.path.isdir(real):
                        continue
                    folders.append({"name": de.name})
                elif de.is_file(follow_symlinks=False) or (
                        de.is_symlink() and os.path.isfile(real)):
                    kind = media_kind(de.name)
                    if not kind:
                        continue
                    st = de.stat()
                    items.append({
                        "name": de.name,
                        "path": os.path.relpath(de.path, root_real).replace(os.sep, "/"),
                        "type": kind,
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                    })
            except OSError:
                continue

        folders.sort(key=lambda x: x["name"].lower())
        items.sort(key=lambda x: x["name"].lower())

        rel = os.path.relpath(target, root_real).replace(os.sep, "/")
        if rel == ".":
            rel = ""
        crumbs = [{"name": entry.get("name") or os.path.basename(root_real), "path": ""}]
        acc = ""
        for part in [p for p in rel.split("/") if p]:
            acc = part if not acc else acc + "/" + part
            crumbs.append({"name": part, "path": acc})

        self._json({
            "root": qs.get("root", [""])[0],
            "root_name": entry.get("name") or os.path.basename(root_real),
            "path": rel,
            "breadcrumbs": crumbs,
            "folders": folders,
            "items": items,
        })

    def _active_roots(self, cfg):
        """返回 (id, entry, root_real)；嵌套在另一个已添加目录内的根自动去重。"""
        roots = []
        for i, r in enumerate(cfg["roots"]):
            real = os.path.realpath(r["path"])
            if os.path.isdir(real):
                roots.append((i, r, real))
        roots.sort(key=lambda t: len(t[2]))
        kept = []
        for i, r, real in roots:
            if any(is_within(k, real) for _, _, k in kept):
                continue
            kept.append((i, r, real))
        return kept

    def handle_all(self, qs):
        cfg = load_config(self.app_dir)
        try:
            start = max(0, int(qs.get("start", ["0"])[0]))
            count = int(qs.get("count", ["120"])[0])
        except ValueError:
            self._error(400, "分页参数错误")
            return
        count = min(300, max(1, count))

        merged = []
        for rid, entry, real in self._active_roots(cfg):
            rname = entry.get("name") or os.path.basename(real)
            for it in SCAN.root_items(real):
                it = dict(it)
                it["root"] = rid
                it["root_name"] = rname
                merged.append(it)
        merged.sort(key=lambda it: it["mtime"], reverse=True)

        page = merged[start:start + count]
        self._json({"items": page, "total": len(merged),
                    "start": start, "count": count})

    def handle_refresh(self, qs):
        cfg = load_config(self.app_dir)
        for _, _, real in self._active_roots(cfg):
            SCAN.root_items(real, force=True)
        self._json({"ok": True})

    # ---- 缩略图 ----
    def handle_thumb(self, qs):
        root_real, target = self._resolve_target(qs)
        if target is None:
            return
        if not os.path.isfile(target):
            self._error(404, "文件不存在")
            return
        kind = media_kind(target)
        if kind is None:
            self._error(400, "非媒体文件")
            return

        st = os.stat(target)
        cache_dir = paths(self.app_dir)["cache"]
        os.makedirs(cache_dir, exist_ok=True)
        dst = thumb_cache_path(cache_dir, target, st)
        etag = '"%s"' % os.path.basename(dst)[:-4]

        inm = self.headers.get("If-None-Match")
        if inm and etag in inm and os.path.exists(dst):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return

        headers = {"ETag": etag, "Cache-Control": "public, max-age=31536000, immutable"}
        if os.path.exists(dst):
            return self._serve_file(dst, "image/jpeg", st_mtime=os.stat(dst).st_mtime,
                                    extra=headers, head_only=False)

        lk = thumb_lock(os.path.basename(dst))
        with lk:
            if not os.path.exists(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                ok = (generate_image_thumb(target, dst) if kind == "image"
                      else generate_video_thumb(target, dst))
                if not ok:
                    self.send_response(302)
                    self.send_header("Location", "/static/video.svg")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
        self._serve_file(dst, "image/jpeg", st_mtime=os.stat(dst).st_mtime,
                         extra=headers, head_only=False)

    # ---- 原文件（只读 + Range） ----
    def handle_raw(self, qs):
        root_real, target = self._resolve_target(qs)
        if target is None:
            return
        if not os.path.isfile(target):
            self._error(404, "文件不存在")
            return
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        self._serve_file(target, ctype)

    def _serve_file(self, full, ctype, st_mtime=None, extra=None, head_only=False):
        st = os.stat(full)
        fsize = st.st_size
        etag = '"%x-%x"' % (st.st_mtime_ns, fsize)
        last_mod = time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                 time.gmtime(st_mtime or st.st_mtime))

        inm = self.headers.get("If-None-Match")
        if inm and etag in inm:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return

        rng = self.headers.get("Range")
        start, end = 0, fsize - 1
        partial = False
        if rng:
            try:
                if not rng.startswith("bytes="):
                    raise ValueError
                rs, re_ = rng[6:].split("-", 1)
                if rs == "":
                    length = int(re_)
                    start = max(0, fsize - length)
                    end = fsize - 1
                else:
                    start = int(rs)
                    end = int(re_) if re_ else fsize - 1
                if start > end or start >= fsize:
                    raise ValueError
                end = min(end, fsize - 1)
                partial = True
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % fsize)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, fsize))
        self.send_header("Content-Length", str(length))
        self.end_headers()

        if self.command == "HEAD" or head_only:
            return
        try:
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return


# ---------------------------------------------------------------- 网络信息

def lan_ip():
    # 1) 以系统默认路由的物理接口为准（避开代理软件的 TUN 虚拟网段，如 198.18.x）
    try:
        if sys.platform != "darwin":
            raise OSError("使用跨平台地址探测")
        out = subprocess.run(["route", "-n", "get", "default"],
                             capture_output=True, text=True, timeout=2).stdout
        iface = None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("interface:"):
                iface = line.split(":", 1)[1].strip()
        if iface:
            a = subprocess.run(["ipconfig", "getifaddr", iface],
                               capture_output=True, text=True, timeout=2).stdout.strip()
            if a:
                return a
    except Exception:
        pass
    # 2) UDP 探测兜底，排除基准/虚拟网段 198.18.0.0/15
    try:
        import struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("192.168.0.1", 80))
        cand = s.getsockname()[0]
        s.close()
        n = struct.unpack("!I", socket.inet_aton(cand))[0]
        if not (0xC6120000 <= n <= 0xC613FFFF):
            return cand
    except OSError:
        pass
    return "127.0.0.1"


# ---------------------------------------------------------------- 服务进程控制

def process_running(pid):
    """只查询进程；Windows 的 os.kill(pid, 0) 会终止进程，不能使用。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # 访问被拒绝仍表示进程存在
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def cmd_serve(app_dir):
    cfg = load_config(app_dir)
    port = int(cfg.get("port", 9265))
    handler = type("H", (GalleryHandler,), {"app_dir": app_dir})
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True

    def shutdown(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    ip = lan_ip()
    sys.stderr.write("相册服务已启动: http://%s:%d  (端口 %d)\n" % (ip, port, port))
    sys.stderr.flush()
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def cmd_start(app_dir):
    p = paths(app_dir)
    if os.path.exists(p["pid"]):
        try:
            old = int(open(p["pid"]).read().strip())
            if process_running(old):
                print("服务已在运行中 (PID %d)" % old)
                return
            os.unlink(p["pid"])
        except (OSError, ValueError):
            os.unlink(p["pid"])

    cfg = load_config(app_dir)
    options = ({"creationflags": subprocess.DETACHED_PROCESS |
                subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    with open(p["log"], "ab") as logf:
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", os.path.abspath(__file__),
             "--app-dir", app_dir, "serve"],
            stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
            **options,
        )
    with open(p["pid"], "w") as f:
        f.write(str(proc.pid))
    time.sleep(1.2)
    if proc.poll() is not None:
        print("启动失败，请查看日志: %s" % p["log"])
        try:
            os.unlink(p["pid"])
        except OSError:
            pass
        sys.exit(1)
    ip = lan_ip()
    print("服务已启动 (PID %d)" % proc.pid)
    print("iPhone Safari 打开: http://%s:%d" % (ip, cfg["port"]))
    print("用户名: %s" % cfg["username"])
    print("密码:   %s" % cfg["password"])
    print("日志:   %s" % p["log"])


def cmd_stop(app_dir):
    p = paths(app_dir)
    if not os.path.exists(p["pid"]):
        print("服务未运行")
        return
    try:
        pid = int(open(p["pid"]).read().strip())
    except (OSError, ValueError):
        os.unlink(p["pid"])
        return
    if not process_running(pid):
        os.unlink(p["pid"])
        print("进程不存在，已清理 PID 文件")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        print("进程不存在，已清理 PID 文件")
        os.unlink(p["pid"])
        return
    for _ in range(40):
        if not process_running(pid):
            break
        time.sleep(0.2)
    else:
        try:
            os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
        except OSError:
            pass
    try:
        os.unlink(p["pid"])
    except OSError:
        pass
    print("服务已停止 (PID %d)" % pid)


def cmd_status(app_dir):
    p = paths(app_dir)
    running = False
    pid = None
    if os.path.exists(p["pid"]):
        try:
            pid = int(open(p["pid"]).read().strip())
            running = process_running(pid)
        except (OSError, ValueError):
            pass
    cfg = load_config(app_dir)
    if running:
        print("运行中 (PID %d)" % pid)
        print("地址: http://%s:%d" % (lan_ip(), cfg["port"]))
    else:
        print("未运行")


# ---------------------------------------------------------------- 目录管理

def cmd_add(app_dir, path, name=None):
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        print("错误: 文件夹不存在: %s" % path)
        sys.exit(1)
    real = os.path.realpath(path)
    cfg = load_config(app_dir)
    for r in cfg["roots"]:
        if os.path.realpath(r["path"]) == real:
            print("该目录已添加: %s (%s)" % (r["name"], r["path"]))
            return
    if not name:
        name = os.path.basename(real.rstrip("/")) or real
    cfg["roots"].append({"name": name, "path": path})
    save_config(app_dir, cfg)
    print("已添加目录: %s -> %s" % (name, path))


def cmd_remove(app_dir, key):
    cfg = load_config(app_dir)
    idx = None
    try:
        n = int(key)
        if 0 <= n < len(cfg["roots"]):
            idx = n
    except ValueError:
        for i, r in enumerate(cfg["roots"]):
            if r["name"] == key or os.path.realpath(r["path"]) == os.path.realpath(
                    os.path.expanduser(key)):
                idx = i
                break
    if idx is None:
        print("未找到: %s（用 list 查看编号）" % key)
        sys.exit(1)
    removed = cfg["roots"].pop(idx)
    save_config(app_dir, cfg)
    print("已移除目录: %s (%s)（文件本身未改动）" % (removed["name"], removed["path"]))


def cmd_choose(app_dir):
    """在运行服务的电脑上选择目录；手机端仍保持只读。"""
    window = None
    try:
        import tkinter
        from tkinter import filedialog
        window = tkinter.Tk()
        window.withdraw()
        selected = filedialog.askdirectory(title="选择相册媒体目录", mustexist=True)
    except Exception as e:
        print("无法打开目录选择窗口: %s；请改用 add 文件夹路径" % e)
        sys.exit(1)
    finally:
        if window is not None:
            window.destroy()
    if selected:
        cmd_add(app_dir, selected)
    else:
        print("已取消选择")


def cmd_list(app_dir):
    cfg = load_config(app_dir)
    if not cfg["roots"]:
        print("（还没有添加任何目录）")
    for i, r in enumerate(cfg["roots"]):
        mark = " " if os.path.isdir(r["path"]) else "✗"
        print("%s [%d] %-20s %s" % (mark, i, r["name"], r["path"]))


def cmd_passwd(app_dir, new_pw=None):
    cfg = load_config(app_dir)
    cfg["password"] = new_pw or secrets.token_urlsafe(9)
    save_config(app_dir, cfg)
    print("新密码: %s（用户名: %s）" % (cfg["password"], cfg["username"]))


def cmd_clear_cache(app_dir):
    import shutil
    d = paths(app_dir)["cache"]
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    print("缩略图缓存已清空: %s" % d)


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="Wi-Fi 相册（局域网只读媒体库）")
    parser.add_argument("--app-dir", default=APP_DIR, help="配置/缓存目录")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="前台运行服务")
    sub.add_parser("start", help="后台启动服务")
    sub.add_parser("stop", help="停止后台服务")
    sub.add_parser("status", help="查看服务状态")
    sub.add_parser("choose", help="打开窗口选择媒体目录")

    p_add = sub.add_parser("add", help="添加媒体目录")
    p_add.add_argument("path")
    p_add.add_argument("--name", default=None)

    p_rm = sub.add_parser("remove", help="移除媒体目录（不删除原文件）")
    p_rm.add_argument("key", help="目录编号或名称或路径")

    sub.add_parser("list", help="列出已添加目录")

    p_pw = sub.add_parser("passwd", help="重置（或设置）访问密码")
    p_pw.add_argument("password", nargs="?", default=None)

    sub.add_parser("clear-cache", help="清空缩略图缓存")

    args = parser.parse_args()
    app_dir = os.path.abspath(args.app_dir)
    os.makedirs(app_dir, exist_ok=True)
    load_config(app_dir)  # 确保 config.json 存在

    if args.cmd == "serve":
        cmd_serve(app_dir)
    elif args.cmd == "start":
        cmd_start(app_dir)
    elif args.cmd == "stop":
        cmd_stop(app_dir)
    elif args.cmd == "status":
        cmd_status(app_dir)
    elif args.cmd == "choose":
        cmd_choose(app_dir)
    elif args.cmd == "add":
        cmd_add(app_dir, args.path, args.name)
    elif args.cmd == "remove":
        cmd_remove(app_dir, args.key)
    elif args.cmd == "list":
        cmd_list(app_dir)
    elif args.cmd == "passwd":
        cmd_passwd(app_dir, args.password)
    elif args.cmd == "clear-cache":
        cmd_clear_cache(app_dir)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
