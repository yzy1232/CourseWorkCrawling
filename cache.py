"""本地 JSON 缓存：课程列表 / 作业列表，带抓取时间戳与陈旧判断。

缓存文件位于 .cache/data/{kind}_{key}.json，结构：
    {"cached_at": "2026-06-02T12:00:00", "data": <任意可 JSON 序列化对象>}

kind 约定："courses"（key=用户名）、"homeworks"（key=course_id）、
"unfinished_homeworks"（key=用户名）。
超过 STALE_DAYS 天的缓存视为可能已更新；未完成作业缓存半天过期。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

CACHE_DIR = Path(".cache") / "data"
STALE_DAYS = 3
UNFINISHED_HOMEWORK_STALE_DAYS = 0.5

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]")


def _path(kind: str, key: str) -> Path:
    safe = _SAFE_KEY.sub("_", f"{kind}_{key}")
    return CACHE_DIR / f"{safe}.json"


def save(kind: str, key: str, data: Any) -> None:
    """写入缓存，cached_at 记为当前本地时间。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"cached_at": datetime.now().isoformat(), "data": data}
    _path(kind, key).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load(kind: str, key: str) -> tuple[Any | None, datetime | None]:
    """读出 (data, cached_at)；文件不存在或损坏返回 (None, None)。"""
    p = _path(kind, key)
    if not p.exists():
        return None, None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None, None
    data = payload.get("data")
    cached_at = None
    raw = payload.get("cached_at")
    if isinstance(raw, str):
        try:
            cached_at = datetime.fromisoformat(raw)
        except ValueError:
            cached_at = None
    return data, cached_at


def age_days(cached_at: datetime | None) -> float | None:
    """缓存距今的天数；cached_at 为 None 时返回 None。"""
    if cached_at is None:
        return None
    return (datetime.now() - cached_at).total_seconds() / 86400.0


def is_stale(cached_at: datetime | None, days: float = STALE_DAYS) -> bool:
    """缓存是否已超过 days 天（无时间戳视为陈旧）。"""
    age = age_days(cached_at)
    return age is None or age > days


def is_unfinished_homework_stale(cached_at: datetime | None) -> bool:
    """未完成作业缓存是否超过半天。"""
    return is_stale(cached_at, UNFINISHED_HOMEWORK_STALE_DAYS)
