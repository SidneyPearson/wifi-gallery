import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from PIL import Image
from pillow_heif import register_heif_opener

import gallery


SCRIPT = Path(gallery.__file__).resolve()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, path, method="GET", headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def cli(app_dir, *args):
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), "--app-dir", str(app_dir), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=True,
    ).stdout


class GalleryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gallery test ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.app = self.base / "应用 配置"
        self.media = self.base / "媒体 文件"
        self.app.mkdir()
        self.media.mkdir()
        self.image = self.media / "照片 你好.png"
        Image.new("RGBA", (800, 400), (40, 100, 200, 128)).save(self.image)
        self.original_hash = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.port = free_port()
        cfg = gallery.load_config(str(self.app))
        cfg["port"] = self.port
        cfg["roots"] = [{"name": "测试相册", "path": str(self.media)}]
        gallery.save_config(str(self.app), cfg)
        basic = base64.b64encode((cfg["username"] + ":" + cfg["password"]).encode()).decode()
        self.headers = {"Authorization": "Basic " + basic}
        self.cfg = cfg
        self.log = (self.base / "server.log").open("wb")
        self.addCleanup(self.log.close)
        self.server = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--app-dir", str(self.app), "serve"],
            stdout=self.log, stderr=self.log,
        )
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                self.fail((self.base / "server.log").read_text(encoding="utf-8"))
            try:
                if request(self.port, "/login")[0] == 200:
                    return
            except OSError:
                time.sleep(0.05)
        self.fail("服务未在限定时间内启动")

    def stop_server(self):
        if self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=5)

    def test_login_and_password_change(self):
        self.assertEqual(request(self.port, "/api/roots")[0], 401)
        form = urlencode({"username": self.cfg["username"], "password": self.cfg["password"]})
        status, headers, _ = request(
            self.port, "/login", method="POST", body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        self.assertEqual(request(self.port, "/api/roots", headers=cookie)[0], 200)
        cli(self.app, "passwd", "changed-test-password")
        self.assertEqual(request(self.port, "/api/roots", headers=cookie)[0], 401)

    def test_live_folder_management_and_nested_browsing(self):
        nested = self.media / "子目录"
        nested.mkdir()
        Image.new("RGB", (20, 30), "red").save(nested / "child.jpg")
        status, _, body = request(self.port, "/api/list?root=0", headers=self.headers)
        self.assertEqual(status, 200)
        listing = json.loads(body)
        self.assertEqual(listing["folders"], [{"name": "子目录"}])
        self.assertEqual(listing["items"][0]["name"], self.image.name)
        other = self.base / "另一个目录"
        other.mkdir()
        cli(self.app, "add", str(other), "--name", "新增目录")
        roots = json.loads(request(self.port, "/api/roots", headers=self.headers)[2])["roots"]
        self.assertEqual(len(roots), 2)
        cli(self.app, "remove", "新增目录")
        roots = json.loads(request(self.port, "/api/roots", headers=self.headers)[2])["roots"]
        self.assertEqual(len(roots), 1)
        self.assertTrue(other.is_dir())
        path = "/api/list?" + urlencode({"root": 0, "path": "子目录"})
        nested_listing = json.loads(request(self.port, path, headers=self.headers)[2])
        self.assertEqual(nested_listing["items"][0]["name"], "child.jpg")

    def test_read_only_raw_range_thumbnail_and_traversal(self):
        raw = "/raw?" + urlencode({"root": 0, "path": self.image.name})
        headers = {**self.headers, "Range": "bytes=0-7"}
        status, response_headers, body = request(self.port, raw, headers=headers)
        self.assertEqual(status, 206)
        self.assertEqual(body, self.image.read_bytes()[:8])
        self.assertTrue(response_headers["Content-Range"].startswith("bytes 0-7/"))
        thumb = "/thumb?" + urlencode({"root": 0, "path": self.image.name})
        status, _, data = request(self.port, thumb, headers=self.headers)
        self.assertEqual(status, 200)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertLessEqual(max(image.size), gallery.THUMB_SIZE)
        outside = self.base / "outside.txt"
        outside.write_text("private fixture", encoding="utf-8")
        for path in ("../outside.txt", "..\\outside.txt", str(outside)):
            url = "/raw?" + urlencode({"root": 0, "path": path})
            self.assertEqual(request(self.port, url, headers=self.headers)[0], 403)
        self.assertEqual(request(self.port, raw, method="POST", headers=self.headers)[0], 404)
        self.assertEqual(hashlib.sha256(self.image.read_bytes()).hexdigest(), self.original_hash)

    def test_image_and_video_thumbnails(self):
        register_heif_opener()
        heic = self.media / "手机 图片.heic"
        Image.new("RGB", (800, 400), "blue").save(heic, format="HEIF")
        video = self.media / "短 视频.mp4"
        gallery.run([
            gallery.ffmpeg_executable(), "-y", "-f", "lavfi", "-i",
            "color=c=blue:s=640x360:d=0.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ])
        for media in (heic, video):
            before = hashlib.sha256(media.read_bytes()).hexdigest()
            url = "/thumb?" + urlencode({"root": 0, "path": media.name})
            status, _, data = request(self.port, url, headers=self.headers)
            self.assertEqual(status, 200, media.name)
            with Image.open(io.BytesIO(data)) as thumbnail:
                self.assertEqual(thumbnail.format, "JPEG")
                self.assertLessEqual(max(thumbnail.size), gallery.THUMB_SIZE)
            self.assertEqual(hashlib.sha256(media.read_bytes()).hexdigest(), before)

    def test_all_media_deduplicates_nested_roots(self):
        nested = self.media / "nested"
        nested.mkdir()
        Image.new("RGB", (10, 10), "red").save(nested / "nested.jpg")
        cli(self.app, "add", str(nested))
        listing = json.loads(request(self.port, "/api/all", headers=self.headers)[2])
        self.assertEqual(listing["total"], 2)
        self.assertEqual(len(listing["items"]), 2)


class PortabilityTests(unittest.TestCase):
    def test_new_install_has_independent_empty_configuration(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            a, b = gallery.load_config(first), gallery.load_config(second)
            self.assertEqual(a["roots"], [])
            self.assertEqual(b["roots"], [])
            self.assertNotEqual(a["password"], b["password"])
            self.assertNotEqual(a["secret"], b["secret"])

    def test_volume_root_and_windows_drive_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            real = os.path.realpath(temporary)
            volume = Path(real).anchor
            relative = os.path.relpath(real, volume)
            self.assertEqual(gallery.safe_resolve(volume, relative), real)
            if os.name == "nt":
                for value in ("C:relative.jpg", "C:\\outside.jpg", "\\\\server\\share\\image.jpg"):
                    with self.assertRaises(gallery.UnsafePath):
                        gallery.safe_resolve(real, value)

    def test_pillow_preserves_orientation_and_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "portrait.jpg"
            destination = Path(temporary) / "thumb.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (800, 400), "red").save(source, exif=exif)
            before = source.read_bytes()
            with mock.patch.object(gallery, "run", side_effect=FileNotFoundError):
                self.assertTrue(gallery.generate_image_thumb(str(source), str(destination)))
            with Image.open(destination) as result:
                self.assertEqual(result.size, (250, 500))
            self.assertEqual(source.read_bytes(), before)

    def test_process_query_keeps_child_alive(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            for _ in range(3):
                self.assertTrue(gallery.process_running(child.pid))
                self.assertIsNone(child.poll())
            self.assertFalse(gallery.process_running(0))
            self.assertFalse(gallery.process_running(-1))
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertFalse(gallery.process_running(child.pid))

    def test_background_start_status_repeat_start_stop(self):
        with tempfile.TemporaryDirectory(prefix="gallery background ") as temporary:
            cfg = gallery.load_config(temporary)
            cfg["port"] = free_port()
            gallery.save_config(temporary, cfg)
            try:
                self.assertIn("服务已启动", cli(temporary, "start"))
                pid_file = Path(temporary) / "gallery.pid"
                pid = pid_file.read_text()
                self.assertIn("运行中", cli(temporary, "status"))
                if os.name == "nt":
                    batch = subprocess.run(
                        ["cmd.exe", "/d", "/c", str(SCRIPT.parent / "run.bat"),
                         "--app-dir", temporary, "status"],
                        capture_output=True, text=True, encoding="utf-8", timeout=20, check=True,
                    )
                    self.assertIn("运行中", batch.stdout)
                self.assertEqual(request(cfg["port"], "/login")[0], 200)
                self.assertIn("已在运行中", cli(temporary, "start"))
                self.assertEqual(pid_file.read_text(), pid)
                self.assertEqual(request(cfg["port"], "/login")[0], 200)
            finally:
                cli(temporary, "stop")
            self.assertFalse((Path(temporary) / "gallery.pid").exists())
            self.assertIn("未运行", cli(temporary, "status"))


if __name__ == "__main__":
    unittest.main()
