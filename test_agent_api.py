"""Focused HTTP integration proof; all media generation uses an isolated fake gateway."""

import base64
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server
from agent_api import AgentAPI


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5XkAAAAASUVORK5CYII=")
VIDEO = b"isolated-test-video-content"
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value, mime="application/json"):
        raw = json.dumps(value).encode() if mime == "application/json" else value
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/v1/models":
            if getattr(self.server, "invalid_catalog", False):
                self.reply(200, b"invalid JSON", "text/plain")
            else:
                self.reply(200, {"data": [{"id": "test-image"}, {"id": "test-video"}]})
        elif self.path.endswith("/content"):
            self.reply(200, VIDEO, "video/mp4")
        elif self.path == "/v1/videos/remote-video":
            self.reply(200, {"data": {"status": "completed"}})
        elif self.path == "/result.png":
            self.reply(200, PNG, "image/png")
        elif self.path == "/wrong-type":
            self.reply(200, b"<html>not media</html>", "text/html")
        else:
            self.reply(404, {"error": "fixture endpoint not found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path in {"/v1/images/edits", "/v1/assets"}:
            message = BytesParser(policy=default).parsebytes(b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
            fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in message.iter_parts()}
            self.server.calls.append((self.path, fields))
            if self.path == "/v1/assets":
                self.reply(201, {"data": {"asset": "https://assets.example.test/reference"}})
                return
            self.reply(200, {"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
            return
        payload = json.loads(raw)
        self.server.calls.append((self.path, payload))
        if payload["prompt"] == "hold":
            self.server.release.wait(10)
        if payload["prompt"] == "fail":
            self.reply(503, {"error": {"message": "fixture model unavailable"}})
        elif payload["prompt"] == "empty":
            self.reply(200, {"data": []})
        elif payload["prompt"] == "bad-json":
            self.reply(200, b"not json", "text/plain")
        elif payload["prompt"] == "wrong-mime":
            self.reply(200, {"data": [{"url": "/wrong-type"}]})
        elif self.path == "/v1/videos/generations":
            self.reply(202, {"data": {"id": "remote-video"}})
        elif payload["prompt"] == "url":
            self.reply(200, {"data": [{"url": "/result.png"}]})
        else:
            self.reply(200, {"data": [{"b64_json": base64.b64encode(PNG).decode()}]})


class QuietHandler(server.Handler):
    def log_message(self, *args):
        pass


class AgentAPITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        library = root / "library"
        library.mkdir()
        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        self.gateway.calls = []
        self.gateway.release = threading.Event()
        self.upstream = f"http://127.0.0.1:{self.gateway.server_port}"
        for name, value in {"ROOT": root, "LIBRARY": library, "INDEX": library / "index.json", "GATEWAY": self.upstream, "ASSET_API_BASE": self.upstream, "ASSET_API_KEY": "test-fixture-only"}.items():
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = AgentAPI(self.upstream, library, server.store_media, server.upload_asset)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.http.agent_api = self.service
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        for http in (self.gateway, self.http):
            thread = threading.Thread(target=http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            thread.start()
        self.addCleanup(self.stop_servers)

    def stop_servers(self):
        self.gateway.release.set()
        deadline = time.monotonic() + 12
        while any(job["status"] in {"running", "queued"} for job in self.service.jobs.values()) and time.monotonic() < deadline:
            time.sleep(0.01)
        for http in (self.http, self.gateway):
            http.shutdown()
            http.server_close()

    def request(self, path, body=None, method=None, headers=None):
        raw = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, raw, {"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            response = urlopen(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            return response.status, json.loads(content) if response.headers.get_content_type() == "application/json" else content

    def submit(self, **body):
        status, job = self.request("/api/agent/jobs", {"model": "test-image", "prompt": "test", **body})
        self.assertEqual(status, 202, job)
        return job

    def finish(self, job):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, job = self.request(job["status_url"])
            self.assertEqual(status, 200)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.02)
        self.fail("fixture job did not finish")

    def test_discovery_catalog_and_openapi_without_authentication(self):
        self.assertEqual(self.request("/api/agent")[1]["authentication"], "none")
        self.assertEqual(len(self.request("/api/agent/models")[1]["data"]), 2)
        status, spec = self.request("/api/agent/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertEqual(spec["security"], [])
        for path in spec["paths"].values():
            for operation in path.values():
                self.assertIn("operationId", operation)
        self.gateway.invalid_catalog = True
        self.assertEqual(self.request("/api/agent/models")[0], 502)

    def test_image_and_file_share_browser_library(self):
        job = self.finish(self.submit())
        self.assertEqual(job["status"], "completed", job)
        item = job["results"][0]
        self.assertEqual(item["parameters"], {"size": "1024x1024", "reference_count": 0})
        self.assertEqual(self.request(item["url"]), (200, PNG))
        self.assertEqual(self.request("/api/library")[1], [item])
        self.assertEqual(self.request("/api/agent/jobs")[1]["data"][0]["id"], job["id"])
        status, browser_item = self.request("/api/library", {"data_url": DATA_URL, "prompt": "browser"})
        self.assertEqual(status, 201)
        self.assertNotIn("parameters", browser_item)
        self.assertEqual(len(self.request("/api/library")[1]), 2)
        self.assertEqual(self.request(browser_item["url"])[1], PNG)

    def test_image_from_relative_upstream_url(self):
        job = self.finish(self.submit(prompt="url"))
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(self.request(job["results"][0]["url"])[1], PNG)

    def test_browser_generation_parameters_survive_library_reload(self):
        parameters = {"size": "1792x1024", "reference_count": 2}
        status, item = self.request("/api/library", {"data_url": DATA_URL, "parameters": {**parameters, "references": [DATA_URL]}})
        self.assertEqual(status, 201)
        self.assertEqual(item["parameters"], parameters)
        self.assertEqual(self.request("/api/library")[1][0]["parameters"], parameters)
        self.assertNotIn(DATA_URL, server.INDEX.read_text())

    def test_invalid_library_parameters_do_not_create_files(self):
        for parameters in ([], {"size": "invalid"}, {"seconds": True}, {"generate_audio": "yes"}, {"reference_count": 11}):
            with self.subTest(parameters=parameters):
                self.assertEqual(self.request("/api/library", {"data_url": DATA_URL, "parameters": parameters})[0], 400)
        self.assertEqual(self.request("/api/library")[1], [])
        self.assertEqual(list(server.LIBRARY.glob("*.png")), [])

    def test_multipart_and_seedream_references(self):
        item = self.request("/api/library", {"data_url": DATA_URL})[1]
        job = self.finish(self.submit(references=[item["url"]]))
        self.assertEqual(job["status"], "completed", job)
        path, payload = self.gateway.calls[-1]
        self.assertEqual(path, "/v1/images/edits")
        self.assertEqual(payload["image[]"], PNG)
        job = self.finish(self.submit(model="seedream-5-0", references=[DATA_URL]))
        self.assertEqual(job["status"], "completed", job)
        path, payload = self.gateway.calls[-1]
        self.assertEqual(path, "/v1/images/generations")
        self.assertEqual(payload["image"], DATA_URL)
        self.assertEqual(payload["size"], "2048x2048")

    def test_video_poll_download_and_audio(self):
        job = self.finish(self.submit(type="video", model="grok-imagine-video-1.5", seconds=6, generate_audio=True, references=[DATA_URL]))
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["upstream_task_id"], "remote-video")
        self.assertEqual(job["results"][0]["parameters"], {"size": "1280x720", "seconds": 6, "generate_audio": True, "reference_count": 1})
        self.assertEqual(self.request(job["results"][0]["url"])[1], VIDEO)
        payload = self.gateway.calls[-1][1]
        self.assertIs(payload["generate_audio"], True)
        self.assertEqual(payload["input_reference"], DATA_URL)
        self.assertEqual(payload["seconds"], "6")

    def test_seedance_uses_shared_asset_upload(self):
        job = self.finish(self.submit(type="video", model="dreamina-seedance-2-5-260628", references=[DATA_URL]))
        self.assertEqual(job["status"], "completed", job)
        asset_path, asset_payload = self.gateway.calls[0]
        self.assertEqual(asset_path, "/v1/assets")
        self.assertEqual(asset_payload["file"], PNG)
        payload = self.gateway.calls[1][1]
        self.assertEqual(payload["images"], ["https://assets.example.test/reference"])
        self.assertEqual(payload["aspect_ratio"], "16:9")
        self.assertIn("Image 1", payload["prompt"])
        self.assertIs(payload["generate_audio"], False)

    def test_browser_reference_upload_remains_compatible(self):
        raw, mime = AgentAPI.multipart({}, [(PNG, "image/png")])
        request = Request(self.base + "/api/video-assets", raw, {"Content-Type": mime})
        with urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(json.load(response)["asset"], "https://assets.example.test/reference")

    def test_capacity_idempotency_and_concurrent_library_writes(self):
        body = {"model": "test-image", "prompt": "hold"}
        headers = {"Idempotency-Key": "unique-test-request"}
        status, first = self.request("/api/agent/jobs", body, headers=headers)
        self.assertEqual(status, 202)
        status, duplicate = self.request("/api/agent/jobs", body, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(self.request("/api/agent/jobs", {**body, "prompt": "changed"}, headers=headers)[0], 409)
        second = self.submit(prompt="hold")
        self.assertEqual(self.request("/api/agent/jobs", body)[0], 429)
        with ThreadPoolExecutor(max_workers=6) as pool:
            saves = list(pool.map(lambda i: self.request("/api/library", {"data_url": DATA_URL, "prompt": f"browser-{i}"}), range(6)))
        self.assertTrue(all(status == 201 for status, _ in saves))
        self.gateway.release.set()
        for job in (first, second):
            self.assertEqual(self.finish(job)["status"], "completed")
        self.assertEqual(len(self.request("/api/library")[1]), 8)
        self.assertEqual(len(self.gateway.calls), 2)

    def test_validation_and_missing_routes(self):
        valid = {"model": "test-image", "prompt": "test"}
        cases = [[], {}, {"model": "x", "prompt": " "}, *({**valid, **change} for change in ({"type": "audio"}, {"seconds": True}, {"generate_audio": True}, {"references": ["/etc/passwd"]}, {"model": "grok-imagine-image-2.0", "references": [DATA_URL] * 6}, {"extra": 1}))]
        for body in cases:
            with self.subTest(body=body):
                status, value = self.request("/api/agent/jobs", body)
                self.assertEqual(status, 400, value)
                self.assertIn("message", value["error"])
        self.assertEqual(self.request("/api/agent/jobs/missing")[0], 404)
        self.assertEqual(self.request("/api/agent/unknown")[0], 404)
        self.assertEqual(self.request("/api/agent/jobs", method="DELETE")[0], 404)
        self.assertEqual(self.gateway.calls, [])

    def test_upstream_and_reference_errors_are_terminal(self):
        for prompt in ("fail", "empty", "bad-json", "wrong-mime"):
            with self.subTest(prompt=prompt):
                job = self.finish(self.submit(prompt=prompt))
                self.assertEqual(job["status"], "failed", job)
                self.assertTrue(job["error"]["message"])
                self.assertEqual(job["results"], [])
        for reference in ("/library/../index.json", DATA_URL.replace("image/png", "video/mp4"), DATA_URL.replace("image/png", "image/png\r\nX-Injected: yes")):
            job = self.finish(self.submit(references=[reference]))
            self.assertEqual(job["status"], "failed", job)
        self.assertEqual(self.request("/api/library")[1], [])

    def test_restart_preserves_history_and_never_resubmits(self):
        body = {"model": "test-image", "prompt": "test"}
        headers = {"Idempotency-Key": "persisted-request"}
        status, job = self.request("/api/agent/jobs", body, headers=headers)
        self.assertEqual(status, 202)
        self.assertEqual(self.finish(job)["status"], "completed")
        restarted = AgentAPI(self.upstream, server.LIBRARY, server.store_media, server.upload_asset)
        self.assertEqual(restarted.get(job["status_url"])[1]["status"], "completed")
        self.service.update(job["id"], status="running", upstream_task_id="known-remote-task")
        restarted = AgentAPI(self.upstream, server.LIBRARY, server.store_media, server.upload_asset)
        self.assertEqual(restarted.get(job["status_url"])[1]["status"], "interrupted")
        self.assertEqual(restarted.get(job["status_url"])[1]["upstream_task_id"], "known-remote-task")
        self.assertEqual(restarted.submit(body, "persisted-request")[0], 200)
        self.assertEqual(len(self.gateway.calls), 1)
        self.service = restarted


if __name__ == "__main__":
    unittest.main()
