"""Probe TronClass upload endpoints without submitting homework.

Default mode logs in and searches the SPA HTML/JS for upload-related paths.
With --post, it uploads a tiny temp file to candidate endpoints and reports the
first JSON response that looks like an upload object. It does not call any
homework submission endpoint.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

from auth import BASE_URL, login
from crawler import _write_headers, upload_file

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


UPLOAD_CANDIDATES = [
    "api/uploads/reference",
    "api/uploads",
    "api/upload",
    "api/attachments",
    "api/resources",
    "api/user/resources/files",
    "api/uploads/blob",
]

FILE_FIELDS = ["file", "upload", "attachment"]


def _find_asset_urls(html: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for attr in ("src", "href"):
        pattern = rf'{attr}=["\']([^"\']+\.(?:js|css)(?:\?[^"\']*)?)["\']'
        for match in re.finditer(pattern, html, re.IGNORECASE):
            urls.append(urljoin(base_url, match.group(1)))
    return list(dict.fromkeys(urls))


def _upload_snippets(text: str) -> list[str]:
    hits: list[str] = []
    patterns = [
        r'["\']([^"\']*(?:upload|uploads|attachment|resource|files)[^"\']*)["\']',
        r'\bapi/[A-Za-z0-9_./?=&:-]*(?:upload|uploads|attachment|resource|files)[A-Za-z0-9_./?=&:-]*',
    ]
    for pat in patterns:
        for match in re.finditer(pat, text, re.IGNORECASE):
            val = match.group(1) if match.lastindex else match.group(0)
            if len(val) <= 180 and not val.startswith(("data:", "blob:")):
                hits.append(val)
    return list(dict.fromkeys(hits))


def scan_frontend(session: requests.Session, course_id: str) -> None:
    page_url = f"{BASE_URL}/course/{course_id}/homework"
    resp = session.get(page_url, timeout=30)
    print(f"PAGE {resp.status_code} {page_url}")
    if resp.status_code != 200:
        print(resp.text[:300])
        return

    assets = _find_asset_urls(resp.text, resp.url)
    print(f"ASSETS {len(assets)}")

    all_hits = _upload_snippets(resp.text)
    for url in assets:
        try:
            r = session.get(url, timeout=30)
        except requests.RequestException as exc:
            print(f"ASSET ERR {url} {exc}")
            continue
        if r.status_code == 200:
            hits = _upload_snippets(r.text)
            if hits:
                print(f"\n# {url}")
                for hit in hits[:80]:
                    print(hit)
            all_hits.extend(hits)

    merged = list(dict.fromkeys(all_hits))
    print(f"\nTOTAL_UPLOAD_RELATED_HITS {len(merged)}")
    for hit in merged[:200]:
        print(hit)


def grep_frontend(session: requests.Session, course_id: str, pattern: str) -> None:
    page_url = f"{BASE_URL}/course/{course_id}/homework"
    resp = session.get(page_url, timeout=30)
    print(f"PAGE {resp.status_code} {page_url}")
    if resp.status_code != 200:
        print(resp.text[:300])
        return

    assets = [page_url, *_find_asset_urls(resp.text, resp.url)]
    needle = re.compile(pattern, re.IGNORECASE)
    for url in assets:
        text = resp.text if url == page_url else None
        if text is None:
            try:
                r = session.get(url, timeout=30)
            except requests.RequestException as exc:
                print(f"ASSET ERR {url} {exc}")
                continue
            if r.status_code != 200:
                continue
            text = r.text
        matches = list(needle.finditer(text))
        if not matches:
            continue
        print(f"\n# {url} matches={len(matches)}")
        for match in matches[:20]:
            start = max(0, match.start() - 260)
            end = min(len(text), match.end() + 360)
            snippet = text[start:end].replace("\n", "\\n")
            print(snippet)
            print("---")


def urls_frontend(session: requests.Session, course_id: str, pattern: str) -> None:
    page_url = f"{BASE_URL}/course/{course_id}/homework"
    resp = session.get(page_url, timeout=30)
    print(f"PAGE {resp.status_code} {page_url}")
    if resp.status_code != 200:
        print(resp.text[:300])
        return
    assets = [page_url, *_find_asset_urls(resp.text, resp.url)]
    needle = re.compile(pattern, re.IGNORECASE)
    urls: list[tuple[str, str]] = []
    url_pat = re.compile(r'["\'](/?api/[^"\']{0,220})["\']')
    concat_pat = re.compile(r'["\'](/?api/[^"\']{0,160})["\']\.concat\(([^)]{0,160})\)')
    for url in assets:
        text = resp.text if url == page_url else None
        if text is None:
            try:
                r = session.get(url, timeout=30)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                continue
            text = r.text
        for pat in (url_pat, concat_pat):
            for match in pat.finditer(text):
                val = match.group(0)
                if needle.search(val):
                    urls.append((url, val))
    seen: set[str] = set()
    for source, val in urls:
        if val in seen:
            continue
        seen.add(val)
        print(f"{source}\n  {val}")


def _extract_upload_obj(data):
    if isinstance(data, dict):
        for key in ("upload", "data", "resource", "attachment", "file"):
            obj = data.get(key)
            if isinstance(obj, dict) and (obj.get("id") is not None or obj.get("reference_id") is not None):
                return obj
        if data.get("id") is not None or data.get("reference_id") is not None:
            return data
    return None


def post_probe(session: requests.Session, file_path: str | None) -> int:
    cleanup = False
    if file_path is None:
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
        tmp.write("upload api probe\n")
        tmp.close()
        file_path = tmp.name
        cleanup = True

    path = Path(file_path).resolve()
    print(f"PROBE_FILE {path.name} {path.stat().st_size} bytes")

    try:
        for ep in UPLOAD_CANDIDATES:
            for field in FILE_FIELDS:
                url = f"{BASE_URL}/{ep}"
                with path.open("rb") as fh:
                    resp = session.post(
                        url,
                        files={field: (path.name, fh)},
                        headers=_write_headers(session),
                        timeout=120,
                    )
                ctype = resp.headers.get("Content-Type", "")
                print(f"POST {resp.status_code} field={field} {url} {ctype}")
                if resp.status_code not in (200, 201, 404):
                    print(resp.text[:500])
                if resp.status_code in (401, 403):
                    print(resp.text[:300])
                    return 2
                if resp.status_code in (200, 201):
                    try:
                        data = resp.json()
                    except ValueError:
                        print(resp.text[:300])
                        continue
                    obj = _extract_upload_obj(data)
                    print(f"JSON_KEYS {list(data)[:20] if isinstance(data, dict) else type(data)}")
                    print(str(data)[:500])
                    if obj is not None:
                        print(f"FOUND endpoint={ep} field={field} id={obj.get('id')} reference_id={obj.get('reference_id')}")
                        return 0
    finally:
        if cleanup:
            try:
                os.unlink(file_path)
            except OSError:
                pass
    return 1


def preupload_probe(session: requests.Session, file_path: str | None) -> int:
    cleanup = False
    if file_path is None:
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
        tmp.write("upload api probe\n")
        tmp.close()
        file_path = tmp.name
        cleanup = True

    path = Path(file_path).resolve()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    ext = path.suffix[1:].lower() or "txt"
    payloads = [
        {"name": path.name, "size": path.stat().st_size, "type": ext},
        {"name": path.name, "size": path.stat().st_size, "type": mime},
        {"name": path.name, "filename": path.name, "size": path.stat().st_size, "type": ext},
    ]

    try:
        for payload in payloads:
            resp = session.post(
                f"{BASE_URL}/api/uploads",
                json=payload,
                headers=_write_headers(session),
                timeout=60,
            )
            print(f"PREUPLOAD {resp.status_code} payload={payload}")
            print(resp.text[:800])
            if resp.status_code not in (200, 201):
                continue
            try:
                data = resp.json()
            except ValueError:
                continue
            obj = _extract_upload_obj(data)
            if obj is None and isinstance(data, dict):
                obj = data
            if not isinstance(obj, dict):
                continue
            upload_url = obj.get("upload_url")
            if not upload_url:
                print("FOUND_PREUPLOAD_WITHOUT_UPLOAD_URL")
                return 0
            put_url = upload_url if str(upload_url).startswith("http") else urljoin(BASE_URL, str(upload_url))
            with path.open("rb") as fh:
                put = session.put(
                    put_url,
                    files={"file": (path.name, fh, mime)},
                    timeout=120,
                )
            print(f"PUT {put.status_code} {upload_url}")
            print(put.text[:800])
            if put.status_code in (200, 201, 204):
                print(f"FOUND_PREUPLOAD id={obj.get('id')} upload_url={upload_url}")
                return 0
    finally:
        if cleanup:
            try:
                os.unlink(file_path)
            except OSError:
                pass
    return 1


def crawler_upload_probe(session: requests.Session, file_path: str | None) -> int:
    cleanup = False
    if file_path is None:
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
        tmp.write("crawler upload_file probe\n")
        tmp.close()
        file_path = tmp.name
        cleanup = True
    try:
        obj = upload_file(session, file_path)
        print(f"CRAWLER_UPLOAD_OK id={obj.get('id')} name={obj.get('name')} status={obj.get('status')}")
        print(str(obj)[:800])
        return 0
    finally:
        if cleanup:
            try:
                os.unlink(file_path)
            except OSError:
                pass


def submit_endpoint_probe(session: requests.Session, homework_id: str) -> int:
    paths = [
        f"api/course/activities/{homework_id}/submissions",
        f"api/course/activities/{homework_id}/submission",
        f"api/course/activities/{homework_id}/submit",
        f"api/activities/{homework_id}/submissions",
        f"api/homeworks/{homework_id}/submissions",
        f"api/homework/{homework_id}/submissions",
        f"api/submissions/{homework_id}/",
    ]
    for path in paths:
        url = f"{BASE_URL}/{path}"
        for method in ("OPTIONS", "GET"):
            resp = session.request(
                method,
                url,
                headers=_write_headers(session),
                timeout=30,
                allow_redirects=False,
            )
            allow = resp.headers.get("Allow", "")
            print(f"{method} {resp.status_code} {url} allow={allow} ctype={resp.headers.get('Content-Type','')}")
            if resp.status_code not in (404, 405):
                print(resp.text[:300])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--post", action="store_true", help="POST a tiny file to candidate upload endpoints")
    parser.add_argument("--preupload", action="store_true", help="Probe the two-step /api/uploads preupload flow")
    parser.add_argument("--crawler-upload", action="store_true", help="Call crawler.upload_file with a tiny temp file")
    parser.add_argument("--grep", help="Search authenticated frontend assets for a regex pattern")
    parser.add_argument("--urls", help="List frontend api URL literals matching a regex")
    parser.add_argument("--probe-submit", help="Probe homework submit endpoint existence by homework/activity id")
    parser.add_argument("--file", help="file to upload when using --post")
    args = parser.parse_args()

    load_dotenv()
    username = os.getenv("HZCU_USERNAME")
    password = os.getenv("HZCU_PASSWORD")
    course_id = os.getenv("COURSE_ID", "53472")
    if not username or not password:
        print("missing HZCU_USERNAME / HZCU_PASSWORD in .env")
        return 2

    session = login(username, password)
    if args.urls:
        urls_frontend(session, course_id, args.urls)
        return 0
    if args.probe_submit:
        return submit_endpoint_probe(session, args.probe_submit)
    if args.grep:
        grep_frontend(session, course_id, args.grep)
        return 0
    if args.crawler_upload:
        return crawler_upload_probe(session, args.file)
    if args.preupload:
        return preupload_probe(session, args.file)
    if args.post:
        return post_probe(session, args.file)
    scan_frontend(session, course_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
