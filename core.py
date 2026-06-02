"""核心抓取/下载流程，供 CLI 与 GUI 共用。

对外只暴露 run_download()，通过回调上报进度，避免与具体界面耦合。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import requests

from attachments import extract_attachments
from auth import login
from crawler import (
    ApiError,
    get_current_user_id,
    get_homework_detail,
    get_my_submission,
    list_homeworks,
    verify_logged_in,
)
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
    session: requests.Session | None = None,
    log: Logger = _noop,
    progress: Progress = _noop,
) -> dict:
    """执行完整流程，返回结果汇总 dict。

    若传入已登录的 session 则复用，否则用账号密码登录。
    """
    if session is None:
        session = authenticate(username, password, log=log)

    # 取当前用户内部 id（取“我的提交”要用）；authenticate 已尝试缓存
    user_id = getattr(session, "user_id", None)
    if user_id is None:
        user_id = get_current_user_id(session)

    log(f"获取课程 {course_id} 的作业列表 ...")
    homeworks = list_homeworks(session, course_id)
    log(f"共 {len(homeworks)} 个作业")
    if not homeworks:
        return {"course_id": course_id, "homeworks": [], "output_root": None}

    output_root = Path(output_dir) / f"course_{course_id}"
    records: list[dict] = []
    total = len(homeworks)

    for idx, hw in enumerate(homeworks, 1):
        title = hw["title"]
        log(f"[{idx}/{total}] {title} (id={hw['id']})")
        progress(idx - 1, total)

        # 题目附件
        try:
            detail = get_homework_detail(session, hw["id"])
        except ApiError as exc:
            log(f"  取详情失败: {exc}")
            detail = hw["raw"]
        assign_atts = _merge_unique(
            extract_attachments(detail), extract_attachments(hw["raw"])
        )

        # 我的提交附件
        submit_atts: list[dict] = []
        if download_submissions:
            try:
                sub = get_my_submission(session, hw["id"], user_id)
                if sub:
                    submit_atts = extract_attachments(sub)
            except ApiError as exc:
                log(f"  取提交失败: {exc}")

        hw_dir = output_root / f"{idx:02d}_{sanitize(title)}"
        rec = {"id": hw["id"], "title": title, "assignment": [], "submission": []}

        rec["assignment"] = _download_group(
            session, assign_atts, hw_dir / "题目附件", list_only, log
        )
        if download_submissions:
            rec["submission"] = _download_group(
                session, submit_atts, hw_dir / "我的提交", list_only, log
            )

        n_a, n_s = len(rec["assignment"]), len(rec["submission"])
        if n_a == 0 and n_s == 0:
            log("  · 无关联附件")
        else:
            log(f"  · 题目 {n_a} 个，提交 {n_s} 个")
        records.append(rec)

    progress(total, total)
    output_root.mkdir(parents=True, exist_ok=True)
    manifest = write_manifest(output_root, str(course_id), _flatten_for_manifest(records))
    log(f"清单已写入 {manifest}")
    return {
        "course_id": course_id,
        "homeworks": records,
        "output_root": str(output_root),
        "manifest": str(manifest),
    }


def _download_group(
    session: requests.Session,
    atts: list[dict],
    dest: Path,
    list_only: bool,
    log: Logger,
) -> list[dict]:
    out = []
    for a in atts:
        saved = None
        if not list_only:
            saved = download_file(session, a["url"], dest, a["name"])
            if saved:
                log(f"      ✓ {saved.name}")
        out.append(
            {
                "name": a["name"],
                "url": a["url"],
                "size": a.get("size"),
                "saved_path": str(saved) if saved else None,
            }
        )
    return out


def _flatten_for_manifest(records: list[dict]) -> list[dict]:
    """把 assignment/submission 合并成 attachments 字段，兼容 write_manifest。"""
    out = []
    for r in records:
        atts = []
        for a in r["assignment"]:
            atts.append({**a, "kind": "assignment"})
        for s in r["submission"]:
            atts.append({**s, "kind": "submission"})
        out.append({"id": r["id"], "title": r["title"], "attachments": atts})
    return out
