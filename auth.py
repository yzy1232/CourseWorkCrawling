"""统一身份认证 (CAS) 登录模块。

登录链路：
  courses.hzcu.edu.cn/login
    -> ca.hzcu.edu.cn/cas/login        (CAS 登录页, 提交账号密码)
    -> identity.hzcu.edu.cn (Keycloak)  (broker 回调)
    -> courses.hzcu.edu.cn              (拿到 TronClass 会话 Cookie)

密码在前端用 AES-ECB/Pkcs7 加密后再提交，密钥写死在登录页 JS 里：
    var aseKey = CryptoJS.enc.Utf8.parse("c6dda3852e2d4be2");
    CryptoJS.AES.encrypt(..., aseKey, {mode: ECB, padding: Pkcs7})
"""

from __future__ import annotations

import base64
import re

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# 与登录页 JS 中一致的固定 AES 密钥
_AES_KEY = b"c6dda3852e2d4be2"

# 课程平台首页 / 登录入口
BASE_URL = "https://courses.hzcu.edu.cn"
LOGIN_ENTRY = f"{BASE_URL}/login?next=/user/index"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class LoginError(RuntimeError):
    """登录过程中的可预期错误（账号密码错误、token 缺失等）。"""


def encrypt_password(password: str) -> str:
    """复刻登录页的 AES-ECB/Pkcs7 加密，返回 base64 字符串。"""
    cipher = AES.new(_AES_KEY, AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(password.encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode("ascii")


def _extract_hidden(html: str, name: str) -> str | None:
    """从 CAS 登录页 HTML 中取出隐藏域的值（lt / execution 等）。"""
    pattern = (
        rf'<input[^>]*name=["\']{re.escape(name)}["\'][^>]*value=["\']([^"\']*)["\']'
    )
    m = re.search(pattern, html, re.IGNORECASE)
    if m:
        return m.group(1)
    # 兼容 value 在 name 之前的写法
    pattern2 = (
        rf'<input[^>]*value=["\']([^"\']*)["\'][^>]*name=["\']{re.escape(name)}["\']'
    )
    m = re.search(pattern2, html, re.IGNORECASE)
    return m.group(1) if m else None


def _find_cas_form_action(html: str) -> str | None:
    """取出 CAS 登录表单 fm1 的 action（带 jsessionid 与 service 参数）。"""
    m = re.search(
        r'<form[^>]*id=["\']fm1["\'][^>]*action=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # 退而求其次：第一个 method=post 的表单
    m = re.search(
        r'<form[^>]*action=["\']([^"\']+)["\'][^>]*method=["\']post["\']',
        html,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def login(username: str, password: str, timeout: int = 30) -> requests.Session:
    """执行完整登录流程，返回已携带会话 Cookie 的 requests.Session。

    成功的判据：最终能以认证态访问 TronClass API（由调用方校验），
    本函数仅负责走完 CAS 重定向链并在表单层面检测明显的失败。
    """
    session = requests.Session()
    session.headers.update({"User-Agent": _UA})

    # 1) 访问课程平台登录入口，被 302 引导到 CAS 登录页
    resp = session.get(LOGIN_ENTRY, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    login_html = resp.text
    cas_url = resp.url  # 最终落在 ca.hzcu.edu.cn/cas/login?...

    if "statichtml" in login_html and "fm1" not in login_html:
        # 极少数情况下首页是静态壳，这里直接报错让用户知道
        raise LoginError("未取到 CAS 登录页，请检查网络或 baseUrl 是否可达")

    # 2) 解析隐藏 token
    lt = _extract_hidden(login_html, "lt") or ""
    execution = _extract_hidden(login_html, "execution")
    event_id = _extract_hidden(login_html, "_eventId") or "submit"
    valid_time = _extract_hidden(login_html, "validTime") or "5"

    if execution is None:
        raise LoginError("CAS 登录页缺少 execution 字段，登录页结构可能已变更")

    action = _find_cas_form_action(login_html)
    if not action:
        raise LoginError("未找到 CAS 登录表单 action")
    if action.startswith("/"):
        # 用 cas_url 的协议+域名补全
        m = re.match(r"^(https?://[^/]+)", cas_url)
        action = (m.group(1) if m else "http://ca.hzcu.edu.cn") + action

    # 3) 组装并提交账号登录表单 (authType=0 账号登录)
    form = {
        "username": username,
        "password": encrypt_password(password),
        "authType": "0",
        "lt": lt,
        "execution": execution,
        "_eventId": event_id,
        "validTime": valid_time,
    }
    post = session.post(
        action,
        data=form,
        timeout=timeout,
        allow_redirects=True,
        headers={
            "Referer": cas_url,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    post.raise_for_status()

    # 4) 检测明显的失败信号
    body = post.text
    fail_markers = [
        "用户名或密码错误",
        "密码错误",
        "账号或密码",
        "Invalid credentials",
        "认证信息不正确",
    ]
    for marker in fail_markers:
        if marker in body:
            raise LoginError(f"登录失败：{marker}")

    # 如果最终还停在 CAS 域名且页面仍是登录表单，说明没成功
    if "ca.hzcu.edu.cn" in post.url and 'id="fm1"' in body:
        raise LoginError("登录失败：仍停留在 CAS 登录页（账号密码可能有误或需验证码）")

    return session
