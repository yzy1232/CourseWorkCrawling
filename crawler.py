"""TronClass 作业与附件抓取。

平台是 TronClass（畅课）。已确认以下 API 需登录态访问（未登录返回 401）：
  GET /api/courses/{course_id}/homework-activities
  GET /api/course/{course_id}/homework-list

作业详情里包含 uploads / resources / reference 等附件结构，
附件实际下载走 /api/uploads/{id}/blob 或 download_url。

由于不同 TronClass 版本字段略有差异，这里对列表/详情/附件都做多路兼容解析。
"""

from __future__ import annotations

from typing import Any, Iterable

import requests

from auth import BASE_URL

_JSON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    # TronClass 的 /api/* 接口要求带此头，否则返回 403 FORBIDDEN
    "X-Requested-With": "XMLHttpRequest",
    # 接口有 Referer/CSRF 守卫，必须带同源 Referer，否则 403 "您没有权限"
    "Referer": f"{BASE_URL}/user/index",
}


class ApiError(RuntimeError):
    pass


def _get_json(session: requests.Session, path: str, **params) -> Any:
    url = path if path.startswith("http") else f"{BASE_URL}/{path.lstrip('/')}"
    resp = session.get(url, params=params or None, headers=_JSON_HEADERS, timeout=30)
    if resp.status_code in (401, 403):
        raise ApiError(
            f"未认证 ({resp.status_code})：{url} —— 登录态无效或缺少必要请求头"
        )
    # 404 视为“此端点不存在”，让多路回退能继续尝试下一个候选，而不是直接崩溃
    if resp.status_code == 404:
        raise ApiError(f"端点不存在 (404)：{url}")
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError as exc:  # 返回了 HTML 等非 JSON
        raise ApiError(f"响应不是 JSON：{url}") from exc


def verify_logged_in(session: requests.Session) -> dict | None:
    """校验登录态并返回当前用户信息（含内部数字 id）。

    经实测：/api/user 对学生账号返回 403（管理向端点），真正的“当前用户”
    端点是 /api/profile，返回 {id, user_no, name, ...}，其中 id 是内部数字 id
    （如 20310），后续取“我的提交”必须用它，而不是登录学号。
    """
    # 首选 /api/profile —— SPA 实际使用的“我”端点
    try:
        data = _get_json(session, "api/profile")
        if isinstance(data, dict) and data.get("id") is not None:
            return data
    except ApiError:
        pass

    # 退路：直接探测会话是否还活着（被重定向到 CAS = 未登录）
    url = f"{BASE_URL}/api/profile"
    resp = session.get(url, headers=_JSON_HEADERS, timeout=30, allow_redirects=False)
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("Location", "")
        if "cas" in loc or "login" in loc or "identity" in loc:
            return None
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError:
            pass
    return None


def get_current_user_id(session: requests.Session) -> Any:
    """取当前登录用户的内部数字 id（用于拼“我的提交”端点）。"""
    try:
        data = _get_json(session, "api/profile")
    except ApiError:
        return None
    if isinstance(data, dict):
        return data.get("id")
    return None


def _iter_homework_pages(
    session: requests.Session, course_id: str
) -> Iterable[dict]:
    """逐页拉取作业列表，自动翻页直到取完。

    关键点：TronClass 接口的页大小参数是下划线写法 page_size，写成 pageSize 会被
    忽略并退回服务端默认页大小（约 10）。因此这里把几种常见写法都带上，
    并且**按内容判断翻页是否结束**——某页没有新增条目就停，不再依赖
    “本页数量是否小于请求页大小”这种会被默认页大小骗到的判断。
    """
    candidates = [
        "api/courses/{cid}/homework-activities",
        "api/course/{cid}/homework-list",
        "api/courses/{cid}/activities",
    ]
    page_size = 100
    max_pages = 200  # 兜底，避免服务端忽略翻页参数时无限循环
    last_err: Exception | None = None
    for tmpl in candidates:
        path = tmpl.format(cid=course_id)
        seen: set[Any] = set()
        emitted = 0
        try:
            for page in range(1, max_pages + 1):
                data = _get_json(
                    session,
                    path,
                    page=page,
                    page_size=page_size,
                    pageSize=page_size,
                    pageIndex=page,
                )
                items = _extract_list(data)
                if not items:
                    break
                new_in_page = 0
                for it in items:
                    key = _item_key(it)
                    if key is not None and key in seen:
                        continue
                    if key is not None:
                        seen.add(key)
                    new_in_page += 1
                    emitted += 1
                    yield it
                # 本页没有任何新条目 = 服务端在重复返回同一页，停止翻页
                if new_in_page == 0:
                    break
                # 已知总数且已取够，停止
                total = _extract_total(data)
                if total is not None and emitted >= total:
                    break
            if emitted:
                return
        except ApiError as exc:
            last_err = exc
            continue
    if last_err:
        raise last_err


def _item_key(it: dict) -> Any:
    return it.get("id") or it.get("activity_id") or it.get("homework_id")


def _extract_list(data: Any) -> list[dict]:
    """从各种响应包裹结构里取出作业数组。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("homework_activities", "activities", "homeworks", "data", "list", "items", "results"):
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):  # data.list 再嵌一层
                inner = _extract_list(val)
                if inner:
                    return inner
    return []


def _extract_total(data: Any) -> int | None:
    if isinstance(data, dict):
        for key in ("total", "totalCount", "count", "totalElements"):
            v = data.get(key)
            if isinstance(v, int):
                return v
    return None


def list_homeworks(session: requests.Session, course_id: str) -> list[dict]:
    """返回作业列表，每项规范化为 {id, title, raw}。"""
    homeworks: list[dict] = []
    seen: set[Any] = set()
    for it in _iter_homework_pages(session, course_id):
        hid = it.get("id") or it.get("activity_id") or it.get("homework_id")
        if hid is None or hid in seen:
            continue
        seen.add(hid)
        title = (
            it.get("title")
            or it.get("name")
            or it.get("activity_name")
            or f"homework_{hid}"
        )
        homeworks.append({"id": hid, "title": str(title).strip(), "raw": it})
    return homeworks


def get_my_submission(
    session: requests.Session, homework_id: Any, user_id: Any = None
) -> dict:
    """拉取当前用户对某作业的提交（若已提交）。

    实测真实端点为：
        GET /api/activities/{activity_id}/students/{user_id}/submission_list
    返回 {"list":[{..., "uploads":[{id,name,size,allow_download}]}]}，
    其中 user_id 必须是内部数字 id（如 20310），不是登录学号。

    取不到提交不算致命错误，返回空 dict。
    """
    if user_id is None:
        user_id = get_current_user_id(session)

    candidates: list[str] = []
    if user_id is not None:
        candidates += [
            f"api/activities/{homework_id}/students/{user_id}/submission_list",
            f"api/homework/{homework_id}/students/{user_id}/submission",
        ]
    # 历史兜底端点
    candidates += [
        f"api/course/homework-activities/{homework_id}/submissions",
        f"api/homework-activities/{homework_id}/submissions",
        f"api/homework-activities/{homework_id}/my-submission",
    ]

    for path in candidates:
        try:
            data = _get_json(session, path)
        except ApiError:
            continue
        # {"list":[{...}]} —— 取最新一条（is_latest_version 或第一条）
        if isinstance(data, dict):
            lst = data.get("list")
            if isinstance(lst, list) and lst:
                latest = next(
                    (x for x in lst if isinstance(x, dict) and x.get("is_latest_version")),
                    None,
                )
                return latest or lst[0]
            inner = _extract_list(data)
            if inner:
                return inner[0]
            if any(
                k in data
                for k in ("uploads", "submission_uploads", "answers", "submitted_at")
            ):
                return data
        elif isinstance(data, list) and data:
            items = [x for x in data if isinstance(x, dict)]
            if items:
                return items[0]
    return {}


def get_homework_detail(session: requests.Session, homework_id: Any) -> dict:
    """拉取单个作业详情（题目附件在返回的 uploads[] 里）。

    实测真实端点为 /api/activities/{id}?sub_course_id=0，返回的顶层 uploads
    即题目附件。保留其它历史端点作为兜底。
    """
    candidates = [
        (f"api/activities/{homework_id}", {"sub_course_id": 0}),
        (f"api/homework-activities/{homework_id}", {}),
        (f"api/homework/{homework_id}", {}),
    ]
    last_err: Exception | None = None
    for path, params in candidates:
        try:
            data = _get_json(session, path, **params)
            if isinstance(data, dict) and data:
                return data
        except ApiError as exc:
            last_err = exc
            continue
    if last_err:
        raise last_err
    return {}
