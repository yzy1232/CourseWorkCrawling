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
import traceback
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core import (
    coursewares_with_cache,
    courses_with_cache,
    download_coursewares,
    homeworks_with_cache,
    run_download,
)

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
HOST = "127.0.0.1"
PORT = 8765


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
                    "output_dir": os.getenv("OUTPUT_DIR", "downloads"),
                    "has_password": bool(os.getenv("HZCU_PASSWORD")),
                }
            )
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._payload()
            log_lines: list[str] = []

            def log(msg: str) -> None:
                log_lines.append(msg)

            if self.path == "/api/courses":
                username, password, _course = self._creds(payload, need_course=False)
                courses, cached_at, stale = courses_with_cache(
                    username,
                    password,
                    refresh=bool(payload.get("refresh", True)),
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
                username, password, course = self._creds(payload)
                records, cached_at, stale = homeworks_with_cache(
                    username,
                    password,
                    course,
                    refresh=bool(payload.get("refresh", True)),
                    download_submissions=bool(payload.get("download_submissions", True)),
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
                username, password, course = self._creds(payload)
                records, cached_at, stale = coursewares_with_cache(
                    username,
                    password,
                    course,
                    refresh=bool(payload.get("refresh", True)),
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
                result = run_download(
                    username,
                    password,
                    course,
                    payload.get("output_dir") or "downloads",
                    download_submissions=bool(payload.get("download_submissions", True)),
                    list_only=bool(payload.get("list_only", False)),
                    parallel=int(payload.get("parallel") or 4),
                    log=log,
                )
                result["logs"] = log_lines
                self._json(result)
                return

            if self.path == "/api/download/coursewares":
                username, password, course = self._creds(payload)
                result = download_coursewares(
                    username,
                    password,
                    course,
                    payload.get("output_dir") or "downloads",
                    list_only=bool(payload.get("list_only", False)),
                    parallel=int(payload.get("parallel") or 4),
                    selected_ids=payload.get("selected_ids") or None,
                    log=log,
                )
                result["logs"] = log_lines
                self._json(result)
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

    def _creds(
        self, payload: dict[str, Any], *, need_course: bool = True
    ) -> tuple[str, str, str]:
        username = str(payload.get("username") or os.getenv("HZCU_USERNAME") or "").strip()
        password = str(payload.get("password") or os.getenv("HZCU_PASSWORD") or "")
        course = str(payload.get("course_id") or os.getenv("COURSE_ID") or "").strip()
        if not username or not password:
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

    def guess_type(self, path: str) -> str:
        if path.endswith(".js"):
            return "text/javascript"
        return mimetypes.guess_type(path)[0] or "application/octet-stream"


def main() -> int:
    load_dotenv()
    if not (WEB_ROOT / "index.html").exists():
        print("web/index.html 不存在")
        return 2
    httpd = ThreadingHTTPServer((HOST, PORT), ApiHandler)
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
