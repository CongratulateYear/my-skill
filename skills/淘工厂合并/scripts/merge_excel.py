#!/usr/bin/env python3
"""淘工厂合并：把淘工厂导出的流程 xlsx 合并成一个盘算式工作簿。

汇总逻辑用本 skill 内置的 pan_suan_builder.py（源自「资产盘算」skill）：
以 盘算模板.xlsx 为底，把各流程明细写入对应 Sheet，并按 30 列固定表头重建
「公司汇总」（商品行 SUMIFS 公式 + 总计行）——与资产盘算产出的结构一致。

文件来源两种：
1. 用户直接给文件（--files a.xlsx b.xlsx …）
2. 「淘工厂导出」生成的导出文件夹（默认 --export-root ~/Downloads/淘工厂导出，
   自动定位 <公司名称>_<结算日期>/ ；或 --export-folder 直接指定文件夹）

输出：<输出目录>/<公司名称>_<结算日期>.xlsx
- 结算日期：用户给（YYYY-MM-DD 或区间）；没给用当天。
- 输出目录默认：方式2=导出根目录；方式1=第一个文件所在目录。
- 公司名称写入「公司汇总」店铺列。

纯本地合并，不联网、不需要登录态。默认用 LibreOffice 计算公式缓存
（--no-recalculate 关闭；机器上没有 soffice 时自动跳过，Excel 打开时计算）。
默认从干净模板重建（幂等）；--accumulate 在已有输出上续并（保留手工列）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

EXPORT_ROOT_DEFAULT = Path("~/Downloads/淘工厂导出").expanduser()
# 合并内核已随本 skill 内置（源自 asset-inventory 的 pan_suan_builder.py +
# 盘算模板.xlsx）；asset-inventory skill 若还在，其副本作为兼容回退。
_SKILL_ROOT = Path(__file__).resolve().parents[1]
PAN_SUAN_BUILDER = _SKILL_ROOT / "scripts" / "pan_suan_builder.py"
PAN_SUAN_TEMPLATE = _SKILL_ROOT / "assets" / "盘算模板.xlsx"
if not PAN_SUAN_BUILDER.is_file() or not PAN_SUAN_TEMPLATE.is_file():
    _LEGACY = Path("C:\\Users\\Administrator\\.dsh\\skills/asset-inventory")
    if (_LEGACY / "scripts" / "pan_suan_builder.py").is_file():
        PAN_SUAN_BUILDER = _LEGACY / "scripts" / "pan_suan_builder.py"
        PAN_SUAN_TEMPLATE = _LEGACY / "assets" / "盘算模板.xlsx"

DEFAULT_SPREADSHEET_PYTHON = Path(
    "/Users/rabbit/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
)
EXTRA_SPREADSHEET_PYTHONS = (
    Path("/tmp/aivenv/bin/python"),
    Path.home() / ".local/bin/python3.12",
    Path.home() / ".local/bin/python3.13",
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def find_spreadsheet_python() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("CODEX_SPREADSHEET_PYTHON")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend([DEFAULT_SPREADSHEET_PYTHON, *EXTRA_SPREADSHEET_PYTHONS, Path(sys.executable)])
    checked: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in checked or not candidate.exists():
            continue
        checked.add(candidate)
        probe = subprocess.run(
            [str(candidate), "-c", "import openpyxl"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if probe.returncode == 0:
            return candidate
    raise RuntimeError("未找到可导入 openpyxl 的 Python；可用 CODEX_SPREADSHEET_PYTHON 指定")


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "-", name.strip()) or "未命名"


def normalize_settlement(value: str | None) -> str:
    """结算日期：单个 YYYY-MM-DD 或区间（_ 或 ~ 分隔）；没给用当天。"""
    if value is None or not value.strip():
        return date.today().isoformat()
    parts = [p.strip() for p in re.split(r"[~_]+", value.strip()) if p.strip()]
    if not parts or len(parts) > 2 or not all(DATE_RE.match(p) for p in parts):
        raise ValueError(
            f"结算日期格式不对：{value!r}（应为 YYYY-MM-DD 或 YYYY-MM-DD_YYYY-MM-DD）"
        )
    if len(parts) == 2 and parts[0] > parts[1]:
        parts.reverse()
    return "_".join(parts)


def locate_export_folder(export_root: Path, company: str, settlement: str) -> Path:
    """在导出根目录下定位 <公司>_<结算区间>/ 文件夹；找不到就列出可选项报错。"""
    wanted = f"{safe_name(company)}_{settlement}"
    exact = export_root / wanted
    if exact.is_dir():
        return exact
    prefix = f"{safe_name(company)}_"
    available = sorted(p.name for p in export_root.glob(f"{prefix}*/") if p.is_dir()) if export_root.is_dir() else []
    if available:
        raise ValueError(
            f"没找到导出文件夹 {exact}；{export_root} 下现有：{'、'.join(available)}"
        )
    raise ValueError(f"导出根目录不存在或里面没有公司文件夹：{export_root}")


def collect_sources(args: Any, settlement: str) -> tuple[list[Path], Path]:
    """返回（来源文件列表, 输出目录）。"""
    if args.files:
        files = [Path(p).expanduser() for p in args.files]
        missing = [str(p) for p in files if not p.is_file()]
        if missing:
            raise ValueError(f"文件不存在：{'、'.join(missing)}")
        out_dir = args.output_dir or files[0].resolve().parent
        return files, out_dir
    export_root = args.export_root
    folder = Path(args.export_folder).expanduser() if args.export_folder else locate_export_folder(
        export_root, args.company_name, settlement,
    )
    if not folder.is_dir():
        raise ValueError(f"导出文件夹不存在：{folder}")
    files = sorted(p for p in folder.glob("*.xlsx") if not p.name.startswith("~$"))
    if not files:
        raise ValueError(f"文件夹里没有 xlsx：{folder}")
    out_dir = args.output_dir or (export_root if args.export_folder is None else folder.parent)
    return files, out_dir


def run_builder(
    sources: list[Path],
    out_path: Path,
    company: str,
    accumulate: bool,
    strict: bool,
    recalculate: bool,
) -> dict[str, Any]:
    """调 asset-inventory 的 pan_suan_builder 完成实际合并，返回其 JSON 结果。"""
    if not PAN_SUAN_BUILDER.is_file():
        raise FileNotFoundError(f"找不到 pan_suan_builder.py：{PAN_SUAN_BUILDER}")
    if not PAN_SUAN_TEMPLATE.is_file():
        raise FileNotFoundError(f"找不到盘算模板：{PAN_SUAN_TEMPLATE}")
    template = out_path if (accumulate and out_path.exists()) else PAN_SUAN_TEMPLATE
    python = find_spreadsheet_python()
    command = [
        str(python), str(PAN_SUAN_BUILDER),
        "--template", str(template),
        "--output", str(out_path),
        "--json",
        "--shop-name", company,
    ]
    if strict:
        command.append("--strict")
    if recalculate:
        command.append("--recalculate")
    else:
        command.append("--no-recalculate")
    for path in sources:
        command.extend(["--input", str(path)])
    # 子进程强制 UTF-8 输出 + 父进程按 UTF-8 解码：Windows 控制台默认 GBK，
    # 含中文的 JSON（ensure_ascii=False）曾按 GBK 解出 0xb0 崩溃。
    child_env = dict(os.environ)
    child_env["PYTHONUTF8"] = "1"
    completed = subprocess.run(command, text=True, capture_output=True, check=False,
                               encoding="utf-8", errors="replace", env=child_env)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"盘算构建失败：{detail[-2000:]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("pan_suan_builder 返回了无法解析的结果") from exc


def report(result: dict[str, Any]) -> None:
    for stat in result.get("import_stats", []):
        source_names = "、".join(Path(s).name for s in str(stat.get("source", "")).split("；") if s)
        line = (
            f"  {stat['target']} ← {source_names}："
            f"读取 {stat['read_rows']} 行 → 写入 {stat['written_rows']} 行"
            f"（重复 {stat['duplicates']}）"
        )
        if stat.get("amount_header"):
            line += f"，{stat['amount_header']}合计 {stat['amount_total']}"
        print(line)
    print(f"公司汇总：{result.get('product_count', 0)} 个商品行"
          f"（费用归集 {result.get('allocated_rows', 0)} 行 / 未归属 {result.get('unassigned_rows', 0)} 行）")
    skipped = result.get("skipped_files") or []
    for path in skipped:
        print(f"跳过 {path}（无法识别流程类型，未写入）", file=sys.stderr)
    if result.get("formula_cache_recalculated"):
        print("公式缓存：已由 LibreOffice 计算写入")
    else:
        print("公式缓存：未计算（Excel/WPS 打开时自动计算公司汇总）")


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="淘工厂合并：流程 xlsx 合并成盘算式工作簿（公司汇总+各流程Sheet）")
    p.add_argument("--company-name", required=True, help="公司/店铺名称，取用户消息原文；写入公司汇总「店铺」列")
    p.add_argument("--settlement-date", help="结算日期 YYYY-MM-DD 或 YYYY-MM-DD_YYYY-MM-DD；缺省用当天")
    p.add_argument("--files", nargs="+", type=str, help="用户直接给的文件路径（与导出文件夹二选一）")
    p.add_argument("--export-root", type=Path, default=EXPORT_ROOT_DEFAULT,
                   help="淘工厂导出的输出根目录（默认 ~/Downloads/淘工厂导出）")
    p.add_argument("--export-folder", type=Path, help="直接指定导出文件夹，跳过自动定位")
    p.add_argument("--output-dir", type=Path, help="输出目录（默认：导出根目录或第一个文件所在目录）")
    p.add_argument("--accumulate", action="store_true",
                   help="输出文件已存在时在其基础上续并（保留公司汇总手工列）；默认从干净模板重建")
    p.add_argument("--strict", action="store_true", help="遇到无法识别的 xlsx 直接失败（默认跳过并提示）")
    p.add_argument("--no-recalculate", action="store_true",
                   help="不用 LibreOffice 计算公式缓存（默认计算，无 soffice 时自动跳过）")
    args = p.parse_args()

    settlement = normalize_settlement(args.settlement_date)
    sources, out_dir = collect_sources(args, settlement)
    out_path = out_dir.expanduser() / f"{safe_name(args.company_name)}_{settlement}.xlsx"
    out_resolved = out_path.resolve()
    sources = [s for s in sources if s.resolve() != out_resolved]

    if args.accumulate and not out_path.exists():
        print("输出文件不存在，--accumulate 无从续并，改用干净模板重建", file=sys.stderr)

    result = run_builder(
        sources, out_path, args.company_name,
        accumulate=args.accumulate, strict=args.strict, recalculate=not args.no_recalculate,
    )
    report(result)
    print(f"\n合并完成：{result.get('output', str(out_path))}")
    print("结构：公司汇总（30列固定表头+总计行）+ 各流程明细Sheet + 费用归集等模板页")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
