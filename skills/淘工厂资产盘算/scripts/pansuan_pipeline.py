#!/usr/bin/env python3
"""淘工厂资产盘算：单公司流水线 —— 登录闸门 → 淘工厂导出 → 淘工厂合并。

三种用法（对应 SKILL.md 的调用词映射）：
  淘工厂资产盘算          --mode full（默认）  登录验证 → 导出全部17流程 → 合并盘算工作簿
  淘工厂资产盘算导出      --mode export        登录验证 → 导出全部17流程（不合并）
  淘工厂资产盘算导出罚款  --mode export --module 罚款   登录验证 → 仅导出罚款（处罚数据）

依赖（安装本 skill 时必须一并安装，缺一不可）：
  淘工厂登录 /Users/rabbit/.agents/skills/淘工厂登录/
  淘工厂导出 /Users/rabbit/.agents/skills/淘工厂导出/
  淘工厂合并 /Users/rabbit/.agents/skills/淘工厂合并/
脚本启动时逐一自检，缺哪个就报清单并退出。

多公司：本脚本一次只处理一家公司。多公司由上层按用户给定顺序逐家调用，
必须等上一家全部完成（含合并）才轮到下一家；换公司前先按「淘工厂登录」
的流程在当前浏览器里退出并登录目标公司。

退出码：0=成功；2=参数/依赖错误；3=需要先完成浏览器登录；其他=导出/合并失败。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SKILLS_ROOT = Path("/Users/rabbit/.agents/skills")
DEPENDENCIES = [
    ("淘工厂登录", SKILLS_ROOT / "淘工厂登录" / "scripts" / "ensure_login.py"),
    ("淘工厂导出", SKILLS_ROOT / "淘工厂导出" / "scripts" / "taogongchang_export.py"),
    ("淘工厂合并", SKILLS_ROOT / "淘工厂合并" / "scripts" / "merge_excel.py"),
]
EXPORT_ROOT_DEFAULT = Path("~/Downloads/淘工厂导出").expanduser()
ALL_MODULE_WORDS = {"all", "全部", ""}


def check_dependencies() -> None:
    missing = [
        f"  {name}：{script}（应位于 {script.parent.parent}）"
        for name, script in DEPENDENCIES
        if not script.is_file()
    ]
    if missing:
        raise SystemExit(
            "缺少依赖 skill，本 skill 无法运行：\n" + "\n".join(missing)
            + "\n「淘工厂资产盘算」与 淘工厂登录/淘工厂导出/淘工厂合并 是一套，"
            "安装本 skill 时必须同时安装这三个依赖 skill，请补装后重跑。"
        )


def run(command: list[str]) -> int:
    print("+", " ".join(str(c) for c in command), flush=True)
    return subprocess.run(command).returncode


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="淘工厂资产盘算：登录闸门 → 导出 → 合并（单公司）")
    p.add_argument("--company-name", required=True, help="公司/店铺名称，取用户消息原文")
    p.add_argument("--mode", choices=["full", "export"], default="full",
                   help="full=导出+合并（淘工厂资产盘算）；export=仅导出（淘工厂资产盘算导出）")
    p.add_argument("--module", default="all",
                   help="流程：默认 all 全部17流程；罚款模式传 罚款（淘工厂资产盘算导出罚款）")
    p.add_argument("--order-pay-start", help="支付开始 YYYY-MM-DD（用户给的支付日期）")
    p.add_argument("--order-pay-end", help="支付结束 YYYY-MM-DD（用户给的支付日期）")
    p.add_argument("--write-off-start", help="结算开始 YYYY-MM-DD（用户给的结算日期）")
    p.add_argument("--write-off-end", help="结算结束 YYYY-MM-DD（用户给的结算日期）")
    p.add_argument("--settlement-date",
                   help="合并用结算日期；缺省用结算区间，再退支付区间，再退当天")
    p.add_argument("--export-root", type=Path, default=EXPORT_ROOT_DEFAULT,
                   help="淘工厂导出输出根目录（默认 ~/Downloads/淘工厂导出）")
    p.add_argument("--fetch-file", type=Path,
                   help="用户贴的 queryBillDetail fetch 文本文件；提供后跳过登录闸门")
    p.add_argument("--login-wait", type=float, default=10.0,
                   help="登录闸门等待 cookie 落库的秒数（默认 10）")
    args = p.parse_args()

    check_dependencies()
    ensure_login, export_script, merge_script = (str(script) for _, script in DEPENDENCIES)
    module = str(args.module).strip()
    is_all_module = module.lower() in ALL_MODULE_WORDS or module in ALL_MODULE_WORDS

    # 1) 登录闸门：导出走浏览器 cookie 库，先确认当前浏览器登录的正是目标公司
    if args.fetch_file is None:
        gate = [sys.executable, ensure_login, "--expect-company", args.company_name,
                "--wait", str(max(args.login_wait, 0.0))]
        rc = run(gate)
        if rc == 3:
            print(
                "\n需要先完成浏览器登录/切换：按「淘工厂登录」skill 在【当前已打开的浏览器】里"
                "新开 Magpie 标签页操作（不要新开浏览器）；若当前登录的是别的公司，先在 "
                "tgc.tmall.com 右上角公司名 → 退出登录，再登目标公司账号。完成后重跑本命令。",
                file=sys.stderr,
            )
            return 3
        if rc != 0:
            print(f"登录闸门异常（exit {rc}），停止执行", file=sys.stderr)
            return rc

    # 2) 导出
    export_cmd = [
        sys.executable, export_script,
        "--company-name", args.company_name,
        "--module", module,
        "--output-root", str(args.export_root),
    ]
    for key in ("order_pay_start", "order_pay_end", "write_off_start", "write_off_end"):
        value = getattr(args, key)
        if value:
            export_cmd += [f"--{key.replace('_', '-')}", value]
    if args.fetch_file:
        export_cmd += ["--fetch-file", str(args.fetch_file)]
    rc = run(export_cmd)
    if rc != 0:
        print(f"淘工厂导出失败（exit {rc}）；已成功流程的文件保留在导出文件夹，"
              f"可加 --resume 续跑", file=sys.stderr)
        return rc

    # 3) 合并：仅 full + 全部流程 时执行
    if args.mode == "full" and is_all_module:
        settlement = (
            args.settlement_date
            or (f"{args.write_off_start}_{args.write_off_end}"
                if args.write_off_start and args.write_off_end else None)
            or (f"{args.order_pay_start}_{args.order_pay_end}"
                if args.order_pay_start and args.order_pay_end else None)
        )
        merge_cmd = [
            sys.executable, merge_script,
            "--company-name", args.company_name,
            "--export-root", str(args.export_root),
        ]
        if settlement:
            merge_cmd += ["--settlement-date", settlement]
        rc = run(merge_cmd)
        if rc != 0:
            print(f"淘工厂合并失败（exit {rc}）", file=sys.stderr)
            return rc
    elif args.mode == "full":
        print("仅导出部分流程（--module 非 all），跳过合并；需要合并时跑「淘工厂合并」skill")
    else:
        print("export 模式：不合并；需要合并时跑「淘工厂合并」skill")

    print(f"\n淘工厂资产盘算流水线完成：{args.company_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
