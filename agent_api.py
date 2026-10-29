"""Unauthenticated local agent API, using only Python's standard library."""

import base64
import hashlib
import json
import mimetypes
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen


PREFIX = "/api/agent"
MAX_MEDIA = 100 * 1024 * 1024
MAX_REFERENCE = 10 * 1024 * 1024
VIDEO_AUDIO = {
    "dreamina-seedance-2-5-260628", "dreamina-seedance-2-0-260128",
    "dreamina-seedance-2-0-fast-260128", "dreamina-seedance-2-0-mini-260615",
    "grok-imagine-video", "grok-imagine-video-1.5",
}
SEEDREAM_SIZES = {
    "1024x1024": "2048x2048", "1536x1024": "2400x1600", "1024x1536": "1600x2400",
    "1792x1024": "2560x1440", "1024x1792": "1440x2560",
}


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat()


class AgentAPI:
    def __init__(self, gateway, library, store_media, upload_asset):
        self.gateway = gateway.rstrip("/")
        self.library = library.resolve()
        self.directory = library / "agent-jobs"
        self.directory.mkdir(exist_ok=True)
        self.store_media = store_media
        self.upload_asset = upload_asset
        self.lock = threading.RLock()
        self.slots = threading.BoundedSemaphore(2)
        self.jobs = {}
        for path in self.directory.glob("*.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            self.jobs[job["id"]] = job
            if job["status"] in {"queued", "running"}:
                self.update(job["id"], status="interrupted", error={"message": "Server restarted; generation was not retried. Check upstream_task_id before submitting again."})

    def update(self, job_id, **changes):
        with self.lock:
            job = {**self.jobs[job_id], **changes, "updatedAt": now()}
            path = self.directory / f"{job_id}.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
            self.jobs[job_id] = job
            return self.public(job)

    @staticmethod
    def public(job):
        return {key: value for key, value in job.items() if key not in {"fingerprint", "idempotency_key"}}

    def get(self, path):
        if path in {PREFIX, PREFIX + "/"}:
            return 200, {
                "name": "Media Lab Agent API", "version": "1", "authentication": "none",
                "openapi": PREFIX + "/openapi.json", "models": PREFIX + "/models",
                "jobs": PREFIX + "/jobs", "library": "/api/library",
                "workflow": "Get models, POST a job, poll its status_url until completed/failed/interrupted, download results[].url. Reuse Idempotency-Key when retrying POST.",
                "max_concurrent_jobs": 2,
            }
        if path == PREFIX + "/openapi.json":
            return 200, openapi()
        if path == PREFIX + "/models":
            return 200, self.gateway_json("/v1/models")
        with self.lock:
            if path == PREFIX + "/jobs":
                return 200, {"data": [self.public(job) for job in sorted(self.jobs.values(), key=lambda job: job["createdAt"], reverse=True)[:50]]}
            job_id = path.removeprefix(PREFIX + "/jobs/")
            if path.startswith(PREFIX + "/jobs/") and job_id in self.jobs:
                return 200, self.public(self.jobs[job_id])
        raise APIError(404, "agent endpoint or job not found")

    def submit(self, body, idempotency_key=None):
        if not isinstance(body, dict):
            raise APIError(400, "body must be a JSON object")
        allowed = {"type", "model", "prompt", "size", "seconds", "generate_audio", "references"}
        if set(body) - allowed:
            raise APIError(400, "unknown fields: " + ", ".join(sorted(set(body) - allowed)))
        for name, limit in (("model", 300), ("prompt", 4000)):
            if not isinstance(body.get(name), str) or not body[name].strip() or len(body[name]) > limit:
                raise APIError(400, f"{name} must be a nonempty string of at most {limit} characters")
        media_type = body.get("type", "image")
        if media_type not in {"image", "video"}:
            raise APIError(400, "type must be image or video")
        size = body.get("size", "1280x720" if media_type == "video" else "1024x1024")
        if not isinstance(size, str) or not re.fullmatch(r"[1-9]\d{1,4}x[1-9]\d{1,4}", size):
            raise APIError(400, "size must have WIDTHxHEIGHT format")
        seconds = body.get("seconds", 5)
        if type(seconds) is not int or not 1 <= seconds <= 60:
            raise APIError(400, "seconds must be an integer between 1 and 60")
        model = body["model"].lower().split("/")[-1]
        if "generate_audio" in body and (type(body["generate_audio"]) is not bool or media_type != "video" or model not in VIDEO_AUDIO):
            raise APIError(400, "generate_audio requires a supported video model and a boolean")
        references = body.get("references", [])
        limit = 5 if model == "grok-imagine-image-2.0" else 10
        if not isinstance(references, list) or len(references) > limit or any(not isinstance(ref, str) or not ref.startswith(("data:", "http://", "https://", "/library/")) for ref in references):
            raise APIError(400, f"references must contain at most {limit} data URLs, HTTP URLs, or /library/ URLs")
        if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 200):
            raise APIError(400, "Idempotency-Key must contain 1 to 200 characters")
        request = {**body, "type": media_type, "size": size, "seconds": seconds, "references": references}
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if idempotency_key:
                existing = next((job for job in self.jobs.values() if job.get("idempotency_key") == idempotency_key), None)
                if existing:
                    if existing["fingerprint"] != fingerprint:
                        raise APIError(409, "Idempotency-Key already used with different parameters")
                    return 200, self.public(existing)
            if not self.slots.acquire(blocking=False):
                raise APIError(429, "Two generations are already active; wait and retry")
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = {
                "id": job_id, "type": media_type, "model": body["model"], "prompt": body["prompt"],
                "status": "queued", "status_url": f"{PREFIX}/jobs/{job_id}", "createdAt": now(),
                "upstream_task_id": None, "results": [], "error": None,
                "fingerprint": fingerprint, "idempotency_key": idempotency_key,
            }
            try:
                job = self.update(job_id)
                threading.Thread(target=self.run, args=(job_id, request), daemon=True).start()
            except Exception:
                self.jobs.pop(job_id, None)
                self.slots.release()
                raise
            return 202, job

    def fetch(self, url, data=None, content_type=None, limit=MAX_MEDIA):
        headers = {"User-Agent": "MediaLab/1.0"}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            with urlopen(Request(url, data=data, headers=headers), timeout=180) as response:
                content = response.read(limit + 1)
                if len(content) > limit:
                    raise APIError(502, "upstream response exceeds size limit")
                return content, response.headers.get_content_type()
        except HTTPError as error:
            with error:
                detail = error.read(8192).decode("utf-8", "replace")
            try:
                payload = json.loads(detail)
                detail = payload.get("error", payload.get("message", detail))
                if isinstance(detail, dict):
                    detail = detail.get("message", "upstream request failed")
            except (ValueError, AttributeError):
                detail = "upstream request failed"
            raise APIError(502, f"Upstream HTTP {error.code}: {str(detail)[:2000]}") from error
        except (URLError, TimeoutError) as error:
            raise APIError(502, f"Upstream unavailable: {error.reason if isinstance(error, URLError) else error}") from error

    def gateway_json(self, path, payload=None, multipart=None):
        data, mime = None, None
        if payload is not None:
            data, mime = json.dumps(payload).encode(), "application/json"
        if multipart:
            data, mime = multipart
        content, _ = self.fetch(self.gateway + path, data, mime)
        try:
            result = json.loads(content)
        except (ValueError, UnicodeDecodeError) as error:
            raise APIError(502, "upstream returned invalid JSON") from error
        if not isinstance(result, dict):
            raise APIError(502, "upstream must return a JSON object")
        return result

    def reference(self, url):
        if url.startswith("/library/"):
            path = (self.library / unquote(url[len("/library/"):])).resolve()
            if path.parent != self.library or not path.is_file() or path.stat().st_size > MAX_REFERENCE:
                raise APIError(400, "reference must be a library media file of at most 10 MB")
            content, mime = path.read_bytes(), mimetypes.guess_type(path.name)[0]
        elif url.startswith("data:"):
            match = re.fullmatch(r"data:([^;,]+);base64,(.+)", url, re.S)
            if not match:
                raise APIError(400, "reference must be a base64 data URL")
            mime, encoded = match.groups()
            if len(encoded) > (MAX_REFERENCE + 2) // 3 * 4:
                raise APIError(400, "reference exceeds 10 MB")
            content = base64.b64decode(encoded, validate=True)
        else:
            content, mime = self.fetch(url, limit=MAX_REFERENCE)
        if not content or len(content) > MAX_REFERENCE or not mime or not re.fullmatch(r"(?:image|video)/[A-Za-z0-9.+-]+", mime):
            raise APIError(400, "reference must be an image or video of at most 10 MB")
        return content, mime

    @staticmethod
    def multipart(payload, references):
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in payload.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        for index, (content, mime) in enumerate(references):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="reference-{index}{mimetypes.guess_extension(mime) or ".bin"}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def generate(self, job_id, request):
        media_type, model = request["type"], request["model"]
        short_model = model.lower().split("/")[-1]
        references = [self.reference(ref) for ref in request["references"]]
        data_urls = [f"data:{mime};base64,{base64.b64encode(content).decode()}" for content, mime in references]
        payload = {"model": model, "prompt": request["prompt"], "size": request["size"]}
        if media_type == "image":
            if any(not mime.startswith("image/") for _, mime in references):
                raise APIError(400, "image generation requires image references")
            payload.update(n=1, response_format="url")
            if "seedream" in short_model:
                payload["size"] = SEEDREAM_SIZES.get(request["size"], request["size"])
                if data_urls:
                    payload["image"] = data_urls[0] if len(data_urls) == 1 else data_urls
            elif references:
                return self.gateway_json("/v1/images/edits", multipart=self.multipart(payload, references))
            return self.gateway_json("/v1/images/generations", payload)
        payload.update(seconds=str(request["seconds"]), resolution="720p")
        if short_model in VIDEO_AUDIO:
            payload["generate_audio"] = request.get("generate_audio", False)
        if short_model == "dreamina-seedance-2-5-260628":
            payload.update(duration=request["seconds"], aspect_ratio={"720x1280": "9:16", "1024x1024": "1:1"}.get(request["size"], "16:9"))
            labels = []
            for index, (content, mime) in enumerate(references):
                kind = "videos" if mime.startswith("video/") else "images"
                asset = self.upload_asset(content, mime, f"reference-{index}{mimetypes.guess_extension(mime) or '.bin'}")
                payload.setdefault(kind, []).append(asset)
                labels.append(f"{'Video' if kind == 'videos' else 'Image'} {len(payload[kind])}")
            if labels:
                payload["prompt"] = f"Используй загруженные материалы в указанном порядке: {', '.join(labels)}. Их роль определяется описанием задачи ниже.\n\n{request['prompt']}"
        elif data_urls:
            payload["input_reference"] = data_urls[0] if len(data_urls) == 1 else data_urls
        result = self.gateway_json("/v1/videos/generations", payload)
        nested = result.get("data") if isinstance(result.get("data"), dict) else {}
        task_id = result.get("task_id") or result.get("id") or nested.get("task_id") or nested.get("id")
        if not task_id:
            return result
        self.update(job_id, upstream_task_id=str(task_id))
        while True:
            nested = result.get("data") if isinstance(result.get("data"), dict) else {}
            status = str(result.get("status") or nested.get("status") or "").lower()
            if status in {"failed", "error", "cancelled", "canceled"}:
                raise APIError(502, str(result.get("error") or nested.get("error") or f"Video {status}"))
            if self.media_items(result):
                return result
            if status in {"completed", "success", "succeeded"}:
                content, mime = self.fetch(f"{self.gateway}/v1/videos/{quote(str(task_id), safe='')}/content")
                return {"data": [{"b64_json": base64.b64encode(content).decode(), "mime": mime}]}
            time.sleep(4)
            result = self.gateway_json(f"/v1/videos/{quote(str(task_id), safe='')}")

    @staticmethod
    def media_items(payload):
        for key in ("data", "output", "result"):
            if isinstance(payload.get(key), list):
                return [media for item in payload[key] if isinstance(item, dict) for media in AgentAPI.media_items(item)]
            if isinstance(payload.get(key), dict):
                found = AgentAPI.media_items(payload[key])
                if found:
                    return found
        if any(payload.get(key) for key in ("url", "video_url", "output_url", "b64_json", "base64")):
            return [payload]
        return []

    def run(self, job_id, request):
        try:
            self.update(job_id, status="running")
            result = self.generate(job_id, request)
            media = self.media_items(result)
            if not media:
                raise APIError(502, "Gateway returned no media or video task ID")
            results = []
            for item in media:
                url = item.get("url") or item.get("video_url") or item.get("output_url")
                if url:
                    if url.startswith("data:"):
                        mime, encoded = url[5:].split(";base64,", 1)
                        content = base64.b64decode(encoded, validate=True)
                    else:
                        if not urlparse(url).scheme:
                            url = self.gateway + "/" + url.lstrip("/")
                        content, mime = self.fetch(url)
                else:
                    content = base64.b64decode(item.get("b64_json") or item["base64"], validate=True)
                    mime = item.get("mime") or ("video/mp4" if request["type"] == "video" else "image/png")
                if not mime.startswith(request["type"] + "/"):
                    raise APIError(502, "Gateway returned an unexpected media content type")
                results.append(self.store_media(content, mime, request["type"], request["prompt"], request["model"]))
            self.update(job_id, status="completed", results=results)
        except Exception as error:
            self.update(job_id, status="failed", error={"message": str(error)[:2000]})
        finally:
            self.slots.release()


def openapi():
    def response(schema, description="Success"):
        return {"description": description, "content": {"application/json": {"schema": schema}}}

    job = {"$ref": "#/components/schemas/Job"}
    error = response({"type": "object", "properties": {"error": {"type": "object", "properties": {"message": {"type": "string"}}}}}, "Request failed")
    paths = {
        PREFIX: {"get": {"operationId": "describeMediaLab", "summary": "Discover the unauthenticated local agent API", "responses": {"200": response({"type": "object"})}}},
        PREFIX + "/models": {"get": {"operationId": "listModels", "summary": "Read the live gateway catalog; listing does not guarantee generation availability", "responses": {"200": response({"type": "object", "properties": {"data": {"type": "array", "items": {"type": "object"}}}}), "502": error}}},
        PREFIX + "/jobs": {
            "get": {"operationId": "listJobs", "summary": "List the latest 50 jobs", "responses": {"200": response({"type": "object", "properties": {"data": {"type": "array", "items": job}}})}},
            "post": {
                "operationId": "createGeneration", "summary": "Start image/video generation and save results in the shared library",
                "description": "Poll status_url every 4 seconds. completed, failed and interrupted are terminal. No automatic resubmission or cancellation. At most two active jobs. A submitted job may use a paid upstream provider.",
                "parameters": [{"in": "header", "name": "Idempotency-Key", "schema": {"type": "string", "minLength": 1, "maxLength": 200}, "description": "Reuse the same key and body to avoid duplicate generation, including after restart."}],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Generation"}}}},
                "responses": {"202": response(job, "Queued"), "200": response(job, "Existing idempotent job"), **{str(status): error for status in (400, 409, 429, 500)}},
            },
        },
        PREFIX + "/jobs/{job_id}": {"get": {"operationId": "getGeneration", "parameters": [{"in": "path", "name": "job_id", "required": True, "schema": {"type": "string"}}], "responses": {"200": response(job), "404": error}}},
        "/api/library": {"get": {"operationId": "listLibrary", "summary": "List shared browser and agent results; download each item's relative URL from this server", "responses": {"200": response({"type": "array", "items": {"$ref": "#/components/schemas/Media"}})}}},
    }
    return {
        "openapi": "3.1.0", "info": {"title": "Media Lab Agent API", "version": "1.0.0"},
        "servers": [{"url": "/"}], "security": [], "paths": paths,
        "components": {"schemas": {
            "Generation": {"type": "object", "additionalProperties": False, "required": ["model", "prompt"], "properties": {
                "type": {"type": "string", "enum": ["image", "video"], "default": "image"},
                "model": {"type": "string", "minLength": 1, "maxLength": 300},
                "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
                "size": {"type": "string", "pattern": "^[1-9]\\d{1,4}x[1-9]\\d{1,4}$", "description": "Defaults: image 1024x1024, video 1280x720. Seedream UI sizes are mapped to its larger native sizes."},
                "seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 5, "description": "Video duration; actual support depends on the selected model."},
                "generate_audio": {"type": "boolean", "description": "Video only. Supported models: " + ", ".join(sorted(VIDEO_AUDIO)) + ". Defaults to false for these models."},
                "references": {"type": "array", "maxItems": 10, "items": {"type": "string"}, "description": "Up to 10 image/video references, 5 for Grok image 2.0; at most 10 MB each. Use base64 data URLs, HTTP(S) URLs, or existing /library/ file URLs. Image generation accepts images only. Seedance 2.5 uploads through the existing server asset service."},
            }},
            "Media": {"type": "object", "required": ["id", "url", "type"], "properties": {name: {"type": "string"} for name in ("id", "url", "type", "prompt", "model", "createdAt")}},
            "Job": {"type": "object", "required": ["id", "status", "status_url", "results"], "properties": {
                **{name: {"type": "string"} for name in ("id", "type", "model", "prompt", "status_url", "createdAt", "updatedAt")},
                "status": {"type": "string", "enum": ["queued", "running", "completed", "failed", "interrupted"]},
                "upstream_task_id": {"type": ["string", "null"]},
                "results": {"type": "array", "items": {"$ref": "#/components/schemas/Media"}},
                "error": {"type": ["object", "null"], "properties": {"message": {"type": "string"}}},
            }},
        }},
    }
