#!/usr/bin/env python3
import base64
import json
import mimetypes
import os
import re
import sys
import threading
import uuid
from email.parser import BytesParser
from email.policy import default
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen

from agent_api import AgentAPI, APIError

ROOT = Path(__file__).resolve().parent
LIBRARY = ROOT / "library"
INDEX = LIBRARY / "index.json"
GATEWAY = os.environ.get("MEDIA_GATEWAY_URL", "http://127.0.0.1:8317").rstrip("/")
ASSET_API_BASE = os.environ.get("MEDIA_ASSET_API_BASE", "https://newapi.apiaw.com").rstrip("/")
ASSET_API_KEY = os.environ.get("MEDIA_ASSET_API_KEY", "")
ASSET_ENV_FILE = os.environ.get("MEDIA_ASSET_ENV_FILE", str(Path.home() / "cliproxyapi/video-gateway.env"))
LIBRARY.mkdir(exist_ok=True)
LIBRARY_LOCK = threading.RLock()


def env_file_value(name):
    if ASSET_API_KEY:
        return ASSET_API_KEY
    try:
        for line in Path(ASSET_ENV_FILE).read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip().strip("\"'")
    except OSError:
        pass
    return ""


def read_items():
    try:
        value = json.loads(INDEX.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def write_items(items):
    temp = INDEX.with_suffix(".tmp")
    temp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(INDEX)


def extension_for(mime, media_type):
    known = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4", "video/webm": ".webm"}
    return known.get(mime) or (".mp4" if media_type == "video" else mimetypes.guess_extension(mime) or ".bin")


def store_media(content, mime, media_type, prompt, model, parameters=None):
    if not content or len(content) > 100 * 1024 * 1024:
        raise ValueError("media must be between 1 byte and 100 MB")
    if parameters is not None:
        if not isinstance(parameters, dict):
            raise ValueError("parameters must be an object")
        parameters = {key: parameters[key] for key in ("size", "seconds", "generate_audio", "reference_count") if key in parameters}
        if "size" in parameters and (not isinstance(parameters["size"], str) or not re.fullmatch(r"[1-9]\d{1,4}x[1-9]\d{1,4}", parameters["size"])):
            raise ValueError("parameters.size must have WIDTHxHEIGHT format")
        if "seconds" in parameters and (type(parameters["seconds"]) is not int or not 1 <= parameters["seconds"] <= 60):
            raise ValueError("parameters.seconds must be an integer between 1 and 60")
        if "generate_audio" in parameters and type(parameters["generate_audio"]) is not bool:
            raise ValueError("parameters.generate_audio must be a boolean")
        if "reference_count" in parameters and (type(parameters["reference_count"]) is not int or not 0 <= parameters["reference_count"] <= 10):
            raise ValueError("parameters.reference_count must be an integer between 0 and 10")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"{stamp}-{uuid.uuid4().hex[:10]}{extension_for(mime, media_type)}"
    item = {
        "id": uuid.uuid4().hex,
        "url": f"/library/{filename}",
        "type": media_type,
        "prompt": str(prompt)[:4000],
        "model": str(model)[:300],
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    if parameters is not None:
        item["parameters"] = parameters
    with LIBRARY_LOCK:
        (LIBRARY / filename).write_bytes(content)
        items = read_items()
        items.insert(0, item)
        write_items(items)
    return item


def upload_asset(content, mime, filename):
    api_key = env_file_value("APIAW_API_KEY")
    if not api_key:
        raise ValueError("APIAW_API_KEY is not configured for reference uploads")
    boundary = uuid.uuid4().hex
    filename = re.sub(r'[\r\n"\\]', "_", filename)
    upload_body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        + f"Content-Type: {mime}\r\n\r\n".encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    request = Request(
        f"{ASSET_API_BASE}/v1/assets", data=upload_body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "MediaLab/1.0"},
        method="POST",
    )
    with urlopen(request, timeout=120) as response:
        payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    asset = payload.get("asset") or (payload.get("data") or {}).get("asset")
    if not asset:
        raise ValueError("asset service did not return an asset reference")
    return asset


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, status, value):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def proxy_video_content(self, task_id):
        target = f"{GATEWAY}/v1/videos/{quote(task_id, safe='')}/content"
        headers = {"User-Agent": "MediaLab/1.0"}
        authorization = self.headers.get("Authorization")
        if authorization:
            headers["Authorization"] = authorization
        try:
            with urlopen(Request(target, headers=headers), timeout=180) as response:
                content = response.read(100 * 1024 * 1024 + 1)
                if len(content) > 100 * 1024 * 1024:
                    raise ValueError("video content exceeds 100 MB")
                mime = response.headers.get_content_type() or "video/mp4"
                self.send_response(response.status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
        except Exception as error:
            status = getattr(error, "code", 502)
            detail = getattr(error, "reason", str(error))
            self.send_json(status if isinstance(status, int) and 400 <= status <= 599 else 502, {"error": str(detail)})

    def upload_video_asset(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("reference upload must use multipart/form-data")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 12 * 1024 * 1024:
            raise ValueError("reference file must be between 1 byte and 10 MB")
        raw = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii", "ignore") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
        )
        part = next((item for item in message.iter_attachments() if item.get_payload(decode=True)), None)
        if part is None:
            raise ValueError("reference file is required")
        content = part.get_payload(decode=True)
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("reference file must not exceed 10 MB")
        filename = part.get_filename() or "reference.bin"
        mime = part.get_content_type() or "application/octet-stream"
        asset = upload_asset(content, mime, filename)
        self.send_json(201, {"asset": asset, "name": filename, "mime": mime})

    def agent_request(self, method):
        path = urlparse(self.path).path
        if path != "/api/agent" and not path.startswith("/api/agent/"):
            return False
        try:
            if method == "GET":
                status, value = self.server.agent_api.get(path)
            elif method == "POST" and path == "/api/agent/jobs":
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 100 * 1024 * 1024:
                    raise APIError(400, "JSON body must be between 1 byte and 100 MB")
                body = json.loads(self.rfile.read(length))
                status, value = self.server.agent_api.submit(body, self.headers.get("Idempotency-Key"))
            else:
                raise APIError(404, "agent endpoint not found")
            self.send_json(status, value)
        except APIError as error:
            self.send_json(error.status, {"error": {"message": str(error)}})
        except (ValueError, TypeError) as error:
            self.send_json(400, {"error": {"message": str(error)}})
        except Exception:
            self.send_json(500, {"error": {"message": "agent API storage or internal error"}})
        return True

    def do_GET(self):
        if self.agent_request("GET"):
            return
        path = urlparse(self.path).path
        if path == "/api/library":
            self.send_json(200, read_items())
            return
        if path.startswith("/api/videos/") and path.endswith("/content"):
            task_id = unquote(path[len("/api/videos/"):-len("/content")]).strip("/")
            if task_id:
                self.proxy_video_content(task_id)
                return
            self.send_json(400, {"error": "task_id is required"})
            return
        super().do_GET()

    def do_POST(self):
        if self.agent_request("POST"):
            return
        path = urlparse(self.path).path
        if path == "/api/video-assets":
            try:
                self.upload_video_asset()
            except Exception as error:
                status = getattr(error, "code", 400)
                self.send_json(status if isinstance(status, int) and 400 <= status <= 599 else 502, {"error": str(getattr(error, "reason", error))})
            return
        if path != "/api/library":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 100 * 1024 * 1024:
                raise ValueError("invalid payload size")
            body = json.loads(self.rfile.read(length))
            media_type = "video" if body.get("type") == "video" else "image"
            source_url = str(body.get("source_url", ""))
            data_url = str(body.get("data_url", ""))
            if source_url.startswith(("http://", "https://")):
                request = Request(source_url, headers={"User-Agent": "MediaLab/1.0"})
                with urlopen(request, timeout=120) as response:
                    content = response.read(100 * 1024 * 1024 + 1)
                    mime = response.headers.get_content_type()
            else:
                match = re.match(r"^data:([^;,]+);base64,(.+)$", data_url, re.S)
                if not match:
                    raise ValueError("source_url or data_url is required")
                mime, encoded = match.groups()
                content = base64.b64decode(encoded, validate=True)
            item = store_media(content, mime, media_type, body.get("prompt", ""), body.get("model", ""), body.get("parameters"))
            self.send_json(201, item)
        except Exception as error:
            self.send_json(400, {"error": str(error)})

    def do_DELETE(self):
        if self.agent_request("DELETE"):
            return
        if urlparse(self.path).path != "/api/library":
            self.send_error(404)
            return
        with LIBRARY_LOCK:
            for item in read_items():
                url = str(item.get("url", ""))
                if url.startswith("/library/"):
                    (ROOT / unquote(url.lstrip("/"))).unlink(missing_ok=True)
            write_items([])
        self.send_json(200, [])


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4173
    bind_host = os.environ.get("MEDIA_BIND_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((bind_host, port), Handler)
    server.agent_api = AgentAPI(GATEWAY, LIBRARY, store_media, upload_asset)
    display_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::"} else bind_host
    print(f"Media Lab server listening on http://{display_host}:{port} (bind {bind_host})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
