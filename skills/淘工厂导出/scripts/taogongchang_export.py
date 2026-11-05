#!/usr/bin/env python3
"""淘工厂导出 —— 资产盘算 17 个下载流程的纯导出门面脚本。

公司名称、支付日期、结算日期全部来自用户消息（agent 转成参数），不从登录态解析。

只做三件事：
1. 解析参数：--module 支持中文名（罚款/商品贷款/全部 等），公司名与日期原样透传；
2. 生成唯一输出文件夹 `<输出根>/<公司名称>_<结算开始>_<结算结束>/`
   （只给支付日期时后缀用支付区间）；
3. 调内置的 tmall_bill_download.py（源自 asset-inventory）--no-pan-suan 完成查询、
   创建任务、轮询、下载与安全解压（处罚数据/商品效果导出走各自专属链路）。

本脚本不写盘算工作簿；不读取、不回显 fetch 文件内容（路径直接透传给下载脚本，
cookie 只在下载脚本内存中使用）；不落盘任何凭据。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import uuid
import shutil
import subprocess
import sys
from pathlib import Path

# 下载内核已随本 skill 内置（源自 asset-inventory，--no-pan-suan 纯导出模式）；
# asset-inventory skill 若还装着，其副本作为兼容回退。
DOWNLOAD_SCRIPT = Path(__file__).resolve().with_name("tmall_bill_download.py")
if not DOWNLOAD_SCRIPT.is_file():
    DOWNLOAD_SCRIPT = (Path(__file__).resolve().parents[2] / "asset-inventory"
                       / "scripts" / "tmall_bill_download.py")

# 中文名/别名 -> tmall_bill_download.py 的 --module 值
MODULE_ALIASES = {
    "全部": "all",
    "所有流程": "all",
    "消费者体验提升服务": "consumer-experience-service",
    "商品贷款": "product-loan",
    "商品货款": "product-loan",
    "交易补贴": "trade-subsidy",
    "交易佣金": "trade-commission",
    "先用后付": "credit-buy",
    "新疆物流集运": "xinjiang-logistics-consolidation",
    "评价有礼": "review-gift",
    "营销费用": "channel-promotion",
    "商业化账户充值提现": "ad-agency",
    "充值提现": "ad-agency",
    "投流推广佣金": "all-site-channel-promotion",
    "投流": "all-site-channel-promotion",
    "淘客推广佣金": "taoke-channel-promotion",
    "淘客": "taoke-channel-promotion",
    "直播推广佣金": "live-channel-promotion",
    "直播": "live-channel-promotion",
    "客服服务费": "customer-service-fee",
    "赔付追缴": "compensate-recovery",
    "处罚数据": "punishment-data",
    "罚款": "punishment-data",
    "处罚": "punishment-data",
    "违规": "punishment-data",
    "出海增值服务费": "overseas-added-commission",
    "出海": "overseas-added-commission",
    "所有商品效果数据导出": "all-item-effects-export",
    "商品效果": "all-item-effects-export",
    "效果导出": "all-item-effects-export",
}


# module 值 -> 规范流程名（最终 xlsx 文件名）
MODULE_DISPLAY_NAMES = {
    "consumer-experience-service": "消费者体验提升服务",
    "product-loan": "商品贷款",
    "trade-subsidy": "交易补贴",
    "trade-commission": "交易佣金",
    "credit-buy": "先用后付",
    "xinjiang-logistics-consolidation": "新疆物流集运",
    "review-gift": "评价有礼",
    "channel-promotion": "营销费用",
    "ad-agency": "商业化账户充值提现",
    "all-site-channel-promotion": "投流推广佣金",
    "taoke-channel-promotion": "淘客推广佣金",
    "live-channel-promotion": "直播推广佣金",
    "customer-service-fee": "客服服务费",
    "compensate-recovery": "赔付追缴",
    "punishment-data": "处罚数据",
    "overseas-added-commission": "出海增值服务费",
    "all-item-effects-export": "所有商品效果数据导出",
}


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "-", str(name).strip())
    return cleaned or "未知公司"


def folder_name(company: str, start: str | None, end: str | None) -> str:
    """Public company/date folder naming helper (no filesystem side effects)."""
    suffix = f"{start}_{end}" if start and end else "未给日期"
    return f"{safe_name(company)}_{suffix}"


def build_folder(output_root: Path, company: str, args: argparse.Namespace) -> Path:
    start = args.write_off_start or args.order_pay_start
    end = args.write_off_end or args.order_pay_end
    root = getattr(args, "batch_folder", None) or output_root
    return Path(root).expanduser().resolve() / folder_name(company, start, end)


# 根目录裸文件名前缀 → 流程名（单流程裸跑/历史文件识别）
_ROOT_SPECIALS = (("商品货款", "商品贷款"), ("GEI@", "所有商品效果数据导出"))
ROOT_PREFIX_RULES = tuple(sorted(
    _ROOT_SPECIALS + tuple((name, name) for name in MODULE_DISPLAY_NAMES.values()),
    key=lambda pair: -len(pair[0]),
))


def display_for_path(xlsx: Path, folder: Path, module_key: str | None) -> str | None:
    """Infer ownership before applying a single-module fallback to unknown files."""
    for part in xlsx.relative_to(folder).parts[:-1]:
        m = re.match(r"^asset-inventory-(.+?)-\d{8}-\d{8}$", part)
        if m and m.group(1) in MODULE_DISPLAY_NAMES:
            return MODULE_DISPLAY_NAMES[m.group(1)]
    for prefix, name in ROOT_PREFIX_RULES:
        if xlsx.stem.startswith(prefix):
            return name
    # Unknown files are deliberately NOT attributed to a selected module.
    return None


def _is_final(path: Path, display: str) -> bool:
    return bool(re.fullmatch(re.escape(display) + r"(?:_[2-9]\d*|_1\d+)?", path.stem))


def finalize_folder(folder: Path, module_key: str | None = None, *,
                    output_folder: Path | None = None,
                    modules: list[str] | None = None,
                    outcomes: dict | None = None) -> list[Path]:
    """Publish only owned XLSX files; preserve unrelated files and interrupted work.

    New shards replace the complete old shard set, never append to it. Copies are
    staged before any output is changed; failed publication restores old outputs
    and leaves source files intact. Unknown files and other modules are untouched.
    With output_folder, folder must be an isolated, successfully completed run;
    zero-result selected modules then remove their previous final outputs.
    """
    folder = folder.resolve()
    target_root = (output_folder or folder).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    keys = modules if modules is not None else (
        [module_key] if module_key and module_key != "all" else list(MODULE_DISPLAY_NAMES))
    selected = {MODULE_DISPLAY_NAMES[key] for key in keys}
    groups: dict[str, list[Path]] = {}
    for xlsx in sorted(folder.rglob("*")):
        if any(part.startswith(".") for part in xlsx.relative_to(folder).parts[:-1]):
            continue
        if not xlsx.is_file() or xlsx.is_symlink() or xlsx.suffix.lower() != ".xlsx":
            continue
        if any(parent.is_symlink() for parent in xlsx.parents if parent != folder):
            continue
        display = display_for_path(xlsx, folder, None)
        if display is None and output_folder is not None and len(keys) == 1:
            display = MODULE_DISPLAY_NAMES[keys[0]]
        if display not in selected:
            continue
        if target_root == folder and xlsx.parent == folder and _is_final(xlsx, display):
            continue
        groups.setdefault(display, []).append(xlsx)
    if output_folder is not None:
        if outcomes is None:
            raise ValueError("发布需要显式 completed/zero 状态，不能由缺文件推断零数据")
        groups = {}
        for key in keys:
            result = outcomes.get(key, {})
            status = result.get("status")
            files = result.get("publishFiles")
            if status == "zero" and files == []:
                continue
            if status != "completed" or not isinstance(files, list) or not files:
                raise ValueError(f"{key} 缺少有效发布清单；旧状态须重新导出，不能猜测零数据")
            sources = []
            for raw in files:
                source = Path(raw).resolve()
                raw_path = Path(raw).absolute()
                unsafe_link = False
                for part in [raw_path, *raw_path.parents]:
                    if part.resolve() == folder:
                        break
                    if part.is_symlink():
                        unsafe_link = True
                if (not source.is_relative_to(folder) or not source.is_file()
                        or source.suffix.lower() != ".xlsx" or unsafe_link):
                    raise ValueError(f"{key} 发布源缺失或不安全；保留旧结果")
                sources.append(source)
            groups[MODULE_DISPLAY_NAMES[key]] = sources
    affected = selected if output_folder is not None else set(groups)
    published: list[Path] = []
    # Transaction is inside the company folder (same filesystem on Windows/macOS).
    with tempfile.TemporaryDirectory(prefix=".finalize-", dir=target_root) as temp:
        staging = Path(temp)
        plans: list[tuple[Path, Path]] = []
        for display, sources in groups.items():
            for index, source in enumerate(sources, 1):
                name = f"{display}{'_' + str(index) if index > 1 else ''}.xlsx"
                staged = staging / name
                shutil.copy2(source, staged)
                plans.append((staged, target_root / name))
        old = [p for p in target_root.iterdir() if p.is_file() and not p.is_symlink()
               and p.suffix.lower() == ".xlsx"
               and any(_is_final(p, display) for display in affected)]
        backups: list[tuple[Path, Path]] = []
        try:
            for index, path in enumerate(old):
                backup = staging / f"backup-{index}"
                os.replace(path, backup)
                backups.append((backup, path))
            for staged, target in plans:
                os.replace(staged, target)
                published.append(target)
        except Exception:
            for path in published:
                path.unlink(missing_ok=True)
            for backup, path in backups:
                os.replace(backup, path)
            raise
    # Isolated runs retain ALL sources until main atomically retires the work
    # directory. A cleanup interruption can never leave a resumable partial set.
    if output_folder is None:
        for sources in groups.values():
            for source in sources:
                source.unlink()
    # Do not recursively delete directories: they can contain another module,
    # unknown documents, or resume state. Main owns cleanup of isolated runs.
    return sorted(p for p in target_root.iterdir() if p.is_file()
                  and p.suffix.lower() == ".xlsx"
                  and any(_is_final(p, display) for display in selected))


def main() -> int:
    p = argparse.ArgumentParser(description="淘工厂导出：17 个账单流程纯导出到单一文件夹")
    p.add_argument("--module", default="all",
                   help="流程名（中英文均可），默认 all 全部17个流程")
    p.add_argument("--company-name", required=True,
                   help="公司/店铺名称，取用户消息原文，用于输出文件夹命名")
    p.add_argument("--write-off-start", help="结算开始 YYYY-MM-DD（用户给的结算日期）")
    p.add_argument("--write-off-end", help="结算结束 YYYY-MM-DD（用户给的结算日期）")
    p.add_argument("--order-pay-start", help="支付开始 YYYY-MM-DD（用户给的支付日期）")
    p.add_argument("--order-pay-end", help="支付结束 YYYY-MM-DD（用户给的支付日期）")
    p.add_argument("--fetch-file", type=Path,
                   help="用户贴的 queryBillDetail fetch 文本文件；省略则用本机浏览器登录态")
    p.add_argument("--output-root", type=Path, default=Path.home() / "Downloads" / "淘工厂导出",
                   help="输出根目录，默认 ~/Downloads/淘工厂导出")
    p.add_argument("--batch-folder", type=Path, help="批次总目录；公司日期文件夹放在其内部，优先于 output-root")
    p.add_argument("--browser", choices=["auto", "chrome", "chrome-beta", "chrome-canary", "edge", "brave", "chromium"])
    p.add_argument("--transport", choices=["auto", "cookie", "bridge"])
    p.add_argument("--bridge-url", help="原样透传给内置下载器")
    p.add_argument("--resume", action="store_true", help="跳过状态文件中已完成的流程")
    p.add_argument("--skip-module", action="append", default=[], help="跳过指定流程，可重复")
    p.add_argument("--dry-run", action="store_true", help="只显示请求计划，不实际导出")
    p.add_argument("--result-file", type=Path, help="本次发布凭证JSON（供总编排核实，不含凭据）")
    p.add_argument("--run-id", help="总编排本次运行标识")
    args = p.parse_args()

    module = MODULE_ALIASES.get(str(args.module).strip(), str(args.module).strip())
    if module not in {"all", *MODULE_DISPLAY_NAMES}:
        print("错误：--module 必须是已知流程", file=sys.stderr)
        return 2

    # Pure local date validation before creating/quarantining any directory.
    import importlib.util
    spec = importlib.util.spec_from_file_location("_export_download", DOWNLOAD_SCRIPT)
    downloader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(downloader)
    keys = list(MODULE_DISPLAY_NAMES) if module == "all" else [module]
    for key in keys:
        try:
            downloader.module_date_filters(args, {}, downloader.MODULES[key])
        except ValueError as exc:
            p.error(str(exc))

    folder = build_folder(args.output_root, args.company_name, args)
    args.skip_module = [MODULE_ALIASES.get(key, key) for key in args.skip_module]
    if any(key not in MODULE_DISPLAY_NAMES for key in args.skip_module):
        p.error("未知 --skip-module")
    work = folder / f".work-{module}"
    if not args.dry_run:
        folder.mkdir(parents=True, exist_ok=True)
        if args.resume and not work.is_dir():
            p.error("没有对应的未完成工作目录；成功导出后无需 --resume")
        if not args.resume and work.exists():
            # Never destroy an interrupted run just because a new export starts.
            work.rename(folder / f".previous-{module}-{uuid.uuid4().hex}")
        work.mkdir(exist_ok=True)
        if args.resume:
            states = list(work.glob("*.asset-inventory-state.json"))
            if len(states) == 1:
                completed = json.loads(states[0].read_text(encoding="utf-8")).get("completedModules", [])
                # Keep completed module results; quarantine incomplete downloads
                # so a retry cannot count their old shards as fresh data.
                stale = [child for child in work.iterdir() if child != states[0]
                         and (module != "all" and module not in completed or
                              module == "all" and not any(
                                  child.name.startswith(f"asset-inventory-{key}-") for key in completed))]
                if stale:
                    backup = folder / f".previous-{module}-{uuid.uuid4().hex}"
                    backup.mkdir()
                    for child in stale:
                        child.rename(backup / child.name)

    cmd = [
        sys.executable, str(DOWNLOAD_SCRIPT),
        "--module", module,
        "--no-pan-suan",
        "--output-dir", str(work),
        # 店铺名传给下载脚本：处罚数据/赔付追缴的「店铺」列取 --shop-name
        "--shop-name", args.company_name,
        "--expect-company", args.company_name,
    ]
    for key in ("write_off_start", "write_off_end", "order_pay_start", "order_pay_end"):
        value = getattr(args, key)
        if value:
            cmd += [f"--{key.replace('_', '-')}", value]
    for key in ("browser", "transport", "bridge_url"):
        value = getattr(args, key)
        if value is not None:
            cmd += ["--" + key.replace("_", "-"), value]
    if args.fetch_file:
        cmd += ["--fetch-file", str(args.fetch_file)]
    for skip in args.skip_module:
        cmd += ["--skip-module", skip]
    if args.resume:
        cmd.append("--resume")
    if args.dry_run:
        cmd.append("--dry-run")

    print(f"公司名称：{args.company_name}")
    print(f"流程：{module}")
    print(f"导出文件夹：{folder}")
    proc = subprocess.run(cmd)
    if args.dry_run:
        return proc.returncode
    if proc.returncode == 0:
        states = list(work.glob("*.asset-inventory-state.json"))
        if len(states) != 1:
            raise ValueError("缺少唯一执行状态；拒绝发布")
        state = json.loads(states[0].read_text(encoding="utf-8"))
        skipped = set(state.get("manualSkippedModules", [])) | set(args.skip_module)
        selected = [key for key in MODULE_DISPLAY_NAMES
                    if key not in skipped and (module == "all" or key == module)]
        completed = set(state.get("completedModules", []))
        if not set(selected).issubset(completed):
            raise ValueError("执行状态含未完成流程；拒绝发布")
        kept = finalize_folder(work, module, output_folder=folder, modules=selected,
                               outcomes=state.get("results", {}))
        if args.result_file:
            import hashlib
            receipt = {
                "version": 1, "run_id": args.run_id,
                "company": args.company_name, "folder": str(folder.resolve()),
                "dates": {key: getattr(args, key) for key in
                          ("order_pay_start", "order_pay_end", "write_off_start", "write_off_end")},
                "module": module, "skipped": sorted(skipped),
                "results": {key: {"status": state["results"][key]["status"]} for key in selected},
                "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in kept},
            }
            args.result_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.result_file.with_name(args.result_file.name + ".tmp-" + uuid.uuid4().hex)
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, args.result_file)
        # Only this invocation's isolated working directory is disposable.
        if work.parent != folder or not work.name.startswith(".work-"):
            raise RuntimeError("拒绝清理非工作目录")
        # Rename is atomic on this filesystem. Before it: all sources remain
        # resumable. After it: partial cleanup is NEVER offered as a resume run.
        retired = folder / f".published-{module}-{uuid.uuid4().hex}"
        work.rename(retired)
        try:
            shutil.rmtree(retired)
        except OSError:
            print(f"警告：发布已完成，清理中断；保留目录 {retired.name}，不要用于 --resume", file=sys.stderr)
        print(f"\n导出完成，最终 xlsx 共 {len(kept)} 个，全部在：{folder}")
        for path in kept:
            print(f"  - {path.name}")
    else:
        print(f"\n导出脚本退出码 {proc.returncode}；已成功流程的文件保留在 {folder}，"
              f"可用 --resume 续跑（整理只在全部成功后进行）。", file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
