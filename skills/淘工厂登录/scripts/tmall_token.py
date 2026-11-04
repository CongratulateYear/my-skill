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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tmall_session  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEED_LOGIN = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="获取当前浏览器天猫会话的 XSRF token（不落盘）")
    parser.add_argument("--browser", default="auto", choices=[*tmall_session.BROWSERS, "auto"])
    parser.add_argument("--domain", default=tmall_session.DEFAULT_DOMAIN)
    parser.add_argument("--expect-account", default="",
                        help="期望登录的账号提示；不一致时输出 wrong-account 并退出 3")
    parser.add_argument("--expect-company", default="",
                        help="期望的公司/店铺名（通常为简称，如「埔鸣」）；"
                             "不匹配时输出 wrong-company 并退出 3")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="刚点完登录时最多等这么多秒让 cookie 落库")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--with-headers", action="store_true",
                        help="JSON 里附带完整请求头（含 cookie，仅限调用方内存中使用）")
    args = parser.parse_args(argv)

    started = time.monotonic()
    expected = args.expect_account.strip()
    expected_company = args.expect_company.strip()
    company_seen: list[str] = []  # stash from fetch() so we do not hit the API twice

    def fetch() -> tuple[bool, str, dict]:
        try:
            headers = tmall_session.build_headers(args.browser, args.domain)
        except tmall_session.SessionError as exc:
            return False, f"no-cookie: {str(exc).splitlines()[0]}", {}
        if not tmall_session.check_session(headers):
            return False, "expired", headers
        if expected and tmall_session.account_hint(headers) != expected:
            return False, "wrong-account", headers
        if expected_company:
            info = tmall_session.fetch_company(headers)
            company_seen[:] = [info["company"]]
            if not info["company"]:
                return False, "company-unknown", headers
            if not tmall_session.company_matches(expected_company, info["company"]):
                return False, "wrong-company", headers
        return True, "logged-in", headers

    try:
        ok, reason, headers = fetch()
        terminal = {"wrong-account", "wrong-company", "company-unknown"}
        deadline = started + max(args.wait, 0.0)
        while not ok and reason not in terminal and time.monotonic() < deadline:
            time.sleep(max(min(args.interval, 5.0), 0.5))
            ok, reason, headers = fetch()
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
            "action": "先按 tmall-login skill 的登录流程用 magpie-browser 登录，再重跑本命令。",
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
    if args.with_headers:
        payload["headers"] = headers
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"token 已获取：{payload['accountHint']}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
