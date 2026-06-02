"""下载附件并按作业分文件夹组织。"""

from __future__ import annotations

import json
import re
from pathlib import Path
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
