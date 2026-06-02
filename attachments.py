"""从作业详情 JSON 中提取附件，并构造下载链接。

TronClass 附件常见形态：
  - uploads: [{id, name, size, reference_id, ...}]
  - resources / reference_resources / attachments
  - 单个文件对象里带 download_url / url / blob 链接

下载地址优先用对象自带的 download_url；否则回退到 /api/uploads/{id}/blob。
"""

from __future__ import annotations

from typing import Any

from auth import BASE_URL

# 详情里可能承载附件的字段名
_ATTACHMENT_KEYS = (
    "uploads",
    "attachments",
    "resources",
    "reference_resources",
    "reference",
    "files",
    "upload_files",
    "homework_uploads",
    "submission_uploads",
    "submit_uploads",
    "answer_uploads",
)


def _looks_like_attachment(obj: dict) -> bool:
    """判断一个 dict 是否是附件对象（有 id/name，且像文件）。"""
    if not isinstance(obj, dict):
        return False
    has_name = any(k in obj for k in ("name", "file_name", "filename", "title"))
    has_ref = any(
        k in obj for k in ("id", "upload_id", "reference_id", "resource_id", "download_url", "url")
    )
    return has_name and has_ref


def _attachment_id(obj: dict) -> Any:
    for k in ("id", "upload_id", "reference_id", "resource_id"):
        if obj.get(k) is not None:
            return obj[k]
    return None


def _attachment_name(obj: dict, fallback_id: Any) -> str:
    for k in ("name", "file_name", "filename", "title"):
        v = obj.get(k)
        if v:
            return str(v)
    return f"file_{fallback_id}"


def _build_download_url(obj: dict) -> str | None:
    """优先用对象自带链接，否则用 uploads blob 端点。"""
    for k in ("download_url", "url", "blob_url", "preview_url"):
        v = obj.get(k)
        if v and isinstance(v, str):
            return v if v.startswith("http") else f"{BASE_URL}/{v.lstrip('/')}"
    upload_id = obj.get("id") or obj.get("upload_id") or obj.get("reference_id")
    if upload_id is not None:
        return f"{BASE_URL}/api/uploads/{upload_id}/blob"
    return None


def extract_attachments(detail: dict) -> list[dict]:
    """递归遍历详情 JSON，收集所有附件。

    返回 [{id, name, url, size, source_key}]，按 id 去重。
    """
    found: list[dict] = []
    seen: set[Any] = set()

    def add(obj: dict, source_key: str) -> None:
        att_id = _attachment_id(obj)
        url = _build_download_url(obj)
        if url is None:
            return
        dedup_key = att_id if att_id is not None else url
        if dedup_key in seen:
            return
        seen.add(dedup_key)
        found.append(
            {
                "id": att_id,
                "name": _attachment_name(obj, att_id),
                "url": url,
                "size": obj.get("size") or obj.get("file_size"),
                "source_key": source_key,
            }
        )

    def walk(node: Any, key_hint: str) -> None:
        if isinstance(node, dict):
            # 命中已知附件字段：其下数组里的元素当附件处理
            for k in _ATTACHMENT_KEYS:
                v = node.get(k)
                if isinstance(v, list):
                    for el in v:
                        if isinstance(el, dict):
                            add(el, k)
                        walk(el, k)
                elif isinstance(v, dict):
                    if _looks_like_attachment(v):
                        add(v, k)
                    walk(v, k)
            # 节点本身就像附件
            if key_hint in _ATTACHMENT_KEYS and _looks_like_attachment(node):
                add(node, key_hint)
            # 继续向其它子节点递归
            for k, v in node.items():
                if k in _ATTACHMENT_KEYS:
                    continue
                walk(v, k)
        elif isinstance(node, list):
            for el in node:
                walk(el, key_hint)

    walk(detail, "")
    return found
