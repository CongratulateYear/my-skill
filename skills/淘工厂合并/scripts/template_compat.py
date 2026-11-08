"""Rebuild obsolete spill helpers without changing bill or summary semantics."""
import re
from openpyxl.utils import get_column_letter, quote_sheetname

BLOCKS = [
    (2, '商品货款', 'G', 'E', 1), (7, '交易补贴', 'H', 'E', 1),
    (10, '交易佣金', 'G', 'E', -1), (13, '先用后付', 'E', 'C', -1),
    (16, '消费者体验提升服务', 'G', 'E', -1), (19, '新疆物流集运', 'E', 'D', -1),
    (22, '出海增值服务费', 'E', 'D', -1), (25, '营销费用', 'G', 'E', -1),
    (28, '所有商品效果数据导出', 'B', 'H', 1), (31, '所有商品效果数据导出', 'B', 'K', 1),
    (34, '投流推广佣金', 'E', 'C', -1), (37, '淘客推广佣金', 'G', 'E', -1),
    (40, '直播推广佣金', 'G', 'E', -1), (43, '处罚数据', 'H', 'K', 1),
]

def rebuild_legacy_helpers(wb):
    """Materialize distinct nonblank detail IDs; retain ordinary bounded calculations.

    Helper sheets are retained. Only their legacy computed regions are rebuilt.
    No bill sheet or company-summary cell is modified. Re-run after adding bills.
    """
    # Refuse unexpected live dependencies rather than silently changing their contract.
    for name, definition in wb.defined_names.items():
        if any(sheet in str(definition.attr_text or '') for sheet in ('中间表', 'id汇总')):
            raise ValueError(f'遗留辅助表存在名称依赖，拒绝自动改写：{name}')
    for sheet in wb:
        if sheet.title in {'中间表', 'id汇总'}:
            continue
        for row in sheet:
            for cell in row:
                value = cell.value
                text = value if isinstance(value, str) else getattr(value, 'text', '')
                if text.startswith('=') and any(name in text for name in ('中间表', 'id汇总')):
                    raise ValueError(f'遗留辅助表存在真实依赖，拒绝自动改写：{sheet.title}!{cell.coordinate}')
    for name in ('中间表', 'id汇总'):
        for row in wb[name]:
            for cell in row:
                cell.value = None
    ws = wb['中间表']
    union = []
    seen_union = set()
    counts = {}
    for start, sheet, idcol, amount, sign in BLOCKS:
        source = wb[sheet]
        last = max(2, source.max_row)
        ids = []
        seen = set()
        for row in source.iter_rows(min_row=2):
            value = row[source[idcol+'1'].column-1].value
            if value is None or not str(value).strip():
                continue
            key = str(value).strip()
            if key not in seen:
                ids.append(key)
                seen.add(key)
            if sheet != '所有商品效果数据导出' and key not in seen_union:
                seen_union.add(key)
                union.append(key)
        counts[sheet] = len(ids)
        q = quote_sheetname(sheet)
        letter = get_column_letter(start)
        ws.cell(1, start, sheet+'商品ID')
        ws.cell(1, start+1, '产品名称' if start == 2 else '金额')
        if start == 2:
            ws.cell(1, start+2, '销售数量')
            ws.cell(1, start+3, '收入')
        for r, key in enumerate(ids, 2):
            ws.cell(r, start, key).data_type = 's'
            criterion = f'${letter}{r}'
            formula = f'SUMIFS({q}!${amount}$2:${amount}${last},{q}!${idcol}$2:${idcol}${last},{criterion})'
            if sign < 0:
                formula = '-'+formula
            if start == 2:
                ws.cell(r, start+1, f'=IFERROR(INDEX({q}!$H$2:$H${last},MATCH({criterion},{q}!$G$2:$G${last},0)),"")')
                ws.cell(r, start+2, f'=SUMIFS({q}!$K$2:$K${last},{q}!$G$2:$G${last},{criterion},{q}!$A$2:$A${last},"销售")')
                ws.cell(r, start+3, '='+formula)
            else:
                ws.cell(r, start+1, '='+formula)
    for r, key in enumerate(union, 1):
        wb['id汇总'].cell(r, 3, key).data_type = 's'
    # Actual-size diagnostic ranges: retain all checks, including duplicate-ID checks.
    diagnostic = wb['数据检查']
    pattern = re.compile(r"('?)([^'!,()=]+)\1!\$([A-Z]+)\$2:\$([A-Z]+)\$50000")
    for row in diagnostic:
        for cell in row:
            if cell.data_type != 'f':
                continue
            def bounded(match):
                sheet = match.group(2)
                if sheet not in wb.sheetnames:
                    return match.group(0)
                return f'{quote_sheetname(sheet)}!${match.group(3)}$2:${match.group(4)}${max(2, wb[sheet].max_row)}'
            cell.value = pattern.sub(bounded, cell.value)
    return {'helper_distinct_ids': counts, 'union_ids': len(union), 'helper_live_dependencies': 0}
