"""TronClass 作业与附件抓取。

平台是 TronClass（畅课）。已确认以下 API 需登录态访问（未登录返回 401）：
  GET /api/courses/{course_id}/homework-activities
  GET /api/course/{course_id}/homework-list

作业详情里包含 uploads / resources / reference 等附件结构，
附件实际下载走 /api/uploads/{id}/blob 或 download_url。

由于不同 TronClass 版本字段略有差异，这里对列表/详情/附件都做多路兼容解析。
"""

from __future__ import annotations

import os
import mimetypes
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


def _course_name(it: dict) -> str:
    for k in ("name", "course_name", "title"):
        v = it.get(k)
        if v:
            return str(v).strip()
    return f"course_{it.get('id')}"


def list_courses(session: requests.Session, user_id: Any = None) -> list[dict]:
    """拉取“我的课程”，返回 [{id, name, raw}]，用于 id<=>课程名 映射。

    实测 SPA 用 /api/user/{uid}/courses 取我的课程（需内部数字 id）。
    不同版本端点略有差异，这里做多路兼容回退，并自动翻页。
    """
    if user_id is None:
        user_id = get_current_user_id(session)

    candidates: list[str] = []
    if user_id is not None:
        candidates += [
            f"api/user/{user_id}/courses",
            f"api/users/{user_id}/courses",
        ]
    candidates += [
        "api/my-courses",
        "api/courses",
        "api/course/my-courses",
    ]

    max_pages = 100
    last_err: Exception | None = None
    for path in candidates:
        seen: set[Any] = set()
        out: list[dict] = []
        try:
            for page in range(1, max_pages + 1):
                data = _get_json(
                    session,
                    path,
                    page=page,
                    page_size=100,
                    pageSize=100,
                    pageIndex=page,
                )
                items = _extract_course_list(data)
                if not items:
                    break
                new_in_page = 0
                for it in items:
                    cid = it.get("id") or it.get("course_id")
                    if cid is None or cid in seen:
                        continue
                    seen.add(cid)
                    new_in_page += 1
                    out.append(
                        {"id": cid, "name": _course_name(it), "raw": it}
                    )
                if new_in_page == 0:
                    break
                total = _extract_total(data)
                if total is not None and len(out) >= total:
                    break
            if out:
                return out
        except ApiError as exc:
            last_err = exc
            continue
    if last_err:
        raise last_err
    return []


def _extract_course_list(data: Any) -> list[dict]:
    """从各种响应包裹结构里取出课程数组。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("courses", "course_list", "data", "list", "items", "results"):
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):
                inner = _extract_course_list(val)
                if inner:
                    return inner
    return []


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def homework_status(raw: dict, submission: dict | None = None) -> dict:
    """从作业原始 JSON（+可选的“我的提交”）归一化出提交状态。

    返回 {submitted: bool, submit_time, deadline, score, score_status, is_overdue}。
    TronClass 不同版本字段名差异较大，这里把常见写法都覆盖一遍；
    最终是否已交以“拿到提交记录”优先，原始标志位兜底。
    """
    submit_time = _first(
        submission or {}, "submitted_at", "submit_time", "created_at"
    )
    submitted = bool(submit_time) or bool(
        submission and (
            submission.get("uploads")
            or submission.get("id")
            or submission.get("submission_uploads")
        )
    )
    if not submitted:
        flag = _first(
            raw,
            "is_submitted",
            "submitted",
            "has_submitted",
        )
        if isinstance(flag, bool):
            submitted = flag
        status = str(_first(raw, "submit_status", "submitStatus", "status") or "")
        if status and status not in ("not_submitted", "unsubmitted", "0", "none"):
            if any(s in status for s in ("submit", "已交", "done", "completed", "finished")):
                submitted = True

    deadline = _first(
        raw, "end_time", "deadline", "due_date", "close_time", "endTime"
    )
    score = _first(raw, "score", "final_score", "grade")
    if submission and score is None:
        score = _first(submission, "score", "final_score", "grade")

    return {
        "submitted": submitted,
        "submit_time": submit_time,
        "deadline": deadline,
        "score": score,
        "score_status": _first(raw, "score_status", "scoreStatus"),
    }


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
            if "端点不存在 (404)" not in str(exc):
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


# ---------- 课件（课程资料） ----------
#
# 经 Playwright 实测：课件并没有独立列表端点，而是和作业同在
#   GET /api/courses/{course_id}/activities?sub_course_id=0
# 该端点一次性返回全部活动，每项带 type 字段：
#   type == "homework" 为作业，type == "material" 为课件（课程资料）。
# 课件项直接带顶层 uploads:[{id,name,size,allow_download,...}]，
# 下载仍走 /api/uploads/{id}/blob（会 302 到真实地址，requests 自动跟随）。

def _iter_activities(session: requests.Session, course_id: str) -> list[dict]:
    """拉取课程的全部活动（作业 + 课件等），返回原始 dict 列表。"""
    path = f"api/courses/{course_id}/activities"
    data = _get_json(session, path, sub_course_id=0)
    return _extract_list(data)


def list_coursewares(session: requests.Session, course_id: str) -> list[dict]:
    """返回课件列表（type == "material"），每项规范化为 {id, title, raw}。"""
    out: list[dict] = []
    seen: set[Any] = set()
    for it in _iter_activities(session, course_id):
        if it.get("type") != "material":
            continue
        cid = _item_key(it)
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        title = (
            it.get("title")
            or it.get("name")
            or it.get("activity_name")
            or f"courseware_{cid}"
        )
        out.append({"id": cid, "title": str(title).strip(), "raw": it})
    return out


def get_courseware_detail(session: requests.Session, courseware_id: Any) -> dict:
    """拉取单个课件详情。课件与作业共用活动详情端点。"""
    return get_homework_detail(session, courseware_id)


def get_upload_meta(session: requests.Session, upload_id: Any) -> dict:
    """取单个文件的元信息，含 allow_download 等字段。失败返回 {}。"""
    try:
        data = _get_json(session, f"api/uploads/{upload_id}")
        return data if isinstance(data, dict) else {}
    except ApiError:
        return {}


def get_upload_pdf_url(session: requests.Session, upload_id: Any) -> str | None:
    """取文件的 PDF 兜底地址（转码预览）。

    实测 GET /api/uploads/{id}/preview 返回 {extension, name, url}，
    其中 url 指向 download/processed/<key>.pdf，即不可下载课件的兜底路径。
    取不到则返回 None。
    """
    try:
        data = _get_json(session, f"api/uploads/{upload_id}/preview")
    except ApiError:
        return None
    if isinstance(data, dict):
        url = data.get("url")
        if isinstance(url, str) and url:
            return url
    return None


# ---------- 提交作业 ----------

class SubmitError(RuntimeError):
    pass


def _csrf_token(session: requests.Session) -> str | None:
    """TronClass 的写操作要带 CSRF token，取自登录后下发的 Cookie。

    常见 Cookie 名：XSRF-TOKEN（值即 token，请求头用 X-XSRF-TOKEN 回带）。
    """
    for name in ("XSRF-TOKEN", "csrf_token", "CSRF-TOKEN", "X-CSRF-TOKEN"):
        val = session.cookies.get(name)
        if val:
            return val
    return None


def _write_headers(session: requests.Session) -> dict:
    headers = dict(_JSON_HEADERS)
    token = _csrf_token(session)
    if token:
        headers["X-XSRF-TOKEN"] = token
        headers["X-CSRF-TOKEN"] = token
    return headers


def upload_file(session: requests.Session, file_path: str) -> dict:
    """上传单个文件，返回 TronClass 的 upload 对象（含 id/name/size）。

    经 Playwright/前端 JS 实测，TronClass 当前上传是二段式：
      1. POST /api/uploads 发送 {name, size, type}，取得 upload_url 与 id；
      2. PUT upload_url，multipart/form-data 字段名 file，媒体服务返回 file_key；
      3. 尝试 POST /api/upload/callback/{id} 回传 file_key。
    """
    path = os.path.abspath(file_path)
    if not os.path.isfile(path):
        raise SubmitError(f"文件不存在：{path}")
    fname = os.path.basename(path)
    size = os.path.getsize(path)
    ext = os.path.splitext(fname)[1].lstrip(".").lower() or "bin"
    mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"

    pre_url = f"{BASE_URL}/api/uploads"
    payload = {"name": fname, "size": size, "type": ext}
    pre = session.post(pre_url, json=payload, headers=_write_headers(session), timeout=60)
    if pre.status_code == 404:
        raise SubmitError(f"上传端点不存在 (404)：{pre_url}")
    if pre.status_code in (401, 403):
        raise SubmitError(f"上传未授权 ({pre.status_code})：登录态或 CSRF 失效")
    if pre.status_code not in (200, 201):
        raise SubmitError(f"预上传失败 HTTP {pre.status_code}：{pre.text[:200]}")
    try:
        obj = pre.json()
    except ValueError as exc:
        raise SubmitError(f"预上传响应不是 JSON：{pre_url}") from exc
    if not isinstance(obj, dict) or obj.get("id") is None:
        raise SubmitError(f"预上传响应缺少 id：{str(obj)[:200]}")

    upload_url = obj.get("upload_url") or obj.get("url")
    if not upload_url:
        return obj

    try:
        with open(path, "rb") as fh:
            put = session.put(
                upload_url,
                files={"file": (fname, fh, mime)},
                timeout=120,
            )
    except OSError as exc:
        raise SubmitError(f"读取文件失败：{exc}") from exc
    if put.status_code not in (200, 201, 204):
        raise SubmitError(f"上传文件失败 HTTP {put.status_code}：{put.text[:200]}")

    file_key = None
    if put.text:
        try:
            put_data = put.json()
            if isinstance(put_data, dict):
                file_key = put_data.get("file_key")
        except ValueError:
            file_key = None
    if file_key:
        callback_url = f"{BASE_URL}/api/upload/callback/{obj['id']}"
        callback = session.post(
            callback_url,
            json={"file_key": file_key},
            headers=_write_headers(session),
            timeout=60,
        )
        if callback.status_code not in (200, 201, 204, 404):
            raise SubmitError(
                f"上传回调失败 HTTP {callback.status_code}：{callback.text[:200]}"
            )
        if callback.status_code in (200, 201) and callback.text:
            try:
                callback_data = callback.json()
                if isinstance(callback_data, dict):
                    obj.update(callback_data.get("upload") or callback_data.get("data") or callback_data)
            except ValueError:
                pass
    return obj


def submit_homework(
    session: requests.Session,
    homework_id: Any,
    upload_ids: list[Any],
    *,
    comment: str = "",
) -> dict:
    """提交作业：把已上传的附件 id 关联到作业并提交。

    ⚠ 这是一个写操作，会真实改变服务器上的提交状态，不可自动撤销。
    调用方必须在确认后才调用本函数。

    实测端点为 POST /api/course/{?}/homework/{id}/submissions 或
    /api/activities/{id}/submissions，body 携带 upload ids。
    不同版本字段名不一，这里同时带 uploads / upload_ids 两种写法。
    """
    if not upload_ids:
        raise SubmitError("没有可提交的附件")

    payload = {
        "uploads": list(upload_ids),
        "upload_ids": list(upload_ids),
        "comment": comment,
        "is_draft": False,
    }
    candidates = [
        f"api/course/activities/{homework_id}/submissions",
        f"api/activities/{homework_id}/submissions",
        f"api/homework-activities/{homework_id}/submissions",
        f"api/homework/{homework_id}/submissions",
    ]
    last_err: Exception | None = None
    for ep in candidates:
        url = f"{BASE_URL}/{ep}"
        resp = session.post(url, json=payload, headers=_write_headers(session), timeout=60)
        if resp.status_code == 405:
            resp = session.put(url, json=payload, headers=_write_headers(session), timeout=60)
        if resp.status_code == 404:
            last_err = SubmitError(f"提交端点不存在 (404)：{url}")
            continue
        if resp.status_code in (401, 403):
            raise SubmitError(f"提交未授权 ({resp.status_code})：登录态或 CSRF 失效")
        if resp.status_code not in (200, 201):
            last_err = SubmitError(f"提交失败 HTTP {resp.status_code}：{resp.text[:200]}")
            continue
        try:
            return resp.json() if resp.text else {"ok": True}
        except ValueError:
            return {"ok": True}
    if last_err:
        raise last_err
    raise SubmitError("提交失败：所有端点均不可用")
