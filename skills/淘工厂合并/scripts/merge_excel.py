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
- 输出目录默认：方式2=公司导出文件夹的父目录（主表与原始文件夹并列）；方式1=第一个文件所在目录。
- 公司名称写入「公司汇总」店铺列。

纯本地合并，不联网、不需要登录态。默认用 LibreOffice 计算公式缓存
（--no-recalculate 关闭；机器上没有 soffice 时自动跳过，Excel 打开时计算）。
默认从干净模板重建（幂等）；--accumulate 在已有输出上续并（保留手工列）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

EXPORT_ROOT_DEFAULT = Path("~/Downloads/淘工厂导出").expanduser()
# 合并内核已随本 skill 内置（源自 asset-inventory 的 pan_suan_builder.py +
# 盘算模板.xlsx）；asset-inventory skill 若还在，其副本作为兼容回退。
_SKILL_ROOT = Path(__file__).resolve().parents[1]
def find_builder_resources(skill_root: Path = _SKILL_ROOT) -> tuple[Path, Path]:
    """优先内置资源；兼容同级中文/英文资产盘算技能，不混用两套资源。"""
    roots = [skill_root]
    roots.extend(skill_root.parent / name for name in ("asset-inventory", "资产盘算"))
    for root in roots:
        builder = root / "scripts" / "pan_suan_builder.py"
        template = root / "assets" / "盘算模板.xlsx"
        if builder.is_file() and template.is_file():
            return builder, template
    return skill_root / "scripts" / "pan_suan_builder.py", skill_root / "assets" / "盘算模板.xlsx"


PAN_SUAN_BUILDER, PAN_SUAN_TEMPLATE = find_builder_resources()


def spreadsheet_python_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("CODEX_SPREADSHEET_PYTHON")
    if configured:
        candidates.append(Path(shutil.which(configured) or configured).expanduser())
    if sys.executable:
        candidates.append(Path(sys.executable))
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    home = Path.home()
    for runtime_root in (home / ".dsh/dsh-runtimes", home / ".codex/dsh-runtimes",
                         home / ".cache/codex-runtimes"):
        for runtime_name in ("dsh-primary-runtime", "codex-primary-runtime"):
            python_root = runtime_root / runtime_name / "dependencies/python"
            candidates.extend([python_root / "python.exe", python_root / "bin/python3"])
    candidates.extend([
        Path("/tmp/aivenv/bin/python"),
        home / ".local/bin/python3.12", home / ".local/bin/python3.13",
    ])
    return candidates

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def find_spreadsheet_python() -> Path:
    checked: set[Path] = set()
    for candidate in spreadsheet_python_candidates():
        # 不 resolve 后执行：venv 的 python 常为符号链接，解引用会丢失其依赖环境。
        candidate = candidate.expanduser().absolute()
        if candidate in checked or not candidate.is_file():
            continue
        checked.add(candidate)
        try:
            probe = subprocess.run(
                [str(candidate), "-c", "import openpyxl"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
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
    # Validate the calendar, not just the YYYY-MM-DD shape.
    dates = [date.fromisoformat(part) for part in parts]
    if len(dates) == 2 and dates[0] > dates[1]:
        raise ValueError(f"结算日期开始日期不得晚于结束日期：{value!r}")
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


def is_generated_workbook(path: Path) -> bool:
    """只读 xlsx 元数据，避免将任何名称的盘算输出再次作为流程输入。"""
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("xl/workbook.xml"))
        return any(node.get("name") == "公司汇总" for node in root.iter()
                   if node.tag.rsplit("}", 1)[-1] == "sheet")
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError):
        # 损坏/不支持的源文件留给 builder 报读取错误，不伪装成生成工作簿。
        return False


def collect_sources(args: Any, settlement: str) -> tuple[list[Path], Path]:
    """返回（来源文件列表, 输出目录）；export-folder 优先于 batch-folder。"""
    if args.files:
        files = [Path(p).expanduser() for p in args.files]
        missing = [str(p) for p in files if not p.is_file()]
        if missing:
            raise ValueError(f"文件不存在：{'、'.join(missing)}")
        default_out = files[0].resolve().parent
    else:
        if args.export_folder:
            folder = Path(args.export_folder).expanduser().resolve()
        else:
            root = getattr(args, "batch_folder", None) or args.export_root
            folder = locate_export_folder(Path(root).expanduser().resolve(), args.company_name, settlement)
        if not folder.is_dir():
            raise ValueError(f"导出文件夹不存在：{folder}")
        files = sorted(p for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() == ".xlsx" and not p.name.startswith("~$"))
        default_out = folder.parent
    out_dir = Path(args.output_dir).expanduser() if args.output_dir else default_out
    out_path = (out_dir / f"{safe_name(args.company_name)}_{settlement}.xlsx").resolve()
    sources: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved == out_path or is_generated_workbook(path):
            print(f"跳过盘算工作簿：{path}", file=sys.stderr)
        elif resolved not in seen:
            sources.append(path)
            seen.add(resolved)
    if not sources:
        raise ValueError("没有可合并的来源 xlsx（已排除输出文件和含公司汇总的盘算工作簿）")
    return sources, out_dir


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
    if accumulate:
        command.append("--accumulate")
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
    p.add_argument("--export-folder", type=Path, help="直接指定公司导出文件夹，优先于 --batch-folder 和 --export-root")
    p.add_argument("--batch-folder", type=Path, help="批次目录，在其中定位 <公司名称>_<结算日期> 公司文件夹")
    p.add_argument("--output-dir", type=Path, help="显式覆盖输出目录，优先级最高（默认：公司导出文件夹的父目录；--files 为第一个文件所在目录）")
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
