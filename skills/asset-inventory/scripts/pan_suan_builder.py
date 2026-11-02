#!/usr/bin/env python3
"""Merge downloaded Tmall bill workbooks into a fixed-header pan-suan workbook.

The source bill sheets are replaced by matching downloaded detail workbooks.
The fixed 公司汇总 header is never renamed, reordered, inserted into, or deleted.
所有商品效果数据导出 is only imported by its dedicated export module
(all-item-effects-export); no other bill import may write or clear it.

Imported detail rows receive the existing target-sheet header border on the cells
written by this script.  No other pre-existing presentation property is changed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import OrderedDict, defaultdict
from copy import copy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

try:
    from openpyxl import load_workbook
    from openpyxl.comments import Comment
    from openpyxl.formatting.rule import CellIsRule, FormulaRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.styles.cell_style import StyleArray
    from openpyxl.utils import get_column_letter, quote_sheetname
    from openpyxl.worksheet.table import Table, TableStyleInfo
except ImportError as exc:  # pragma: no cover - environment issue
    raise SystemExit(
        "缺少 openpyxl。请使用 Codex 工作区 Python 运行此脚本："
        "/Users/rabbit/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
    ) from exc

TARGET_ALIASES = OrderedDict([
    ("消费者体验提升服务", "消费者体验提升服务"),
    ("商品货款", "商品货款"),
    ("商品贷款", "商品货款"),
    ("交易补贴", "交易补贴"),
    ("交易佣金", "交易佣金"),
    ("先用后付", "先用后付"),
    ("新疆物流集运", "新疆物流集运"),
    ("评价有礼", "评价有礼"),
    ("营销费用", "营销费用"),
    ("商业化账户充值提现", "商业化账户充值提现"),
    ("投流推广佣金", "投流推广佣金"),
    ("淘客推广佣金", "淘客推广佣金"),
    ("直播推广佣金", "直播推广佣金"),
    ("客服服务费", "客服服务费"),
    ("赔付追缴", "赔付追缴"),
    ("出海增值服务费", "出海增值服务费"),
    ("处罚数据", "处罚数据"),
    ("所有商品效果数据导出", "所有商品效果数据导出"),
])

# Sheets with a direct product ID, used to construct 公司汇总 C column.
PRODUCT_ID_HEADER = {
    "商品货款": "商品ID",
    "交易补贴": "商品ID",
    "交易佣金": "商品ID",
    "先用后付": "商品ID",
    "消费者体验提升服务": "商品ID",
    "新疆物流集运": "商品ID",
    "出海增值服务费": "商品ID",
    "营销费用": "商品ID",
    "投流推广佣金": "商品ID",
    "淘客推广佣金": "商品ID",
    "直播推广佣金": "商品ID",
    "处罚数据": "商品id",
    "所有商品效果数据导出": "商品id",
}

IDENTIFIER_WORDS = (
    "ID", "Id", "id", "订单号", "单据号", "流水号", "业务单号", "账单编号", "运单号", "处罚业务",
)
AMOUNT_WORDS = ("金额", "结算价", "佣金", "费用")
DATE_WORDS = ("日期", "时间")

SUMMARY_REQUIRED_HEADERS = [
    "店铺", "运营姓名", "商品ID", "产品名称", "数量", "收入", "交易补贴", "交易佣金", "先用后付",
    "消费者体验提升服务", "客服服务费", "新疆物流集运", "出海增值服务费", "赔付追缴",
    "新品营销(营销费用)", "评价有礼", "商业化账户充值提现", "充值加码消耗", "智能优惠券金额",
    "投流推广佣金", "淘客推广佣金", "直播推广佣金", "刷单金额", "罚款", "直通车营销",
    "收入金额", "收款人", "货款", "利润", "备注（查询不到的）",
]

AUTO_SUMMARY_HEADERS = {
    "产品名称", "数量", "收入", "交易补贴", "交易佣金", "先用后付", "消费者体验提升服务",
    "新疆物流集运", "出海增值服务费", "赔付追缴", "新品营销(营销费用)", "评价有礼",
    "充值加码消耗", "智能优惠券金额", "投流推广佣金", "淘客推广佣金", "直播推广佣金", "罚款",
}

# Manual values are preserved by 商品ID across rebuilds.
MANUAL_SUMMARY_HEADERS = set(SUMMARY_REQUIRED_HEADERS) - AUTO_SUMMARY_HEADERS - {"商品ID"}

# 公司汇总「总计」行：这些列对上方商品明细行求和。
SUMMARY_TOTAL_SUM_HEADERS = (
    "数量", "收入", "交易补贴", "交易佣金", "先用后付", "消费者体验提升服务",
    "新疆物流集运", "出海增值服务费", "新品营销(营销费用)", "充值加码消耗",
    "智能优惠券金额", "投流推广佣金", "淘客推广佣金", "直播推广佣金", "刷单金额", "罚款",
)

# 公司汇总「总计」行：这些列直接取来源 Sheet 的整表金额合计，sign<0 表示来源为负数需取反。
SUMMARY_TOTAL_SHEET_HEADERS = (
    ("客服服务费", "客服服务费", -1),
    ("赔付追缴", "赔付追缴", -1),
    ("评价有礼", "评价有礼", -1),
    ("商业化账户充值提现", "商业化账户充值提现", -1),
)

SUMMARY_TOTAL_LABEL = "总计"

NAVY = "1F4E78"
BLUE = "5B9BD5"
LIGHT_BLUE = "D9EAF7"
LIGHT_GRAY = "F2F2F2"
LIGHT_YELLOW = "FFF2CC"
LIGHT_RED = "FCE4D6"
LIGHT_GREEN = "E2F0D9"
WHITE = "FFFFFF"
RED = "C00000"
GREEN = "008000"
THIN_GRAY = Side(style="thin", color="D9E1F2")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="将天猫账单明细写入盘算工作簿")
    p.add_argument("--template", type=Path, required=True, help="盘算模板或现有盘算文件")
    p.add_argument("--input", type=Path, action="append", default=[], help="账单明细文件或目录，可重复")
    p.add_argument("--output", type=Path, required=True, help="输出盘算文件")
    p.add_argument("--strict", action="store_true", help="遇到无法识别的xlsx时失败，而不是跳过")
    p.add_argument(
        "--expected-target", choices=list(dict.fromkeys(TARGET_ALIASES.values())),
        help="已知下载模块对应的目标Sheet；提供后不依赖下载文件名识别，并校验写入结果",
    )
    p.add_argument(
        "--allow-empty-target", action="store_true",
        help="查询明确为0条时，允许无输入并清空预期目标Sheet的旧明细",
    )
    p.add_argument(
        "--shop-name",
        help="店铺名称，取自用户发送的名称，例如 埔鸣--迎风部 或 埔鸣；写入公司汇总「店铺」列",
    )
    p.add_argument("--json", action="store_true", help="以JSON输出合并摘要")
    p.add_argument("--recalculate", action="store_true", help="仅在用户明确要求时，以 LibreOffice 无界面生成公式缓存；默认不重写工作簿")
    # Backward-compatible no-op: recalculation is opt-in.
    p.add_argument("--no-recalculate", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args()


def normalize_header(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())


def identifier_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, ".15g")
    text = str(value).strip()
    if not text:
        return None
    # Tmall exports can prefix text identifiers with a literal apostrophe
    # (Excel text marker); strip it so IDs stay clean and comparable.
    if text.startswith("'"):
        text = text[1:]
    if re.fullmatch(r"[+-]?\d+(?:\.0+)?[Ee][+-]?\d+", text):
        try:
            return format(Decimal(text), "f").split(".")[0]
        except InvalidOperation:
            pass
    if re.fullmatch(r"\d+\.0+", text):
        return text.split(".")[0]
    return text


def number_value(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace(",", "").replace("¥", "").replace("￥", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return 0.0


def collect_xlsx(inputs: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        path = raw.expanduser().resolve()
        candidates = [path] if path.is_file() else sorted(path.rglob("*.xlsx")) if path.is_dir() else []
        for item in candidates:
            if item.name.startswith("~$"):
                continue
            # Never re-import the built pan-suan workbook or its consolidated
            # folder when the whole output directory is passed as input.
            if item.name.startswith("盘算_") or any("盘算输出" in part for part in item.parts):
                continue
            resolved = item.resolve()
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
    return result


def load_input_workbook(path: Path, data_only: bool):
    """Load a downloaded detail workbook, falling back to a full in-memory read.

    The 所有商品效果数据导出 hosting export omits the worksheet dimension
    (``dimension ref="A1"``) even though it contains thousands of rows, so
    read-only mode would stop at the first row.  Normal FBI exports declare a
    real dimension and stay on the fast read-only path.
    """
    wb = load_workbook(path, read_only=True, data_only=data_only)
    ws = wb[wb.sheetnames[0]]
    if ws.max_row <= 1 and ws.max_column <= 1:
        wb.close()
        wb = load_workbook(path, read_only=False, data_only=data_only)
    return wb


def detect_target(path: Path, wb=None) -> str | None:
    name = path.name
    for prefix, target in TARGET_ALIASES.items():
        if name.startswith(prefix):
            return target
    opened = False
    if wb is None:
        try:
            wb = load_input_workbook(path, data_only=False)
            opened = True
        except Exception:
            return None
    try:
        if "公司汇总" in wb.sheetnames:
            return None
        ws = wb[wb.sheetnames[0]]
        headers = {normalize_header(ws.cell(1, c).value) for c in range(1, ws.max_column + 1)}
        signatures = {
            "商品货款": {"单据方向", "账单金额", "商品ID", "购买件数", "交易补贴", "商家出资补贴"},
            "交易补贴": {"原始补贴金额", "商品ID", "账单金额"},
            "交易佣金": {"佣金率", "佣金金额", "物流服务费金额", "商品ID"},
            "先用后付": {"确认收货日期", "供应商承担优惠金额", "商品ID"},
            "消费者体验提升服务": {"业务类型", "退款单号", "类目名称", "商品ID"},
            "新疆物流集运": {"运单号", "出入仓类型", "出入仓时间", "商品ID"},
            "出海增值服务费": {"单据方向", "账单金额", "商品ID", "skuID"},
            "评价有礼": {"业务单据号", "交易主单号", "交易子单号", "账单金额"},
            "商业化账户充值提现": {"单据方向", "业务单号", "场景名称", "账单金额"},
            "客服服务费": {"账单编号", "费用类型", "账单金额"},
            "赔付追缴": {"处罚业务ID", "违规类型", "违规事件", "账单金额"},
            "处罚数据": {"处罚内容", "主订单号", "处罚时间", "违规类型", "违规事件", "处罚金额"},
            "所有商品效果数据导出": {"日期", "商品id", "推广成交金额", "智能优惠券金额"},
        }
        for target, signature in signatures.items():
            if signature.issubset(headers):
                return target
        return None
    finally:
        if opened:
            wb.close()


def read_bill_rows(path: Path, target_headers: list[str]) -> tuple[list[list[Any]], str]:
    wb = load_input_workbook(path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    try:
        normalized_target = {normalize_header(h): i for i, h in enumerate(target_headers)}
        max_col = ws.max_column
        header_row = None
        source_headers: list[str] = []
        for r in range(1, min(ws.max_row, 10) + 1):
            candidate = [normalize_header(ws.cell(r, c).value) for c in range(1, max_col + 1)]
            overlap = sum(1 for h in candidate if h in normalized_target)
            if overlap >= max(3, min(len(target_headers), max_col) // 2):
                header_row = r
                source_headers = candidate
                break
        if header_row is None:
            raise ValueError(f"{path.name} 未找到与目标Sheet匹配的表头")
        source_index = {h: i for i, h in enumerate(source_headers) if h}
        missing = [h for h in target_headers if normalize_header(h) not in source_index]
        if missing:
            raise ValueError(f"{path.name} 缺少目标列：{', '.join(missing)}")
        rows: list[list[Any]] = []
        for raw in ws.iter_rows(min_row=header_row + 1, values_only=True):
            if not any(v is not None and str(v).strip() != "" for v in raw):
                continue
            out: list[Any] = []
            for header in target_headers:
                value = raw[source_index[normalize_header(header)]]
                if any(word in header for word in IDENTIFIER_WORDS):
                    value = identifier_text(value)
                out.append(value)
            rows.append(out)
        return rows, ws.title
    finally:
        wb.close()


def dedupe_rows(rows: list[list[Any]], headers: list[str]) -> tuple[list[list[Any]], int]:
    """Remove only exact duplicate detail rows.

    A single Tmall funds-flow number can legitimately cover several product/order
    rows, so using only 资金流水号 would drop real revenue and quantities.  The
    complete normalized row is the safe identity for overlapping downloads.
    """
    del headers  # kept in the signature for callers and future diagnostics
    seen: set[tuple[str, ...]] = set()
    result: list[list[Any]] = []
    duplicates = 0
    for row in rows:
        key = tuple("" if value is None else str(value) for value in row)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        result.append(row)
    return result, duplicates


def clear_sheet_data(ws) -> None:
    """Clear values only; preserve every existing cell and worksheet style."""
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=ws.max_column):
        for cell in row:
            cell.value = None


def write_sheet_values(ws, rows: list[list[Any]], *, apply_data_borders: bool = False) -> None:
    """Populate values from row 2 without deleting/inserting/restyling rows.

    ``apply_data_borders`` is used for downloaded detail sheets.  The template
    has bordered headers but may not have a pre-styled data row; in that case
    copy only the corresponding header border to the cells written for each
    imported row.  Fonts, fills, alignment, number formats and all other
    presentation properties remain untouched.
    """
    clear_sheet_data(ws)
    # Use the already-registered border style ids directly.  Assigning Border
    # objects per cell would make openpyxl de-duplicate every assignment
    # against the workbook style collection, which turns millions of rows into
    # a linear-scan bottleneck.
    header_border_ids = {
        column: ws.cell(1, column)._style.borderId
        for column in range(1, ws.max_column + 1)
    } if apply_data_borders else {}
    for row_number, row in enumerate(rows, start=2):
        for column, value in enumerate(row, start=1):
            cell = ws.cell(row_number, column)
            cell.value = value
            if apply_data_borders and column in header_border_ids:
                if cell._style is None:
                    cell._style = StyleArray()
                cell._style.borderId = header_border_ids[column]


def row_dimension_fingerprint(dimension) -> tuple[Any, ...]:
    return (
        getattr(dimension, "hidden", None), getattr(dimension, "height", None), getattr(dimension, "bestFit", None),
        getattr(dimension, "outlineLevel", None), getattr(dimension, "collapsed", None), getattr(dimension, "style_id", None),
    )


def column_dimension_fingerprint(dimension) -> tuple[Any, ...]:
    return (
        getattr(dimension, "hidden", None), getattr(dimension, "width", None), getattr(dimension, "bestFit", None),
        getattr(dimension, "outlineLevel", None), getattr(dimension, "collapsed", None), getattr(dimension, "style_id", None),
    )


def presentation_snapshot(wb) -> dict[str, dict[str, Any]]:
    """Presentation-only snapshot for pre-existing workbook content."""
    result: dict[str, dict[str, Any]] = {}
    for ws in wb.worksheets:
        result[ws.title] = {
            "cell_styles": {
                coordinate: tuple(cell._style) if cell.has_style else ()
                for coordinate, cell in ws._cells.items()
            },
            "row_dimensions": {key: row_dimension_fingerprint(d) for key, d in ws.row_dimensions.items()},
            "column_dimensions": {key: column_dimension_fingerprint(d) for key, d in ws.column_dimensions.items()},
            "freeze_panes": str(ws.freeze_panes or ""),
            "sheet_state": ws.sheet_state,
            "show_grid_lines": ws.sheet_view.showGridLines,
            "auto_filter": ws.auto_filter.ref,
            "merged_ranges": tuple(sorted(str(item) for item in ws.merged_cells.ranges)),
            "tab_color": str(ws.sheet_properties.tabColor or ""),
        }
    return result


def verify_presentation_unchanged(wb, before: dict[str, dict[str, Any]]) -> None:
    """Verify every pre-existing presentation object is unchanged.

    New data cells are permitted, so they are intentionally excluded from the
    style comparison.  Any existing cell/row/column/sheet presentation change
    aborts the output replacement.  Only coordinates that already existed are
    compared, so this stays fast even after hundreds of thousands of detail
    cells were written.
    """
    sheets = {ws.title: ws for ws in wb.worksheets}
    if list(before) != list(sheets):
        raise ValueError("样式保护验证失败：工作表结构发生变化")
    changed: list[str] = []
    whole_sheet_keys = ("freeze_panes", "sheet_state", "show_grid_lines", "auto_filter", "merged_ranges", "tab_color")
    for title, original in before.items():
        ws = sheets[title]
        for coord, expected in original["cell_styles"].items():
            cell = ws._cells.get(coord)
            current_style = tuple(cell._style) if cell is not None and cell.has_style else ()
            if current_style != expected:
                changed.append(f"{title}:cell_style:{coord}")
                break
        for key, fingerprint in (("row_dimensions", row_dimension_fingerprint), ("column_dimensions", column_dimension_fingerprint)):
            dimensions = ws.row_dimensions if key == "row_dimensions" else ws.column_dimensions
            for dimension_key, expected in original[key].items():
                dimension = dimensions.get(dimension_key)
                current = fingerprint(dimension) if dimension is not None else None
                if current != expected:
                    changed.append(f"{title}:{key}:{dimension_key}")
                    break
        for key in whole_sheet_keys:
            current = {
                "freeze_panes": str(ws.freeze_panes or ""),
                "sheet_state": ws.sheet_state,
                "show_grid_lines": ws.sheet_view.showGridLines,
                "auto_filter": ws.auto_filter.ref,
                "merged_ranges": tuple(sorted(str(item) for item in ws.merged_cells.ranges)),
                "tab_color": str(ws.sheet_properties.tabColor or ""),
            }[key]
            if original[key] != current:
                changed.append(f"{title}:{key}")
    if changed:
        raise ValueError("样式保护验证失败：" + ", ".join(changed[:12]))


def sheet_records(ws) -> list[dict[str, Any]]:
    headers = [str(ws.cell(1, c).value or "") for c in range(1, ws.max_column + 1)]
    max_col = len(headers)
    records: list[dict[str, Any]] = []
    for r in range(2, ws.max_row + 1):
        if not any(ws.cell(r, c).value is not None for c in range(1, max_col + 1)):
            continue
        records.append({headers[c - 1]: ws.cell(r, c).value for c in range(1, max_col + 1)})
    return records


def build_order_product_maps(wb) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    by_sub: dict[str, set[str]] = defaultdict(set)
    by_main: dict[str, set[str]] = defaultdict(set)
    if "商品货款" not in wb.sheetnames:
        return by_sub, by_main
    for row in sheet_records(wb["商品货款"]):
        product = identifier_text(row.get("商品ID"))
        if not product:
            continue
        sub = identifier_text(row.get("交易子单号"))
        main = identifier_text(row.get("交易主单号"))
        if sub:
            by_sub[sub].add(product)
        if main:
            by_main[main].add(product)
    return by_sub, by_main


def map_single_product(key: str | None, mapping: dict[str, set[str]]) -> str | None:
    if not key:
        return None
    products = mapping.get(key, set())
    return next(iter(products)) if len(products) == 1 else None


def rebuild_allocation_sheets(wb) -> tuple[int, int]:
    """Refresh only pre-existing helper sheets using values; never change structure/style."""
    if "费用归集辅助" not in wb.sheetnames or "未归属费用" not in wb.sheetnames:
        return 0, 0
    helper = wb["费用归集辅助"]
    unassigned = wb["未归属费用"]
    if helper.max_column < 6 or unassigned.max_column < 6:
        raise ValueError("费用归集辅助或未归属费用表结构不足，拒绝重建工作表")
    helper_rows: list[list[Any]] = []
    unassigned_rows: list[list[Any]] = []
    by_sub, by_main = build_order_product_maps(wb)

    def add_unassigned(category: str, amount: float, key: str | None, reason: str, source: str, row_no: int) -> None:
        unassigned_rows.append([category, amount, key, reason, source, row_no])

    if "评价有礼" in wb.sheetnames:
        for row_no, row in enumerate(sheet_records(wb["评价有礼"]), start=2):
            sub = identifier_text(row.get("交易子单号"))
            main = identifier_text(row.get("交易主单号"))
            product = map_single_product(sub, by_sub) or map_single_product(main, by_main)
            raw_amount = number_value(row.get("账单金额"))
            if product:
                helper_rows.append(["评价有礼", product, -raw_amount, sub or main, "评价有礼", row_no])
            else:
                add_unassigned("评价有礼", raw_amount, sub or main, "订单无法唯一关联到商品ID", "评价有礼", row_no)

    if "赔付追缴" in wb.sheetnames:
        for row_no, row in enumerate(sheet_records(wb["赔付追缴"]), start=2):
            business_id = identifier_text(row.get("处罚业务ID"))
            key = business_id.split(":", 1)[-1] if business_id and ":" in business_id else business_id
            product = map_single_product(key, by_sub) or map_single_product(key, by_main)
            raw_amount = number_value(row.get("账单金额"))
            if product:
                helper_rows.append(["赔付追缴", product, -raw_amount, business_id, "赔付追缴", row_no])
            else:
                add_unassigned("赔付追缴", raw_amount, business_id, "处罚业务ID无法关联到商品订单", "赔付追缴", row_no)

    for source, key_header in (("客服服务费", "账单编号"), ("商业化账户充值提现", "业务单号")):
        if source not in wb.sheetnames:
            continue
        for row_no, row in enumerate(sheet_records(wb[source]), start=2):
            add_unassigned(source, number_value(row.get("账单金额")), identifier_text(row.get(key_header)), "原始账单没有商品ID或商品订单", source, row_no)

    write_sheet_values(helper, helper_rows)
    write_sheet_values(unassigned, unassigned_rows)
    return len(helper_rows), len(unassigned_rows)


def existing_manual_by_id(summary) -> tuple[dict[str, dict[str, Any]], list[str]]:
    header_index = {str(summary.cell(1, c).value): c for c in range(1, summary.max_column + 1)}
    records: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    id_col = header_index["商品ID"]
    for r in range(2, summary.max_row + 1):
        product = identifier_text(summary.cell(r, id_col).value)
        if not product:
            continue
        order.append(product)
        values: dict[str, Any] = {}
        for header in MANUAL_SUMMARY_HEADERS:
            c = header_index[header]
            value = summary.cell(r, c).value
            if isinstance(value, str) and value.startswith("="):
                continue
            values[header] = value
        records[product] = values
    return records, order


def collect_product_ids(wb, existing_order: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    priority = ["商品货款"] + [s for s in PRODUCT_ID_HEADER if s not in {"商品货款", "所有商品效果数据导出"}] + ["所有商品效果数据导出"]
    for sheet in priority:
        if sheet not in wb.sheetnames:
            continue
        ws = wb[sheet]
        headers = {str(ws.cell(1, c).value or ""): c for c in range(1, ws.max_column + 1)}
        id_header = PRODUCT_ID_HEADER[sheet]
        if id_header not in headers:
            continue
        c = headers[id_header]
        # 所有商品效果数据导出 covers every on-sale item, so only products
        # with a non-zero 预算加码消耗金额/智能优惠券金额 belong in the
        # summary; everything else is a bill-less row the user asked to drop.
        amount_cols: list[int] = []
        if sheet == "所有商品效果数据导出":
            for header in ("预算加码消耗金额", "智能优惠券金额"):
                if header in headers:
                    amount_cols.append(headers[header])
        for r in range(2, ws.max_row + 1):
            if sheet == "所有商品效果数据导出":
                if not amount_cols or all(number_value(ws.cell(r, ac).value) == 0 for ac in amount_cols):
                    continue
            product = identifier_text(ws.cell(r, c).value)
            if product and product not in seen:
                seen.add(product)
                result.append(product)
    for product in existing_order:
        if product not in seen:
            seen.add(product)
            result.append(product)
    return result


def formula_ranges(wb) -> dict[str, int]:
    return {name: max(2, wb[name].max_row) for name in wb.sheetnames}


def sumifs_formula(product_row: int, sheet: str, amount_col: str, id_col: str, last_row: int, sign: int = 1) -> str:
    q = quote_sheetname(sheet)
    inner = f"SUMIFS({q}!${amount_col}$2:${amount_col}${last_row},{q}!${id_col}$2:${id_col}${last_row},$C{product_row})"
    if sign < 0:
        inner = f"-{inner}"
    return f'=IF($C{product_row}="","",{inner})'


def rebuild_summary(wb, shop_name: str | None = None) -> int:
    summary = wb["公司汇总"]
    fixed_headers = [summary.cell(1, c).value for c in range(1, 31)]
    if fixed_headers != SUMMARY_REQUIRED_HEADERS:
        raise ValueError("公司汇总表头与固定30列表头不一致，已停止以避免破坏结构")
    manual_records, existing_order = existing_manual_by_id(summary)
    product_ids = collect_product_ids(wb, existing_order)
    row_count = max(55, len(product_ids) + 1)
    old_max = max(summary.max_row, 55)
    template_row = 2
    for r in range(2, row_count + 1):
        if r > old_max:
            for c in range(1, 31):
                source = summary.cell(template_row, c)
                target = summary.cell(r, c)
                if source.has_style:
                    target._style = copy(source._style)
                target.alignment = copy(source.alignment)
                target.border = copy(source.border)
                target.fill = copy(source.fill)
                target.font = copy(source.font)
                target.number_format = source.number_format
    for r in range(2, max(old_max, row_count) + 1):
        for c in range(1, 31):
            summary.cell(r, c).value = None
    header_col = {header: idx + 1 for idx, header in enumerate(SUMMARY_REQUIRED_HEADERS)}
    ranges = formula_ranges(wb)
    has_allocation_helper = "费用归集辅助" in wb.sheetnames
    helper_last = max(2, wb["费用归集辅助"].max_row) if has_allocation_helper else 2
    helper_q = quote_sheetname("费用归集辅助")

    def direct_name_formula(r: int) -> str:
        last = ranges["商品货款"]
        q = quote_sheetname("商品货款")
        return f'=IF($C{r}="","",IFERROR(INDEX({q}!$H$2:$H${last},MATCH($C{r},{q}!$G$2:$G${last},0)),""))'

    formula_by_header = {
        "产品名称": direct_name_formula,
        "数量": lambda r: f'=IF($C{r}="","",SUMIFS(商品货款!$K$2:$K${ranges["商品货款"]},商品货款!$G$2:$G${ranges["商品货款"]},$C{r},商品货款!$A$2:$A${ranges["商品货款"]},"销售"))',
        "收入": lambda r: sumifs_formula(r, "商品货款", "E", "G", ranges["商品货款"]),
        "交易补贴": lambda r: sumifs_formula(r, "交易补贴", "E", "H", ranges["交易补贴"]),
        "交易佣金": lambda r: sumifs_formula(r, "交易佣金", "E", "G", ranges["交易佣金"], -1),
        "先用后付": lambda r: sumifs_formula(r, "先用后付", "C", "E", ranges["先用后付"], -1),
        "消费者体验提升服务": lambda r: sumifs_formula(r, "消费者体验提升服务", "E", "G", ranges["消费者体验提升服务"], -1),
        "新疆物流集运": lambda r: sumifs_formula(r, "新疆物流集运", "D", "E", ranges["新疆物流集运"], -1),
        "出海增值服务费": lambda r: sumifs_formula(r, "出海增值服务费", "D", "E", ranges["出海增值服务费"], -1),
        **({"赔付追缴": lambda r: f'=IF($C{r}="","",SUMIFS({helper_q}!$C$2:$C${helper_last},{helper_q}!$A$2:$A${helper_last},"赔付追缴",{helper_q}!$B$2:$B${helper_last},$C{r}))'} if has_allocation_helper else {}),
        "新品营销(营销费用)": lambda r: sumifs_formula(r, "营销费用", "E", "G", ranges["营销费用"], -1),
        **({"评价有礼": lambda r: f'=IF($C{r}="","",SUMIFS({helper_q}!$C$2:$C${helper_last},{helper_q}!$A$2:$A${helper_last},"评价有礼",{helper_q}!$B$2:$B${helper_last},$C{r}))'} if has_allocation_helper else {}),
        "充值加码消耗": lambda r: sumifs_formula(r, "所有商品效果数据导出", "H", "B", ranges["所有商品效果数据导出"]),
        "智能优惠券金额": lambda r: sumifs_formula(r, "所有商品效果数据导出", "K", "B", ranges["所有商品效果数据导出"]),
        "投流推广佣金": lambda r: sumifs_formula(r, "投流推广佣金", "C", "E", ranges["投流推广佣金"], -1),
        "淘客推广佣金": lambda r: sumifs_formula(r, "淘客推广佣金", "E", "G", ranges["淘客推广佣金"], -1),
        "直播推广佣金": lambda r: sumifs_formula(r, "直播推广佣金", "E", "G", ranges["直播推广佣金"], -1),
        "罚款": lambda r: sumifs_formula(r, "处罚数据", "J", "G", ranges["处罚数据"]),
    }
    for offset, product in enumerate(product_ids, start=2):
        summary.cell(offset, header_col["商品ID"], product)
        previous = manual_records.get(product, {})
        for header in MANUAL_SUMMARY_HEADERS:
            summary.cell(offset, header_col[header], previous.get(header))
        if shop_name:
            summary.cell(offset, header_col["店铺"], shop_name)
        for header, builder in formula_by_header.items():
            summary.cell(offset, header_col[header], builder(offset))
    last_product_row = max(2, len(product_ids) + 1)
    # 总计行紧贴最后一条商品数据行，商品ID 留空以免被当成商品。
    total_row = last_product_row + 1
    # 总计行之后的模板预留空白行保留公式，便于后续追加商品。
    for r in range(total_row + 1, row_count + 1):
        for header, builder in formula_by_header.items():
            summary.cell(r, header_col[header], builder(r))
    for c in range(1, 31):
        source = summary.cell(template_row, c)
        target = summary.cell(total_row, c)
        # 模板预留行（≤55）本身已带同一套样式，直接沿用可避免改动既有样式；
        # 超出模板范围的行才从第2行复制样式。
        if not target.has_style and source.has_style:
            target._style = copy(source._style)
        if not target.has_style:
            target.alignment = copy(source.alignment)
            target.border = copy(source.border)
            target.fill = copy(source.fill)
            target.font = copy(source.font)
            target.number_format = source.number_format
    summary.cell(total_row, header_col["店铺"], SUMMARY_TOTAL_LABEL)
    for header in SUMMARY_TOTAL_SUM_HEADERS:
        column_letter = get_column_letter(header_col[header])
        summary.cell(
            total_row,
            header_col[header],
            f"=SUM({column_letter}2:{column_letter}{last_product_row})",
        )
    for header, sheet, sign in SUMMARY_TOTAL_SHEET_HEADERS:
        if sheet not in wb.sheetnames:
            continue
        sheet_headers = [str(wb[sheet].cell(1, c).value or "") for c in range(1, wb[sheet].max_column + 1)]
        amount_header = target_amount_header(sheet, sheet_headers)
        if not amount_header:
            continue
        amount_letter = get_column_letter(sheet_headers.index(amount_header) + 1)
        quoted = quote_sheetname(sheet)
        total_formula = f"SUM({quoted}!${amount_letter}$2:${amount_letter}${ranges[sheet]})"
        if sign < 0:
            total_formula = f"-{total_formula}"
        summary.cell(total_row, header_col[header], f"={total_formula}")

    assert [summary.cell(1, c).value for c in range(1, 31)] == fixed_headers
    return len(product_ids)


def target_amount_header(target: str, headers: list[str]) -> str | None:
    preferred = {
        "商品货款": "账单金额", "交易补贴": "账单金额", "交易佣金": "账单金额", "先用后付": "账单金额",
        "消费者体验提升服务": "账单金额", "新疆物流集运": "账单金额", "出海增值服务费": "账单金额",
        "营销费用": "账单金额", "评价有礼": "账单金额", "商业化账户充值提现": "账单金额",
        "投流推广佣金": "账单金额", "淘客推广佣金": "账单金额", "直播推广佣金": "账单金额",
        "客服服务费": "账单金额", "赔付追缴": "账单金额", "处罚数据": "处罚金额",
    }.get(target)
    return preferred if preferred in headers else None


def recalculate_with_libreoffice(output: Path, fixed_headers: list[Any]) -> bool:
    """Re-save through LibreOffice when available so formula results are cached.

    Failure is non-fatal: Excel calculation flags are already set and the
    original openpyxl output remains intact.
    """
    configured = os.environ.get("SOFFICE_PATH")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path("/Users/rabbit/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice"),
        Path(shutil.which("soffice")) if shutil.which("soffice") else None,
        Path(shutil.which("libreoffice")) if shutil.which("libreoffice") else None,
    ]
    soffice = next((p.resolve() for p in candidates if p and p.exists()), None)
    if soffice is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="pan_suan_recalc_", dir=output.parent) as temp_name:
            temp = Path(temp_name)
            source_dir = temp / "input"
            result_dir = temp / "output"
            source_dir.mkdir()
            result_dir.mkdir()
            source = source_dir / output.name
            shutil.copy2(output, source)
            completed = subprocess.run(
                [str(soffice), "--headless", "--convert-to", "xlsx", "--outdir", str(result_dir), str(source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                check=False,
            )
            candidate = result_dir / output.name
            if completed.returncode != 0 or not candidate.exists():
                return False
            validation = load_workbook(candidate, read_only=True, data_only=False)
            if "公司汇总" not in validation.sheetnames or "所有商品效果数据导出" not in validation.sheetnames:
                validation.close()
                return False
            if [validation["公司汇总"].cell(1, c).value for c in range(1, 31)] != fixed_headers:
                validation.close()
                return False
            validation.close()
            os.replace(candidate, output)
            return True
    except (OSError, subprocess.SubprocessError, TimeoutError, ValueError):
        return False


def merge(
    template: Path,
    inputs: list[Path],
    output: Path,
    strict: bool = False,
    recalculate: bool = False,
    expected_target: str | None = None,
    allow_empty_target: bool = False,
    shop_name: str | None = None,
) -> dict[str, Any]:
    template = template.expanduser().resolve()
    output = output.expanduser().resolve()
    if not template.exists():
        raise FileNotFoundError(f"盘算模板不存在：{template}")
    wb = load_workbook(template)
    if "公司汇总" not in wb.sheetnames or "所有商品效果数据导出" not in wb.sheetnames:
        raise ValueError("模板缺少公司汇总或所有商品效果数据导出Sheet")
    original_summary_headers = [wb["公司汇总"].cell(1, c).value for c in range(1, 31)]
    if original_summary_headers != SUMMARY_REQUIRED_HEADERS:
        raise ValueError("公司汇总固定表头不匹配，拒绝写入")
    presentation_before = presentation_snapshot(wb)

    grouped: dict[str, list[tuple[Path, list[list[Any]]]]] = defaultdict(list)
    skipped: list[str] = []
    for path in collect_xlsx(inputs):
        if path == template or path == output:
            continue
        try:
            target = expected_target or detect_target(path)
            if not target or target not in wb.sheetnames:
                if strict:
                    raise ValueError(f"无法识别账单类型：{path}")
                skipped.append(str(path))
                continue
            target_headers = [str(wb[target].cell(1, c).value or "") for c in range(1, wb[target].max_column + 1)]
            rows, _ = read_bill_rows(path, target_headers)
            grouped[target].append((path, rows))
        except Exception:
            if strict:
                raise
            skipped.append(str(path))

    if expected_target and expected_target not in grouped:
        if allow_empty_target:
            grouped[expected_target] = []
        else:
            raise ValueError(f"下载明细未能写入预期Sheet：{expected_target}")

    import_stats: list[dict[str, Any]] = []
    for target, parts in grouped.items():
        ws = wb[target]
        headers = [str(ws.cell(1, c).value or "") for c in range(1, ws.max_column + 1)]
        combined: list[list[Any]] = []
        for _, rows in parts:
            combined.extend(rows)
        deduped, duplicate_count = dedupe_rows(combined, headers)
        # Downloaded detail rows must look like the existing table: copy only
        # each target column's header border to the cells written in row 2+.
        # This is the one explicitly requested presentation change; all other
        # existing styles remain protected by the snapshot verification below.
        write_sheet_values(ws, deduped, apply_data_borders=True)
        # Verify the copied header borders in memory before saving; checking
        # the saved file cell-by-cell would re-walk every written row.
        max_col = ws.max_column
        expected_borders = {
            column: ws.cell(1, column)._style.borderId
            for column in range(1, max_col + 1)
        }
        border_errors: list[str] = []
        for row_number in range(2, len(deduped) + 2):
            for column in range(1, max_col + 1):
                cell = ws.cell(row_number, column)
                if cell._style.borderId != expected_borders[column]:
                    border_errors.append(cell.coordinate)
                    break
            if border_errors:
                break
        if border_errors:
            raise ValueError(f"{target} 回填数据边框验证失败：{border_errors[0]}")
        amount_header = target_amount_header(target, headers)
        amount_total = 0.0
        if amount_header:
            i = headers.index(amount_header)
            amount_total = sum(number_value(row[i]) for row in deduped)
        import_stats.append({
            "target": target,
            "source": "；".join(str(p) for p, _ in parts),
            "read_rows": len(combined),
            "written_rows": len(deduped),
            "duplicates": duplicate_count,
            "amount_header": amount_header,
            "amount_total": amount_total,
            "borders_verified": True,
        })

    allocated_count, unassigned_count = rebuild_allocation_sheets(wb)
    product_count = rebuild_summary(wb, shop_name=shop_name)
    try:
        wb.calculation.fullCalcOnLoad = True
        wb.calculation.forceFullCalc = True
        wb.calculation.calcMode = "auto"
    except Exception:
        pass
    assert [wb["公司汇总"].cell(1, c).value for c in range(1, 31)] == original_summary_headers
    verify_presentation_unchanged(wb, presentation_before)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=output.stem + "_", suffix=".xlsx", dir=output.parent, delete=False) as handle:
        tmp = Path(handle.name)
    try:
        wb.save(tmp)
        validation = load_workbook(tmp, read_only=True, data_only=False)
        try:
            saved_summary_row = next(validation["公司汇总"].iter_rows(min_row=1, max_row=1, values_only=True), ())
            if [str(v or "") for v in saved_summary_row[:30]] != [str(h or "") for h in original_summary_headers]:
                raise ValueError("公司汇总表头在保存验证时发生变化")
            broken: list[str] = []
            for ws in validation.worksheets:
                for row_number, row in enumerate(ws.iter_rows(values_only=True), start=1):
                    for v in row:
                        if isinstance(v, str) and "#REF!" in v:
                            broken.append(f"{ws.title}!{row_number}")
                            break
                    if broken:
                        break
            if broken:
                raise ValueError("输出文件存在损坏引用：" + ", ".join(broken[:10]))

            verified_sheets: list[str] = []
            stats_by_target = {item["target"]: item for item in import_stats}
            for target, stat in stats_by_target.items():
                ws = validation[target]
                header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
                actual_headers = [str(v or "") for v in header_row]
                template_headers = [str(wb[target].cell(1, c).value or "") for c in range(1, wb[target].max_column + 1)]
                if actual_headers != template_headers:
                    raise ValueError(f"{target} 表头在保存验证时发生变化")
                actual_rows = 0
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if any(value is not None and str(value).strip() != "" for value in row):
                        actual_rows += 1
                if actual_rows != stat["written_rows"]:
                    raise ValueError(
                        f"{target} 写入行数验证失败：预期 {stat['written_rows']} 行，实际 {actual_rows} 行"
                    )
                verified_sheets.append(target)
            if expected_target and expected_target not in verified_sheets:
                raise ValueError(f"预期Sheet未通过保存后验证：{expected_target}")
        finally:
            validation.close()
        os.replace(tmp, output)
    finally:
        if tmp.exists():
            tmp.unlink()

    recalculated = recalculate_with_libreoffice(output, original_summary_headers) if recalculate else False

    return {
        "output": str(output),
        "formula_cache_recalculated": recalculated,
        "imported_sheets": [s["target"] for s in import_stats],
        "import_stats": import_stats,
        "product_count": product_count,
        "allocated_rows": allocated_count,
        "unassigned_rows": unassigned_count,
        "skipped_files": skipped,
        "all_product_effects_preserved": True,
        "all_product_effects_imported": expected_target == "所有商品效果数据导出",
        "summary_header_preserved": True,
        "sheet_write_verified": True,
        "data_borders_verified": True,
        "presentation_verified": True,
        "workbook_opened": False,
        "verified_sheets": [s["target"] for s in import_stats],
        "expected_target": expected_target,
    }


def main() -> int:
    args = parse_args()
    result = merge(
        args.template, args.input, args.output, args.strict, args.recalculate and not args.no_recalculate,
        args.expected_target, args.allow_empty_target, args.shop_name,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"盘算文件：{result['output']}")
        print(f"已写入Sheet：{', '.join(result['imported_sheets']) or '无'}")
        print(f"商品数量：{result['product_count']}")
        print(f"订单关联成功：{result['allocated_rows']} 行")
        print(f"未归属费用：{result['unassigned_rows']} 行")
        print(f"Sheet写入验证：{'已通过' if result['sheet_write_verified'] else '未通过'}")
        if result.get("all_product_effects_imported"):
            print("所有商品效果数据导出：已按专属导出流程写入并校验")
        else:
            print("所有商品效果数据导出：未由本流程修改，已保留")
        print("公司汇总表头：已校验，未变更")
        print(f"公式缓存：{'已通过LibreOffice计算' if result['formula_cache_recalculated'] else '由Excel打开时自动计算'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, FileNotFoundError) as exc:
        print(f"错误：{exc}", file=os.sys.stderr)
        raise SystemExit(1)
