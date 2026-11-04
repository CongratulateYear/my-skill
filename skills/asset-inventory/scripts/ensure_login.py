#!/usr/bin/env python3
"""Single entry point for the tmall login gate.

Prints ONE JSON object on stdout so callers can branch on it, and exits with:

  0  LOGGED_IN    the browser's tmll cookies are usable right now
  3  NEED_LOGIN   cookies missing or expired -> the agent must drive the login
                  form through magpie-browser in the *already open* browser
  1  ERROR        unexpected failure (keychain / decrypt / cookie database)

This script never opens a browser window, never launches a second browser, and
never touches a password: driving the form is the agent's job (SKILL.md
「登录态自愈」). It only reports state, plus the concrete next `action` for the
agent, and can optionally wait for the login to land so the caller does not race
the browser. Pass `--expect-account <hint>` in multi-shop runs so a still-logged-in
previous shop is reported as `wrong-account` instead of a false success.
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
        "拿不到登录态。先按顺序试：①起桥 `python scripts/browser_bridge.py --port 8899`"
        "（后台常驻）并用 magpie-browser 的 evaluate 在已登录的 tgc.tmall.com 标签里执行 "
        "scripts/bridge_agent.js 的内容（返回 \"bridge agent started\"），然后重跑本命令；"
        "②no-cookie 仍可能是桥未连接，不能据此认定未登录；确认桥就绪后校验登录态：用 magpie-browser 的 tabs 找 tgcwork.tmall.com 标签，"
        f"没有就 tab_new 在【当前这个已经打开的浏览器】里打开 {LOGIN_URL}"
        "（不要新开浏览器、不要用 chrome-devtools），登录后重跑本命令。"
    ),
    "expired": (
        "登录态还在但已过期。用 magpie-browser 复用现有的 "
        f"tgcwork.tmall.com 标签（没有就 tab_new 一个，仍在当前浏览器里）打开 {LOGIN_URL} "
        "重新登录，随后打开 tgc.tmall.com 并重新注入页面代理，再重跑本命令。"
    ),
    "wrong-account": (
        "当前浏览器登录的是另一个天猫账号。用 magpie-browser 在该标签页退出登录，"
        f"再用目标账号登录 {LOGIN_URL}（复用同一标签，不要新开浏览器、不要把密码落盘），"
        "登录后重跑本命令。"
    ),
}


def probe(browser: str, domain: str, transport: str = "auto",
          bridge_url: str | None = None) -> dict[str, object]:
    """Build a session (cookie store, else in-page bridge) and ask tmall if it is alive."""
    try:
        built = tmall_session.build_session(transport, browser, domain, bridge_url)
    except tmall_session.SessionError as exc:
        detail = str(exc).splitlines()[0] if str(exc) else "未知原因"
        return {
            "loggedIn": False,
            "reason": "no-cookie",
            "detail": detail,
            "accountHint": "",
            "cookieCount": 0,
            "hasXsrf": False,
            "transport": "",
            "action": ACTION["no-cookie"],
        }
    headers = built["headers"]
    ok = tmall_session.check_session(headers, built["session"])
    return {
        "loggedIn": ok,
        "reason": "logged-in" if ok else "expired",
        "detail": "",
        "accountHint": tmall_session.account_hint(headers),
        "cookieCount": len(str(headers.get("cookie", "")).split("; ")),
        "hasXsrf": "x-xsrf-token" in headers,
        "transport": built["transport"],
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
                        help="期望登录的账号提示；不一致时判为 wrong-account（多店换号用）")
    parser.add_argument("--transport", default="auto", choices=tmall_session.TRANSPORTS,
                        help="auto=cookie 库优先、失败回退浏览器桥；cookie=只读 cookie 库；bridge=只用页面桥")
    parser.add_argument("--bridge-url", default="", help="桥地址，默认 http://127.0.0.1:8899")
    args = parser.parse_args(argv)

    started = time.monotonic()
    expected = args.expect_account.strip()

    def judge(state: dict) -> dict:
        """Downgrade a live session to wrong-account when it is not the expected shop."""
        if expected and state["loggedIn"] and state["accountHint"] != expected:
            state.update({
                "loggedIn": False,
                "reason": "wrong-account",
                "expectedAccount": expected,
                "action": ACTION["wrong-account"],
            })
        return state

    try:
        state = judge(probe(args.browser, args.domain, args.transport, args.bridge_url or None))
        deadline = started + max(args.wait, 0.0)
        while not state["loggedIn"] and time.monotonic() < deadline:
            time.sleep(max(min(args.interval, 5.0), 0.5))
            state = judge(probe(args.browser, args.domain, args.transport, args.bridge_url or None))
    except Exception as exc:  # never leak a traceback into the agent's JSON
        print(json.dumps({
            "loggedIn": False,
            "reason": "error",
            "detail": f"{type(exc).__name__}: {exc}",
            "action": "登录态检查失败，请人工确认浏览器与钥匙串状态后重试。",
            "loginUrl": LOGIN_URL,
            "waitedSeconds": round(time.monotonic() - started, 1),
        }, ensure_ascii=False, indent=2))
        return EXIT_ERROR

    state["loginUrl"] = LOGIN_URL
    state["waitedSeconds"] = round(time.monotonic() - started, 1)
    state.setdefault("action", ACTION["loggedIn"])
    print(json.dumps(state, ensure_ascii=False, indent=2))
    if state["loggedIn"]:
        print(f"登录态有效：{state['accountHint']}（传输：{state.get('transport') or '-'}）", file=sys.stderr)
        return EXIT_OK
    print(f"需要登录（{state['reason']}）：{state['detail'] or state['accountHint']}", file=sys.stderr)
    return EXIT_NEED_LOGIN


if __name__ == "__main__":
    raise SystemExit(main())
