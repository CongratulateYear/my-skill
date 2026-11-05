#!/usr/bin/env python3
"""Fetch the tmall API token (XSRF) for the CURRENT browser session.

Prints ONE JSON object on stdout:

    {
      "ok": true,
      "accountHint": "scm65253097",
      "xsrfToken": "....",
      "cookieCount": 37
    }

Exit codes (same contract as ensure_login.py):
  0  ok           headers + token ready in the JSON on stdout
                 (and, with --expect-company, the logged-in company matches)
  3  need-login   run the SKILL.md login flow first, then re-run
                 (reasons: no-cookie / expired / wrong-account / wrong-company
                  / company-unknown)
  1  error        unexpected failure

The XSRF token is a session credential. It lives in the caller's memory only:
never echo it into chat replies, logs, state files, or documents. Use
--with-headers to also receive the full ready-to-use header dict (cookie
included) for immediate in-process API calls; do not persist it anywhere.

    python3 tmall_token.py --with-headers | python3 -c 'import json,sys; print(json.load(sys.stdin)["headers"]["x-xsrf-token"])'
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmall_session  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEED_LOGIN = 3


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - non-fatal convenience
                pass

    parser = argparse.ArgumentParser(description="获取当前浏览器天猫会话的 XSRF token（不落盘）")
    parser.add_argument("--browser", default="auto", choices=[*tmall_session.BROWSERS, "auto"])
    parser.add_argument("--domain", default=tmall_session.DEFAULT_DOMAIN)
    parser.add_argument("--transport", default="auto", choices=tmall_session.TRANSPORTS,
                        help="auto=cookie 库优先、失败回退浏览器桥；cookie=只读 cookie 库；bridge=只用页面桥")
    parser.add_argument("--bridge-url", default="", help="桥地址，默认 http://127.0.0.1:8899")
    parser.add_argument("--expect-account", default="",
                        help="账号提示；公司匹配时默认仅诊断，不作为登录身份约束")
    parser.add_argument("--strict-account", action="store_true",
                        help="显式严格账号核对：可比账号不匹配仍报 wrong-account；须同时给 --expect-account")
    parser.add_argument("--expect-company", default="",
                        help="期望的公司/店铺名（通常为简称，如「埔鸣」）；"
                             "不匹配时输出 wrong-company 并退出 3")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="刚点完登录时最多等这么多秒让 cookie 落库")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--with-headers", action="store_true",
                        help="JSON 里附带完整请求头（含 cookie，仅限调用方内存中使用）")
    args = parser.parse_args(argv)
    if args.strict_account and not args.expect_account.strip():
        parser.error("--strict-account requires --expect-account")

    started = time.monotonic()
    expected = args.expect_account.strip()
    expected_company = args.expect_company.strip()
    company_seen: list[str] = []  # stash from fetch() so we do not hit the API twice
    note_seen: list[str] = []
    account_seen: list[str] = []

    def fetch() -> tuple[bool, str, dict, Any]:
        try:
            built = tmall_session.build_session(
                args.transport, args.browser, args.domain, args.bridge_url or None)
        except tmall_session.BridgeSessionError as exc:
            return False, f"no-session: {str(exc).splitlines()[0]}", {}, None
        except tmall_session.SessionError as exc:
            return False, f"no-cookie: {str(exc).splitlines()[0]}", {}, None
        headers = built["headers"]
        if not tmall_session.check_session(headers, built["session"]):
            return False, "expired", headers, built["session"]
        info: dict = {}
        if expected or expected_company:
            info = tmall_session.fetch_company(headers, built["session"])
        if expected_company:
            company_seen[:] = [info.get("company", "")]
            if not info.get("company"):
                return False, "company-unknown", headers, built["session"]
            if not tmall_session.company_matches(expected_company, info["company"]):
                return False, "wrong-company", headers, built["session"]
        if expected:
            verdict = tmall_session.account_verdict(
                expected, info.get("erpMobile", ""), tmall_session.account_hint(headers))
            account_seen[:] = [verdict]
            if verdict == "mismatch" and (args.strict_account or not expected_company):
                return False, "wrong-account", headers, built["session"]
            if verdict == "mismatch":
                account_seen[:] = ["mismatch-ignored-company-match"]
                note_seen[:] = ["公司匹配；登录手机号与 erpMobile 可不同，不阻断。"]
            if verdict == "unverified":
                note_seen[:] = [
                    f"账号未能核对（expected={expected}，erpMobile/hint 均不可比），未阻断"]
        return True, "logged-in", headers, built["session"]

    try:
        ok, reason, headers, _session = fetch()
        terminal = {"wrong-account", "wrong-company", "company-unknown"}
        deadline = started + max(args.wait, 0.0)
        while not ok and reason not in terminal and time.monotonic() < deadline:
            time.sleep(max(min(args.interval, 5.0), 0.5))
            ok, reason, headers, _session = fetch()
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "reason": "error",
            "detail": f"{type(exc).__name__}: {exc}",
            "waitedSeconds": round(time.monotonic() - started, 1),
        }, ensure_ascii=False, indent=2))
        return EXIT_ERROR

    if not ok:
        payload = {
            "ok": False,
            "reason": reason,
            "accountHint": tmall_session.account_hint(headers) if headers else "",
            "loginUrl": "https://tgcwork.tmall.com/login",
            "action": "先按「淘工厂登录」skill 的登录流程用 magpie-browser 登录，再重跑本命令。",
            "waitedSeconds": round(time.monotonic() - started, 1),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"需要登录（{reason}）", file=sys.stderr)
        return EXIT_NEED_LOGIN

    payload = {
        "ok": True,
        "reason": "logged-in",
        "accountHint": tmall_session.account_hint(headers),
        "company": company_seen[0] if company_seen else "",
        "cookieCount": len(headers.get("cookie", "").split("; ")),
        "xsrfToken": headers.get("x-xsrf-token", ""),
        "waitedSeconds": round(time.monotonic() - started, 1),
    }
    if account_seen:
        payload["accountCheck"] = account_seen[0]
    if note_seen:
        payload["note"] = note_seen[0]
    if args.with_headers:
        payload["headers"] = headers
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"token 已获取：{payload['accountHint']}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
