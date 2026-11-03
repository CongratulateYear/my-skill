#!/usr/bin/env python3
"""按用户给定顺序，一个账户一个账户地导出罚款。

设计要点（与资产盘算 multi_shop_runner 同一套闸门语义）：
- 一次只处理一个账户：登录态校验、输出目录、日志、状态全部按账户隔离。
- 账户之间严格串行，前一个账户完整导出后才进入下一个。
- 不接收、不保存密码：密码绝不落盘，也绝不从钥匙串 / Chrome Login Data 里挖。
  登录由 agent 用 magpie-browser 在【当前已打开的浏览器】里复用（没有才新开）一个
  tgcwork.tmall.com 标签、驱动登录表单完成；本脚本只校验“当前登录态是否真的换成了这个账户”。
- 未登录 / 登录态与账号不匹配 / 登录态没有切换 时立即停下（退出码 3），并输出可直接照抄的
  action（复用标签重登、不要新开浏览器）。agent 按 action 登录后重跑同一条命令即可续跑，
  已完成的账户不会重跑。
- 不写任何凭据：状态文件只记录店铺名、账号、日期、路径和结果摘要。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

FINE_SCRIPT = SCRIPT_DIR / "fine_export.py"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEED_LOGIN = 3
EXIT_ACCOUNT_FAILED = 4


def safe_prefix(name: str) -> str:
    text = str(name or "").strip()
    for char in ('/', '\\', ':', '*', '?', '"', '<', '>', '|', '\n', '\r', '\t'):
        text = text.replace(char, "-")
    return text.strip(" .-")


def parse_account_spec(spec: str) -> dict[str, Any]:
    """解析 `店铺名,账号,支付开始,支付结束[,账号提示]`。"""
    parts = [part.strip() for part in spec.split(",")]
    if len(parts) < 4:
        raise ValueError(f"--account 至少需要 店铺名,账号,支付开始,支付结束：{spec}")
    shop, account, pay_start, pay_end = parts[0], parts[1], parts[2], parts[3]
    hint = parts[4] if len(parts) > 4 else ""
    return {
        "shop": shop,
        "account": account,
        "accountHint": hint,
        "payStart": pay_start,
        "payEnd": pay_end,
    }


def load_accounts(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"账户清单无法读取：{path}") from exc
    accounts = data.get("accounts") if isinstance(data, dict) else data
    if isinstance(accounts, dict) and isinstance(accounts.get("shops"), list):
        accounts = accounts["shops"]
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("账户清单必须是非空数组，或含 accounts/shops 数组的对象")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(accounts, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个账户不是对象")
        shop = str(item.get("shop") or item.get("shopName") or "").strip()
        if not shop:
            raise ValueError(f"第 {index} 个账户缺少 shop（店铺名称）")
        pay_start = item.get("payStart") or item.get("orderPayStart")
        pay_end = item.get("payEnd") or item.get("orderPayEnd")
        if not pay_start or not pay_end:
            raise ValueError(f"账户 {shop} 缺少 payStart/payEnd")
        normalized.append({
            "shop": shop,
            "account": str(item.get("account") or "").strip(),
            "accountHint": str(item.get("accountHint") or "").strip(),
            "payStart": str(pay_start),
            "payEnd": str(pay_end),
        })
    return normalized


def session_snapshot() -> dict[str, Any]:
    """读取当前浏览器登录态与 token（不打印、不落盘任何凭据）。"""
    import tmall_session

    try:
        headers = tmall_session.build_headers()
    except tmall_session.SessionError as exc:
        return {
            "loggedIn": False,
            "hint": "",
            "cookieCount": 0,
            "hasXsrfToken": False,
            "reason": "no-cookie",
            "detail": str(exc).splitlines()[0] if str(exc) else "",
        }
    cookie = headers.get("cookie", "")
    parts = [part for part in cookie.split("; ") if "=" in part]
    logged_in = tmall_session.check_session(headers)
    return {
        "loggedIn": logged_in,
        "hint": tmall_session.account_hint(headers),
        "cookieCount": len(parts),
        "hasXsrfToken": bool(headers.get("x-xsrf-token")),
        "reason": "logged-in" if logged_in else "expired",
    }


def fine_file_for(account: dict[str, Any], account_dir: Path) -> Path:
    prefix = safe_prefix(account["shop"])
    return account_dir / f"{prefix}_罚款_{account['payStart']}_{account['payEnd']}.xlsx"


def run_step(command: list[str], log_path: Path) -> tuple[int, str]:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    output = (completed.stdout or "") + (completed.stderr or "")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    return completed.returncode, output


def run_account(account: dict[str, Any], output_root: Path) -> dict[str, Any]:
    name = account["shop"]
    prefix = safe_prefix(name)
    account_dir = (output_root / prefix).resolve()
    account_dir.mkdir(parents=True, exist_ok=True)
    fine_file = fine_file_for(account, account_dir)
    logs = account_dir / "logs"

    result: dict[str, Any] = {
        "shop": name,
        "account": account["account"],
        "outputDir": str(account_dir),
        "fineFile": str(fine_file),
        "dates": {"payStart": account["payStart"], "payEnd": account["payEnd"]},
        "stages": {},
    }

    command = [
        sys.executable, str(FINE_SCRIPT),
        "--shop-name", name,
        "--pay-start", account["payStart"], "--pay-end", account["payEnd"],
        "--output-dir", str(account_dir),
    ]
    code, output = run_step(command, logs / "export.log")
    result["stages"]["export"] = {
        "ok": code == EXIT_OK,
        "exitCode": code,
        "log": str(logs / "export.log"),
    }
    if code == EXIT_NEED_LOGIN:
        result["needLogin"] = True
        result["ok"] = False
        return result
    if code != EXIT_OK:
        result["ok"] = False
        result["reason"] = output.strip()[-1500:]
        return result
    if not fine_file.is_file():
        result["ok"] = False
        result["reason"] = f"导出命令成功但未找到输出文件：{fine_file}"
        return result
    result["ok"] = True
    return result


def load_batch_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "accounts": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "accounts": []}
    return state if isinstance(state, dict) else {"version": 1, "accounts": []}


def save_batch_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="按顺序一个账户一个账户地导出罚款")
    p.add_argument("--accounts-file", type=Path,
                   help="账户清单 JSON：{\"accounts\":[{\"shop\":...,\"account\":...,\"payStart\":...,\"payEnd\":...,\"accountHint\":可选}]}，也接受裸数组")
    p.add_argument("--account", action="append", default=[],
                   metavar="店名,账号,支付开始,支付结束[,账号提示]",
                   help="直接在命令行给出一个账户；可重复，顺序即执行顺序")
    p.add_argument("--output-root", type=Path, help="多账户输出根目录，每个账户一个子目录")
    p.add_argument("--only", action="append", default=[], help="只跑指定店铺（可重复），按清单顺序")
    p.add_argument("--skip-completed", action="store_true", help="跳过批次状态里已完成的账户")
    p.add_argument("--skip-login-check", action="store_true", help="跳过登录态校验（仅调试用）")
    p.add_argument("--continue-on-error", action="store_true",
                   help="某账户失败后继续下一账户（默认立即停止）")
    p.add_argument("--check-login", action="store_true",
                   help="只检查当前浏览器登录态并输出脱敏 JSON，不执行任何导出")
    args = p.parse_args()
    if args.check_login:
        return args
    if not args.account and not args.accounts_file:
        p.error("需要 --account 或 --accounts-file 提供账户")
    if not args.output_root:
        p.error("需要 --output-root")
    return args


def main() -> int:
    args = parse_args()
    if args.check_login:
        snapshot = session_snapshot()
        print(json.dumps({
            "loggedIn": snapshot.get("loggedIn"),
            "currentHint": snapshot.get("hint"),
            "cookieCount": snapshot.get("cookieCount"),
            "hasXsrfToken": snapshot.get("hasXsrfToken"),
            "reason": snapshot.get("reason"),
        }, ensure_ascii=False, indent=2))
        return EXIT_OK if snapshot.get("loggedIn") else EXIT_NEED_LOGIN

    accounts = load_accounts(args.accounts_file) if args.accounts_file else []
    accounts.extend(parse_account_spec(spec) for spec in args.account)
    output_root = args.output_root.expanduser().resolve()
    state_path = output_root / "fine-export-state.json"
    state = load_batch_state(state_path)
    done = {item.get("shop") for item in state.get("accounts", []) if item.get("ok")}

    selected = [a for a in accounts if not args.only or a["shop"] in args.only]
    if args.skip_completed:
        selected = [a for a in selected if a["shop"] not in done]
    if not selected:
        print("没有待执行账户。")
        return EXIT_OK

    previous_hint: str | None = None
    failures: list[dict[str, Any]] = []
    for index, account in enumerate(selected, start=1):
        print(f"\n===== [{index}/{len(selected)}] {account['shop']}（{account['account'] or '账号未填'}）=====")
        if not args.skip_login_check:
            snapshot = session_snapshot()
            hint = str(snapshot.get("hint") or "")
            if not snapshot.get("loggedIn"):
                print(json.dumps({
                    "status": "需要登录",
                    "shop": account["shop"],
                    "account": account["account"],
                    "reason": snapshot.get("reason"),
                    "action": (
                        "用 magpie-browser 在当前已打开的浏览器里打开 "
                        f"https://tgcwork.tmall.com/login 登录店铺「{account['shop']}」"
                        f"（账号 {account['account'] or '未填'}）的天猫账号"
                        f"（复用已有标签，没有才 tab_new；不要新开浏览器、不要把密码落盘），"
                        "登录后用 scripts/ensure_login.py --wait 30 确认 "
                        "loggedIn=true，再重跑本命令。"
                    ),
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            expected = account.get("accountHint")
            if expected and hint != expected:
                print(json.dumps({
                    "status": "登录态与店铺不匹配",
                    "shop": account["shop"],
                    "expectedHint": expected,
                    "currentHint": hint,
                    "action": (
                        "当前浏览器登录的不是这家店。用 magpie-browser 在该标签页退出并登录"
                        f"店铺「{account['shop']}」（账号 {account['account'] or '未填'}），"
                        "用 ensure_login.py --wait 30 确认后重跑本命令"
                    ),
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            if previous_hint is not None and hint == previous_hint:
                print(json.dumps({
                    "status": "登录态没有切换",
                    "shop": account["shop"],
                    "currentHint": hint,
                    "action": (
                        "仍是上一个账户的登录态。用 magpie-browser 在当前浏览器里退出该账号，"
                        f"改登店铺「{account['shop']}」（账号 {account['account'] or '未填'}），"
                        "用 ensure_login.py --wait 30 确认后再重跑本命令"
                    ),
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            previous_hint = hint
            print(f"登录态校验通过：当前账号 {hint}")

        result = run_account(account, output_root)
        state.setdefault("accounts", [])
        state["accounts"] = [item for item in state["accounts"] if item.get("shop") != account["shop"]]
        state["accounts"].append(result)
        save_batch_state(state_path, state)

        if result.get("ok"):
            print(f"完成：{account['shop']} → {result['fineFile']}")
        elif result.get("needLogin"):
            print(json.dumps({
                "status": "需要登录",
                "shop": account["shop"],
                "account": account["account"],
                "action": "导出过程中登录态失效，请重新登录该账号后重跑本命令",
            }, ensure_ascii=False, indent=2))
            return EXIT_NEED_LOGIN
        else:
            failures.append(result)
            print(f"失败：{account['shop']}，详见 {result['outputDir']}/logs", file=sys.stderr)
            if not args.continue_on_error:
                print("已停止后续账户；已完成账户的产物全部保留。", file=sys.stderr)
                return EXIT_ACCOUNT_FAILED

    print("\n===== 多账户执行汇总 =====")
    for item in state.get("accounts", []):
        mark = "✓" if item.get("ok") else "✗"
        print(f"{mark} {item['shop']}｜{item.get('fineFile')}")
    print(f"批次状态：{state_path}")
    return EXIT_ACCOUNT_FAILED if failures else EXIT_OK


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
