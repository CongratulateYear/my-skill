#!/usr/bin/env python3
"""Build a tmall session from the local Chromium-family cookie store.

The user logs in once in their real browser; when the session has expired the
agent drives the login form through magpie-browser in that same browser (never a
new browser window, never a stored password). This module then reads the
browser's cookie database, decrypts it with the OS keychain key, and produces the
request headers needed by the tmall bill APIs.

Credentials never leave memory: nothing is written to the skill directory, the
workbook, the state file, or logs. Only non-secret diagnostics are printed.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

LOGIN_CHECK_URL = "https://scm.tmall.com/loginStatus"
TMALL_HOME_URL = "https://tgc.tmall.com/ds/page/supplier/home"
DEFAULT_DOMAIN = "tgc.tmall.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

# Tmall has shipped the XSRF cookie under both names; the live one is XSRF-TOKEN.
XSRF_COOKIE_NAMES = ("XSRF-TOKEN", "X-XSRF-TOKEN")
# Identity cookies, most reliable first; empty values are skipped.
HINT_COOKIE_ORDER = ("tracknick", "lgc", "cookie17", "unb", "sn")

# Browser key -> (support dir relative to ~/Library/Application Support, keychain label)
BROWSERS: dict[str, dict[str, str]] = {
    "chrome": {
        "dir": "Google/Chrome",
        "label": "Chrome Safe Storage",
        "name": "Google Chrome",
    },
    "chrome-beta": {
        "dir": "Google/Chrome Beta",
        "label": "Chrome Safe Storage",
        "name": "Google Chrome Beta",
    },
    "chrome-canary": {
        "dir": "Google/Chrome Canary",
        "label": "Chrome Safe Storage",
        "name": "Google Chrome Canary",
    },
    "edge": {
        "dir": "Microsoft Edge",
        "label": "Microsoft Edge Safe Storage",
        "name": "Microsoft Edge",
    },
    "brave": {
        "dir": "BraveSoftware/Brave-Browser",
        "label": "Brave Safe Storage",
        "name": "Brave",
    },
    "chromium": {
        "dir": "Chromium",
        "label": "Chromium Safe Storage",
        "name": "Chromium",
    },
}


class SessionError(RuntimeError):
    """The local browser session could not be turned into usable headers."""


def _support_root(browser: str) -> Path:
    if browser not in BROWSERS:
        raise SessionError(f"不支持的浏览器：{browser}（可选：{', '.join(BROWSERS)}）")
    return Path.home() / "Library" / "Application Support" / BROWSERS[browser]["dir"]


def cookie_files(browser: str) -> list[Path]:
    root = _support_root(browser)
    if not root.is_dir():
        return []
    found = [path for path in sorted(root.glob("*/Cookies")) if path.is_file()]
    legacy = root / "Cookies"
    if legacy.is_file():
        found.insert(0, legacy)
    return found


def _keychain_key(label: str, attempts: int = 12) -> bytes:
    """Read the browser's cookie encryption password from the macOS keychain.

    `security` intermittently returns 161 (item not found) or blocks for tens of
    seconds while the login keychain is unlocked, so retry with a short timeout.
    """
    flags = ["-l", label]
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(
                ["security", "find-generic-password", *flags, "-w"],
                capture_output=True, text=True, timeout=8,
            )
        except subprocess.TimeoutExpired:
            last_error = "钥匙串响应超时"
            continue
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = f"无法调用 keychain：{exc}"
            continue
        password = result.stdout.strip()
        if result.returncode == 0 and password:
            return password.encode("utf-8")
        last_error = f"security 返回 {result.returncode}"
        time.sleep(min(1.0 + attempt * 0.2, 3.0))
    raise SessionError(
        f"无法从钥匙串读取「{label}」（{last_error}）。请在钥匙串访问中允许 `security` 读取该项，"
        "或改用 --fetch-file 复用 DevTools 复制的请求。"
    )


def _decrypt(encrypted: bytes, key: bytes, host: str) -> str:
    import hashlib

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not encrypted:
        return ""
    if encrypted[:3] in (b"v10", b"v11"):
        derived = hashlib.pbkdf2_hmac("sha1", key, b"saltysalt", 1003, 16)
        decryptor = Cipher(algorithms.AES(derived), modes.CBC(b" " * 16)).decryptor()
        plain = decryptor.update(encrypted[3:]) + decryptor.finalize()
        if plain:
            plain = plain[:-plain[-1]]
        # Chrome >= 24 prefixes the SHA-256 of the host for domain-bound cookies.
        if len(plain) >= 32 and hashlib.sha256(host.encode("utf-8")).digest() == plain[:32]:
            plain = plain[32:]
        return plain.decode("utf-8", "replace")
    if encrypted[:3] == b"v20":
        raise SessionError(
            "该浏览器的 cookie 使用 v20 应用绑定加密（App-Bound Encryption），本工具无法解密；"
            "请改用 --fetch-file 或换用未启用该加密的浏览器配置文件。"
        )
    return encrypted.decode("utf-8", "replace")


def load_cookies(cookies_path: Path, key: bytes) -> list[dict[str, Any]]:
    """Copy the live SQLite store (Chrome keeps it locked) and read every cookie."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copy2(cookies_path, copy)
        for suffix in ("-journal", "-wal"):
            extra = cookies_path.with_name(cookies_path.name + suffix)
            if extra.exists():
                shutil.copy2(extra, copy.with_name(copy.name + suffix))
        connection = sqlite3.connect(str(copy))
        try:
            rows = connection.execute(
                "select host_key, name, encrypted_value, value, expires_utc, "
                "is_httponly, is_secure, path from cookies"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise SessionError(f"读取 cookie 数据库失败：{exc}") from exc
        finally:
            connection.close()
    cookies: list[dict[str, Any]] = []
    for host, name, encrypted, value, expires_utc, httponly, secure, path in rows:
        if not name:
            continue
        try:
            plain = _decrypt(encrypted, key, host) if encrypted else (value or "")
        except SessionError:
            raise
        except Exception:  # a single undecryptable row must not abort the session
            continue
        cookies.append({
            "host": host,
            "name": name,
            "value": plain,
            "path": path or "/",
            "httpOnly": bool(httponly),
            "secure": bool(secure),
            "expiresUtc": expires_utc,
        })
    return cookies


def _domain_matches(host: str, domain: str) -> bool:
    """RFC 6265 domain matching (host-only rows plus parent-domain rows)."""
    if host == domain:
        return True
    if not host.startswith("."):
        return False
    bare = host[1:]
    return domain == bare or domain.endswith("." + bare)


def cookie_header(cookies: list[dict[str, Any]], domain: str = DEFAULT_DOMAIN) -> str:
    """Serialize cookies exactly as the browser would send them to `domain`."""
    applicable = [c for c in cookies if _domain_matches(c["host"], domain)]
    # Most specific domain wins when names collide (.tmall.com vs tgc.tmall.com).
    applicable.sort(key=lambda c: (-len(c["host"].lstrip(".")), c["name"]))
    merged: dict[str, str] = {}
    for cookie in applicable:
        merged.setdefault(cookie["name"], cookie["value"])
    return "; ".join(f"{name}={value}" for name, value in merged.items())


def build_headers(browser: str = "chrome", domain: str = DEFAULT_DOMAIN) -> dict[str, str]:
    """Return the API headers for an already-logged-in local browser session."""
    errors: list[str] = []
    candidates = [browser] if browser != "auto" else list(BROWSERS)
    for name in candidates:
        files = cookie_files(name)
        if not files:
            errors.append(f"{name}: 未找到 cookie 数据库")
            continue
        try:
            key = _keychain_key(BROWSERS[name]["label"])
        except SessionError as exc:
            errors.append(f"{name}: {exc}")
            continue
        for path in files:
            try:
                cookies = load_cookies(path, key)
            except SessionError as exc:
                errors.append(f"{name}/{path.parent.name}: {exc}")
                continue
            header = cookie_header(cookies, domain)
            if "_tb_token_" not in header or "cookie2=" not in header:
                errors.append(f"{name}/{path.parent.name}: 未找到登录 cookie")
                continue
            headers = {
                "cookie": header,
                "user-agent": DEFAULT_UA,
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": f"https://{domain}",
                "referer": TMALL_HOME_URL,
                "x-requested-with": "XMLHttpRequest",
            }
            # Tmall has shipped both spellings; the cookie actually present is the
            # non-`X-` form (host tgc.tmall.com), so accept either spelling.
            for cookie in cookies:
                if cookie["name"] in XSRF_COOKIE_NAMES and cookie["value"]:
                    headers["x-xsrf-token"] = cookie["value"]
                    break
            # Keep stdout clean: callers parse --json output.
            print(
                f"已读取登录态：{BROWSERS[name]['name']} / {path.parent.name}"
                f"（{len(cookies)} 条 cookie）",
                file=sys.stderr,
            )
            return headers
    raise SessionError(
        "未能在本机浏览器中找到可用的天猫登录态（浏览器里没有有效登录 cookie）。"
        "请先在当前已打开的浏览器里打开 https://tgcwork.tmall.com/login 完成登录"
        "（复用已有标签，没有才新开一个；不要新开浏览器实例，不要把密码落盘）。\n"
        + "\n".join(f"- {item}" for item in errors)
    )


def check_session(headers: dict[str, str]) -> bool:
    """True when scm.tmall.com/loginStatus reports an authenticated session."""
    opener = build_opener()
    try:
        request = Request(
            LOGIN_CHECK_URL,
            headers={k: v for k, v in headers.items() if k in {"cookie", "user-agent", "referer"}},
        )
        with opener.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (HTTPError, URLError, OSError, ValueError):
        return False
    return bool(isinstance(payload, dict) and payload.get("data") is True)


def account_hint(headers: dict[str, str]) -> str:
    """Non-secret identity hint (login nick) for diagnostics.

    Empty values are skipped on purpose: tmall leaves `lgc=`/`sn=` blank on some
    sessions, and a blank hint would silently disable the multi-shop / multi-account gate.
    """
    cookies: dict[str, str] = {}
    for part in headers.get("cookie", "").split("; "):
        name, _, value = part.partition("=")
        if name and value:
            cookies[name] = value
    for name in HINT_COOKIE_ORDER:
        if cookies.get(name):
            return cookies[name]
    return "未知"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="检查并构建天猫登录会话（不输出凭据）")
    parser.add_argument("--browser", default="chrome", choices=[*BROWSERS, "auto"])
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--json", action="store_true", help="输出脱敏后的会话诊断 JSON")
    args = parser.parse_args(argv)
    try:
        headers = build_headers(args.browser, args.domain)
    except SessionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    ok = check_session(headers)
    summary = {
        "loginCheck": ok,
        "account": account_hint(headers),
        "cookieCount": len(headers.get("cookie", "").split("; ")),
        "cookieBytes": len(headers.get("cookie", "")),
        "hasXsrf": "x-xsrf-token" in headers,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"登录态：{'有效' if ok else '无效，需要重新登录'}")
        print(f"账号：{summary['account']}")
        print(f"cookie：{summary['cookieCount']} 项 / {summary['cookieBytes']} 字节")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
