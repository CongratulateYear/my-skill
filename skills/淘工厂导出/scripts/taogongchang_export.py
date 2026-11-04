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
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 下载内核已随本 skill 内置（源自 asset-inventory，--no-pan-suan 纯导出模式）；
# asset-inventory skill 若还装着，其副本作为兼容回退。
DOWNLOAD_SCRIPT = Path(__file__).resolve().with_name("tmall_bill_download.py")
if not DOWNLOAD_SCRIPT.is_file():
    DOWNLOAD_SCRIPT = Path(
        "C:\\Users\\Administrator\\.dsh\\skills/asset-inventory/scripts/tmall_bill_download.py"
    )

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


def build_folder(output_root: Path, company: str, args: argparse.Namespace) -> Path:
    start = args.write_off_start or args.order_pay_start
    end = args.write_off_end or args.order_pay_end
    suffix = f"{start}_{end}" if start and end else "未给日期"
    return output_root / f"{safe_name(company)}_{suffix}"


# 根目录裸文件名前缀 → 流程名（单流程裸跑/历史文件识别）
_ROOT_SPECIALS = (("商品货款", "商品贷款"), ("GEI@", "所有商品效果数据导出"))
ROOT_PREFIX_RULES = tuple(sorted(
    _ROOT_SPECIALS + tuple((name, name) for name in MODULE_DISPLAY_NAMES.values()),
    key=lambda pair: -len(pair[0]),
))


def display_for_path(xlsx: Path, folder: Path, module_key: str | None) -> str | None:
    """解析一个 xlsx 应该叫什么流程名：单流程模式优先用 wrapper 传入的 module。"""
    if module_key and module_key in MODULE_DISPLAY_NAMES:
        return MODULE_DISPLAY_NAMES[module_key]
    # 多流程模式：从流程子目录名 asset-inventory-<module>-<起><止> 解析
    for part in xlsx.relative_to(folder).parts[:-1]:
        m = re.match(r"^asset-inventory-(.+?)-\d{8}-\d{8}$", part)
        if m and m.group(1) in MODULE_DISPLAY_NAMES:
            return MODULE_DISPLAY_NAMES[m.group(1)]
    # 根目录裸文件名前缀识别（如 处罚数据_2026-09-01_2026-09-30.xlsx、商品货款_500…）
    stem = xlsx.stem
    for prefix, name in ROOT_PREFIX_RULES:
        if stem == prefix or stem.startswith(prefix):
            return name
    return None


def finalize_folder(folder: Path, module_key: str | None = None) -> list[Path]:
    """导出成功后整理文件夹：最终 xlsx 以流程名命名放到根目录，删除其余全部内容。

    - 单流程模式把 module_key 传进来，所有 xlsx 一律按该流程命名；
    - 多流程（module=all）模式从流程子目录名 asset-inventory-<module>-<起><止>
      解析 module；根目录裸文件按前缀识别（商品货款→商品贷款、GEI@→所有商品
      效果数据导出，其余按流程名前缀去后缀）；
    - 已在根目录且名字就是流程名的文件跳过（幂等）；
    - 重名 xlsx 追加 _2/_3 后缀，不覆盖；
    - zip、_extracted 目录、流程子目录、状态文件等全部删除。
    """
    if module_key == "all":
        module_key = None
    kept: list[Path] = []
    for xlsx in sorted(folder.rglob("*.xlsx")):
        display = display_for_path(xlsx, folder, module_key)
        target = folder / f"{display}.xlsx" if display else folder / xlsx.name
        if xlsx.resolve() == target.resolve():
            kept.append(xlsx)
            continue
        if target.exists():
            stem, suffix = target.stem, target.suffix
            n = 2
            while target.exists():
                target = folder / f"{stem}_{n}{suffix}"
                n += 1
        xlsx.rename(target)
        kept.append(target)
    for child in folder.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        elif child.suffix.lower() != ".xlsx":
            child.unlink()
    return kept


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
    p.add_argument("--output-root", type=Path, default=Path("outputs"), help="输出根目录")
    p.add_argument("--resume", action="store_true", help="跳过状态文件中已完成的流程")
    p.add_argument("--skip-module", action="append", default=[], help="跳过指定流程，可重复")
    p.add_argument("--dry-run", action="store_true", help="只显示请求计划，不实际导出")
    args = p.parse_args()

    module = MODULE_ALIASES.get(str(args.module).strip(), str(args.module).strip())
    if not module:
        print("错误：--module 不能为空", file=sys.stderr)
        return 2

    folder = build_folder(args.output_root, args.company_name, args)
    if not args.dry_run:
        folder.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(DOWNLOAD_SCRIPT),
        "--module", module,
        "--no-pan-suan",
        "--output-dir", str(folder),
        # 店铺名传给下载脚本：处罚数据/赔付追缴的「店铺」列取 --shop-name
        "--shop-name", args.company_name,
    ]
    for key in ("write_off_start", "write_off_end", "order_pay_start", "order_pay_end"):
        value = getattr(args, key)
        if value:
            cmd += [f"--{key.replace('_', '-')}", value]
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
        kept = finalize_folder(folder, module)
        print(f"\n导出完成，最终 xlsx 共 {len(kept)} 个，全部在：{folder}")
        for path in kept:
            print(f"  - {path.name}")
    else:
        print(f"\n导出脚本退出码 {proc.returncode}；已成功流程的文件保留在 {folder}，"
              f"可用 --resume 续跑（整理只在全部成功后进行）。", file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
