"""核心抓取/下载流程，供 CLI 与 GUI 共用。

对外只暴露 run_download()，通过回调上报进度，避免与具体界面耦合。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterator

import requests

import cache
from attachments import extract_attachments
from auth import login
from crawler import (
    ApiError,
    SubmitError,
    get_courseware_detail,
    get_current_user_id,
    get_homework_detail,
    get_my_submission,
    get_upload_meta,
    get_upload_pdf_url,
    homework_status,
    list_courses,
    list_coursewares,
    list_homeworks,
    list_unfinished_homeworks,
    submit_homework,
    upload_file,
    verify_logged_in,
)
from download_manager import DownloadManager, DownloadTask
from downloader import download_file, sanitize, write_manifest

# 进度回调：(message: str) -> None
Logger = Callable[[str], None]
# 进度比例回调：(done: int, total: int) -> None
Progress = Callable[[int, int], None]


def _noop(*_a, **_k) -> None:
    pass


def _merge_unique(*lists: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[Any] = set()
    for lst in lists:
        for a in lst:
            key = a.get("id") if a.get("id") is not None else a.get("url")
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
    return out


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n[ \t]*")


def _html_to_text(html: str) -> str:
    """把作业正文里的 HTML 粗略转成纯文本（去标签、合并空行）。"""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = _TAG.sub("", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
    )
    text = _WS.sub("\n", text)
    return text.strip()


def _homework_description(detail: dict, raw: dict) -> str:
    """从作业详情提取题目正文/说明，转为纯文本。

    TronClass 把正文放在 detail["data"]["description"]（HTML），
    因此除顶层外还需深入嵌套的 data 字典查找。
    """
    keys = ("description", "content", "body", "requirement", "detail", "intro")
    # 候选来源：detail 顶层、detail["data"]、raw 顶层、raw["data"]
    sources: list[dict] = []
    for src in (detail, raw):
        if isinstance(src, dict):
            sources.append(src)
            if isinstance(src.get("data"), dict):
                sources.append(src["data"])
    for src in sources:
        for k in keys:
            v = src.get(k)
            if v and isinstance(v, str) and v.strip():
                return _html_to_text(v)
    return ""


def _collect_homework_record(
    session: requests.Session,
    hw: dict,
    idx: int,
    user_id: Any,
    output_root: Path | None,
    download_submissions: bool,
    log: Logger,
) -> dict:
    """抓取单个作业的详情、提交与附件清单（不下载），归一化成一条记录。

    output_root 为 None 时附件不带 dest_dir（仅用于概览/缓存）。
    """
    title = hw["title"]
    log(f"[{idx}] {title} (id={hw['id']})")

    try:
        detail = get_homework_detail(session, hw["id"])
    except ApiError as exc:
        log(f"  取详情失败: {exc}")
        detail = hw["raw"]
    assign_atts = _merge_unique(
        extract_attachments(detail), extract_attachments(hw["raw"])
    )

    submit_atts: list[dict] = []
    sub: dict = {}
    if download_submissions:
        try:
            sub = get_my_submission(session, hw["id"], user_id)
            if sub:
                submit_atts = extract_attachments(sub)
        except ApiError as exc:
            log(f"  取提交失败: {exc}")

    status = homework_status(hw["raw"], sub or None)
    hw_dir = (
        output_root / f"{idx:02d}_{sanitize(title)}" if output_root else None
    )

    def with_dest(atts: list[dict], subdir: str) -> list[dict]:
        dest = str(hw_dir / subdir) if hw_dir else None
        return [
            {
                "name": a["name"],
                "url": a["url"],
                "size": a.get("size"),
                "dest_dir": dest,
                "saved_path": None,
            }
            for a in atts
        ]

    return {
        "id": hw["id"],
        "title": title,
        "status": status,
        "description": _homework_description(detail, hw["raw"]),
        "submission_info": _submission_info(sub) if sub else None,
        "assignment": with_dest(assign_atts, "题目附件"),
        "submission": with_dest(submit_atts, "我的提交"),
    }


def _submission_info(sub: dict) -> dict:
    """从“我的提交”原始 JSON 提取展示用信息。"""
    return {
        "submitted_at": _first(sub, "submitted_at", "submit_time", "created_at"),
        "comment": _first(sub, "comment", "content"),
        "score": _first(sub, "score", "final_score", "grade"),
        "score_status": _first(sub, "score_status", "scoreStatus", "status"),
        "files": [
            a["name"] for a in extract_attachments(sub)
        ],
    }


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def authenticate(username: str, password: str, log: Logger = _noop) -> requests.Session:
    """登录并校验，返回可用会话；失败抛异常。"""
    log(f"正在登录 {username} ...")
    session = login(username, password)
    user = verify_logged_in(session)
    if user is None:
        raise RuntimeError("登录后无法访问受保护接口（账号密码可能有误或需验证码）")
    uname = user.get("name") or user.get("user_name") or username
    # 缓存内部数字 id，取“我的提交”要用
    session.user_id = user.get("id")  # type: ignore[attr-defined]
    log(f"登录成功：{uname}")
    return session


def run_download(
    username: str,
    password: str,
    course_id: str,
    output_dir: str = "downloads",
    *,
    download_submissions: bool = True,
    list_only: bool = False,
    parallel: int = 4,
    selected_homework_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    selected_assignment_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    selected_submission_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    session: requests.Session | None = None,
    log: Logger = _noop,
    progress: Progress = _noop,
) -> dict:
    """执行完整流程，返回结果汇总 dict。

    非 list_only 时通过 DownloadManager 并行下载（支持断点续传）。
    若传入已登录的 session 则复用，否则用账号密码登录。
    """
    prep = prepare_download(
        username, password, course_id, output_dir,
        download_submissions=download_submissions,
        selected_homework_ids=selected_homework_ids,
        selected_assignment_ids=selected_assignment_ids,
        selected_submission_ids=selected_submission_ids,
        session=session, log=log,
    )
    records = prep["records"]
    output_root = prep["output_root"]
    if not records:
        return {"course_id": course_id, "homeworks": [], "output_root": None}

    if list_only:
        for r in records:
            n_a, n_s = len(r["assignment"]), len(r["submission"])
            log(f"· {r['title']}：题目 {n_a} 个，提交 {n_s} 个")
        progress(len(records), len(records))
        return {
            "course_id": course_id,
            "homeworks": records,
            "output_root": None,
        }

    tasks: list[DownloadTask] = prep["tasks"]
    total = len(tasks)
    log(f"开始下载 {total} 个附件（并行 {parallel}）...")

    done_count = [0]

    def on_update(task: DownloadTask) -> None:
        if task.status in ("done", "error", "skipped"):
            done_count[0] += 1
            mark = {"done": "✓", "error": "✗", "skipped": "·"}[task.status]
            log(f"  {mark} {task.name}")
            progress(done_count[0], total)

    mgr = DownloadManager(
        prep["session"], tasks, parallel=parallel, on_update=on_update
    )
    mgr.start()
    mgr.join()

    _backfill_saved_paths(records, tasks)

    output_root.mkdir(parents=True, exist_ok=True)
    manifest = write_manifest(
        output_root, str(course_id), _flatten_for_manifest(records)
    )
    log(f"清单已写入 {manifest}")
    return {
        "course_id": course_id,
        "homeworks": records,
        "output_root": str(output_root),
        "manifest": str(manifest),
    }


def _backfill_saved_paths(records: list[dict], tasks: list[DownloadTask]) -> None:
    """把下载结果（saved_path）按 (dest_dir, name) 回填进 records 的附件项。"""
    by_key = {
        (str(t.dest_dir), t.name): t for t in tasks
    }
    for r in records:
        for group in ("assignment", "submission"):
            for a in r[group]:
                t = by_key.get((str(a.get("dest_dir")), a["name"]))
                if t and t.saved_path:
                    a["saved_path"] = str(t.saved_path)


def _flatten_for_manifest(records: list[dict]) -> list[dict]:
    """把 assignment/submission 合并成 attachments 字段，兼容 write_manifest。"""
    out = []
    for r in records:
        atts = []
        for a in r["assignment"]:
            atts.append({**a, "kind": "assignment"})
        for s in r["submission"]:
            atts.append({**s, "kind": "submission"})
        out.append({
            "id": r["id"],
            "title": r["title"],
            "status": r.get("status"),
            "attachments": atts,
        })
    return out


def iter_homework_overview(
    username: str,
    password: str,
    course_id: str,
    *,
    download_submissions: bool = True,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> Iterator[tuple[int, int, dict]]:
    """逐条抓取课程作业概览，yield (当前序号, 总数, 记录)。"""
    if session is None:
        session = authenticate(username, password, log=log)
    user_id = getattr(session, "user_id", None) or get_current_user_id(session)

    log(f"获取课程 {course_id} 的作业列表 ...")
    homeworks = list_homeworks(session, course_id)
    total = len(homeworks)
    log(f"共 {total} 个作业")

    output_root = Path("downloads") / f"course_{course_id}"
    for idx, hw in enumerate(homeworks, 1):
        yield idx, total, _collect_homework_record(
            session, hw, idx, user_id, output_root,
            download_submissions, log,
        )


def get_homework_overview(
    username: str,
    password: str,
    course_id: str,
    *,
    download_submissions: bool = True,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> list[dict]:
    """抓取课程的作业概览（含详情/提交/附件清单，但不下载文件）。

    返回的记录可 JSON 序列化，直接用于「作业」页签展示与本地缓存。
    """
    return [
        record
        for _idx, _total, record in iter_homework_overview(
            username, password, course_id,
            download_submissions=download_submissions,
            session=session, log=log,
        )
    ]


def _collect_courseware_record(
    session: requests.Session,
    cw: dict,
    idx: int,
    output_root: Path | None,
    log: Logger,
) -> dict:
    """抓取单个课件的附件清单（不下载），归一化成一条记录。

    课件附件已随活动列表返回（顶层 uploads），仍取一次详情兜底更全的字段。
    """
    title = cw["title"]
    log(f"[{idx}] {title} (id={cw['id']})")
    try:
        detail = get_courseware_detail(session, cw["id"])
    except ApiError as exc:
        log(f"  取详情失败: {exc}")
        detail = cw["raw"]
    atts = _merge_unique(
        extract_attachments(detail), extract_attachments(cw["raw"])
    )

    cw_dir = (
        output_root / f"{idx:02d}_{sanitize(title)}" if output_root else None
    )
    dest = str(cw_dir) if cw_dir else None
    return {
        "id": cw["id"],
        "title": title,
        "description": _homework_description(detail, cw["raw"]),
        "materials": [
            _resolve_material(session, a, dest, log) for a in atts
        ],
    }


def _resolve_material(
    session: requests.Session, att: dict, dest: str | None, log: Logger
) -> dict:
    """把一个课件附件归一化成 material 记录，并处理「不可下载」兜底。

    课件项带 allow_download 字段：为 False 时原始文件禁止下载，改走
    /api/uploads/{id}/preview 拿转码后的 PDF 地址，文件名换成 .pdf。
    allow_download 缺省视为可下载（多数课件可直接下）。
    """
    name = att["name"]
    url = att["url"]
    size = att.get("size")
    fallback = False
    allow = att.get("allow_download")
    upload_id = att.get("id")

    if allow is False and upload_id is not None:
        pdf_url = get_upload_pdf_url(session, upload_id)
        if pdf_url:
            url = pdf_url
            name = _as_pdf_name(name)
            size = None  # 转码后大小未知
            fallback = True
            log(f"  · {att['name']} 不允许下载，改用 PDF 兜底")
        else:
            log(f"  · {att['name']} 不允许下载，且无 PDF 兜底，跳过")
            url = None

    return {
        "name": name,
        "url": url,
        "size": size,
        "allow_download": allow,
        "pdf_fallback": fallback,
        "dest_dir": dest if url else None,
        "saved_path": None,
    }


def _as_pdf_name(name: str) -> str:
    """把任意文件名换成 .pdf 后缀。"""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem}.pdf"


def iter_courseware_overview(
    username: str,
    password: str,
    course_id: str,
    *,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> Iterator[tuple[int, int, dict]]:
    """逐条抓取课程课件概览，yield (当前序号, 总数, 记录)。"""
    if session is None:
        session = authenticate(username, password, log=log)

    log(f"获取课程 {course_id} 的课件列表 ...")
    coursewares = list_coursewares(session, course_id)
    total = len(coursewares)
    log(f"共 {total} 个课件")

    output_root = Path("downloads") / f"course_{course_id}" / "课件"
    for idx, cw in enumerate(coursewares, 1):
        yield idx, total, _collect_courseware_record(
            session, cw, idx, output_root, log
        )


def get_courseware_overview(
    username: str,
    password: str,
    course_id: str,
    *,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> list[dict]:
    """抓取课程的课件概览（含附件清单，但不下载文件）。

    返回的记录可 JSON 序列化，直接用于「课件」展示与本地缓存。
    """
    return [
        record
        for _idx, _total, record in iter_courseware_overview(
            username, password, course_id, session=session, log=log
        )
    ]


def prepare_download(
    username: str,
    password: str,
    course_id: str,
    output_dir: str = "downloads",
    *,
    download_submissions: bool = True,
    records: list[dict] | None = None,
    selected_homework_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    selected_assignment_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    selected_submission_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> dict:
    """把作业概览展开成待下载的 DownloadTask 列表（未启动）。

    若传入已有的 records（如来自缓存或概览），则复用、避免重复抓取，
    但仍需要一个已登录 session 用于实际下载。
    返回 {records, tasks, output_root, session}。
    """
    if session is None:
        session = authenticate(username, password, log=log)
    output_root = Path(output_dir) / f"course_{course_id}"

    if records is None:
        records = get_homework_overview(
            username, password, course_id,
            download_submissions=download_submissions,
            session=session, log=log,
        )
    records = _filter_homework_selection(
        records, selected_homework_ids, selected_assignment_ids, selected_submission_ids
    )

    tasks: list[DownloadTask] = []
    tid = 0
    for r in records:
        groups = [("assignment", r["assignment"])]
        if download_submissions:
            groups.append(("submission", r["submission"]))
        for kind, atts in groups:
            for a in atts:
                dest = a.get("dest_dir")
                if not dest:
                    continue
                tasks.append(
                    DownloadTask(
                        id=tid, name=a["name"], url=a["url"],
                        dest_dir=Path(dest), hw_title=r["title"], kind=kind,
                        progress_key=str(a.get("progress_key") or "") or None,
                        total_bytes=int(a.get("size") or 0),
                    )
                )
                tid += 1
    return {
        "records": records,
        "tasks": tasks,
        "output_root": output_root,
        "session": session,
    }


def _filter_homework_selection(
    records: list[dict],
    selected_homework_ids: set[str] | list[str] | tuple[str, ...] | None,
    selected_assignment_ids: set[str] | list[str] | tuple[str, ...] | None,
    selected_submission_ids: set[str] | list[str] | tuple[str, ...] | None,
) -> list[dict]:
    """按作业、题目附件或提交附件选择过滤；三者取并集。"""
    if not selected_homework_ids and not selected_assignment_ids and not selected_submission_ids:
        return records
    selected_homeworks = {str(i) for i in selected_homework_ids or ()}
    selected_assignments = {str(i) for i in selected_assignment_ids or ()}
    selected_submissions = {str(i) for i in selected_submission_ids or ()}
    filtered: list[dict] = []
    for record in records:
        if str(record.get("id")) in selected_homeworks:
            filtered.append(record)
            continue
        assignments = []
        for idx, assignment in enumerate(record.get("assignment") or []):
            keys = {
                str(assignment.get("id")),
                str(assignment.get("url")),
                str(assignment.get("name")),
                f"{record.get('id')}:{assignment.get('id') or assignment.get('url') or assignment.get('name') or idx}",
            }
            if keys & selected_assignments:
                assignments.append(assignment)
        submissions = []
        for idx, submission in enumerate(record.get("submission") or []):
            keys = {
                str(submission.get("id")),
                str(submission.get("url")),
                str(submission.get("name")),
                f"{record.get('id')}:{submission.get('id') or submission.get('url') or submission.get('name') or idx}",
            }
            if keys & selected_submissions:
                submissions.append(submission)
        if assignments or submissions:
            filtered.append({**record, "assignment": assignments, "submission": submissions})
    return filtered


def courses_with_cache(
    username: str,
    password: str,
    *,
    refresh: bool = False,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> tuple[list[dict], Any, bool]:
    """带缓存的“我的课程”。返回 (courses, cached_at, stale)。

    refresh=False 且有缓存时直接返回缓存；否则联网抓取并写入缓存。
    """
    if not refresh:
        data, cached_at = cache.load("courses", username)
        if data is not None:
            return data, cached_at, cache.is_stale(cached_at)
    courses = get_courses(username, password, session=session, log=log)
    cache.save("courses", username, courses)
    _data, cached_at = cache.load("courses", username)
    return courses, cached_at, False


def homeworks_with_cache(
    username: str,
    password: str,
    course_id: str,
    *,
    refresh: bool = False,
    download_submissions: bool = True,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> tuple[list[dict], Any, bool]:
    """带缓存的作业概览。返回 (records, cached_at, stale)。"""
    if not refresh:
        data, cached_at = cache.load("homeworks", str(course_id))
        if data is not None:
            return data, cached_at, cache.is_stale(cached_at)
    records = get_homework_overview(
        username, password, course_id,
        download_submissions=download_submissions,
        session=session, log=log,
    )
    cache.save("homeworks", str(course_id), records)
    _data, cached_at = cache.load("homeworks", str(course_id))
    return records, cached_at, False


def coursewares_with_cache(
    username: str,
    password: str,
    course_id: str,
    *,
    refresh: bool = False,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> tuple[list[dict], Any, bool]:
    """带缓存的课件概览。返回 (records, cached_at, stale)。"""
    if not refresh:
        data, cached_at = cache.load("coursewares", str(course_id))
        if data is not None:
            return data, cached_at, cache.is_stale(cached_at)
    records = get_courseware_overview(
        username, password, course_id, session=session, log=log
    )
    cache.save("coursewares", str(course_id), records)
    _data, cached_at = cache.load("coursewares", str(course_id))
    return records, cached_at, False


def prepare_courseware_download(
    username: str,
    password: str,
    course_id: str,
    output_dir: str = "downloads",
    *,
    records: list[dict] | None = None,
    selected_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    selected_material_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> dict:
    """把课件概览展开成待下载的 DownloadTask 列表（未启动）。

    返回 {records, tasks, output_root, session}，结构与 prepare_download 一致，
    可直接交给 DownloadManager。
    """
    if session is None:
        session = authenticate(username, password, log=log)
    output_root = Path(output_dir) / f"course_{course_id}" / "课件"

    if records is None:
        records = get_courseware_overview(
            username, password, course_id, session=session, log=log
        )
    _apply_courseware_dest_dirs(records, output_root)
    records = _filter_courseware_selection(records, selected_ids, selected_material_ids)

    tasks: list[DownloadTask] = []
    tid = 0
    for r in records:
        for a in r["materials"]:
            dest = a.get("dest_dir")
            if not dest:
                continue
            tasks.append(
                DownloadTask(
                    id=tid, name=a["name"], url=a["url"],
                    dest_dir=Path(dest), hw_title=r["title"], kind="material",
                    progress_key=str(a.get("progress_key") or "") or None,
                    total_bytes=int(a.get("size") or 0),
                )
            )
            tid += 1
    return {
        "records": records,
        "tasks": tasks,
        "output_root": output_root,
        "session": session,
    }


def _filter_courseware_selection(
    records: list[dict],
    selected_ids: set[str] | list[str] | tuple[str, ...] | None,
    selected_material_ids: set[str] | list[str] | tuple[str, ...] | None,
) -> list[dict]:
    """按课件或附件选择过滤；课件选择与附件选择取并集。"""
    if not selected_ids and not selected_material_ids:
        return records
    selected_coursewares = {str(i) for i in selected_ids or ()}
    selected_materials = {str(i) for i in selected_material_ids or ()}
    filtered: list[dict] = []
    for record in records:
        if str(record.get("id")) in selected_coursewares:
            filtered.append(record)
            continue
        materials = []
        for idx, material in enumerate(record.get("materials") or []):
            keys = {
                str(material.get("id")),
                str(material.get("url")),
                str(material.get("name")),
                f"{record.get('id')}:{material.get('id') or material.get('url') or material.get('name') or idx}",
            }
            if keys & selected_materials:
                materials.append(material)
        if materials:
            filtered.append({**record, "materials": materials})
    return filtered


def _apply_courseware_dest_dirs(records: list[dict], output_root: Path) -> None:
    """把课件附件目录重写到当前下载根目录，兼容缓存记录复用。"""
    for idx, record in enumerate(records, 1):
        dest = output_root / f"{idx:02d}_{sanitize(record.get('title') or 'courseware')}"
        for material in record.get("materials") or []:
            material["dest_dir"] = str(dest) if material.get("url") else None


def download_coursewares(
    username: str,
    password: str,
    course_id: str,
    output_dir: str = "downloads",
    *,
    list_only: bool = False,
    parallel: int = 4,
    selected_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    selected_material_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    session: requests.Session | None = None,
    log: Logger = _noop,
    progress: Progress = _noop,
) -> dict:
    """抓取并下载课程的全部课件附件，返回结果汇总 dict。"""
    prep = prepare_courseware_download(
        username, password, course_id, output_dir,
        selected_ids=selected_ids,
        selected_material_ids=selected_material_ids,
        session=session,
        log=log,
    )
    records = prep["records"]
    output_root = prep["output_root"]
    if not records:
        return {"course_id": course_id, "coursewares": [], "output_root": None}

    if list_only:
        for r in records:
            log(f"· {r['title']}：{len(r['materials'])} 个附件")
        progress(len(records), len(records))
        return {
            "course_id": course_id,
            "coursewares": records,
            "output_root": None,
        }

    tasks: list[DownloadTask] = prep["tasks"]
    total = len(tasks)
    log(f"开始下载 {total} 个课件附件（并行 {parallel}）...")

    done_count = [0]

    def on_update(task: DownloadTask) -> None:
        if task.status in ("done", "error", "skipped"):
            done_count[0] += 1
            mark = {"done": "✓", "error": "✗", "skipped": "·"}[task.status]
            log(f"  {mark} {task.name}")
            progress(done_count[0], total)

    mgr = DownloadManager(
        prep["session"], tasks, parallel=parallel, on_update=on_update
    )
    mgr.start()
    mgr.join()

    _backfill_courseware_paths(records, tasks)
    return {
        "course_id": course_id,
        "coursewares": records,
        "output_root": str(output_root),
    }


def _backfill_courseware_paths(
    records: list[dict], tasks: list[DownloadTask]
) -> None:
    """把下载结果按 (dest_dir, name) 回填进 records 的 materials 项。"""
    by_key = {(str(t.dest_dir), t.name): t for t in tasks}
    for r in records:
        for a in r["materials"]:
            t = by_key.get((str(a.get("dest_dir")), a["name"]))
            if t and t.saved_path:
                a["saved_path"] = str(t.saved_path)


def get_courses(
    username: str,
    password: str,
    *,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> list[dict]:
    """登录并返回“我的课程”列表 [{id, name, raw}]，用于 id<=>课程名 映射。"""
    if session is None:
        session = authenticate(username, password, log=log)
    user_id = getattr(session, "user_id", None) or get_current_user_id(session)
    courses = list_courses(session, user_id)
    log(f"共 {len(courses)} 门课程")
    return courses


def list_unsubmitted(
    username: str,
    password: str,
    course_id: str,
    *,
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> list[dict]:
    """返回该课程中“未提交”的作业概览 [{id, title, deadline}]。"""
    if session is None:
        session = authenticate(username, password, log=log)
    log(f"获取课程 {course_id} 的未提交作业 ...")
    return [
        {
            "id": item.get("id"),
            "title": item.get("title") or "未命名作业",
            "deadline": item.get("deadline"),
        }
        for item in list_unfinished_homeworks(session, course_id)
    ]


def submit_homework_files(
    username: str,
    password: str,
    homework_id: str,
    files: list[str],
    *,
    comment: str = "",
    session: requests.Session | None = None,
    log: Logger = _noop,
) -> dict:
    """上传文件并提交到指定作业。

    ⚠ 写操作：会真实改变服务器上的提交状态。调用方必须在确认后才调用。
    返回 {homework_id, uploads:[{id,name}], response}。
    """
    if session is None:
        session = authenticate(username, password, log=log)

    uploads: list[dict] = []
    for fp in files:
        log(f"上传 {fp} ...")
        up = upload_file(session, fp)
        uploads.append({"id": up.get("id"), "name": up.get("name")})
        log(f"  ✓ 已上传 (id={up.get('id')})")

    upload_ids = [u["id"] for u in uploads if u["id"] is not None]
    log(f"提交作业 {homework_id}（{len(upload_ids)} 个附件）...")
    resp = submit_homework(session, homework_id, upload_ids, comment=comment)
    log("✓ 提交完成")
    return {"homework_id": homework_id, "uploads": uploads, "response": resp}
