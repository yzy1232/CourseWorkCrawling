"""下载附件并按作业分文件夹组织。"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import unquote

import requests

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, default: str = "untitled") -> str:
    """清洗成合法且不至于过长的文件/目录名。"""
    name = unquote(str(name)).strip().rstrip(". ")
    name = _INVALID.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = default
    return name[:120]


def _filename_from_headers(resp: requests.Response) -> str | None:
    cd = resp.headers.get("Content-Disposition", "")
    # 优先 RFC 5987 形式：filename*=UTF-8''xxx ，本身就是 percent-encoded 的 UTF-8
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        return unquote(m.group(1))
    # 退回普通 filename="xxx"。HTTP 头按 latin-1 解码，中文名到这里会变成乱码，
    # 需把它按 latin-1 还原成原始字节，再尝试用 utf-8 / gbk 解出真正的文件名。
    m = re.search(r'filename="?([^";]+)"?', cd, re.IGNORECASE)
    if m:
        name = m.group(1)
        try:
            raw = name.encode("latin-1")
        except UnicodeEncodeError:
            return name  # 已经是正常字符串，直接用
        for enc in ("utf-8", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return name  # 都解不出就保持原样
    return None


def download_file(
    session: requests.Session, url: str, dest_dir: Path, fallback_name: str
) -> Path | None:
    """流式下载单个文件到 dest_dir，返回最终路径；失败返回 None。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with session.get(
            url,
            stream=True,
            timeout=60,
            headers={"X-Requested-With": "XMLHttpRequest"},
        ) as resp:
            if resp.status_code != 200:
                print(f"      ✗ HTTP {resp.status_code}  {url}")
                return None
            name = _filename_from_headers(resp) or fallback_name
            path = dest_dir / sanitize(name, fallback_name)
            path = _dedupe_path(path)
            with open(path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
            return path
    except requests.RequestException as exc:
        print(f"      ✗ 下载出错: {exc}")
        return None


# 进度回调： (downloaded_bytes, total_bytes) -> None；total 未知时为 0
ProgressCb = Callable[[int, int], None]


def download_file_resumable(
    session: requests.Session,
    url: str,
    dest_dir: Path,
    fallback_name: str,
    *,
    pause_event: "threading.Event | None" = None,
    cancel_event: "threading.Event | None" = None,
    on_progress: ProgressCb | None = None,
) -> Path | None:
    """带断点续传的流式下载，供下载管理器调用。

    - 文件名确定后，先把数据写入同名的 ``.part`` 临时文件；完整下完才重命名为最终文件。
    - 若 ``.part`` 已存在，则尝试 ``Range: bytes={existing}-`` 续传；服务器返回 206 时
      以 ``ab`` 追加，否则（200/不支持 Range）截断重下。
    - chunk 循环中：``cancel_event`` 置位则保留 ``.part`` 并返回 None（视为暂停/取消，
      进度不丢）；``pause_event`` 提供且被 clear 时阻塞在 ``wait()`` 直到再次 set。
    - 每写一块调用 ``on_progress(downloaded, total)``。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    if cancel_event is not None and cancel_event.is_set():
        return None

    # 第一次请求：拿真实文件名（不带 Range，确保能读到 Content-Disposition）
    try:
        head = session.get(
            url, stream=True, timeout=60,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
    except requests.RequestException as exc:
        print(f"      ✗ 下载出错: {exc}")
        return None
    with head:
        if head.status_code != 200:
            print(f"      ✗ HTTP {head.status_code}  {url}")
            return None
        name = _filename_from_headers(head) or fallback_name
        final_path = dest_dir / sanitize(name, fallback_name)
        part_path = final_path.with_name(final_path.name + ".part")
        try:
            total = int(head.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0

    existing = part_path.stat().st_size if part_path.exists() else 0

    # 第二次请求：能续传就带 Range
    headers = {"X-Requested-With": "XMLHttpRequest"}
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
    try:
        resp = session.get(url, stream=True, timeout=60, headers=headers)
    except requests.RequestException as exc:
        print(f"      ✗ 下载出错: {exc}")
        return None
    with resp:
        if resp.status_code == 206:
            mode = "ab"
            downloaded = existing
            if total == 0:
                # 206 的 Content-Length 是剩余量，补回已有部分得到总大小
                try:
                    total = existing + int(resp.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    total = 0
        elif resp.status_code == 200:
            mode = "wb"            # 不支持 Range，从头重下
            downloaded = 0
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
        else:
            print(f"      ✗ HTTP {resp.status_code}  {url}")
            return None

        if on_progress:
            on_progress(downloaded, total)
        try:
            with open(part_path, mode) as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if cancel_event is not None and cancel_event.is_set():
                        return None          # 暂停/取消：保留 .part
                    if pause_event is not None:
                        pause_event.wait()   # 暂停时阻塞，不中断连接
                    if chunk:
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if on_progress:
                            on_progress(downloaded, total)
        except requests.RequestException as exc:
            print(f"      ✗ 下载出错: {exc}")
            return None

    final_path = _dedupe_path(final_path)
    part_path.replace(final_path)
    return final_path


def _dedupe_path(path: Path) -> Path:
    """若文件已存在则追加 (1)、(2) ... 避免覆盖。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


def write_manifest(output_root: Path, course_id: str, records: list[dict]) -> Path:
    """写出 JSON 清单，记录每个作业及其附件下载结果。"""
    manifest_path = output_root / "manifest.json"
    payload = {
        "course_id": course_id,
        "homework_count": len(records),
        "attachment_count": sum(len(r["attachments"]) for r in records),
        "homeworks": records,
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest_path
