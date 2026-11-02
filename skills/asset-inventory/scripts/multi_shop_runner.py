#!/usr/bin/env python3
"""按用户给定顺序，一个店铺一个店铺地跑完资产盘算全流程。

设计要点：
- 一次只处理一个店铺：登录态、下载目录、盘算文件、交付文件夹、状态文件全部按店铺隔离。
- 店铺之间严格串行，前一个店铺完整跑完（下载 → 导入模板 → 交付 → 状态）才进入下一个。
- 任一店铺失败立即停止后续店铺，已完成的店铺产物原样保留，可用 `--only` 或 `--skip-completed` 续跑。
- 不接收、不保存密码：登录由调用者在浏览器里完成，本脚本只校验“当前登录态是否真的换成了这家店”。
- 不写任何凭据：状态文件只记录店铺名、日期、完成进度和结果摘要。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SKILL_ROOT_DEFAULT = Path.home() / ".agents" / "skills" / "asset-inventory"


def _resolve_scripts() -> tuple[Path, Path]:
    """定位技能脚本目录，允许脚本被复制到技能目录之外运行。"""
    override = os.environ.get("ASSET_INVENTORY_SKILL")
    candidates = [Path(override).expanduser() if override else None, SKILL_ROOT_DEFAULT, Path(__file__).resolve().parent.parent]
    for candidate in candidates:
        if candidate and (candidate / "scripts" / "tmall_bill_download.py").is_file():
            return candidate / "scripts", candidate
    raise RuntimeError("未找到 asset-inventory 技能目录；请设置 ASSET_INVENTORY_SKILL")


SCRIPT_DIR, SKILL_ROOT = _resolve_scripts()
DOWNLOAD_SCRIPT = SCRIPT_DIR / "tmall_bill_download.py"
BUILDER_SCRIPT = SCRIPT_DIR / "pan_suan_builder.py"
TEMPLATE = SKILL_ROOT / "assets" / "盘算模板.xlsx"

EXIT_NEED_LOGIN = 3
EXIT_SHOP_FAILED = 4


def safe_prefix(name: str) -> str:
    text = str(name or "").strip()
    for char in ('/', '\\', ':', '*', '?', '"', '<', '>', '|', '\n', '\r', '\t'):
        text = text.replace(char, "-")
    return text.strip(" .-")


def load_shops(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"店铺清单无法读取：{path}") from exc
    shops = data.get("shops") if isinstance(data, dict) else data
    if not isinstance(shops, list) or not shops:
        raise ValueError("店铺清单必须是非空数组，或含 shops 数组的对象")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(shops, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个店铺不是对象")
        shop = str(item.get("shop") or item.get("shopName") or "").strip()
        if not shop:
            raise ValueError(f"第 {index} 个店铺缺少 shop（店铺名称）")
        pay_start = item.get("orderPayStart") or item.get("payStart")
        pay_end = item.get("orderPayEnd") or item.get("payEnd")
        if not pay_start or not pay_end:
            raise ValueError(f"店铺 {shop} 缺少 orderPayStart/orderPayEnd")
        normalized.append({
            "shop": shop,
            "account": str(item.get("account") or "").strip(),
            "accountHint": str(item.get("accountHint") or "").strip(),
            "orderPayStart": str(pay_start),
            "orderPayEnd": str(pay_end),
            "writeOffStart": str(item.get("writeOffStart") or pay_start),
            "writeOffEnd": str(item.get("writeOffEnd") or pay_end),
        })
    return normalized


def session_snapshot() -> dict[str, Any]:
    """读取当前浏览器登录态（不打印、不落盘任何凭据）。"""
    sys.path.insert(0, str(SCRIPT_DIR))
    import tmall_session  # noqa: PLC0415  (延迟导入，避免无登录态时也要加载解密依赖)

    headers = tmall_session.build_headers()
    return {
        "loggedIn": tmall_session.check_session(headers),
        "hint": tmall_session.account_hint(headers),
        "cookieCount": len(headers.get("cookie", "").split("; ")),
    }


def run_step(command: list[str], log_path: Path) -> tuple[int, str]:
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    output = (completed.stdout or "") + (completed.stderr or "")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    return completed.returncode, output


def download_dir(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("asset-inventory-*") if p.is_dir())


def collect_deliverables(shop_root: Path, target_sheet_by_module: dict[str, str]) -> list[tuple[str, Path]]:
    """把每个已下载流程的明细 xlsx 按目标 Sheet 命名，用于交付文件夹。"""
    items: list[tuple[str, Path]] = []
    for directory in download_dir(shop_root):
        module_key = directory.name[len("asset-inventory-"):].rsplit("-", 2)[0]
        sheet = target_sheet_by_module.get(module_key)
        if not sheet:
            continue
        candidates = sorted(
            (p for p in directory.rglob("*.xlsx") if p.is_file()),
            key=lambda p: ("_extracted" not in str(p), str(p)),
        )
        if candidates:
            items.append((sheet, candidates[0]))
    return items


def run_shop(shop: dict[str, Any], output_root: Path, options: argparse.Namespace) -> dict[str, Any]:
    name = shop["shop"]
    prefix = safe_prefix(name)
    shop_root = (output_root / prefix).resolve()
    shop_root.mkdir(parents=True, exist_ok=True)
    pan_output = shop_root / f"{prefix}_{shop['writeOffStart']}_{shop['writeOffEnd']}.xlsx"
    deliver_dir = shop_root / f"{prefix}_{shop['writeOffStart']}_{shop['writeOffEnd']}_输出"
    logs = shop_root / "logs"

    result: dict[str, Any] = {
        "shop": name,
        "account": shop["account"],
        "outputDir": str(shop_root),
        "panSuanOutput": str(pan_output),
        "deliverDir": str(deliver_dir),
        "dates": {
            "writeOffTimeStart": shop["writeOffStart"],
            "writeOffTimeEnd": shop["writeOffEnd"],
            "orderPayTimeStart": shop["orderPayStart"],
            "orderPayTimeEnd": shop["orderPayEnd"],
        },
        "stages": {},
    }

    # 阶段1：下载全部流程（不写盘算），避免中途失败留下半成品工作簿。
    if options.reuse_downloads and download_dir(shop_root):
        result["stages"]["download"] = {"skipped": "复用已有下载目录"}
    else:
        command = [
            sys.executable, str(DOWNLOAD_SCRIPT),
            "--shop-name", name,
            "--write-off-start", shop["writeOffStart"], "--write-off-end", shop["writeOffEnd"],
            "--order-pay-start", shop["orderPayStart"], "--order-pay-end", shop["orderPayEnd"],
            "--output-dir", str(shop_root), "--no-pan-suan",
        ]
        if options.module != "all":
            command.extend(["--module", options.module])
        code, output = run_step(command, logs / "1-download.log")
        if code != 0:
            result["stages"]["download"] = {"ok": False, "tail": output.strip()[-1500:]}
            result["ok"] = False
            return result
        result["stages"]["download"] = {"ok": True, "log": str(logs / "1-download.log")}

    directories = download_dir(shop_root)
    if not directories:
        result["stages"]["panSuan"] = {"ok": False, "reason": "没有 asset-inventory-* 下载目录"}
        result["ok"] = False
        return result

    # 阶段2：一次性导入模板并刷新公司汇总。
    command = [
        sys.executable, str(BUILDER_SCRIPT),
        "--template", str(TEMPLATE),
        "--output", str(pan_output),
        "--shop-name", name,
        "--strict", "--json", "--no-recalculate",
    ]
    for directory in directories:
        command.extend(["--input", str(directory)])
    code, output = run_step(command, logs / "2-pan-suan.log")
    if code != 0:
        result["stages"]["panSuan"] = {"ok": False, "tail": output.strip()[-1500:]}
        result["ok"] = False
        return result
    try:
        merge = json.loads(output[output.index("{"):output.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        merge = {}
    result["stages"]["panSuan"] = {
        "ok": True,
        "productCount": merge.get("product_count"),
        "allocatedRows": merge.get("allocated_rows"),
        "unassignedRows": merge.get("unassigned_rows"),
        "sheetWriteVerified": merge.get("sheet_write_verified"),
        "presentationVerified": merge.get("presentation_verified"),
        "skippedFiles": merge.get("skipped_files", []),
        "log": str(logs / "2-pan-suan.log"),
    }

    # 交付文件夹：按目标 Sheet 命名的明细 + 盘算文件副本。
    target_sheet_by_module = {
        key: module["target_sheet"]
        for key, module in _modules().items()
    }
    deliver_dir.mkdir(parents=True, exist_ok=True)
    delivered: list[str] = []
    for sheet, source in collect_deliverables(shop_root, target_sheet_by_module):
        destination = deliver_dir / f"{sheet}.xlsx"
        shutil.copy2(source, destination)
        delivered.append(str(destination))
    if pan_output.exists():
        shutil.copy2(pan_output, deliver_dir / pan_output.name)
        delivered.append(str(deliver_dir / pan_output.name))
    result["stages"]["deliver"] = {"ok": bool(delivered), "files": delivered}
    result["ok"] = bool(result["stages"].get("panSuan", {}).get("ok"))
    return result


def _modules() -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(SCRIPT_DIR))
    import tmall_bill_download  # noqa: PLC0415

    return tmall_bill_download.MODULES


def load_batch_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "shops": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "shops": []}
    return state if isinstance(state, dict) else {"version": 1, "shops": []}


def save_batch_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def parse_shop_spec(spec: str) -> dict[str, Any]:
    """解析 `店铺名,账号,支付开始,支付结束[,结算开始,结算结束[,账号提示]]`。"""
    parts = [part.strip() for part in spec.split(",")]
    if len(parts) < 4:
        raise ValueError(f"--shop 至少需要 店铺名,账号,支付开始,支付结束：{spec}")
    shop, account, pay_start, pay_end = parts[0], parts[1], parts[2], parts[3]
    write_start = parts[4] if len(parts) > 4 and parts[4] else pay_start
    write_end = parts[5] if len(parts) > 5 and parts[5] else pay_end
    hint = parts[6] if len(parts) > 6 else ""
    return {
        "shop": shop, "account": account, "accountHint": hint,
        "orderPayStart": pay_start, "orderPayEnd": pay_end,
        "writeOffStart": write_start, "writeOffEnd": write_end,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="按顺序一个店铺一个店铺地执行资产盘算")
    p.add_argument("--shops-file", type=Path, help="店铺清单 JSON（含店铺名、账号、日期）")
    p.add_argument(
        "--shop", action="append", default=[], metavar="店名,账号,支付开始,支付结束[,结算开始,结算结束[,账号提示]]",
        help="直接在命令行给出一个店铺；可重复，顺序即执行顺序",
    )
    p.add_argument("--output-root", type=Path, help="多店输出根目录，每个店铺一个子目录")
    p.add_argument("--only", action="append", default=[], help="只跑指定店铺（可重复），按清单顺序")
    p.add_argument("--skip-completed", action="store_true", help="跳过批次状态文件里已完成的店铺")
    p.add_argument("--module", default="all", help="默认 all；仅在用户明确限定单流程时传单流程 key")
    p.add_argument("--reuse-downloads", action="store_true", help="复用店铺目录里已有的下载结果，不重新下载")
    p.add_argument("--skip-login-check", action="store_true", help="跳过登录态校验（仅调试用）")
    p.add_argument("--continue-on-error", action="store_true", help="某店失败后继续下一店（默认立即停止）")
    p.add_argument("--check-login", action="store_true", help="只检查当前浏览器登录态并输出账号提示，不执行任何流程")
    args = p.parse_args()
    if args.check_login:
        return args
    if not args.shop and not args.shops_file:
        p.error("需要 --shop 或 --shops-file 提供店铺")
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
        }, ensure_ascii=False, indent=2))
        return 0 if snapshot.get("loggedIn") else EXIT_NEED_LOGIN

    shops = load_shops(args.shops_file) if args.shops_file else []
    shops.extend(parse_shop_spec(spec) for spec in args.shop)
    output_root = args.output_root.expanduser().resolve()
    state_path = output_root / "multi-shop-state.json"
    state = load_batch_state(state_path)
    done = {item.get("shop") for item in state.get("shops", []) if item.get("ok")}

    selected = [s for s in shops if not args.only or s["shop"] in args.only]
    if args.skip_completed:
        selected = [s for s in selected if s["shop"] not in done]
    if not selected:
        print("没有待执行店铺。")
        return 0

    previous_hint: str | None = None
    failures: list[dict[str, Any]] = []
    for index, shop in enumerate(selected, start=1):
        print(f"\n===== [{index}/{len(selected)}] {shop['shop']} =====")
        if not args.skip_login_check:
            snapshot = session_snapshot()
            hint = str(snapshot.get("hint") or "")
            if not snapshot.get("loggedIn"):
                print(json.dumps({
                    "status": "需要登录",
                    "shop": shop["shop"],
                    "account": shop["account"],
                    "action": f"请在 Chrome 中登录店铺「{shop['shop']}」的天猫账号后重跑本命令",
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            expected = shop.get("accountHint")
            if expected and hint != expected:
                print(json.dumps({
                    "status": "登录态与店铺不匹配",
                    "shop": shop["shop"],
                    "expectedHint": expected,
                    "currentHint": hint,
                    "action": "当前浏览器登录的不是这家店，请切换账号后重跑",
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            if previous_hint is not None and hint == previous_hint and not args.reuse_downloads:
                print(json.dumps({
                    "status": "登录态没有切换",
                    "shop": shop["shop"],
                    "currentHint": hint,
                    "action": "仍是上一个店铺的登录态，请先在浏览器切换到这家店的账号",
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            previous_hint = hint
            print(f"登录态校验通过：当前账号 {hint}")

        result = run_shop(shop, output_root, args)
        state.setdefault("shops", [])
        state["shops"] = [item for item in state["shops"] if item.get("shop") != shop["shop"]]
        state["shops"].append(result)
        save_batch_state(state_path, state)

        if result.get("ok"):
            print(f"完成：{shop['shop']} → {result['panSuanOutput']}")
        else:
            failures.append(result)
            print(f"失败：{shop['shop']}，详见 {result['outputDir']}/logs", file=sys.stderr)
            if not args.continue_on_error:
                print("已停止后续店铺；已完成店铺的产物全部保留。", file=sys.stderr)
                return EXIT_SHOP_FAILED

    print("\n===== 多店执行汇总 =====")
    for item in state["shops"]:
        mark = "✓" if item.get("ok") else "✗"
        count = ((item.get("stages", {}).get("panSuan") or {}).get("productCount"))
        print(f"{mark} {item['shop']}｜商品数 {count}｜{item.get('panSuanOutput')}")
    print(f"批次状态：{state_path}")
    return EXIT_SHOP_FAILED if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
