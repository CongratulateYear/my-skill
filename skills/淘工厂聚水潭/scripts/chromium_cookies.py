#!/usr/bin/env python3
"""Read scoped ERP cookies from the local Chromium-family cookie store.

Supported environments (the login form itself is driven by the agent through
magpie-browser in the user's already-open browser; this module only READS the
resulting cookie database):

  * macOS  + Chrome / Edge / Brave / Chromium   (keychain "Safe Storage" key)
  * Windows + Edge / Chrome                     (DPAPI key from "Local State")

When the cookie store cannot be read (Windows v20 app-bound encryption, an
Edge-exclusive Cookies file, or a sandbox that denies the temp area), the
browser-bridge transport reuses the already-logged-in www.erp321.com tab
instead: build_session(transport="auto") falls back to it automatically.

Credentials never leave memory: nothing is written to the skill directory,
state files, or logs. Only non-secret diagnostics are printed (stderr).
"""
from __future__ import annotations

import base64
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

DEFAULT_DOMAIN = "www.erp321.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)
WIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0"
)

# Tmall has shipped the XSRF cookie under both names; the live one is XSRF-TOKEN.
XSRF_COOKIE_NAMES = ("XSRF-TOKEN", "X-XSRF-TOKEN")
# Identity cookies, most reliable first; empty values are skipped.
HINT_COOKIE_ORDER = ("tracknick", "lgc", "cookie17", "unb", "sn")

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


IS_WINDOWS = sys.platform.startswith("win") or os.name == "nt"

# browser key -> {dir, label?, name, win_dir?}
BROWSERS: dict[str, dict[str, str]] = {
    "chrome": {
        "dir": "Google/Chrome",
        "label": "Chrome Safe Storage",
        "name": "Google Chrome",
        "win_dir": r"Google\Chrome\User Data",
    },
    "edge": {
        "dir": "Microsoft Edge",
        "label": "Microsoft Edge Safe Storage",
        "name": "Microsoft Edge",
        "win_dir": r"Microsoft\Edge\User Data",
    },
    "chrome-beta": {
        "dir": "Google/Chrome Beta",
        "label": "Chrome Safe Storage",
        "name": "Google Chrome Beta",
        "win_dir": r"Google\Chrome Beta\User Data",
    },
    "chrome-canary": {
        "dir": "Google/Chrome Canary",
        "label": "Chrome Safe Storage",
        "name": "Google Chrome Canary",
        "win_dir": r"Google\Chrome SxS\User Data",
    },
    "brave": {
        "dir": "BraveSoftware/Brave-Browser",
        "label": "Brave Safe Storage",
        "name": "Brave",
        "win_dir": r"BraveSoftware\Brave-Browser\User Data",
    },
    "chromium": {
        "dir": "Chromium",
        "label": "Chromium Safe Storage",
        "name": "Chromium",
        "win_dir": r"Chromium\User Data",
    },
}


class SessionError(RuntimeError):
    """The local browser session could not be turned into usable headers."""


class BridgeSessionError(SessionError):
    """Cookie store unusable and the in-page fetch bridge is not available."""


# Windows-only: where a logged-in Edge/Chrome keeps its cookies. Edge keeps the
# SQLite file open exclusively while the browser runs, Chrome does not.
WINDOWS_COOKIE_LOCK_HINT = (
    "（Windows 上 Edge/Chrome 运行时会独占 cookie 数据库：若提示无法读取，"
    "可改用浏览器桥传输，或在要读的那个浏览器里登录后完全退出该浏览器，"
    "或换用另一个浏览器登录一次聚水潭）"
)


def _user_data_root(browser: str) -> Path:
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / BROWSERS[browser]["win_dir"]
    return Path.home() / "Library" / "Application Support" / BROWSERS[browser]["dir"]


def cookie_files(browser: str) -> list[Path]:
    if browser not in BROWSERS:
        raise SessionError(f"不支持的浏览器：{browser}（可选：{', '.join(BROWSERS)}）")
    root = _user_data_root(browser)
    if not root.is_dir():
        return []
    # Chromium keeps Cookies either at <profile>/Cookies (mac) or
    # <profile>/Network/Cookies (Windows / newer versions); accept both.
    found = [p for p in sorted(root.glob("*/Cookies")) if p.is_file()]
    found += [p for p in sorted(root.glob("*/Network/Cookies")) if p.is_file()]
    legacy = root / "Cookies"
    legacy_network = root / "Network" / "Cookies"
    if legacy_network.is_file():
        found.insert(0, legacy_network)
    if legacy.is_file():
        found.insert(0, legacy)
    seen: set[str] = set()
    unique: list[Path] = []
    for p in found:
        if str(p) not in seen:
            seen.add(str(p))
            unique.append(p)
    unique.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return unique


def _keychain_key(label: str, attempts: int = 12) -> bytes:
    """macOS: read the browser's cookie encryption password from the keychain.

    `security` intermittently returns 161 (item not found) or blocks for tens
    of seconds while the login keychain is unlocked, so retry with a timeout.
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
        f"无法从钥匙串读取「{label}」（{last_error}）。请在钥匙串访问中允许 `security` 读取该项。"
    )


def _dpapi_decrypt(blob: bytes) -> bytes:
    """Windows: CryptUnprotectData via ctypes (no pywin32 needed)."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise SessionError("CryptUnprotectData 失败（DPAPI），无法解密浏览器主密钥")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _windows_master_key(browser: str) -> bytes:
    """Windows: the AES key under "os_crypt.encrypted_key" in Local State."""
    state_path = _user_data_root(browser) / "Local State"
    if not state_path.is_file():
        raise SessionError(f"未找到 {state_path}")
    try:
        state = json.loads(state_path.read_text("utf-8"))
        encoded = state["os_crypt"]["encrypted_key"]
    except (OSError, ValueError, KeyError) as exc:
        raise SessionError(f"解析 Local State 失败：{exc}") from exc
    blob = base64.b64decode(encoded)
    if blob[:5] == b"DPAPI":
        blob = blob[5:]
    key = _dpapi_decrypt(blob)
    if not key:
        raise SessionError("DPAPI 解出的主密钥为空")
    return key


def _strip_host_hash(plain: bytes, host: str) -> bytes:
    import hashlib

    if len(plain) >= 32 and hashlib.sha256(host.encode("utf-8")).digest() == plain[:32]:
        return plain[32:]
    return plain


def _decrypt_windows(payload: bytes, key: bytes, host: str) -> str:
    """Windows Chrome/Edge: AES-256-GCM (current) or AES-256-CBC (legacy)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    plain: bytes | None = None
    # Current Chromium: 12-byte nonce + ciphertext + 16-byte tag; domain-bound
    # cookies are prefixed with SHA-256(host).
    if len(payload) > 12 + 16:
        nonce, body = payload[:12], payload[12:]
        try:
            plain = AESGCM(key).decrypt(nonce, body, None)
        except Exception:  # noqa: BLE001 - fall through to the legacy format
            plain = None
    if plain is None:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        plain = decryptor.update(payload) + decryptor.finalize()
        if plain:
            plain = plain[: -plain[-1]] if 0 < plain[-1] <= 16 else plain
    return _strip_host_hash(plain, host).decode("utf-8", "replace")


def _decrypt(encrypted: bytes, key: bytes, host: str) -> str:
    import hashlib

    if not encrypted:
        return ""
    prefix = encrypted[:3]
    if prefix in (b"v10", b"v11"):
        if IS_WINDOWS:
            return _decrypt_windows(encrypted[3:], key, host)
        # macOS: AES-128-CBC with the PBKDF2-derived "peanuts" key
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        derived = hashlib.pbkdf2_hmac("sha1", key, b"saltysalt", 1003, 16)
        decryptor = Cipher(algorithms.AES(derived), modes.CBC(b" " * 16)).decryptor()
        plain = decryptor.update(encrypted[3:]) + decryptor.finalize()
        if plain:
            plain = plain[:-plain[-1]]
        if len(plain) >= 32 and hashlib.sha256(host.encode("utf-8")).digest() == plain[:32]:
            plain = plain[32:]
        return plain.decode("utf-8", "replace")
    if prefix == b"v20":
        raise SessionError(
            "该浏览器的 cookie 使用 v20 应用绑定加密（App-Bound Encryption），本工具无法解密；"
            "请改用浏览器桥传输（--transport bridge），或在浏览器设置里关闭应用绑定加密，"
            "或换用未启用该加密的浏览器配置文件。" + (WINDOWS_COOKIE_LOCK_HINT if IS_WINDOWS else "")
        )
    return encrypted.decode("utf-8", "replace")


QUERY_COOKIES = (
    "select host_key, name, encrypted_value, value, expires_utc, "
    "is_httponly, is_secure, path from cookies"
)


def _read_rows(cookies_path: Path) -> list[tuple]:
    """Read the original encrypted DB read-only; never copy credential databases.

    Standard SQLite read-only mode honours WAL; locked stores fail over to the
    browser bridge rather than using immutable stale snapshots or disk copies.
    """
    try:
        connection = sqlite3.connect(cookies_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            return connection.execute(QUERY_COOKIES).fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise SessionError('浏览器Cookie库不可只读访问，请使用浏览器桥') from None


def load_cookies(cookies_path: Path, key: bytes) -> list[dict[str, Any]]:
    """Read and decrypt every cookie row of the given store."""
    rows = _read_rows(cookies_path)
    cookies: list[dict[str, Any]] = []
    for host, name, encrypted, value, expires_utc, httponly, secure, path in rows:
        if not name or not _domain_matches(host, DEFAULT_DOMAIN):
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
    applicable.sort(key=lambda c: (-len(c["host"].lstrip(".")), c["name"]))
    merged: dict[str, str] = {}
    for cookie in applicable:
        merged.setdefault(cookie["name"], cookie["value"])
    return "; ".join(f"{name}={value}" for name, value in merged.items())


def _browser_key(browser: str) -> bytes:
    if IS_WINDOWS:
        return _windows_master_key(browser)
    return _keychain_key(BROWSERS[browser]["label"])
