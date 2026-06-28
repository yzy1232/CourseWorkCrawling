"""React Web GUI for the TronClass downloader.

Run:
    python web_gui.py

Then open http://127.0.0.1:8765/ in a browser.
"""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import tempfile
import time
import traceback
import uuid
import webbrowser
import zipfile
from email.parser import BytesParser
from email.policy import default as email_default_policy
from io import BytesIO
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import cache
from core import (
    coursewares_with_cache,
    courses_with_cache,
    homeworks_with_cache,
    iter_courseware_overview,
    iter_homework_overview,
    prepare_courseware_download,
    prepare_download,
    submit_homework_files,
)
from downloader import sanitize

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
ZIP_CACHE_DIR = ROOT / ".cache" / "zips"
HOST = "127.0.0.1"
PORT = 8765


class SinglePortThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False


class ApiHandler(SimpleHTTPRequestHandler):
    server_version = "CourseWorkWebGUI/1.0"

    def translate_path(self, path: str) -> str:
        if path == "/":
            return str(WEB_ROOT / "index.html")
        clean = path.split("?", 1)[0].lstrip("/")
        return str(WEB_ROOT / clean)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/config"):
            self._json(
                {
                    "username": os.getenv("HZCU_USERNAME", ""),
                    "course_id": os.getenv("COURSE_ID", ""),
                    "has_password": bool(os.getenv("HZCU_PASSWORD")),
                }
            )
            return
        if self.path.startswith("/api/download/file/"):
            self._serve_download_file()
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            log_lines: list[str] = []

            def log(msg: str) -> None:
                log_lines.append(msg)

            if self.path == "/api/submit/homework":
                self._submit_homework(log_lines, log)
                return

            payload = self._payload()

            if self.path == "/api/homeworks/stream":
                self._stream_overview(payload, kind="homeworks")
                return

            if self.path == "/api/coursewares/stream":
                self._stream_overview(payload, kind="coursewares")
                return

            if self.path == "/api/download/homeworks/stream":
                self._stream_download(payload, kind="homeworks")
                return

            if self.path == "/api/download/coursewares/stream":
                self._stream_download(payload, kind="coursewares")
                return

            if self.path == "/api/courses":
                refresh = bool(payload.get("refresh", True))
                username, password, _course = self._creds(
                    payload, need_course=False, need_password=refresh
                )
                courses, cached_at, stale = courses_with_cache(
                    username,
                    password,
                    refresh=refresh,
                    log=log,
                )
                self._json(
                    {
                        "courses": courses,
                        "cached_at": cached_at.isoformat() if cached_at else None,
                        "stale": stale,
                        "logs": log_lines,
                    }
                )
                return

            if self.path == "/api/homeworks":
                refresh = bool(payload.get("refresh", True))
                username, password, course = self._creds(payload, need_password=refresh)
                records, cached_at, stale = homeworks_with_cache(
                    username,
                    password,
                    course,
                    refresh=refresh,
                    download_submissions=True,
                    log=log,
                )
                self._json(
                    {
                        "homeworks": records,
                        "cached_at": cached_at.isoformat() if cached_at else None,
                        "stale": stale,
                        "logs": log_lines,
                    }
                )
                return

            if self.path == "/api/coursewares":
                refresh = bool(payload.get("refresh", True))
                username, password, course = self._creds(payload, need_password=refresh)
                records, cached_at, stale = coursewares_with_cache(
                    username,
                    password,
                    course,
                    refresh=refresh,
                    log=log,
                )
                self._json(
                    {
                        "coursewares": records,
                        "cached_at": cached_at.isoformat() if cached_at else None,
                        "stale": stale,
                        "logs": log_lines,
                    }
                )
                return

            if self.path == "/api/download/homeworks":
                username, password, course = self._creds(payload)
                prep = prepare_download(
                    username,
                    password,
                    course,
                    "downloads",
                    download_submissions=True,
                    records=_records_from_payload(payload),
                    selected_homework_ids=payload.get("selected_homework_ids") or None,
                    selected_assignment_ids=payload.get("selected_assignment_ids") or None,
                    selected_submission_ids=payload.get("selected_submission_ids") or None,
                    log=log,
                )
                self._zip_response(
                    prep["session"],
                    prep["tasks"],
                    filename=f"course_{course}_homeworks.zip",
                    logs=log_lines,
                )
                return

            if self.path == "/api/download/coursewares":
                username, password, course = self._creds(payload)
                prep = prepare_courseware_download(
                    username,
                    password,
                    course,
                    "downloads",
                    records=_records_from_payload(payload),
                    selected_ids=payload.get("selected_ids") or None,
                    selected_material_ids=payload.get("selected_material_ids") or None,
                    log=log,
                )
                self._zip_response(
                    prep["session"],
                    prep["tasks"],
                    filename=f"course_{course}_coursewares.zip",
                    logs=log_lines,
                )
                return

            self.send_error(404, "Unknown API route")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": str(exc)}, status=500)

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _multipart_payload(self) -> tuple[dict[str, str], list[dict[str, Any]]]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("请求格式错误：需要 multipart/form-data")

        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            raise ValueError("请求体为空")

        raw = self.rfile.read(length)
        header = (
            f"Content-Type: {content_type}\r\n"
            "MIME-Version: 1.0\r\n\r\n"
        ).encode("utf-8")
        message = BytesParser(policy=email_default_policy).parsebytes(header + raw)
        if not message.is_multipart():
            raise ValueError("请求格式错误：未找到 multipart 内容")

        fields: dict[str, str] = {}
        files: list[dict[str, Any]] = []
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            content = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename is None:
                charset = part.get_content_charset() or "utf-8"
                fields[str(name)] = content.decode(charset, errors="replace")
            else:
                files.append(
                    {
                        "field": str(name),
                        "filename": filename or "upload",
                        "content": content,
                    }
                )
        return fields, files

    def _submit_homework(self, logs: list[str], log: Any) -> None:
        fields, uploads = self._multipart_payload()
        username, password, _course = self._creds(fields, need_course=False)
        homework_id = str(fields.get("homework_id") or "").strip()
        comment = str(fields.get("comment") or "")
        confirmed = str(fields.get("confirm") or "").strip().lower() in {"1", "true", "yes"}

        if not homework_id:
            raise ValueError("缺少作业 ID")
        if not confirmed:
            raise ValueError("请先确认提交操作")
        if not uploads:
            raise ValueError("请先选择要提交的文件")

        with tempfile.TemporaryDirectory(prefix="coursework_submit_") as temp_dir:
            used: set[str] = set()
            paths: list[str] = []
            for upload in uploads:
                safe_name = _dedupe_name(
                    used,
                    sanitize(str(upload.get("filename") or "upload"), "upload"),
                )
                path = Path(temp_dir) / safe_name
                path.write_bytes(upload["content"])
                paths.append(str(path))

            result = submit_homework_files(
                username,
                password,
                homework_id,
                paths,
                comment=comment,
                log=log,
            )

        self._json(
            {
                "homework_id": homework_id,
                "submitted_count": len(result.get("uploads") or []),
                "uploads": result.get("uploads") or [],
                "response": result.get("response"),
                "logs": logs,
            }
        )

    def _creds(
        self,
        payload: dict[str, Any],
        *,
        need_course: bool = True,
        need_password: bool = True,
    ) -> tuple[str, str, str]:
        username = str(payload.get("username") or os.getenv("HZCU_USERNAME") or "").strip()
        password = str(payload.get("password") or os.getenv("HZCU_PASSWORD") or "")
        course = str(payload.get("course_id") or os.getenv("COURSE_ID") or "").strip()
        if not username:
            raise ValueError("缺少账号")
        if need_password and not password:
            raise ValueError("缺少账号或密码")
        if need_course and not course:
            raise ValueError("缺少课程 ID")
        return username, password, course

    def _json(self, payload: Any, *, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ndjson_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _ndjson(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        self.wfile.write(data)
        self.wfile.flush()

    def _stream_overview(self, payload: dict[str, Any], *, kind: str) -> None:
        refresh = bool(payload.get("refresh", True))
        username, password, course = self._creds(payload, need_password=refresh)
        cache_name = "homeworks" if kind == "homeworks" else "coursewares"
        label = "作业" if kind == "homeworks" else "课件"

        if not refresh:
            cached, cached_at = cache.load(cache_name, str(course))
            if cached is not None:
                self._ndjson_headers()
                total = len(cached)
                self._ndjson({"type": "start", "kind": kind, "total": total})
                for idx, record in enumerate(cached, 1):
                    self._ndjson(
                        {
                            "type": "item",
                            "kind": kind,
                            "index": idx,
                            "total": total,
                            "record": record,
                        }
                    )
                self._ndjson(
                    {
                        "type": "done",
                        "kind": kind,
                        "count": total,
                        "cached_at": cached_at.isoformat() if cached_at else None,
                        "stale": cache.is_stale(cached_at),
                    }
                )
                return
            if not password:
                raise ValueError("缺少账号或密码")

        self._ndjson_headers()
        records: list[dict] = []
        started = False

        def emit(event: dict[str, Any]) -> None:
            self._ndjson(event)

        def log(msg: str) -> None:
            emit({"type": "log", "kind": kind, "message": msg})

        try:
            iterator = (
                iter_homework_overview(
                    username,
                    password,
                    course,
                    download_submissions=True,
                    log=log,
                )
                if kind == "homeworks"
                else iter_courseware_overview(username, password, course, log=log)
            )
            for idx, total, record in iterator:
                if not started:
                    emit({"type": "start", "kind": kind, "total": total})
                    started = True
                records.append(record)
                emit(
                    {
                        "type": "item",
                        "kind": kind,
                        "index": idx,
                        "total": total,
                        "record": record,
                    }
                )

            if not started:
                emit({"type": "start", "kind": kind, "total": 0})

            cache.save(cache_name, str(course), records)
            _cached, cached_at = cache.load(cache_name, str(course))
            emit(
                {
                    "type": "done",
                    "kind": kind,
                    "count": len(records),
                    "cached_at": cached_at.isoformat() if cached_at else None,
                    "stale": False,
                    "message": f"{label}已刷新",
                }
            )
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 流式接口已发响应头，只能用事件返回错误
            traceback.print_exc()
            emit({"type": "error", "kind": kind, "error": str(exc)})

    def _stream_download(self, payload: dict[str, Any], *, kind: str) -> None:
        username, password, course = self._creds(payload)
        label = "作业" if kind == "homeworks" else "课件"
        filename = (
            f"course_{course}_homeworks.zip"
            if kind == "homeworks"
            else f"course_{course}_coursewares.zip"
        )

        self._ndjson_headers()

        def emit(event: dict[str, Any]) -> None:
            self._ndjson(event)

        def log(msg: str) -> None:
            emit({"type": "log", "kind": kind, "message": msg})

        try:
            emit({"type": "log", "kind": kind, "message": f"正在整理{label}下载列表"})
            prep = (
                prepare_download(
                    username,
                    password,
                    course,
                    "downloads",
                    download_submissions=True,
                    records=_records_from_payload(payload),
                    selected_homework_ids=payload.get("selected_homework_ids") or None,
                    selected_assignment_ids=payload.get("selected_assignment_ids") or None,
                    selected_submission_ids=payload.get("selected_submission_ids") or None,
                    log=log,
                )
                if kind == "homeworks"
                else prepare_courseware_download(
                    username,
                    password,
                    course,
                    "downloads",
                    records=_records_from_payload(payload),
                    selected_ids=payload.get("selected_ids") or None,
                    selected_material_ids=payload.get("selected_material_ids") or None,
                    log=log,
                )
            )
            tasks = prep["tasks"]
            if not tasks:
                emit({"type": "error", "kind": kind, "error": "没有可下载的附件"})
                return

            _cleanup_zip_cache()
            token = uuid.uuid4().hex
            archive_path = ZIP_CACHE_DIR / f"{token}.zip"
            archive_path.parent.mkdir(parents=True, exist_ok=True)

            files = [_download_task_payload(task) for task in tasks]
            emit(
                {
                    "type": "start",
                    "kind": kind,
                    "total": len(tasks),
                    "filename": filename,
                    "files": files,
                }
            )

            failures: list[str] = []
            written = 0
            used: set[str] = set()
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for idx, task in enumerate(tasks, 1):
                    task_payload = _download_task_payload(task, index=idx, total=len(tasks))
                    emit({"type": "file_start", "kind": kind, **task_payload})
                    arcname = _download_arcname(used, task)
                    try:
                        with prep["session"].get(
                            task.url,
                            stream=True,
                            timeout=60,
                            headers={"X-Requested-With": "XMLHttpRequest"},
                        ) as resp:
                            if resp.status_code != 200:
                                error = f"HTTP {resp.status_code}"
                                failures.append(f"{task.name}: {error}")
                                emit({"type": "file_error", "kind": kind, "error": error, **task_payload})
                                continue
                            with zf.open(arcname, "w") as fh:
                                for chunk in resp.iter_content(chunk_size=64 * 1024):
                                    if chunk:
                                        fh.write(chunk)
                            written += 1
                            emit({"type": "file_done", "kind": kind, **task_payload})
                    except Exception as exc:  # noqa: BLE001 单个附件失败不影响其它文件
                        error = str(exc)
                        failures.append(f"{task.name}: {error}")
                        emit({"type": "file_error", "kind": kind, "error": error, **task_payload})

                manifest = {
                    "file_count": written,
                    "requested_count": len(tasks),
                    "failures": failures,
                }
                zf.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )

            if written == 0:
                try:
                    archive_path.unlink()
                except OSError:
                    pass
                emit(
                    {
                        "type": "error",
                        "kind": kind,
                        "error": "附件下载失败，未生成可用 ZIP",
                        "failures": failures,
                    }
                )
                return

            meta = {
                "filename": sanitize(filename, "download.zip"),
                "created_at": time.time(),
            }
            archive_path.with_suffix(".json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            emit(
                {
                    "type": "done",
                    "kind": kind,
                    "count": written,
                    "requested_count": len(tasks),
                    "failures": failures,
                    "filename": meta["filename"],
                    "url": f"/api/download/file/{token}",
                }
            )
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 流式接口已发响应头，只能用事件返回错误
            traceback.print_exc()
            emit({"type": "error", "kind": kind, "error": str(exc)})

    def _serve_download_file(self) -> None:
        token = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
        if len(token) != 32 or not all(ch in "0123456789abcdef" for ch in token):
            self.send_error(404, "Download not found")
            return

        archive_path = ZIP_CACHE_DIR / f"{token}.zip"
        if not archive_path.exists():
            self.send_error(404, "Download not found")
            return

        filename = "download.zip"
        meta_path = archive_path.with_suffix(".json")
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                filename = sanitize(str(meta.get("filename") or filename), filename)
            except Exception:  # noqa: BLE001 元数据损坏时仍允许下载 ZIP
                filename = "download.zip"

        stat = archive_path.stat()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        with archive_path.open("rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _zip_response(self, session: Any, tasks: list[Any], *, filename: str, logs: list[str]) -> None:
        if not tasks:
            self._json({"error": "没有可下载的附件", "logs": logs}, status=400)
            return

        buffer = BytesIO()
        failures: list[str] = []
        written = 0
        used: set[str] = set()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for task in tasks:
                arcname = _dedupe_zip_name(
                    used,
                    f"{sanitize(task.hw_title)}/{sanitize(task.kind)}/{sanitize(task.name)}",
                )
                try:
                    with session.get(
                        task.url,
                        stream=True,
                        timeout=60,
                        headers={"X-Requested-With": "XMLHttpRequest"},
                    ) as resp:
                        if resp.status_code != 200:
                            failures.append(f"{task.name}: HTTP {resp.status_code}")
                            continue
                        with zf.open(arcname, "w") as fh:
                            for chunk in resp.iter_content(chunk_size=64 * 1024):
                                if chunk:
                                    fh.write(chunk)
                        written += 1
                except Exception as exc:  # noqa: BLE001 单个附件失败不影响其它文件
                    failures.append(f"{task.name}: {exc}")

            manifest = {
                "file_count": written,
                "requested_count": len(tasks),
                "failures": failures,
                "logs": logs,
            }
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

        if written == 0:
            self._json(
                {
                    "error": "附件下载失败，未生成可用 ZIP",
                    "failures": failures,
                    "logs": logs,
                },
                status=502,
            )
            return

        data = buffer.getvalue()
        safe_filename = sanitize(filename, "download.zip")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        if failures:
            self.send_header("X-Download-Warnings", str(len(failures)))
        self.end_headers()
        self.wfile.write(data)

    def guess_type(self, path: str) -> str:
        if path.endswith(".js"):
            return "text/javascript"
        return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _dedupe_zip_name(used: set[str], name: str) -> str:
    if name not in used:
        used.add(name)
        return name
    base, dot, ext = name.rpartition(".")
    stem = base if dot else name
    suffix = f".{ext}" if dot else ""
    i = 1
    while True:
        candidate = f"{stem} ({i}){suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1


def _download_arcname(used: set[str], task: Any) -> str:
    return _dedupe_zip_name(
        used,
        f"{sanitize(task.hw_title)}/{sanitize(task.kind)}/{sanitize(task.name)}",
    )


def _download_kind_label(kind: str) -> str:
    return {
        "assignment": "题目附件",
        "submission": "提交附件",
        "material": "课件附件",
    }.get(kind, kind)


def _download_task_payload(task: Any, *, index: int | None = None, total: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": task.id,
        "name": task.name,
        "title": task.hw_title,
        "kind_name": task.kind,
        "kind_label": _download_kind_label(str(task.kind)),
        "progress_key": task.progress_key or "",
        "size": task.total_bytes,
    }
    if index is not None:
        payload["index"] = index
    if total is not None:
        payload["total"] = total
    return payload


def _records_from_payload(payload: dict[str, Any]) -> list[dict] | None:
    if "records" not in payload:
        return None
    records = payload.get("records")
    if records is None:
        return None
    if not isinstance(records, list):
        raise ValueError("records 格式错误")
    return records


def _cleanup_zip_cache(max_age_seconds: int = 24 * 60 * 60) -> None:
    if not ZIP_CACHE_DIR.exists():
        return
    cutoff = time.time() - max_age_seconds
    for path in ZIP_CACHE_DIR.glob("*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _dedupe_name(used: set[str], name: str) -> str:
    if name not in used:
        used.add(name)
        return name
    stem, suffix = os.path.splitext(name)
    i = 1
    while True:
        candidate = f"{stem} ({i}){suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1


def main() -> int:
    load_dotenv()
    if not (WEB_ROOT / "index.html").exists():
        print("web/index.html 不存在")
        return 2
    httpd = SinglePortThreadingHTTPServer((HOST, PORT), ApiHandler)
    url = f"http://{HOST}:{PORT}/"
    print(f"React GUI: {url}")
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
