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
import threading
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


def request(port, path, method="GET", headers=None, body=None, timeout=10):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
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


def cleanup_temporary(directory):
    # Windows 终止进程后，日志句柄偶尔稍晚释放；仍然要求目录最终清理成功。
    deadline = time.monotonic() + 3
    while True:
        try:
            directory.cleanup()
            return
        except PermissionError:
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


class GalleryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gallery test ")
        self.addCleanup(cleanup_temporary, self.temporary)
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
        self.assertEqual([folder["name"] for folder in listing["folders"]], ["子目录"])
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

    def test_non_media_files_cannot_be_listed_or_downloaded(self):
        names = ("notes.txt", "config.json", "document.pdf", "report.docx",
                 "archive.zip", "program.exe", "README", ".env")
        original = b"private non-media fixture"
        for name in names:
            (self.media / name).write_bytes(original)
        upper_image = self.media / "UPPER.JPG"
        Image.new("RGB", (10, 10), "green").save(upper_image)

        listing = json.loads(request(self.port, "/api/list?root=0", headers=self.headers)[2])
        expected = {self.image.name, upper_image.name}
        self.assertEqual({item["name"] for item in listing["items"]}, expected)
        all_media = json.loads(request(self.port, "/api/all", headers=self.headers)[2])
        self.assertEqual({item["name"] for item in all_media["items"]}, expected)

        for name in names:
            query = urlencode({"root": 0, "path": name})
            for method, extra in (("GET", {}), ("HEAD", {}),
                                  ("GET", {"Range": "bytes=0-7"})):
                with self.subTest(name=name, method=method, headers=extra):
                    status, _, body = request(self.port, "/raw?" + query,
                        method=method, headers={**self.headers, **extra})
                    self.assertEqual(status, 403)
                    self.assertNotIn(original, body)
            self.assertEqual(request(self.port, "/thumb?" + query, headers=self.headers)[0], 400)
            self.assertEqual((self.media / name).read_bytes(), original)
        image_query = urlencode({"root": 0, "path": upper_image.name})
        self.assertEqual(request(self.port, "/raw?" + image_query, headers=self.headers)[2],
                         upper_image.read_bytes())

    def test_media_symlink_cannot_expose_non_media_target(self):
        document = self.media / "private.txt"
        document.write_text("private alias fixture", encoding="utf-8")
        alias = self.media / "looks-like-a-photo.jpg"
        try:
            alias.symlink_to(document)
        except OSError as error:
            self.skipTest("系统不允许创建软链接: " + str(error))
        for endpoint in ("/api/list?root=0", "/api/all"):
            listing = json.loads(request(self.port, endpoint, headers=self.headers)[2])
            self.assertNotIn(alias.name, {item["name"] for item in listing["items"]})
        query = urlencode({"root": 0, "path": alias.name})
        self.assertEqual(request(self.port, "/raw?" + query, headers=self.headers)[0], 403)
        self.assertEqual(request(self.port, "/thumb?" + query, headers=self.headers)[0], 400)

    @unittest.skipUnless(os.name == "nt", "NTFS 文件数据流仅适用于 Windows")
    def test_windows_data_stream_cannot_bypass_media_restriction(self):
        stream = self.image.name + ":private.jpg"
        try:
            (self.media / stream).write_bytes(b"private stream fixture")
        except OSError as error:
            self.skipTest("测试磁盘不支持文件数据流: " + str(error))
        query = urlencode({"root": 0, "path": stream})
        for endpoint in ("/raw?", "/thumb?"):
            self.assertEqual(request(self.port, endpoint + query, headers=self.headers)[0], 403)
        self.assertEqual(hashlib.sha256(self.image.read_bytes()).hexdigest(), self.original_hash)

    def test_all_media_deduplicates_nested_roots(self):
        nested = self.media / "nested"
        nested.mkdir()
        Image.new("RGB", (10, 10), "red").save(nested / "nested.jpg")
        cli(self.app, "add", str(nested))
        listing = json.loads(request(self.port, "/api/all", headers=self.headers)[2])
        self.assertEqual(listing["total"], 2)
        self.assertEqual(len(listing["items"]), 2)

    def test_folder_totals_and_type_filter_before_pagination(self):
        album = self.media / "album"
        nested = album / "nested"
        nested.mkdir(parents=True)
        Image.new("RGB", (10, 10), "red").save(album / "one.jpg")
        Image.new("RGB", (10, 10), "blue").save(nested / "two.png")
        (album / "one.mp4").write_bytes(b"video fixture")
        (nested / "two.MOV").write_bytes(b"video fixture")
        (nested / "notes.txt").write_text("not media", encoding="utf-8")
        (self.media / "empty").mkdir()
        listing = json.loads(request(self.port, "/api/all", headers=self.headers)[2])
        self.assertEqual(listing["total"], 5)
        self.assertEqual((listing["image_count"], listing["video_count"]), (3, 2))
        root = json.loads(request(self.port, "/api/list?root=0", headers=self.headers)[2])
        self.assertEqual({folder["name"]: folder["count"] for folder in root["folders"]},
                         {"album": 4, "empty": 0})
        self.assertEqual({folder["name"]: (folder["image_count"], folder["video_count"])
                          for folder in root["folders"]}, {"album": (2, 2), "empty": (0, 0)})
        for kind, total in (("image", 3), ("video", 2)):
            first = json.loads(request(self.port, "/api/all?type=%s&count=1" % kind,
                                       headers=self.headers)[2])
            rest = json.loads(request(self.port, "/api/all?type=%s&count=5&start=1" % kind,
                                      headers=self.headers)[2])
            self.assertEqual(first["total"], total)
            combined = first["items"] + rest["items"]
            self.assertEqual(len(combined), total)
            self.assertEqual({item["type"] for item in combined}, {kind})
            self.assertEqual(len({item["path"] for item in combined}), total)
            folder = json.loads(request(self.port, "/api/list?root=0&path=album&type=" + kind,
                                        headers=self.headers)[2])
            self.assertEqual(len(folder["items"]), 1)
            self.assertEqual(folder["items"][0]["type"], kind)
            self.assertEqual(folder["folders"], [{"name": "nested", "path": "album/nested", "count": 2,
                                                 "image_count": 1, "video_count": 1}])
        for endpoint in ("/api/all?type=document", "/api/list?root=0&type=document"):
            self.assertEqual(request(self.port, endpoint, headers=self.headers)[0], 400)

    def visibility_request(self, action, folders, extra_headers=None):
        return request(self.port, "/api/hidden-folders", method="POST",
                       body=json.dumps({"action": action, "folders": folders}).encode(),
                       headers={**self.headers, "Content-Type": "application/json", **(extra_headers or {})})

    def test_hidden_folder_counts_persistence_restore_and_originals(self):
        album = self.media / "album"
        nested = album / "nested"
        nested.mkdir(parents=True)
        Image.new("RGB", (10, 10), "red").save(album / "one.jpg")
        Image.new("RGB", (10, 10), "blue").save(nested / "two.jpg")
        (album / "clip.mp4").write_bytes(b"read-only fixture")
        neighbor = self.media / "album-other"
        neighbor.mkdir()
        Image.new("RGB", (10, 10), "green").save(neighbor / "three.jpg")
        originals = {p: p.read_bytes() for p in self.media.rglob("*") if p.is_file()}
        config_before = (self.app / "config.json").read_bytes()
        request(self.port, "/api/all", headers=self.headers)  # Warm the isolated scan cache.
        self.assertEqual(self.visibility_request("hide", [{"root": 0, "path": "album/nested"}])[0], 200)
        root = json.loads(request(self.port, "/api/roots", headers=self.headers)[2])["roots"][0]
        self.assertEqual((root["image_count"], root["video_count"]), (3, 1))
        # Hiding a parent collapses the child preference and subtracts its subtree once.
        self.assertEqual(self.visibility_request("hide", [{"root": 0, "path": "album"}])[0], 200)
        listing = json.loads(request(self.port, "/api/list?root=0", headers=self.headers)[2])
        self.assertEqual([f["name"] for f in listing["folders"]], ["album-other"])
        all_media = json.loads(request(self.port, "/api/all", headers=self.headers)[2])
        self.assertEqual((all_media["total"], all_media["image_count"], all_media["video_count"]), (2, 2, 0))
        self.assertTrue(all(not it["path"].startswith("album/") for it in all_media["items"]))
        self.assertEqual(request(self.port, "/api/list?root=0&path=album", headers=self.headers)[0], 404)
        hidden = json.loads(request(self.port, "/api/hidden-folders", headers=self.headers)[2])["folders"]
        self.assertEqual([(f["root"], f["path"]) for f in hidden], [(0, "album")])
        self.assertEqual(gallery.load_hidden_folders(str(self.app)), [str(album.resolve())])
        self.stop_server()
        self.server = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(SCRIPT), "--app-dir", str(self.app), "serve"],
            stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 10
        while True:
            try:
                if request(self.port, "/login")[0] == 200:
                    break
            except OSError:
                pass
            self.assertLess(time.monotonic(), deadline, "隔离测试服务未重新启动")
            time.sleep(0.05)
        after_restart = json.loads(request(self.port, "/api/hidden-folders", headers=self.headers)[2])["folders"]
        self.assertEqual(after_restart, hidden)
        self.assertEqual(self.visibility_request("restore", hidden)[0], 200)
        restored = json.loads(request(self.port, "/api/all", headers=self.headers)[2])
        self.assertEqual((restored["image_count"], restored["video_count"]), (4, 1))
        self.assertEqual((self.app / "config.json").read_bytes(), config_before)
        self.assertTrue(all(p.read_bytes() == content for p, content in originals.items()))

    def test_hide_whole_root_and_restore_missing_folder(self):
        self.assertEqual(self.visibility_request("hide", [{"root": 0, "path": ""}])[0], 200)
        self.assertEqual(json.loads(request(self.port, "/api/roots", headers=self.headers)[2])["roots"], [])
        self.assertEqual(json.loads(request(self.port, "/api/all", headers=self.headers)[2])["items"], [])
        self.assertEqual(self.visibility_request("restore", [{"root": 0, "path": ""}])[0], 200)
        self.assertEqual(len(json.loads(request(self.port, "/api/roots", headers=self.headers)[2])["roots"]), 1)
        empty = self.media / "empty"
        empty.mkdir()
        self.assertEqual(self.visibility_request("hide", [{"root": 0, "path": "empty"}])[0], 200)
        empty.rmdir()
        self.assertEqual(self.visibility_request("restore", [{"root": 0, "path": "empty"}])[0], 200)
        self.assertEqual(gallery.load_hidden_folders(str(self.app)), [])

    def test_hide_requires_authentication_safe_paths_and_same_origin(self):
        album = self.media / "album"
        album.mkdir()
        body = json.dumps({"action": "hide", "folders": [{"root": 0, "path": "album"}]}).encode()
        self.assertEqual(request(self.port, "/api/hidden-folders", method="POST", body=body,
                                 headers={"Content-Type": "application/json"})[0], 401)
        self.assertEqual(self.visibility_request("hide", [{"root": 0, "path": "album"}],
                                                {"Origin": "https://other.example"})[0], 403)
        for unsafe in ("../outside", str(self.base), "album/../../outside"):
            self.assertEqual(self.visibility_request("hide", [{"root": 0, "path": "album"},
                                                              {"root": 0, "path": unsafe}])[0], 403)
            self.assertEqual(gallery.load_hidden_folders(str(self.app)), [])
        self.assertEqual(self.visibility_request("hide", [{"root": 0, "path": self.image.name}])[0], 404)
        self.assertEqual(self.visibility_request("hide", [{"root": 99, "path": ""}])[0], 400)
        self.assertEqual(self.visibility_request("unknown", [{"root": 0, "path": "album"}])[0], 400)
        self.assertEqual(self.visibility_request("hide", [{"root": 0, "path": "album"}],
                                                {"Origin": "http://127.0.0.1:" + str(self.port)})[0], 200)
        self.assertEqual(hashlib.sha256(self.image.read_bytes()).hexdigest(), self.original_hash)


class PortabilityTests(unittest.TestCase):
    def test_refresh_is_nonblocking_and_media_remains_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            # Windows TEMP 可能使用 ADMINI~1；与服务规范化后的缓存键保持一致。
            base = Path(temporary).resolve()
            app = base / "app"
            media = base / "media"
            media.mkdir()
            image = media / "photo.png"
            Image.new("RGB", (10, 10), "red").save(image)
            nested = media / "nested"
            nested.mkdir()
            Image.new("RGB", (10, 10), "blue").save(nested / "child.jpg")
            cfg = gallery.load_config(str(app))
            cfg["roots"] = [{"name": "媒体", "path": str(media)}]
            gallery.save_config(str(app), cfg)
            headers = {"Authorization": "Basic " + base64.b64encode(
                (cfg["username"] + ":" + cfg["password"]).encode()).decode()}
            started, release = threading.Event(), threading.Event()
            original_walk = gallery.walk_media
            calls = []

            def delayed_walk(root):
                calls.append(root)
                started.set()
                release.wait(10)
                return original_walk(root)

            cache = gallery.ScanCache()
            handler = type("TestHandler", (gallery.GalleryHandler,), {"app_dir": str(app)})
            server = gallery.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            with mock.patch.object(gallery, "SCAN", cache), mock.patch.object(
                    gallery, "walk_media", side_effect=delayed_walk):
                try:
                    self.assertEqual(request(port, "/api/refresh", headers=headers, timeout=1)[0], 200)
                    self.assertTrue(started.wait(1))
                    roots = json.loads(request(port, "/api/roots", headers=headers, timeout=1)[2])
                    self.assertIsNone(roots["roots"][0]["count"])
                    self.assertTrue(roots["roots"][0]["scanning"])
                    listing = json.loads(request(port, "/api/list?root=0", headers=headers, timeout=1)[2])
                    self.assertEqual(listing["items"][0]["name"], image.name)
                    self.assertEqual(listing["folders"], [{"name": "nested", "path": "nested", "count": None,
                                                          "image_count": None, "video_count": None}])
                    self.assertTrue(listing["scanning"])
                    pending = json.loads(request(port, "/api/all?background=1", headers=headers, timeout=1)[2])
                    self.assertEqual(pending["items"], [])
                    self.assertTrue(pending["scanning"])
                    self.assertEqual(request(port, "/api/refresh", headers=headers, timeout=1)[0], 200)
                    self.assertEqual(len(calls), 1)
                    raw = "/raw?" + urlencode({"root": 0, "path": image.name})
                    self.assertEqual(request(port, raw, headers=headers, timeout=1)[2], image.read_bytes())
                finally:
                    release.set()
                    cache.root_items(str(media))
                    self.assertEqual(cache.folder_counts(str(media)), ({
                        "": {"count": 2, "image_count": 2, "video_count": 0},
                        "nested": {"count": 1, "image_count": 1, "video_count": 0}}, False))
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_background_scan_retains_cached_media_until_new_scan_finishes(self):
        cache = gallery.ScanCache()
        first = [{"mtime": 1, "name": "first.jpg"}]
        with mock.patch.object(gallery, "walk_media", return_value=first):
            self.assertEqual(cache.root_items("test-root"), first)
        started, release = threading.Event(), threading.Event()
        second = [{"mtime": 2, "name": "second.jpg"}]

        def delayed_walk(root):
            started.set()
            release.wait(10)
            return second

        with mock.patch.object(gallery, "walk_media", side_effect=delayed_walk) as walk:
            cache.refresh_async("test-root", force=True)
            try:
                self.assertTrue(started.wait(1))
                self.assertEqual(cache.root_items("test-root", wait=False), first)
                self.assertTrue(cache.snapshot("test-root")[1])
                cache.refresh_async("test-root", force=True)
                walk.assert_called_once()
            finally:
                release.set()
                deadline = time.monotonic() + 2
                while cache.snapshot("test-root")[1] and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertEqual(cache.snapshot("test-root"), (second, False))

    def test_bundled_ffmpeg_makes_video_thumbnail(self):
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "clip.mp4"
            thumbnail = Path(temporary) / "thumb.jpg"
            with mock.patch.object(gallery.shutil, "which", return_value=None):
                gallery.run([
                    gallery.ffmpeg_executable(), "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=640x360:d=0.2", "-c:v", "libx264", str(video),
                ])
                self.assertTrue(gallery.generate_video_thumb(str(video), str(thumbnail)))
            with Image.open(thumbnail) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertLessEqual(max(image.size), gallery.THUMB_SIZE)

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
