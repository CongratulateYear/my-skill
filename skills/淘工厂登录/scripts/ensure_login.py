#!/usr/bin/env python3
"""Single entry point for the tmall login gate (cross-platform).

Prints ONE JSON object on stdout so callers can branch on it, and exits with:

  0  LOGGED_IN    the browser's tmall cookies are usable right now
                  (and, with --expect-company, the logged-in company matches)
  3  NEED_LOGIN   cookies missing or expired, wrong account, or wrong company
                  -> the agent must drive the login form through
                  magpie-browser in the *already open* browser
  1  ERROR        unexpected failure (keychain / DPAPI / decrypt / cookie db)

This script never opens a browser window, never launches a second browser, and
never touches a password: driving the form is the agent's job (SKILL.md
「登录流程」). It only reports state, plus the concrete next `action` for the
agent, and can optionally wait for the login to land so the caller does not
race the browser. Pass `--expect-account <hint>` so a still-logged-in previous
account is reported as `wrong-account` instead of a false success. Pass
`--expect-company <名称>` (user usually sends the short company/shop name,
e.g. 埔鸣) so a logged-in session of a DIFFERENT company is reported as
`wrong-company` instead of a false success.
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

LOGIN_URL = "https://tgcwork.tmall.com/login"

ACTION = {
    "loggedIn": "登录态有效，直接继续原流程，不要再打开登录页。",
    "no-cookie": (
        "浏览器里还没有天猫登录 cookie。用 magpie-browser 的 tabs 先找有没有 "
        "tgcwork.tmall.com 标签，没有就 tab_new 在【当前这个已经打开的浏览器】里"
        f"打开 {LOGIN_URL}（不要新开浏览器、不要用 chrome-devtools），登录后重跑本命令。"
    ),
    "expired": (
        "天猫登录 cookie 还在但已过期。用 magpie-browser 复用现有的 "
        f"tgcwork.tmall.com 标签（没有就 tab_new 一个，仍在当前浏览器里）打开 {LOGIN_URL} "
        "重新登录，登录后重跑本命令。"
    ),
    "wrong-account": (
        "当前浏览器登录的是另一个天猫账号。用 magpie-browser 在该标签页退出登录，"
        f"再用目标账号登录 {LOGIN_URL}（复用同一标签，不要新开浏览器、不要把密码落盘），"
        "登录后重跑本命令。"
    ),
    "wrong-company": (
        "当前浏览器登录的账号不属于目标公司。用 magpie-browser 在 tgc.tmall.com 页面"
        "退出登录（右上角公司名 → 退出），再用目标公司的账号登录 "
        f"{LOGIN_URL}（复用同一标签，不要新开浏览器、不要把密码落盘），登录后重跑本命令。"
    ),
    "company-unknown": (
        "登录态有效，但查询公司名失败（接口异常）。重跑一次本命令；仍失败则人工在"
        " tgc.tmall.com/ds/page/supplier/home 页面右上角核对公司名。"
    ),
}


def probe(browser: str, domain: str) -> dict[str, object]:
    try:
        headers = tmall_session.build_headers(browser, domain)
    except tmall_session.SessionError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else "未知原因"
        return {
            "loggedIn": False,
            "reason": "no-cookie",
            "detail": detail,
            "accountHint": "",
            "cookieCount": 0,
            "hasXsrf": False,
            "action": ACTION["no-cookie"],
        }
    ok = tmall_session.check_session(headers)
    company = ""
    company_detail = ""
    if ok:
        info = tmall_session.fetch_company(headers)
        company = info["company"]
        company_detail = info["detail"]
    return {
        "loggedIn": ok,
        "reason": "logged-in" if ok else "expired",
        "detail": "",
        "accountHint": tmall_session.account_hint(headers),
        "cookieCount": len(str(headers.get("cookie", "")).split("; ")),
        "hasXsrf": "x-xsrf-token" in headers,
        "company": company,
        "companyDetail": company_detail,
        "action": ACTION["loggedIn"] if ok else ACTION["expired"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查天猫登录态；未登录时给出 agent 的下一步动作")
    parser.add_argument("--browser", default="auto", choices=[*tmall_session.BROWSERS, "auto"])
    parser.add_argument("--domain", default=tmall_session.DEFAULT_DOMAIN)
    parser.add_argument("--wait", type=float, default=0.0,
                        help="浏览器里刚点完登录时，最多等这么多秒让 cookie 落库")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数")
    parser.add_argument("--expect-account", default="",
                        help="期望登录的账号提示；不一致时判为 wrong-account（换号场景用）")
    parser.add_argument("--expect-company", default="",
                        help="期望的公司/店铺名（用户通常发简称，如「埔鸣」）；"
                             "登录的公司不匹配时判为 wrong-company")
    args = parser.parse_args(argv)

    started = time.monotonic()
    expected = args.expect_account.strip()
    expected_company = args.expect_company.strip()

    def judge(state: dict) -> dict:
        if expected and state["loggedIn"] and state["accountHint"] != expected:
            state.update({
                "loggedIn": False,
                "reason": "wrong-account",
                "expectedAccount": expected,
                "action": ACTION["wrong-account"],
            })
        if expected_company and state["loggedIn"]:
            actual_company = str(state.get("company") or "")
            if not actual_company:
                state.update({
                    "loggedIn": False,
                    "reason": "company-unknown",
                    "expectedCompany": expected_company,
                    "companyDetail": state.get("companyDetail", ""),
                    "action": ACTION["company-unknown"],
                })
            elif not tmall_session.company_matches(expected_company, actual_company):
                state.update({
                    "loggedIn": False,
                    "reason": "wrong-company",
                    "expectedCompany": expected_company,
                    "action": ACTION["wrong-company"],
                })
        return state

    try:
        state = judge(probe(args.browser, args.domain))
        terminal = {"wrong-account", "wrong-company", "company-unknown"}
        deadline = started + max(args.wait, 0.0)
        while not state["loggedIn"] and state.get("reason") not in terminal \
                and time.monotonic() < deadline:
            time.sleep(max(min(args.interval, 5.0), 0.5))
            state = judge(probe(args.browser, args.domain))
    except Exception as exc:  # never leak a traceback into the agent's JSON
        print(json.dumps({
            "loggedIn": False,
            "reason": "error",
            "detail": f"{type(exc).__name__}: {exc}",
            "action": "登录态检查失败，请人工确认浏览器与系统凭据状态后重试。",
            "loginUrl": LOGIN_URL,
            "waitedSeconds": round(time.monotonic() - started, 1),
        }, ensure_ascii=False, indent=2))
        return EXIT_ERROR

    state["loginUrl"] = LOGIN_URL
    state["waitedSeconds"] = round(time.monotonic() - started, 1)
    state.setdefault("action", ACTION["loggedIn"])
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if state["loggedIn"]:
        suffix = f"（{state.get('company', '')}）" if state.get("company") else ""
        print(f"登录态有效：{state['accountHint']}{suffix}", file=sys.stderr)
        return EXIT_OK
    print(f"需要登录（{state['reason']}）：{state['detail'] or state['accountHint']}", file=sys.stderr)
    return EXIT_NEED_LOGIN


if __name__ == "__main__":
    raise SystemExit(main())
