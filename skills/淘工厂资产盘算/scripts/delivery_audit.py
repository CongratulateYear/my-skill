"""Independent, fail-closed reconciliation for the installed merge builder.

No formula evaluator and no builder imports: expected amounts are Decimal sums of
source detail, NOT sums of 公司汇总 or 费用归集辅助. Signs intentionally follow the
builder contract. A zero-revenue company may still have account-level expenses.

Safe integration: populate_audit(formula_workbook) BEFORE the existing Kit cache
refresh, save once, refresh caches, then verify_audit(candidate_path). Never save
with openpyxl after refresh. ``blocking`` is the publication gate; manual costs
and profit are explicitly outside the independent reconciliation scope.
"""
from __future__ import annotations

from collections import defaultdict, Counter
from hashlib import sha256
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter, quote_sheetname

TOLERANCE = Decimal("0.005")  # half a cent, absolute, no rounding before comparison
AUDIT_SHEET = "金额对账"
SOURCE_SHEET = "对账来源明细"
SOURCE_HEADERS = ("审计页行号", "分片序号", "来源文本（按序无损拼接）")
EXCEL_CELL_LIMIT = 32767
SOURCE_CHUNK_LIMIT = 30000  # UTF-16 units; also safe for supplementary characters
EXCEL_ROW_LIMIT = 1048576


def _source_layout(checks):
    """Deterministic text manifest + exact chunks; never truncate or deduplicate.

    The canonical source order is the existing natural row sort/range compaction.
    Chunk boundaries have no semantic meaning: concatenate column C in sequence.
    The digest is a locator checksum, not a substitute for source recomputation.
    """
    labels, rows = [], []
    for audit_row, check in enumerate(checks, 2):
        text = ";".join(check["source_refs"])
        if len(text.encode("utf-16-le")) // 2 <= EXCEL_CELL_LIMIT:
            labels.append(text)
            continue
        chunks, current, units = [], [], 0
        for char in text:
            width = 2 if ord(char) > 0xFFFF else 1
            if units + width > SOURCE_CHUNK_LIMIT:
                chunks.append("".join(current))
                current, units = [], 0
            current.append(char)
            units += width
        if current:
            chunks.append("".join(current))
        start = len(rows) + 2
        if len(rows) + len(chunks) + 1 > EXCEL_ROW_LIMIT:
            raise ValueError("对账来源明细超过Excel行数上限；禁止截断后发布")
        rows.extend((audit_row, sequence, chunk) for sequence, chunk in enumerate(chunks, 1))
        end = len(rows) + 1
        digest = sha256(text.encode("utf-8")).hexdigest()
        labels.append(f"{quote_sheetname(SOURCE_SHEET)}!C{start}:C{end}；分片={len(chunks)}；SHA256={digest}")
    return labels, rows
PASS, NA, UNKNOWN, FAIL = "通过", "不适用", "无法核对", "差额异常"
# summary header, detail sheet, amount header, product header, expense sign
DIRECT = (
    ("收入", "商品货款", "账单金额", "商品ID", 1),
    ("交易补贴", "交易补贴", "账单金额", "商品ID", 1),
    ("交易佣金", "交易佣金", "账单金额", "商品ID", -1),
    ("先用后付", "先用后付", "账单金额", "商品ID", -1),
    ("消费者体验提升服务", "消费者体验提升服务", "账单金额", "商品ID", -1),
    ("新疆物流集运", "新疆物流集运", "账单金额", "商品ID", -1),
    ("出海增值服务费", "出海增值服务费", "账单金额", "商品ID", -1),
    ("新品营销(营销费用)", "营销费用", "账单金额", "商品ID", -1),
    ("充值加码消耗", "所有商品效果数据导出", "预算加码消耗金额", "商品id", 1),
    ("智能优惠券金额", "所有商品效果数据导出", "智能优惠券金额", "商品id", 1),
    ("投流推广佣金", "投流推广佣金", "账单金额", "商品ID", -1),
    ("淘客推广佣金", "淘客推广佣金", "账单金额", "商品ID", -1),
    ("直播推广佣金", "直播推广佣金", "账单金额", "商品ID", -1),
    ("罚款", "处罚数据", "处罚金额", "商品id", 1),
)
ACCOUNT = ("赔付追缴", "评价有礼", "客服服务费", "商业化账户充值提现")
MANUAL = ("刷单金额", "直通车营销", "收入金额", "货款", "利润")
HEADERS = ("范围", "项目", "商品ID", "来源依据", "独立预期", "实际/缓存", "差额", "状态", "说明", "核对坐标", "绝对容差", "阻断项")


def _id(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValueError("布尔值不是商品/订单标识")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip().lstrip("'")
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _money(value):
    if value is None or value == "" or isinstance(value, bool):
        raise ValueError("金额为空或非金额")
    if isinstance(value, str):
        value = value.strip().replace(",", "").replace("￥", "").replace("¥", "")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"金额不可解析: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("金额非有限数")
    return result


def _records(ws, required):
    rows = ws.iter_rows(values_only=True)
    headers = [str(x or "").strip() for x in next(rows, ())]
    for h in required:
        if headers.count(h) != 1:
            raise ValueError(f"{ws.title}: 必需列缺失或重复: {h}")
    return [(n, dict(zip(headers, row))) for n, row in enumerate(rows, 2) if any(x is not None for x in row)]


def _report(wb, cached, sources=None):
    checks, warnings = [], []
    opened = []
    source_sheets = {}
    # Optional external evidence must be explicit sheet->xlsx mapping. Each file
    # has either the named sheet or exactly one sheet. Never scan sibling files.
    if sources is not None:
        if not isinstance(sources, dict):
            raise TypeError("sources must be a mapping of detail sheet name to xlsx path")
        for name, path in sources.items():
            sw = load_workbook(path, read_only=True, data_only=False)
            opened.append(sw)
            if name in sw.sheetnames:
                source_sheets[name] = sw[name]
            elif len(sw.sheetnames) == 1:
                source_sheets[name] = sw.worksheets[0]
            else:
                raise ValueError(f"外部来源无法唯一定位Sheet: {name}: {path}")

    def add(scope, category, product="", expected=None, actual=None, refs=(), target="", note="", status=None, gate=True):
        difference = None
        if status is None:
            try:
                actual = _money(actual)
                difference = actual - expected
                status = PASS if abs(difference) <= TOLERANCE else FAIL
            except (ValueError, TypeError):
                actual = None
                status = UNKNOWN
                note = (note + "；缓存缺失、公式错误或实际金额不可解析").strip("；")
        # Compact contiguous provenance to avoid Excel's 32767-character cell
        # truncation on large zero-effect partitions; retain exact row coverage.
        compact = []
        def provenance_key(ref):
            sheet, separator, number = ref.rpartition('!row:')
            return (sheet, 0, int(number)) if separator and number.isdigit() else (ref, 1, 0)
        # Totals arrive grouped by product, not source-row order. Canonicalize
        # before lossless range compaction so Excel cannot truncate provenance.
        # Sorting retains duplicates; only consecutive distinct rows are merged.
        for ref in sorted(refs, key=provenance_key):
            if compact and "!row:" in ref and "!row:" in compact[-1]:
                sheet, number = ref.rsplit("!row:", 1)
                previous_sheet, previous = compact[-1].rsplit("!row:", 1)
                if sheet == previous_sheet and number.isdigit() and previous.split("-")[-1].isdigit() and int(number) == int(previous.split("-")[-1]) + 1:
                    compact[-1] = sheet + "!row:" + previous.split("-")[0] + "-" + number
                    continue
            compact.append(ref)
        item = dict(scope=scope, category=category, product_id=product,
                    expected=str(expected) if expected is not None else None,
                    actual=str(actual) if actual is not None else None,
                    difference=str(difference) if difference is not None else None,
                    status=status, blocking=gate and status in (FAIL, UNKNOWN),
                    source_refs=compact, target=target, note=note)
        checks.append(item)
        return item

    try:
        if "公司汇总" not in wb.sheetnames:
            add("结构", "公司汇总", status=UNKNOWN, note="缺少公司汇总Sheet")
            return _finish(checks, warnings)
        summary = wb["公司汇总"]
        heads = [c.value for c in summary[1]]
        needed = {"商品ID", "店铺", "数量"} | {x[0] for x in DIRECT} | set(ACCOUNT) | set(MANUAL)
        if any(heads.count(x) != 1 for x in needed):
            add("结构", "公司汇总", status=UNKNOWN, note="关键表头缺失或重复")
            return _finish(checks, warnings)
        cols = {h: n + 1 for n, h in enumerate(heads)}
        totals, products = [], {}
        for row in summary.iter_rows(min_row=2):
            n = row[0].row
            if row[cols["店铺"] - 1].value == "总计":
                totals.append(n)
            pid = _id(row[cols["商品ID"] - 1].value)
            if pid:
                if pid in products:
                    add("结构", "商品ID", pid, status=FAIL, note="公司汇总商品ID重复")
                products[pid] = n
        if len(totals) != 1:
            add("结构", "总计", status=UNKNOWN, note="总计行必须恰有一行")
        total_row = totals[0] if len(totals) == 1 else None

        def compare(scope, category, expected, row, product="", refs=(), note=""):
            if row is None:
                return
            coord = f"{get_column_letter(cols[category])}{row}"
            target = f"{quote_sheetname('公司汇总')}!{coord}"
            return add(scope, category, product, expected, cached["公司汇总"][coord].value if cached else None, refs, target, note)

        def detail(name, required):
            sheet = source_sheets.get(name)
            if sheet is None:
                if name not in wb.sheetnames:
                    raise ValueError(f"缺少明细Sheet: {name}")
                sheet = wb[name]
            return _records(sheet, required)

        income = None
        for category, name, amount_header, id_header, sign in DIRECT:
            try:
                records = detail(name, (amount_header, id_header))
                amounts, refs = defaultdict(Decimal), defaultdict(list)
                for n, record in records:
                    pid = _id(record[id_header])
                    value = _money(record[amount_header]) * sign
                    amounts[pid] += value
                    refs[pid].append(f"{name}!row:{n}")
                if category == "收入":
                    income = sum(amounts.values(), Decimal(0))
                for pid, row in products.items():
                    compare("逐商品", category, amounts[pid], row, pid, refs[pid], f"{name}.{amount_header} × {sign}，原始带符号")
                selected = sum((amounts[pid] for pid in products), Decimal(0))
                compare("商品合计", category, selected, total_row, refs=[x for pid in products for x in refs[pid]], note="仅商品行口径；不与整账单直接比较")
                for pid in amounts.keys() - products.keys():
                    if pid and amounts[pid] != 0:
                        add("覆盖", category, pid, amounts[pid], status=FAIL, refs=refs[pid], note="源商品未出现在公司汇总，不能伪装未归属")
                unassigned = sum((v for p, v in amounts.items() if p not in products), Decimal(0))
                add("分区说明", category, expected=unassigned, refs=[x for p in amounts if p not in products for x in refs[p]], status=NA, gate=False,
                    note=f"源全量={sum(amounts.values(), Decimal(0))}；商品归集={selected}；未归属/未覆盖={unassigned}；静态分区，不是假造公司汇总差额")
            except ValueError as exc:
                add("源明细", category, status=UNKNOWN, note=str(exc))

        # Quantity is the sole sales-only measure. Refunds remain in revenue.
        try:
            goods = detail("商品货款", ("商品ID", "交易子单号", "交易主单号", "单据方向", "购买件数"))
            sub, main, qty = defaultdict(set), defaultdict(set), defaultdict(Decimal)
            for n, r in goods:
                pid = _id(r["商品ID"])
                if pid:
                    if _id(r["交易子单号"]):
                        sub[_id(r["交易子单号"])].add(pid)
                    if _id(r["交易主单号"]):
                        main[_id(r["交易主单号"])].add(pid)
                    if r["单据方向"] == "销售":
                        qty[pid] += _money(r["购买件数"])
            for pid, row in products.items():
                compare("逐商品", "数量", qty[pid], row, pid, note="仅销售；非收入金额")
            compare("商品合计", "数量", sum((qty[p] for p in products), Decimal(0)), total_row)
        except ValueError as exc:
            sub, main = defaultdict(set), defaultdict(set)
            add("源明细", "数量/订单映射", status=UNKNOWN, note=str(exc))

        def unique(key, mapping):
            candidates = mapping.get(key, set())
            return next(iter(candidates)) if len(candidates) == 1 else ""

        helper_expected, unassigned_expected = [], []
        account_values = {}
        accounts_valid = True
        for name in ACCOUNT:
            try:
                required = ["账单金额"]
                required += {"赔付追缴": ["处罚业务ID"], "评价有礼": ["交易子单号", "交易主单号"], "客服服务费": ["账单编号"], "商业化账户充值提现": ["业务单号"]}[name]
                rows = detail(name, required)
                by_product, all_amount = defaultdict(Decimal), Decimal(0)
                refs = []
                for n, r in rows:
                    raw = _money(r["账单金额"])
                    all_amount -= raw
                    refs.append(f"{name}!row:{n}")
                    pid = ""
                    if name == "评价有礼":
                        key = _id(r["交易子单号"]) or _id(r["交易主单号"])
                        pid = unique(_id(r["交易子单号"]), sub) or unique(_id(r["交易主单号"]), main)
                    elif name == "赔付追缴":
                        key = _id(r["处罚业务ID"])
                        order = key.split(":", 1)[-1]
                        pid = unique(order, sub) or unique(order, main)
                    else:
                        key = _id(r["账单编号" if name == "客服服务费" else "业务单号"])
                    if pid:
                        by_product[pid] -= raw
                        helper_expected.append((name, pid, -raw, key, name, n))
                    else:
                        unassigned_expected.append((name, raw, key, name, n))
                account_values[name] = all_amount
                compare("全账单合计", name, all_amount, total_row, refs=refs, note="整源账单取负；包含商品归集与未归属，不再叠加商品费用")
                if name in ("赔付追缴", "评价有礼"):
                    for pid, row in products.items():
                        compare("逐商品", name, by_product[pid], row, pid, refs=refs, note="从商品货款唯一订单映射独立归集；不是读取辅助表当预期")
                    for pid in by_product.keys() - products.keys():
                        if by_product[pid]:
                            add("覆盖", name, pid, by_product[pid], status=FAIL, note="归集商品不在汇总")
                add("分区说明", name, expected=sum((v for cat, v, *_ in unassigned_expected if cat == name), Decimal(0)) * -1,
                    refs=refs, status=NA, gate=False, note=f"全源同口径={all_amount}；归集={sum(by_product.values(), Decimal(0))}；未归属原表符号已取反；客服/充值不得伪装商品收入")
            except ValueError as exc:
                accounts_valid = False
                add("源明细", name, status=UNKNOWN, note=str(exc))

        # Reconcile auxiliary lines with independently reconstructed provenance,
        # not just their sum: cancelling duplicate/wrong-source lines must fail.
        if accounts_valid:
            for sheet_name, expected, required, fields in (
                ("费用归集辅助", helper_expected, ("类别", "商品ID", "金额", "关联单号", "来源", "原始行号"), ("类别", "商品ID", "金额", "关联单号", "来源", "原始行号")),
                ("未归属费用", unassigned_expected, ("类别", "金额", "关联单号/业务单号", "来源", "原始行号"), ("类别", "金额", "关联单号/业务单号", "来源", "原始行号")),
            ):
                try:
                    if sheet_name not in wb.sheetnames:
                        raise ValueError(f"缺少{sheet_name}")
                    observed = []
                    for _, r in _records(wb[sheet_name], required):
                        vals = [r[f] for f in fields]
                        for index, field in enumerate(fields):
                            if field == "金额": vals[index] = _money(vals[index])
                            elif field == "原始行号": vals[index] = int(vals[index])
                            else: vals[index] = _id(vals[index])
                        observed.append(tuple(vals))
                    same = Counter(expected) == Counter(observed)
                    add("归集追溯", sheet_name, expected=Decimal(len(expected)), actual=Decimal(len(observed)), status=PASS if same else FAIL,
                        note="逐行类别/商品/金额/关联键/源Sheet/原始行号多重集一致" if same else "归集明细不一致（即使净额相等也阻断）")
                except (ValueError, TypeError) as exc:
                    add("归集追溯", sheet_name, status=UNKNOWN, note=str(exc))

        for name in MANUAL:
            populated = any(summary.cell(r, cols[name]).value not in (None, "") for r in products.values())
            add("手工口径", name, status=UNKNOWN if populated else NA, gate=False,
                note="存在手工值；无独立成本/利润依据，未核实" if populated else "手工成本/利润/调整空白，不猜测、不以0补值")
        if income == 0 and any(account_values.get(x, 0) != 0 for x in ("客服服务费", "商业化账户充值提现")):
            warnings.append("主要收入为零，但存在客服服务费或商业化账户充值提现；仅可确认费用/资金流水，不得伪造经营收入、成本或利润。")
        return _finish(checks, warnings)
    finally:
        for book in opened:
            book.close()


def _finish(checks, warnings):
    blocking = any(x["blocking"] for x in checks)
    status = FAIL if any(x["status"] == FAIL for x in checks) else UNKNOWN if blocking else PASS
    return dict(schema_version=1, status=status, blocking=blocking, tolerance=str(TOLERANCE),
                counts=dict(Counter(x["status"] for x in checks)), checks=checks, warnings=warnings,
                scope="源明细独立计算；逐商品、全账单和未归属分区分别核对；手工成本利润不推测")


def audit_workbook(path, sources=None) -> dict:
    """Read-only audit of recalculated caches; sources optionally {sheet: path}.

    Missing cache/schema/source is 无法核对 and blocks automatic checks; a
    difference beyond 0.005 is 差额异常. Manual unknowns are non-blocking and
    explicitly listed. No workbook is written, and no Excel engine is invoked.
    """
    wb = load_workbook(path, data_only=False)
    cached = None
    try:
        cached = load_workbook(path, data_only=True)
        result = _report(wb, cached, sources)
        result["path"] = str(Path(path).resolve())
        return result
    finally:
        wb.close()
        if cached: cached.close()


def populate_audit(wb) -> dict:
    """Add independent static expectations and live references before recalc.

    Mutates only NEW audit/source-detail sheets in the caller-owned candidate.
    Existing reserved sheets are rejected, never silently overwritten. The caller
    must save, recalculate with the approved engine, and call verify_audit.
    """
    if AUDIT_SHEET in wb.sheetnames or SOURCE_SHEET in wb.sheetnames:
        raise ValueError("金额对账/对账来源明细Sheet已存在；拒绝覆盖，须从原始合并候选重新生成")
    report = _report(wb, None)
    source_labels, source_rows = _source_layout(report["checks"])
    ws = wb.create_sheet(AUDIT_SHEET)
    ws.append(HEADERS)
    if source_rows:
        detail = wb.create_sheet(SOURCE_SHEET)
        detail.append(SOURCE_HEADERS)
        for row in source_rows:
            detail.append(row)
            # A fragment can begin '=' after a boundary. Always literal text.
            detail.cell(detail.max_row, 3).data_type = "s"
        for cell in detail[1]: cell.font = Font(bold=True)
        detail.freeze_panes = "A2"
        detail.column_dimensions["C"].width = 100
    for check, provenance in zip(report["checks"], source_labels):
        n = ws.max_row + 1
        target = check["target"]
        # String numeric expectations are deliberately immutable source-derived
        # evidence; formulas coerce them to numbers without binary preprocessing.
        expected = check["expected"]
        actual = f"={target}" if target else check["actual"]
        difference = f"=F{n}-E{n}" if target and expected is not None else None
        state = f'=IF(ISNUMBER(F{n}),IF(ABS(G{n})<=K{n},"{PASS}","{FAIL}"),"{UNKNOWN}")' if target else check["status"]
        ws.append((check["scope"], check["category"], check["product_id"], provenance, expected,
                   actual, difference, state, check["note"].replace("；缓存缺失、公式错误或实际金额不可解析", ""), target,
                   float(TOLERANCE), "是" if target or check["blocking"] else "否"))
        for column in (1, 2, 3, 4, 5, 9, 10, 12):
            ws.cell(n, column).data_type = "s" if ws.cell(n, column).value is not None else "n"
        if not target:
            ws.cell(n, 8).data_type = "s"
    for warning in report["warnings"]:
        ws.append(("提示", "零收入", None, None, None, None, None, NA, warning))
    for cell in ws[1]: cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ("D", "I"): ws.column_dimensions[col].width = 65
    for col in ("A", "B", "C", "J"): ws.column_dimensions[col].width = 24
    for col in ("E", "F", "G", "H"): ws.column_dimensions[col].width = 18
    return dict(schema_version=1, audit_sheet=AUDIT_SHEET, rows=ws.max_row - 1,
                needs_recalculation=True, verified=False,
                next_step="保存候选→批准的引擎重算→verify_audit(path)，重算后不得openpyxl保存")


def append_audit_sheet(source, destination) -> dict:
    """Create a new independent copy, never overwrite either real delivery.

    openpyxl clears all formula caches on save. This API intentionally returns
    needs_recalculation=True, NOT a successful delivery audit.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError("只允许独立副本；source与destination不得相同")
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有目标: {destination}")
    wb = load_workbook(source, data_only=False)
    try:
        result = populate_audit(wb)
        # Exclusive create prevents an accidental overwrite in a concurrent run.
        with destination.open("xb") as stream:
            wb.save(stream)
    finally:
        wb.close()
    result["destination"] = str(destination)
    return result


def verify_audit(path) -> dict:
    """Recompute expectations from detail and verify saved audit/caches read-only.

    Does not trust a green status cell or a previous static expectation. Verifies
    that every original check is represented and its live reference is intact.
    """
    result = audit_workbook(path)
    wb = load_workbook(path, data_only=False)
    cache = load_workbook(path, data_only=True)
    try:
        extra = []
        source_labels, source_rows = _source_layout(result["checks"])
        source_valid = True
        if source_rows:
            source_valid = SOURCE_SHEET in wb.sheetnames
            if source_valid:
                detail = wb[SOURCE_SHEET]
                observed = list(detail.iter_rows(values_only=True))
                source_valid = observed == [SOURCE_HEADERS, *source_rows]
                # Values alone must not allow formulas posing as literal chunks.
                source_valid = source_valid and all(
                    cell.data_type != "f" for row in detail for cell in row)
        elif SOURCE_SHEET in wb.sheetnames:
            source_valid = False  # unexpected/orphan detail is not harmless evidence
        if not source_valid:
            extra.append(dict(scope="审计页", category=SOURCE_SHEET, product_id="", expected=None, actual=None,
                              difference=None, status=FAIL, blocking=True, source_refs=[], target="",
                              note="来源明细缺失/篡改/次序或重复次数不一致/存在额外记录"))
        if AUDIT_SHEET not in wb.sheetnames:
            extra.append(dict(scope="审计页", category=AUDIT_SHEET, product_id="", expected=None, actual=None,
                              difference=None, status=UNKNOWN, blocking=True, source_refs=[], target="", note="缺少金额对账Sheet"))
        else:
            ws = wb[AUDIT_SHEET]
            if tuple(c.value for c in ws[1]) != HEADERS:
                extra.append(dict(scope="审计页", category="表头", product_id="", expected=None, actual=None,
                                  difference=None, status=FAIL, blocking=True, source_refs=[], target="", note="金额对账表头被改动"))
            else:
                saved = list(ws.iter_rows(min_row=2, values_only=True))
                expected_count = len(result["checks"]) + len(result["warnings"])
                nonempty_tail = any(any(v is not None for v in row) for row in saved[expected_count:])
                if len(saved) < expected_count or nonempty_tail:
                    extra.append(dict(scope="审计页", category="行数", product_id="", expected=str(expected_count), actual=str(len(saved)),
                                      difference=None, status=FAIL, blocking=True, source_refs=[], target="", note="金额对账缺行或存在额外记录"))
                for n, warning in enumerate(result["warnings"], len(result["checks"]) + 2):
                    if n - 2 >= len(saved) or saved[n - 2][8] != warning:
                        extra.append(dict(scope="审计页", category="提示", product_id="", expected=None, actual=None,
                                          difference=None, status=FAIL, blocking=True, source_refs=[], target="", note="必需业务风险提示被改动或缺失"))
                for n, check in enumerate(result["checks"], 2):
                    row = saved[n - 2] if n - 2 < len(saved) else ()
                    valid = len(row) >= 12 and row[:3] == (check["scope"], check["category"], check["product_id"] or None)
                    # openpyxl serializes empty strings as blank cells.
                    if not valid and len(row) >= 12:
                        valid = row[:2] == (check["scope"], check["category"]) and (row[2] or "") == check["product_id"]
                    valid = valid and row[4] == check["expected"] and (row[9] or "") == check["target"]
                    valid = valid and (row[3] or "") == source_labels[n - 2]
                    valid = valid and ws.cell(n, 4).data_type != "f"
                    try:
                        valid = valid and _money(row[10]) == TOLERANCE
                    except ValueError:
                        valid = False
                    if not check["target"]:
                        valid = valid and row[7] == check["status"]
                    if check["target"]:
                        expected_state = f'=IF(ISNUMBER(F{n}),IF(ABS(G{n})<=K{n},"{PASS}","{FAIL}"),"{UNKNOWN}")'
                        valid = valid and row[5] == "=" + check["target"] and row[6] == f"=F{n}-E{n}" and row[7] == expected_state and row[11] == "是"
                        try:
                            valid = valid and abs(_money(cache[AUDIT_SHEET].cell(n, 6).value) - _money(check["actual"])) <= TOLERANCE
                            valid = valid and abs(_money(cache[AUDIT_SHEET].cell(n, 7).value) - _money(check["difference"])) <= TOLERANCE
                            valid = valid and cache[AUDIT_SHEET].cell(n, 8).value == check["status"]
                        except ValueError:
                            valid = False
                    if not valid:
                        extra.append(dict(scope="审计页", category=check["category"], product_id=check["product_id"], expected=check["expected"], actual=None,
                                          difference=None, status=FAIL, blocking=True, source_refs=[], target=f"{AUDIT_SHEET}!row:{n}", note="静态预期/行身份/引用/重算缓存与重新独立计算不一致"))
        updated = _finish(result["checks"] + extra, result["warnings"])
        updated["path"] = result["path"]
        updated["verified"] = not updated["blocking"]
        return updated
    finally:
        wb.close()
        cache.close()
