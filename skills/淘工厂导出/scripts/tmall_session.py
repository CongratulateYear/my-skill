#!/usr/bin/env python3
"""Build a tmall session from the local Chromium-family cookie store.

Supported environments (the login form itself is driven by the agent through
magpie-browser in the user's already-open browser; this module only READS the
resulting cookie database):

  * macOS  + Chrome / Edge / Brave / Chromium   (keychain "Safe Storage" key)
  * Windows + Edge / Chrome                     (DPAPI key from "Local State")

When the cookie store cannot be read (Windows v20 app-bound encryption, an
Edge-exclusive Cookies file, or a sandbox that denies the temp area), the
browser-bridge transport reuses the already-logged-in tgc.tmall.com tab
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

LOGIN_CHECK_URL = "https://scm.tmall.com/loginStatus"
TMALL_HOME_URL = "https://tgc.tmall.com/ds/page/supplier/home"
COMPANY_INFO_URL = "https://tgc.tmall.com/api/v1/supplierHome/accountInfo"
DEFAULT_DOMAIN = "tgc.tmall.com"
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
import _bootstrap  # noqa: E402,F401  (adds the skill's vendored libraries)

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
    "或换用另一个浏览器登录一次天猫）"
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


def _copy_cookie_db(cookies_path: Path, copy: Path) -> None:
    """Copy the live SQLite store, retrying while the browser keeps a lock."""
    last: Exception | None = None
    for attempt in range(3):
        try:
            shutil.copy2(cookies_path, copy)
            break
        except OSError as exc:
            last = exc
            time.sleep(0.3 * (attempt + 1))
    else:
        raise SessionError(
            f"无法读取 cookie 数据库（{cookies_path}）：{last}。{WINDOWS_COOKIE_LOCK_HINT}"
        )
    for suffix in ("-journal", "-wal"):
        extra = cookies_path.with_name(cookies_path.name + suffix)
        if extra.exists():
            try:
                shutil.copy2(extra, copy.with_name(copy.name + suffix))
            except OSError:
                pass


def _temp_dir(prefix: str = "tmall_session_") -> Path:
    """Create a scratch directory that cleanup failures cannot turn into an error.

    Sandboxed hosts sometimes deny deleting files the process itself just wrote
    (or deny writing the system temp area at all), so cleanup is best-effort
    instead of `TemporaryDirectory`'s raising `__exit__`.
    """
    return Path(tempfile.mkdtemp(prefix=prefix))


def _drop_dir(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 - leftovers in the temp area are harmless
        pass


QUERY_COOKIES = (
    "select host_key, name, encrypted_value, value, expires_utc, "
    "is_httponly, is_secure, path from cookies"
)


def _read_rows(cookies_path: Path) -> list[tuple]:
    """Read cookie rows without needing a writable scratch copy when possible.

    Windows: open the live store read-only and immutable, so the browser's lock
    does not matter and nothing has to be written to disk first (sandboxed
    hosts often deny the C: temp area outright).
    macOS (and as a fallback): copy the store aside because the browser holds
    it with an exclusive SQLite lock there.
    """
    if IS_WINDOWS:
        uri = f"file:{cookies_path.as_posix()}?mode=ro&immutable=1"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=10)
        except sqlite3.Error:
            connection = None
        if connection is not None:
            try:
                return connection.execute(QUERY_COOKIES).fetchall()
            except sqlite3.DatabaseError as exc:
                raise SessionError(f"读取 cookie 数据库失败：{exc}") from exc
            finally:
                connection.close()
    tmp = _temp_dir()
    try:
        copy = tmp / "Cookies"
        _copy_cookie_db(cookies_path, copy)
        connection = sqlite3.connect(str(copy))
        try:
            return connection.execute(QUERY_COOKIES).fetchall()
        except sqlite3.DatabaseError as exc:
            raise SessionError(f"读取 cookie 数据库失败：{exc}") from exc
        finally:
            connection.close()
    finally:
        _drop_dir(tmp)


def load_cookies(cookies_path: Path, key: bytes) -> list[dict[str, Any]]:
    """Read and decrypt every cookie row of the given store."""
    rows = _read_rows(cookies_path)
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
    applicable.sort(key=lambda c: (-len(c["host"].lstrip(".")), c["name"]))
    merged: dict[str, str] = {}
    for cookie in applicable:
        merged.setdefault(cookie["name"], cookie["value"])
    return "; ".join(f"{name}={value}" for name, value in merged.items())


def _browser_key(browser: str) -> bytes:
    if IS_WINDOWS:
        return _windows_master_key(browser)
    return _keychain_key(BROWSERS[browser]["label"])


def build_headers(browser: str = "auto", domain: str = DEFAULT_DOMAIN) -> dict[str, str]:
    """Return the API headers for an already-logged-in local browser session."""
    errors: list[str] = []
    candidates = [browser] if browser != "auto" else list(BROWSERS)
    for name in candidates:
        files = cookie_files(name)
        if not files:
            errors.append(f"{name}: 未找到 cookie 数据库")
            continue
        try:
            key = _browser_key(name)
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
                "user-agent": WIN_UA if name == "edge" and IS_WINDOWS else DEFAULT_UA,
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": f"https://{domain}",
                "referer": TMALL_HOME_URL,
                "x-requested-with": "XMLHttpRequest",
            }
            for cookie in cookies:
                if cookie["name"] in XSRF_COOKIE_NAMES and cookie["value"]:
                    headers["x-xsrf-token"] = cookie["value"]
                    break
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


TRANSPORTS = ("auto", "cookie", "bridge")


def _bridge_module():
    """延迟导入 browser_bridge；本 skill 未带时回退用 asset-inventory 的副本。

    browser_bridge 是纯 stdlib（无 _bootstrap、无第三方依赖），跨 skill import
    安全。返回模块对象。
    """
    try:
        import browser_bridge
        return browser_bridge
    except ImportError:
        pass
    # 同一 skills 根下已安装的 asset-inventory 提供 browser_bridge.py
    candidates = [Path(__file__).resolve().parents[2] / "asset-inventory" / "scripts"]
    for candidate in candidates:
        if (candidate / "browser_bridge.py").is_file() and str(candidate) not in sys.path:
            sys.path.append(str(candidate))
    import browser_bridge
    return browser_bridge


def build_bridge_session(bridge_url: str | None = None) -> tuple[dict[str, str], Any]:
    """借「已登录浏览器标签」拿会话：返回 (headers, session)。

    页面里的 fetch 由浏览器自己附 HttpOnly cookie，因此在 cookie 库读不出来
    （Windows v20 应用绑定加密 / Edge 独占 Cookies 文件 / 沙箱拦临时目录）的
    机器上仍然可用。
    """
    bridge = _bridge_module()
    try:
        client = bridge.BridgeClient(bridge_url)
        client.wait_ready(bridge.AGENT_READY_SECONDS)
    except bridge.BridgeError as exc:
        raise BridgeSessionError(str(exc)) from exc
    headers: dict[str, str] = {}
    cookie = str(client.session_info().get("cookie") or "")
    if cookie:
        # 只是给诊断/账号提示用；真实请求的 cookie 由浏览器自己加。
        headers["cookie"] = cookie
    headers.update(client.headers())
    return headers, client


def build_session(
    transport: str = "auto",
    browser: str | None = None,
    domain: str = DEFAULT_DOMAIN,
    bridge_url: str | None = None,
) -> dict[str, Any]:
    """统一构建 (headers, session)：cookie 库优先，失败自动回退浏览器桥。

    transport=native 的旧名字仍然接受，等同 cookie。返回
    {"headers": ..., "session": build_opener() 或 BridgeClient, "transport": ...}。
    """
    mode = "cookie" if transport in ("native", "cookie") else transport
    if mode not in TRANSPORTS:
        raise ValueError(f"未知 transport：{transport}（可选 auto / cookie / bridge）")
    order = ("cookie", "bridge") if mode == "auto" else (mode,)
    errors: list[str] = []
    for item in order:
        try:
            if item == "cookie":
                headers = build_headers(browser or "auto", domain)
                return {"headers": headers, "session": build_opener(), "transport": "cookie"}
            headers, client = build_bridge_session(bridge_url)
            return {"headers": headers, "session": client, "transport": "bridge"}
        except SessionError as exc:
            errors.append(f"{item}：{str(exc).splitlines()[0] if str(exc) else '未知原因'}")
        except Exception as exc:  # 桥端口被占、页面代理异常等
            errors.append(f"{item}：{type(exc).__name__}: {exc}")
    detail = "\n".join(f"- {line}" for line in errors)
    if mode == "bridge" or (mode == "auto" and len(errors) > 1):
        raise BridgeSessionError(
            "拿不到天猫登录态：cookie 库与浏览器桥都不可用。\n" + detail
        )
    raise SessionError("拿不到天猫登录态。\n" + detail)


def check_session(headers: dict[str, str], session: Any = None) -> bool:
    """True when scm.tmall.com/loginStatus reports an authenticated session."""
    opener = session if session is not None else build_opener()
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
    cookies: dict[str, str] = {}
    for part in headers.get("cookie", "").split("; "):
        name, _, value = part.partition("=")
        if name and value:
            cookies[name] = value
    for name in HINT_COOKIE_ORDER:
        if cookies.get(name):
            return cookies[name]
    return "未知"


def fetch_company(headers: dict[str, str], session: Any = None) -> dict[str, Any]:
    """Return the supplier identity for the CURRENT session.

    GET https://tgc.tmall.com/api/v1/supplierHome/accountInfo -> {"success":
    true, "companyName": "...", "subCompanyList": [...], "dingdingList": [...]}.
    Returns {"company": <name or "">, "ok": bool, "detail": str,
    "erpMobile": <脱敏手机号，如 "1312****864", 取自 dingdingList[0].dingdingMobile>};
    never raises.
    """
    opener = session if session is not None else build_opener()
    try:
        request = Request(
            COMPANY_INFO_URL,
            headers={k: v for k, v in headers.items()
                     if k in {"cookie", "user-agent", "referer", "x-xsrf-token"}},
        )
        with opener.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        return {"company": "", "ok": False, "detail": f"accountInfo 请求失败: {exc}",
                "erpMobile": ""}
    if not (isinstance(payload, dict) and payload.get("success")):
        return {"company": "", "ok": False,
                "detail": f"accountInfo 返回失败: {payload.get('errorMessage', '未知')}",
                "erpMobile": ""}
    company = str(payload.get("companyName") or "").strip()
    ding = (payload.get("dingdingList") or [{}])[0]
    erp_mobile = str(ding.get("dingdingMobile") or "").strip()
    return {"company": company, "ok": True, "detail": "", "erpMobile": erp_mobile}


def company_matches(expected: str, actual: str) -> bool:
    """True when the user-supplied company name matches the logged-in one.

    The user usually sends a short name (e.g. 埔鸣) while the API returns the
    full registered name (e.g. 义乌市埔鸣日用品有限公司). Match when either
    string contains the other, ignoring case and surrounding whitespace.
    """
    exp = expected.strip().lower()
    act = actual.strip().lower()
    if not exp or not act:
        return False
    return exp in act or act in exp


def is_phone(value: str) -> bool:
    """True when the value looks like a full mainland mobile number (11 digits)."""
    text = (value or "").strip()
    return len(text) == 11 and text.isdigit()


def phone_mask_matches(expected: str, masked: str) -> bool:
    """Full phone number vs the platform's masked form like ``1312****864``.

    The mask keeps the first 4 and last 3 digits; compare those positions only.
    """
    exp = (expected or "").strip()
    mask = (masked or "").strip()
    if not exp or not mask or "*" not in mask:
        return False
    head = mask[:mask.index("*")]
    tail = mask[mask.rindex("*") + 1:]
    if not head or not tail:
        return False
    return exp.startswith(head) and exp.endswith(tail)


def account_verdict(expected: str, erp_mobile: str, hint: str) -> str:
    """期望账号 vs 当前登录账号：返回 "ok" / "mismatch" / "unverified"。

    手机号只信平台的脱敏 erpMobile（accountInfo 的 dingdingMobile，形如
    "1312****864"）；cookie 提示（tracknick 等）可能是上一账号残留或不同
    体系（scm 号），绝不与手机号直接比对，防止跨体系误报 wrong-account。
    比对不上时返回 "unverified"（不阻断），只在有明确证据时才 mismatch。
    """
    exp = (expected or "").strip()
    if not exp:
        return "ok"
    if is_phone(exp):
        masked = (erp_mobile or "").strip()
        if not masked:
            return "unverified"
        return "ok" if phone_mask_matches(exp, masked) else "mismatch"
    text = (hint or "").strip()
    if not text or text == "未知":
        return "unverified"
    return "ok" if text == exp else "mismatch"


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="检查并构建天猫登录会话（不输出凭据）")
    parser.add_argument("--browser", default="auto", choices=[*BROWSERS, "auto"])
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--transport", default="auto", choices=TRANSPORTS,
                        help="auto=cookie 库优先、失败回退浏览器桥；cookie=只读 cookie 库；bridge=只用页面桥")
    parser.add_argument("--bridge-url", default="", help="桥地址，默认 http://127.0.0.1:8899")
    parser.add_argument("--json", action="store_true", help="输出脱敏后的会话诊断 JSON")
    args = parser.parse_args(argv)
    try:
        built = build_session(args.transport, args.browser, args.domain, args.bridge_url or None)
    except SessionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    headers = built["headers"]
    ok = check_session(headers, built["session"])
    summary = {
        "loginCheck": ok,
        "transport": built["transport"],
        "account": account_hint(headers),
        "cookieCount": len(headers.get("cookie", "").split("; ")),
        "cookieBytes": len(headers.get("cookie", "")),
        "hasXsrf": "x-xsrf-token" in headers,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"登录态：{'有效' if ok else '无效，需要重新登录'}")
        print(f"传输：{built['transport']}")
        print(f"账号：{summary['account']}")
        print(f"cookie：{summary['cookieCount']} 项 / {summary['cookieBytes']} 字节")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
