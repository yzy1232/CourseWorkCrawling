"""登录诊断脚本：把 CAS → Keycloak → TronClass 整条链路打印出来。

用法：
    python diag.py
读取 .env 的账号密码，逐跳打印重定向、状态码、落地 URL 与 Cookie，
最后探测 /api/user，帮助定位 401/403 到底卡在哪一步。

不会下载任何文件，也不会回显密码明文。
"""

from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

import auth

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def _dump_history(resp: requests.Response, label: str) -> None:
    print(f"\n=== {label} ===")
    chain = list(resp.history) + [resp]
    for i, r in enumerate(chain):
        loc = r.headers.get("Location", "")
        print(f"  [{i}] {r.status_code} {r.request.method} {r.url}")
        if loc:
            print(f"       -> Location: {loc}")
    print(f"  最终落地: {resp.url}  (status {resp.status_code})")


def _dump_cookies(session: requests.Session) -> None:
    print("\n=== 当前 Cookie ===")
    if not session.cookies:
        print("  (空)")
    for c in session.cookies:
        print(f"  {c.domain}{c.path}  {c.name}={'<len %d>' % len(c.value or '')}")


def main() -> int:
    load_dotenv()
    username = os.getenv("HZCU_USERNAME")
    password = os.getenv("HZCU_PASSWORD")
    if not username or not password:
        print("✗ 缺少 .env 中的 HZCU_USERNAME / HZCU_PASSWORD")
        return 2

    session = requests.Session()
    session.headers.update({"User-Agent": auth._UA})

    # 1) 入口 -> CAS 登录页
    resp = session.get(auth.LOGIN_ENTRY, timeout=30, allow_redirects=True)
    _dump_history(resp, "Step1 入口 -> CAS 登录页")
    html = resp.text
    cas_url = resp.url

    lt = auth._extract_hidden(html, "lt") or ""
    execution = auth._extract_hidden(html, "execution")
    event_id = auth._extract_hidden(html, "_eventId") or "submit"
    valid_time = auth._extract_hidden(html, "validTime") or "5"
    action = auth._find_cas_form_action(html)
    print("\n=== 解析登录表单 ===")
    print(f"  action  = {action}")
    print(f"  execution = {execution!r}  lt = {lt!r}  _eventId = {event_id!r}")
    if execution is None or not action:
        print("  ✗ 表单字段缺失，登录页结构可能已变")
        return 1
    if action.startswith("/"):
        import re
        m = re.match(r"^(https?://[^/]+)", cas_url)
        action = (m.group(1) if m else "http://ca.hzcu.edu.cn") + action

    # 2) 提交账号密码（密码加密）
    form = {
        "username": username,
        "password": auth.encrypt_password(password),
        "authType": "0",
        "lt": lt,
        "execution": execution,
        "_eventId": event_id,
        "validTime": valid_time,
    }
    post = session.post(
        action, data=form, timeout=30, allow_redirects=True,
        headers={"Referer": cas_url, "Content-Type": "application/x-www-form-urlencoded"},
    )
    _dump_history(post, "Step2 提交登录表单")

    body = post.text
    for marker in ["用户名或密码", "密码错误", "验证码", "captcha", "账号或密码", "认证信息不正确"]:
        if marker in body:
            print(f"  ⚠ 页面包含敏感提示词: {marker}")

    _dump_cookies(session)

    # 3) 探测受保护接口（带 Referer，模拟 SPA 请求）
    course_id = os.getenv("COURSE_ID", "53472")
    probes = [
        "api/user",
        "api/me",
        f"api/courses/{course_id}/homework-activities?page=1&pageSize=5",
        f"api/course/{course_id}/homework-list?pageIndex=1&pageSize=5",
        f"api/courses/{course_id}/activities?page=1&pageSize=5",
    ]
    for path in probes:
        url = f"{auth.BASE_URL}/{path}"
        r = session.get(
            url,
            headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{auth.BASE_URL}/user/index",
            },
            timeout=30, allow_redirects=False,
        )
        print(f"\n=== 探测 {path} ===")
        print(f"  status={r.status_code}  ctype={r.headers.get('Content-Type','')}")
        if r.status_code in (301, 302, 303, 307, 308):
            print(f"  -> 被重定向到: {r.headers.get('Location','')}")
            print("  （被重定向到登录 = 会话未建立；是 401/403 = 会话建立但接口拒绝）")
        else:
            print(f"  body[:400]={r.text[:400]!r}")

    # 4) 拿到一个真实作业 id，逐一探测“详情”和“我的提交”端点
    _probe_homework_endpoints(session, course_id)
    return 0


def _probe_homework_endpoints(session: requests.Session, course_id: str) -> None:
    """先取第一个作业 id，再探测详情/提交端点，打印真实 JSON 结构。"""
    import json

    hdr = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{auth.BASE_URL}/user/index",
    }
    list_url = (
        f"{auth.BASE_URL}/api/courses/{course_id}"
        "/homework-activities?page=1&page_size=5"
    )
    r = session.get(list_url, headers=hdr, timeout=30)
    print(f"\n=== 取作业列表以拿 id ({r.status_code}) ===")
    try:
        data = r.json()
    except ValueError:
        print("  响应非 JSON，无法继续探测详情/提交")
        return
    items = data.get("homework_activities") if isinstance(data, dict) else None
    if not items:
        items = data if isinstance(data, list) else []
    if not items:
        print(f"  顶层键: {list(data)[:12] if isinstance(data, dict) else type(data)}")
        print("  未取到作业项，无法继续")
        return
    hw = items[0]
    hid = hw.get("id") or hw.get("activity_id") or hw.get("homework_id")
    print(f"  首个作业 id={hid}  顶层键={list(hw)[:20]}")

    detail_paths = [
        f"api/homework-activities/{hid}",
        f"api/homework/{hid}",
        f"api/activities/{hid}",
    ]
    submission_paths = [
        f"api/course/homework-activities/{hid}/submissions",
        f"api/homework-activities/{hid}/submissions",
        f"api/homework-activities/{hid}/submission",
        f"api/homework-activities/{hid}/my-submission",
        f"api/homework-activities/{hid}/submission-list",
        f"api/courses/{course_id}/homework-activities/{hid}/submissions",
    ]
    for label, paths in (("详情", detail_paths), ("我的提交", submission_paths)):
        for path in paths:
            url = f"{auth.BASE_URL}/{path}"
            rr = session.get(url, headers=hdr, timeout=30, allow_redirects=False)
            line = f"  [{label}] {rr.status_code}  {path}"
            if rr.status_code == 200:
                try:
                    j = rr.json()
                except ValueError:
                    print(line + "  (非 JSON)")
                    continue
                if isinstance(j, dict):
                    keys = list(j)
                    print(line + f"  顶层键={keys[:25]}")
                    # 把疑似附件字段的内容摘要出来
                    for k in ("uploads", "submission_uploads", "attachments",
                              "resources", "answers", "submit_uploads"):
                        v = j.get(k)
                        if v:
                            sample = v[0] if isinstance(v, list) and v else v
                            print(f"        {k} -> {json.dumps(sample, ensure_ascii=False)[:200]}")
                elif isinstance(j, list):
                    print(line + f"  列表长度={len(j)}")
                    if j and isinstance(j[0], dict):
                        print(f"        元素键={list(j[0])[:25]}")
            else:
                print(line)


if __name__ == "__main__":
    sys.exit(main())
