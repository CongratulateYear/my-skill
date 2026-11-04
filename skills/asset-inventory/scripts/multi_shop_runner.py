#!/usr/bin/env python3
"""按用户给定顺序，一个店铺一个店铺地跑完资产盘算全流程。

设计要点：
- 一次只处理一个店铺：登录态、下载目录、盘算文件、交付文件夹、状态文件全部按店铺隔离。
- 店铺之间严格串行，前一个店铺完整跑完（下载 → 导入模板 → 交付 → 状态）才进入下一个。
- 任一店铺失败立即停止后续店铺，已完成的店铺产物原样保留，可用 `--only` 或 `--skip-completed` 续跑。
- 不接收、不保存密码：密码绝不落盘、绝不从钥匙串或 Chrome Login Data 挖；登录由 agent
  用 magpie-browser 在【当前已打开的浏览器】里复用（没有才新开）tgcwork.tmall.com 标签、
  驱动表单完成。本脚本只校验“当前登录态是否真的换成了这家店”。
- 不写任何凭据：状态文件只记录店铺名、日期、完成进度和结果摘要。
- 公司最终结果（各店盘算工作簿）汇总到 <output-root>/资产盘算_<结算开始>_<结算结束>/，
  店铺明细留在各自的 _输出 文件夹，不再重复放盘算副本。
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
import time
from inventory_contract import (atomic_json, context, bind, checkpoint, validate_download_state, validate_builder, verify_files, verify_external, manifest_path)
from pathlib import Path
from typing import Any

SKILL_ROOT_DEFAULT = Path.home() / ".agents" / "skills" / "asset-inventory"


def _resolve_scripts() -> tuple[Path, Path]:
    """定位技能脚本目录，允许脚本被复制到技能目录之外运行。"""
    override = os.environ.get("ASSET_INVENTORY_SKILL")
    candidates = [Path(override).expanduser() if override else None, Path(__file__).resolve().parent.parent, SKILL_ROOT_DEFAULT]
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
        supplier_id_sec = item.get("supplierIdSec")
        normalized.append({
            "shop": shop,
            "account": str(item.get("account") or "").strip(),
            "accountHint": str(item.get("accountHint") or "").strip(),
            "supplierIdSec": str(supplier_id_sec).strip() if supplier_id_sec not in (None, "") else "",
            "companyName": str(item.get("companyName") or "").strip(),
            "orderPayStart": str(pay_start),
            "orderPayEnd": str(pay_end),
            "writeOffStart": str(item.get("writeOffStart") or pay_start),
            "writeOffEnd": str(item.get("writeOffEnd") or pay_end),
        })
    return normalized


def session_snapshot(browser: str = "auto", transport: str = "auto",
                     bridge_url: str | None = None) -> dict[str, Any]:
    """读取当前浏览器登录态（不打印、不落盘任何凭据）。

    transport=auto 时 cookie 库优先、被 v20 加密或文件独占挡住就回退浏览器桥，
    因此这里传的是 "auto"，不再硬编码 chrome。
    """
    sys.path.insert(0, str(SCRIPT_DIR))
    import ensure_login  # noqa: PLC0415  (延迟导入，避免无登录态时也要加载解密依赖)

    state = ensure_login.probe(browser, "tgc.tmall.com", transport, bridge_url)
    return {
        "loggedIn": bool(state.get("loggedIn")),
        "hint": str(state.get("accountHint") or ""),
        "cookieCount": int(state.get("cookieCount") or 0),
        "reason": str(state.get("reason") or ""),
        "action": str(state.get("action") or ""),
        "transport": str(state.get("transport") or ""),
    }


def current_identity(bridge_url: str | None = None) -> dict[str, Any]:
    """问平台「当前登录的是哪家供应商」。

    cookie 里的 tracknick 可能残留上一个账号（实测换号后不刷新），不能用来判断
    登录的是哪家店；这里读 supplierHome/accountInfo，拿 companyName/supplierIdSec。
    读不到就返回空身份，由调用方按「无法确认身份」处理。
    """
    sys.path.insert(0, str(SCRIPT_DIR))
    import supplier_identity  # noqa: PLC0415  (延迟导入，避免无登录态时也要加载依赖)

    try:
        return supplier_identity.read_identity("bridge", bridge_url)
    except Exception:  # noqa: BLE001  (桥未接入 / 会话抖动都只算「没读到」)
        return {}


def identity_label(identity: dict[str, Any]) -> str:
    return str(identity.get("companyName") or identity.get("supplierName") or identity.get("supplierIdSec") or "")


def identity_matches(shop: dict[str, Any], identity: dict[str, Any]) -> bool:
    """店铺声明的 supplierIdSec/companyName 与实际登录身份比对；未声明则放行。"""
    expected_id = str(shop.get("supplierIdSec") or "")
    expected_name = str(shop.get("companyName") or "")
    actual_id = str(identity.get("supplierIdSec") or "")
    actual_name = identity_label(identity)
    if expected_id:
        return actual_id == expected_id
    if expected_name:
        return actual_name == expected_name
    return True


def run_step(command: list[str], log_path: Path) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    started = time.monotonic()
    print(f"开始阶段：{log_path.stem}", flush=True)
    # Files avoid pipe capture, retain live logs and keep stderr separate.
    with log_path.open("w", encoding="utf-8") as stdout, log_path.with_suffix(".stderr.log").open("w", encoding="utf-8") as stderr:
        completed = subprocess.run(command, stdout=stdout, stderr=stderr, env=env, check=False)
    output = log_path.read_text(encoding="utf-8")
    errors = log_path.with_suffix(".stderr.log").read_text(encoding="utf-8")
    print(f"阶段结束：{log_path.stem}，耗时 {time.monotonic()-started:.1f}s，退出码 {completed.returncode}", flush=True)
    return completed.returncode, output + errors


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
    # 公司最终结果（盘算工作簿）统一汇总到输出根目录下按账期命名的文件夹，各店铺明细留在自己的子目录。
    summary_dir = (output_root / f"资产盘算_{shop['writeOffStart']}_{shop['writeOffEnd']}").resolve()
    summary_output = summary_dir / pan_output.name
    logs = shop_root / "logs"
    ctx = context(shop, options.module)
    saved = bind(shop_root, ctx)
    modules = list(_modules()) if options.module == "all" else [options.module]
    if any(m not in _modules() for m in modules): raise ValueError("未知模块")
    state_file = shop_root / f"{prefix}_{shop['writeOffStart']}_{shop['writeOffEnd']}.asset-inventory-state.json"


    result: dict[str, Any] = {
        "shop": name,
        "account": shop["account"],
        "outputDir": str(shop_root),
        "panSuanOutput": str(pan_output),
        "deliverDir": str(deliver_dir),
        "summaryDir": str(summary_dir),
        "summaryOutput": str(summary_output),
        "dates": {
            "writeOffTimeStart": shop["writeOffStart"],
            "writeOffTimeEnd": shop["writeOffEnd"],
            "orderPayTimeStart": shop["orderPayStart"],
            "orderPayTimeEnd": shop["orderPayEnd"],
        },
        "stages": {},
    }

    # 阶段1：下载全部流程（不写盘算），避免中途失败留下半成品工作簿。
    if options.reuse_downloads:
        if not saved.get("files"): raise ValueError("没有可验证的下载清单")
        validate_download_state(shop_root, shop, modules)
        result["stages"]["download"] = {"ok": True, "skipped": "已验证清单、身份、账期和文件摘要"}
    else:
        command = [
            sys.executable, str(DOWNLOAD_SCRIPT),
            "--shop-name", name,
            "--write-off-start", shop["writeOffStart"], "--write-off-end", shop["writeOffEnd"],
            "--order-pay-start", shop["orderPayStart"], "--order-pay-end", shop["orderPayEnd"],
            "--output-dir", str(shop_root), "--no-pan-suan",
        ]
        command.extend(["--browser", options.browser])
        if options.module != "all":
            command.extend(["--module", options.module])
        if getattr(options, "transport", "auto") != "auto":
            command.extend(["--transport", options.transport])
        if getattr(options, "bridge_url", ""):
            command.extend(["--bridge-url", options.bridge_url])
        if state_file.exists(): command.append("--resume")
        code, output = run_step(command, logs / "1-download.log")
        checkpoint(shop_root, ctx)
        if code != 0:
            result["stages"]["download"] = {"ok": False, "tail": output.strip()[-1500:]}
            result["ok"] = False
            return result
        result["stages"]["download"] = {"ok": True, "log": str(logs / "1-download.log")}

    download_state, directories = validate_download_state(shop_root, shop, modules)
    if not directories and any(r.get("queryRows") != 0 for r in download_state["results"].values()):
        result["stages"]["panSuan"] = {"ok": False, "reason": "没有 asset-inventory-* 下载目录"}
        result["ok"] = False
        return result

    # 阶段2：一次性导入模板并刷新公司汇总。
    result_json = logs / "builder-result.json"
    command = [
        sys.executable, str(BUILDER_SCRIPT),
        "--template", str(TEMPLATE),
        "--output", str(pan_output),
        "--shop-name", name,
        "--strict", "--json", "--no-recalculate", "--result-json", str(result_json),
    ]
    for directory in directories:
        command.extend(["--input", str(directory)])
    code, output = run_step(command, logs / "2-pan-suan.log")
    if code != 0:
        result["stages"]["panSuan"] = {"ok": False, "tail": output.strip()[-1500:]}
        result["ok"] = False
        return result
    merge = validate_builder(json.loads(result_json.read_text(encoding="utf-8")))
    if not pan_output.is_file(): raise ValueError("构建成功但工作簿不存在")
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

    # Independent source/structure validation must pass before delivery.
    validation_path = shop_root / "独立校验.json"
    validation_command = [
        sys.executable, str(SCRIPT_DIR / "validate_asset_workbook.py"),
        "--workbook", str(pan_output), "--shop", name,
        "--expected-products", str(merge["product_count"]), "--report", str(validation_path),
    ]
    for module in modules: validation_command.extend(["--expected-modules", module])
    code, output = run_step(validation_command, logs / "3-validation.log")
    validation = json.loads(validation_path.read_text(encoding="utf-8")) if validation_path.exists() else {}
    if code != 0 or validation.get("ok") is not True:
        result["stages"]["validation"] = {"ok": False, "report": str(validation_path)}
        result["ok"] = False
        return result
    result["stages"]["validation"] = {"ok": True, "report": str(validation_path)}
    from asset_report import build_report, write_report
    report = build_report(download_state, merge, validation, shop=name, workbook=str(pan_output))
    result["report"] = write_report(report, shop_root / "执行报告.json", shop_root / "执行报告.md")
    deliver_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)
    delivered = []
    for module in modules:
        record = download_state["results"][module]
        if not record.get("outputDir"): continue
        folder = Path(record["outputDir"])
        candidates = sorted(folder.rglob("*.xlsx"), key=lambda p: ("_extracted" not in str(p), str(p))) if folder.is_dir() else []
        # One output file per sheet is only valid when there is one source workbook.
        for index, source in enumerate(candidates, 1):
            suffix = f"_{index}" if len(candidates) > 1 else ""
            destination = deliver_dir / f"{_modules()[module]['target_sheet']}{suffix}.xlsx"
            shutil.copy2(source, destination)
            delivered.append(str(destination))
    # 盘算工作簿（公司最终结果）只放进汇总文件夹，店铺目录里保留工作副本，不再在明细目录重复一份。
    shutil.copy2(pan_output, summary_output)
    if not filecmp.cmp(pan_output, summary_output, shallow=False): raise ValueError("汇总文件夹的盘算副本与工作副本不一致")
    result["stages"]["deliver"] = {"ok": True, "files": delivered, "workbooks": [str(summary_output)]}
    result["ok"] = True
    atomic_json(shop_root / "execution-result.json", result)
    checkpoint(shop_root, ctx, complete=True, deliverables=delivered + [str(pan_output), str(validation_path)], external=[str(summary_output)])
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
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("批次状态损坏，拒绝忽略") from exc
    return state if isinstance(state, dict) else {"version": 1, "shops": []}


def save_batch_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def parse_shop_spec(spec: str) -> dict[str, Any]:
    """解析 `店铺名,账号,支付开始,支付结束[,结算开始,结算结束[,supplierIdSec[,公司名]]]`。

    第7段若是纯数字按 supplierIdSec 处理，否则按老口径当 accountHint（cookie 提示）。
    第8段为公司名，可省略。
    """
    parts = [part.strip() for part in spec.split(",")]
    if len(parts) < 4:
        raise ValueError(f"--shop 至少需要 店铺名,账号,支付开始,支付结束：{spec}")
    shop, account, pay_start, pay_end = parts[0], parts[1], parts[2], parts[3]
    write_start = parts[4] if len(parts) > 4 and parts[4] else pay_start
    write_end = parts[5] if len(parts) > 5 and parts[5] else pay_end
    extra = parts[6] if len(parts) > 6 else ""
    company = parts[7] if len(parts) > 7 else ""
    supplier_id_sec = extra if extra.isdigit() else ""
    hint = "" if extra.isdigit() else extra
    return {
        "shop": shop, "account": account, "accountHint": hint,
        "supplierIdSec": supplier_id_sec, "companyName": company,
        "orderPayStart": pay_start, "orderPayEnd": pay_end,
        "writeOffStart": write_start, "writeOffEnd": write_end,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="按顺序一个店铺一个店铺地执行资产盘算")
    p.add_argument("--shops-file", type=Path, help="店铺清单 JSON（含店铺名、账号、日期）")
    p.add_argument(
        "--shop", action="append", default=[], metavar="店名,账号,支付开始,支付结束[,结算开始,结算结束[,supplierIdSec[,公司名]]]",
        help="直接在命令行给出一个店铺；可重复，顺序即执行顺序",
    )
    p.add_argument("--output-root", type=Path, help="多店输出根目录，每个店铺一个子目录")
    p.add_argument("--only", action="append", default=[], help="只跑指定店铺（可重复），按清单顺序")
    p.add_argument("--skip-completed", action="store_true", help="跳过批次状态文件里已完成的店铺")
    p.add_argument("--module", default="all", help="默认 all；仅在用户明确限定单流程时传单流程 key")
    p.add_argument("--reuse-downloads", action="store_true", help="复用店铺目录里已有的下载结果，不重新下载")
    p.add_argument("--skip-login-check", action="store_true", help="已禁用；生产执行始终要求身份校验")
    p.add_argument("--continue-on-error", action="store_true", help="某店失败后继续下一店（默认立即停止）")
    p.add_argument("--check-login", action="store_true", help="只检查当前浏览器登录态并输出账号提示，不执行任何流程")
    p.add_argument("--verify-identity", action="store_true",
                   help="即使店铺清单没写 supplierIdSec/companyName，也读 accountInfo 核对当前登录的是哪家店")
    p.add_argument("--browser", default="auto",
                   help="读哪个浏览器的 cookie 库，默认 auto（Windows 上依次试 Edge/Chrome）")
    p.add_argument("--transport", default="auto", choices=["auto", "cookie", "bridge"],
                   help="登录态来源：auto=cookie 库优先、失败自动回退浏览器桥（默认）")
    p.add_argument("--bridge-url", default="",
                   help="浏览器桥地址，默认 http://127.0.0.1:8899（脚本会自动起桥）")
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
        snapshot = session_snapshot(args.browser, args.transport, args.bridge_url or None)
        identity = current_identity(args.bridge_url or None) if snapshot.get("loggedIn") else {}
        print(json.dumps({
            "loggedIn": snapshot.get("loggedIn"),
            "currentHint": snapshot.get("hint"),
            "companyName": identity.get("companyName"),
            "supplierIdSec": identity.get("supplierIdSec"),
            "cookieCount": snapshot.get("cookieCount"),
            "reason": snapshot.get("reason"),
            "transport": snapshot.get("transport"),
            "action": snapshot.get("action"),
        }, ensure_ascii=False, indent=2))
        return 0 if snapshot.get("loggedIn") else EXIT_NEED_LOGIN

    shops = load_shops(args.shops_file) if args.shops_file else []
    shops.extend(parse_shop_spec(spec) for spec in args.shop)
    output_root = args.output_root.expanduser().resolve()
    state_path = output_root / "multi-shop-state.json"
    state = load_batch_state(state_path)
    if args.skip_login_check: raise ValueError("生产执行禁止 --skip-login-check")
    prefixes = [safe_prefix(s["shop"]) for s in shops]
    if any(not p for p in prefixes) or len(prefixes) != len(set(prefixes)):
        raise ValueError("店铺名称为空或映射到相同目录，拒绝混合产物")
    done = set()
    if args.skip_completed:
        for shop in shops:
            root = output_root / safe_prefix(shop["shop"])
            if manifest_path(root).exists():
                saved = bind(root, context(shop, args.module))
                if saved.get("complete") and saved.get("deliverables"):
                    verify_files(root, saved["deliverables"])
                    verify_external(saved.get("externalDeliverables") or {})
                    done.add(shop["shop"])

    selected = [s for s in shops if not args.only or s["shop"] in args.only]
    if args.skip_completed:
        selected = [s for s in selected if s["shop"] not in done]
    if not selected:
        print("没有待执行店铺。")
        return 0

    previous_identity: str | None = None
    failures: list[dict[str, Any]] = []
    for index, shop in enumerate(selected, start=1):
        print(f"\n===== [{index}/{len(selected)}] {shop['shop']} =====")
        if not args.skip_login_check:
            snapshot = session_snapshot(args.browser, args.transport, args.bridge_url or None)
            hint = str(snapshot.get("hint") or "")
            if not snapshot.get("loggedIn"):
                print(json.dumps({
                    "status": "需要登录",
                    "shop": shop["shop"],
                    "account": shop["account"],
                    "reason": snapshot.get("reason"),
                    "action": (
                        "用 magpie-browser 在当前已打开的浏览器里打开 "
                        f"https://tgcwork.tmall.com/login 登录店铺「{shop['shop']}」"
                        f"（账号 {shop['account']}）的天猫账号"
                        f"（复用已有标签，没有才 tab_new；不要新开浏览器、不要把密码落盘），"
                        "登录后先用 ensure_login.py --wait 30 确认 loggedIn=true，再重跑本命令。"
                    ),
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            declares_identity = bool(shop.get("supplierIdSec") or shop.get("companyName"))
            identity = current_identity(args.bridge_url or None) if declares_identity or args.verify_identity else {}
            actual_id = str(identity.get("supplierIdSec") or "")
            actual_name = identity_label(identity)
            if declares_identity and not identity:
                print(json.dumps({
                    "status": "无法确认当前登录店铺",
                    "shop": shop["shop"],
                    "expectedCompanyName": shop.get("companyName"),
                    "expectedSupplierIdSec": shop.get("supplierIdSec") or None,
                    "currentHint": hint,
                    "action": (
                        "读不到 accountInfo（桥可能掉线或会话过期）。先在已登录的 tgc.tmall.com 标签里"
                        "重新注入 scripts/bridge_agent.js，确认 supplier_identity.py 能返回公司名后重跑本命令"
                    ),
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            if declares_identity and not identity_matches(shop, identity):
                print(json.dumps({
                    "status": "登录态与店铺不匹配",
                    "shop": shop["shop"],
                    "expectedCompanyName": shop.get("companyName") or None,
                    "expectedSupplierIdSec": shop.get("supplierIdSec") or None,
                    "currentCompanyName": actual_name or None,
                    "currentSupplierIdSec": actual_id or None,
                    "currentHint": hint,
                    "action": (
                        "当前浏览器登录的不是这家店。用 magpie-browser 在该标签页退出并登录"
                        f"店铺「{shop['shop']}」（账号 {shop['account']}），"
                        "ensure_login.py --wait 30 确认、supplier_identity.py 核对公司名后重跑本命令"
                    ),
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            expected = shop.get("accountHint")
            if expected and not declares_identity and hint != expected:
                print(json.dumps({
                    "status": "登录态与店铺不匹配",
                    "shop": shop["shop"],
                    "expectedHint": expected,
                    "currentHint": hint,
                    "action": (
                        "当前浏览器登录的不是这家店。用 magpie-browser 在该标签页退出并登录"
                        f"店铺「{shop['shop']}」（账号 {shop['account']}），"
                        "ensure_login.py --wait 30 确认后重跑本命令"
                    ),
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            fingerprint = actual_id or actual_name or hint
            if previous_identity is not None and fingerprint == previous_identity and not args.reuse_downloads:
                print(json.dumps({
                    "status": "登录态没有切换",
                    "shop": shop["shop"],
                    "currentCompanyName": actual_name or None,
                    "currentHint": hint,
                    "action": (
                        "仍是上一个店铺的登录态。用 magpie-browser 在当前浏览器里退出该账号，"
                        f"改登店铺「{shop['shop']}」（账号 {shop['account']}），"
                        "ensure_login.py --wait 30 确认后再重跑本命令"
                    ),
                }, ensure_ascii=False, indent=2))
                return EXIT_NEED_LOGIN
            previous_identity = fingerprint
            if actual_name or actual_id:
                print(f"登录态校验通过：{actual_name or actual_id}（{shop['shop']}）")
            else:
                print(f"登录态校验通过：当前账号 {hint}")

        try:
            result = run_shop(shop, output_root, args)
        except (ValueError, OSError, RuntimeError) as exc:
            result = {"shop": shop["shop"], "ok": False, "outputDir": str(output_root / safe_prefix(shop["shop"])), "error": str(exc)}
        if not args.skip_login_check and (actual_name or actual_id):
            result["verifiedIdentity"] = {"companyName": actual_name or None, "supplierIdSec": actual_id or None}
        state.setdefault("shops", [])
        state["shops"] = [item for item in state["shops"] if item.get("shop") != shop["shop"]]
        state["shops"].append(result)
        order = {s["shop"]: i for i,s in enumerate(shops)}
        state["shops"].sort(key=lambda item: order.get(item["shop"], len(order)))
        save_batch_state(state_path, state)

        if result.get("ok"):
            print(f"完成：{shop['shop']} → {result.get('summaryOutput') or result['panSuanOutput']}")
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
        print(f"{mark} {item['shop']}｜商品数 {count}｜{item.get('summaryOutput') or item.get('panSuanOutput')}")
    summaries = sorted({str(item["summaryDir"]) for item in state["shops"] if item.get("summaryDir")})
    for folder in summaries: print(f"汇总文件夹：{folder}")
    print(f"批次状态：{state_path}")
    return EXIT_SHOP_FAILED if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
