#!/usr/bin/env python3
"""Single entry point for the tmall login gate (cross-platform).

Prints ONE JSON object on stdout so callers can branch on it, and exits with:

  0  LOGGED_IN    the browser's tmall cookies are usable right now
                  (and, with --expect-company, the logged-in company matches)
  3  NEED_LOGIN   session unavailable, expired, wrong account, or wrong company
                  -> follow recovery in a NEW own Magpie tab in the already-open
                  browser; fill the login form only when logged out
  1  ERROR        unexpected failure (keychain / DPAPI / decrypt / cookie db)

Transport: --transport auto (default) uses official Playwright Extension and RAM scope reuse. Explicit cookie mode
retains native cookie-store compatibility without cross-process memory reuse.

This script never opens a browser window, never launches a second browser, and
never touches a password: driving the form is the agent's job (SKILL.md
「登录流程」). It only reports state, plus the concrete next `action` for the
agent, and can optionally wait for the login to land so the caller does not
race the browser. Pass `--expect-account <手机号或账号提示>` so a
still-logged-in previous account is reported as `wrong-account` instead of a
false success: phone numbers are compared against the platform's masked
erpMobile (accountInfo.dingdingMobile), non-phone hints against the cookie
hint; when neither is comparable the result is `accountCheck: unverified`
(non-blocking) instead of a false wrong-account. Pass `--expect-company <名称>`
(user usually sends the short company/shop name, e.g. 埔鸣) so a logged-in
session of a DIFFERENT company is reported as `wrong-company` instead of a
false success.
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

# Recovery instructions only: do not change session transport or validation logic.
RECOVERY_ROUTE = (
    "固定恢复路线：用 magpie-browser 在当前已打开的浏览器中 tab_new 新建自己的 Magpie 标签页，"
    "不复用用户、其他 agent 或旧标签页，不新开浏览器或 profile。"
    "同一次恢复继续用自己新建的同一标签页，不必每步再开。"
    "打开 https://tgc.tmall.com/ds/page/supplier/home，用 snapshot 判断："
    "已登录则不要退出，确认未登录才打开登录页真实填表。"
    "凭据只用于 Magpie 表单，不落盘、不回显，不写 shell、文件或日志；验证码由用户人工完成。"
    "补账号后再次检查密码是否有值，保留自动填充。缺凭据只询问一次，无新输入不重复催问；"
    "不得把含凭据历史恢复到临时文件。"
    "登录完成后使用官方 Playwright Extension 读取 Cookie，不启动页面桥或注入脚本。"
    "更换公司或登录恢复后须结束旧执行 scope，并开始新的内存 scope；不静默刷新旧缓存。"
    "随后重跑本命令（保留 --expect-company / --expect-account）；"
    "有目标公司时必须传 --expect-company，以服务端公司验证通过且退出码 0 为成功标准，"
    "页面跳转、扩展已连接或 erpMobile 有值均不代表成功。"
)

ACTION = {
    "loggedIn": "登录态有效，直接继续原流程，不要再打开登录页。", 
    "no-cookie": "本机未取得可用 cookie，不据此认定浏览器未登录。" + RECOVERY_ROUTE,
    "no-session": "会话尚不可用；no-session 不等于未登录，先检查自己新建的页面。" + RECOVERY_ROUTE,
    "expired": "服务端未确认登录态有效，先检查自己新建的页面，不盲目填表。" + RECOVERY_ROUTE,
    "wrong-account": (
        "当前登录的是另一账号；按登录skill区分计划内切换与意外公司不符。"
        "仅用户已给公司顺序、对应凭据并授权依次执行，且当前会话确认为刚完成的上一家公司时，"
        "计划内切换无需再问；其他情况先经用户确认。只在自己的标签页真实退出并登录目标账号"
        "（退出会影响同 profile 的会话）；不扩展为ERP换号授权。"
    ) + RECOVERY_ROUTE,
    "wrong-company": (
        "服务端返回的公司不属于目标公司；意外公司不符须先经用户确认，不能忽略验证。"
        "仅用户已给公司顺序、对应凭据并授权依次执行，且当前会话确认为刚完成的上一家公司时，"
        "计划内切换无需再问；其他情况先核对。只在自己的标签页真实退出并登录目标账号"
        "（退出会影响同 profile 的会话）；不扩展为ERP换号授权。"
    ) + RECOVERY_ROUTE,
    "company-unknown": (
        "服务端公司查询失败，不能宣告公司验证成功。保留 --expect-company 重跑一次；"
        "仍失败则停止原流程并请用户核对，页面公司名不能代替服务端验证。"
    ),
}


def probe(
    browser: str,
    domain: str,
    transport: str = "auto",
    bridge_url: str | None = None,
) -> dict[str, object]:
    try:
        built = tmall_session.build_session(transport, browser, domain, bridge_url)
    except tmall_session.BridgeSessionError as exc:
        # 保留完整多行错误清单（每条浏览器路线的死因），只截长度不砍行。
        detail = "\n".join(str(exc).splitlines())[:1200] if str(exc) else "未知原因"
        return {
            "loggedIn": False,
            "reason": "no-session",
            "detail": detail,
            "accountHint": "",
            "cookieCount": 0,
            "hasXsrf": False,
            "action": ACTION["no-session"],
        }
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
    headers = built["headers"]
    session = built["session"]
    ok = tmall_session.check_session(headers, session)
    company = ""
    company_detail = ""
    erp_mobile = ""
    if ok:
        info = tmall_session.fetch_company(headers, session)
        company = info["company"]
        company_detail = info["detail"]
        erp_mobile = info.get("erpMobile", "")
    return {
        "loggedIn": ok,
        "reason": "logged-in" if ok else "expired",
        "detail": "",
        "transport": built["transport"],
        "accountHint": tmall_session.account_hint(headers),
        "erpMobile": erp_mobile,
        "cookieCount": len([c for c in str(headers.get("cookie", "")).split("; ") if c]),
        "hasXsrf": "x-xsrf-token" in headers,
        "company": company,
        "companyDetail": company_detail,
        "action": ACTION["loggedIn"] if ok else ACTION["expired"],
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - non-fatal convenience
                pass

    parser = argparse.ArgumentParser(description="检查天猫登录态；不可用时给出固定恢复路线，未登录才填表")
    parser.add_argument("--browser", default="auto", choices=[*tmall_session.BROWSERS, "auto"])
    parser.add_argument("--domain", default=tmall_session.DEFAULT_DOMAIN)
    parser.add_argument("--transport", default="auto", choices=tmall_session.TRANSPORTS,
                        help="auto/playwright=官方扩展与执行范围内存复用；cookie=显式旧原生库；bridge=playwright兼容别名")
    parser.add_argument("--bridge-url", default="", help="已弃用；不会连接旧页面桥")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="浏览器里刚点完登录时，最多等这么多秒让 cookie 落库")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数")
    parser.add_argument("--expect-account", default="",
                        help="期望登录的账号（手机号或账号提示）；手机号只与平台脱敏 erpMobile 比对，"
                             "比对不上时报 unverified 而不是 wrong-account")
    parser.add_argument("--expect-company", default="",
                        help="期望的公司/店铺名（用户通常发简称，如「埔鸣」）；"
                             "登录的公司不匹配时判为 wrong-company")
    args = parser.parse_args(argv)

    started = time.monotonic()
    expected = args.expect_account.strip()
    expected_company = args.expect_company.strip()
    bridge_url = args.bridge_url or None

    def judge(state: dict) -> dict:
        if expected and state["loggedIn"]:
            verdict = tmall_session.account_verdict(
                expected, str(state.get("erpMobile") or ""), str(state.get("accountHint") or ""))
            if verdict == "mismatch":
                state.update({
                    "loggedIn": False,
                    "reason": "wrong-account",
                    "expectedAccount": expected,
                    "action": ACTION["wrong-account"],
                })
            elif verdict == "unverified":
                state["accountCheck"] = (
                    f"unverified：无法核对账号（expected={expected}，erpMobile/提示均不可比），未阻断")
            else:
                state["accountCheck"] = "ok"
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
        state = judge(probe(args.browser, args.domain, args.transport, bridge_url))
        terminal = {"wrong-account", "wrong-company", "company-unknown"}
        deadline = started + max(args.wait, 0.0)
        while not state["loggedIn"] and state.get("reason") not in terminal \
                and time.monotonic() < deadline:
            time.sleep(max(min(args.interval, 5.0), 0.5))
            state = judge(probe(args.browser, args.domain, args.transport, bridge_url))
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
        via = f" [via {state.get('transport', '')}]" if state.get("transport") else ""
        print(f"登录态有效：{state['accountHint']}{suffix}{via}", file=sys.stderr)
        return EXIT_OK
    print(f"需要恢复或核对（{state['reason']}）：{state['detail'] or state['accountHint']}", file=sys.stderr)
    return EXIT_NEED_LOGIN


if __name__ == "__main__":
    raise SystemExit(main())
