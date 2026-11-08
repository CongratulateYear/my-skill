"""Local delivery enrichment. Never changes source bills or calls remote ERP."""
import os
from pathlib import Path
import subprocess
import tempfile
from openpyxl import load_workbook
from openpyxl.styles import Font

NODE = Path(r'D:\dsh\resources\runtime\primary-runtime\dependencies\node\bin\node.exe')
CLI = Path(r'D:\dsh\resources\app.asar.unpacked\dsh\node_modules\@deepseek-ai\libreoffice-kit\lib\cli.js')


def add_review(source, destination, erp, fine):
    """Add review to a separate main workbook, never to original fine workbook."""
    wb = load_workbook(source)
    try:
        result = _append_review(wb, erp, fine)
        wb.save(destination)
        return result
    finally:
        wb.close()


def _append_review(wb, erp, fine):
    """Append review to an owned, uncalculated candidate; caller saves once."""
    if '待核查' in wb.sheetnames:
        raise ValueError('待核查 already exists; refusing duplicate enrichment')
    ws = wb.create_sheet('待核查')
    ws.append(['处罚Sheet', '原始行号', '订单号', '原因代码', '核查说明', '处罚金额（元）'])
    matching = (erp or {}).get('matching') or {}
    rows = [r for r in matching.get('report', []) if r.get('reason_code') != 'matched']
    reason_counts = matching.get('reason_counts') or {}
    expected_rows = sum(value for key,value in reason_counts.items() if key != 'matched')
    if expected_rows != len(rows):
        raise ValueError('ERP review row count does not match reason_counts')
    reasons = {'no_order_id':'无有效主订单号，保留原商品字段', 'erp_missing':'ERP缺少订单，保留原商品字段',
               'invalid_fields':'ERP商品字段无效，保留原商品字段', 'ambiguous':'多商品歧义，保留原商品字段'}
    fine_wb = load_workbook(fine, data_only=False) if fine and rows else None
    try:
        sheet_name = matching.get('sheet', '处罚数据')
        fs = fine_wb[sheet_name] if fine_wb else None
        amount_col = None
        if fs:
            for row in fs.iter_rows(min_row=1, max_row=min(10, fs.max_row)):
                hits = [c.column for c in row if c.value in ('处罚金额（元）', '处罚金额(元)', '处罚金额', '罚款金额', '金额')]
                if len(hits) == 1:
                    amount_col = hits[0]; break
        for item in rows:
            row = item['row']
            if not isinstance(row, int) or row < 1 or fs is None or row > fs.max_row:
                raise ValueError('Invalid fine review row')
            amount = fs.cell(row, amount_col).value if amount_col else None
            if isinstance(amount, str) and amount.startswith('='):
                amount = None
            ws.append([sheet_name, row, ','.join(item.get('order_ids', [])), item.get('reason_code'),
                       reasons.get(item.get('reason_code'), '需核查，保留原值'), amount])
            # Treat identifiers and descriptions as literal text, never formulas.
            row_index = ws._current_row
            for col in (1,3,4,5):
                ws.cell(row_index,col).data_type = 's'
        if not rows:
            ws.append([None,None,None,'无待核查记录',(erp or {}).get('skipped','未发现未匹配处罚行'),None])
        if rows and amount_col is None:
            ws.cell(ws.max_row+2,5,'未唯一识别处罚金额列，金额留空，勿当作零元')
    finally:
        if fine_wb: fine_wb.close()
    for cell in ws[1]: cell.font = Font(bold=True)
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = f'A1:F{len(rows)+1}' if rows else 'A1:F2'
    for col,width in {'A':20,'B':14,'C':40,'D':22,'E':52,'F':22}.items(): ws.column_dimensions[col].width=width
    return {'rows':len(rows), 'amount_column_found':amount_col is not None}


def recalculate(source, destination, node=None, cli=None, progress=None):
    node = Path(node or os.environ.get('TGC_RECALC_NODE') or NODE)
    cli = Path(cli or os.environ.get('TGC_RECALC_CLI') or CLI)
    if not node.is_file() or not cli.is_file():
        raise ValueError('Formula recalculation engine unavailable; install configured Office sidecar, do not deliver stale caches')
    import importlib.util
    skill_root = Path(__file__).resolve().parents[2]
    helpers = [skill_root/name/'scripts'/'recalc_cache.py' for name in ('淘工厂合并','taogongchang-merge')]
    helper = next((path for path in helpers if path.is_file()),None)
    if helper is None:
        raise ValueError('Missing cache-preserving recalculation helper from taogongchang-merge')
    spec = importlib.util.spec_from_file_location('tgc_recalc_cache',helper)
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix='lo-cache-',dir=Path(destination).resolve().parent) as td:
        raw = Path(td)/'lo-calculated.xlsx'
        if progress: progress('delivery_engine')
        # 600 seconds bounds only the external formula engine, not delivery as a whole.
        subprocess.run([str(node), str(cli), 'recalculate', '--input', str(Path(source).resolve()),
                        '--output', str(raw)], check=True, timeout=600)
        if progress: progress('delivery_cache_transfer')
        counts = module.transplant_caches(source,raw,destination)
    # transplant_caches already reopens the output and checks original data,
    # formulas and every cache. Do not repeat that scan through openpyxl.
    return {'ok': True, 'formula_count': counts['formula_caches'],
            'engine': 'LibreOffice Kit', 'cache_transfer': counts,
            'validation_basis': 'cache_transplant_verified'}


def verify_calculated(source, calculated):
    """Reject dropped sheets/formulas, changed data and absent/error formula caches."""
    before = load_workbook(source, data_only=False)
    after = load_workbook(calculated, data_only=False)
    cached = load_workbook(calculated, data_only=True)
    # openpyxl maps LibreOffice's valid empty string cache to None.
    # Distinguish explicit t=str + empty <v/> from a missing numeric cache.
    import zipfile
    from lxml import etree
    blank_strings = set()
    ns = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with zipfile.ZipFile(calculated) as archive:
        book = etree.fromstring(archive.read('xl/workbook.xml'))
        rels = etree.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        targets = {r.get('Id'):r.get('Target') for r in rels}
        for sheet in book.find('s:sheets',ns):
            rid = sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
            target = targets[rid]
            member = target.lstrip('/') if target.startswith('/') else 'xl/'+target
            root = etree.fromstring(archive.read(member))
            for cell in root.findall('.//s:c',ns):
                value = cell.find('s:v',ns)
                if cell.get('t') == 'str' and cell.find('s:f',ns) is not None and value is not None and not value.text:
                    blank_strings.add((sheet.get('name'),cell.get('r')))
    count = 0
    try:
        if before.sheetnames != after.sheetnames: raise ValueError('Recalculation changed sheet names')
        for ws in before:
            other = after[ws.title]; values = cached[ws.title]
            if set(map(str,ws.merged_cells.ranges)) != set(map(str,other.merged_cells.ranges)):
                raise ValueError('Recalculation changed merged ranges')
            for row in ws:
                for cell in row:
                    new = other[cell.coordinate]
                    if cell.data_type == 'f':
                        count += 1
                        if new.data_type != 'f': raise ValueError(f'Formula lost: {ws.title}!{cell.coordinate}')
                        def formula_text(value):
                            return value if isinstance(value,str) else (getattr(value,'text',None),getattr(value,'ref',None))
                        if formula_text(cell.value) != formula_text(new.value):
                            raise ValueError(f'Formula changed: {ws.title}!{cell.coordinate}')
                        value = values[cell.coordinate]
                        if (value.value is None and (ws.title,cell.coordinate) not in blank_strings) or value.data_type == 'e':
                            raise ValueError(f'Formula cache missing/error: {ws.title}!{cell.coordinate} {value.value}')
                    elif cell.value != new.value and not (cell.value is None and new.value == ''):
                        raise ValueError(f'Recalculation changed data: {ws.title}!{cell.coordinate}')
            for row in values:
                for cell in row:
                    if cell.data_type == 'e':
                        raise ValueError(f'Calculated workbook error: {ws.title}!{cell.coordinate} {cell.value}')
        return {'ok':True,'formula_count':count,'engine':'LibreOffice Kit'}
    finally:
        before.close(); after.close(); cached.close()


def enrich(output, erp, fine, runtime, receipt=None, progress=None):
    """Prepare verified output; caller journals hashes BEFORE atomic publication."""
    output = Path(output)
    with tempfile.TemporaryDirectory(prefix='delivery-', dir=runtime) as td:
        calculated = Path(td)/'calculated.xlsx'
        if progress: progress('delivery_review')
        import importlib.util
        def helper(name):
            spec = importlib.util.spec_from_file_location('tgc_'+name,Path(__file__).with_name(name+'.py'))
            import sys
            module = importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module)
            return module
        coverage_path = Path(td)/'coverage.xlsx'
        # Review and coverage are both pre-calculation edits. Keep one owned
        # workbook rather than serializing/reloading all unchanged detail sheets.
        coverage_helper = helper('delivery_coverage')
        if coverage_path.exists() or output.resolve() == coverage_path.resolve():
            raise ValueError('Coverage requires a separate new candidate')
        wb = load_workbook(output)
        try:
            review = _append_review(wb, erp, fine)
            if progress: progress('delivery_coverage')
            coverage = coverage_helper._append_coverage(wb, receipt)
            wb.save(coverage_path)
        finally:
            wb.close()
        # User-selected lean flow: no extra financial audit sheet/full-book audit.
        # Matching/fine invariants, one inventory check and cache transfer remain.
        result = recalculate(coverage_path, calculated, progress=progress) if progress else recalculate(coverage_path, calculated)
        # Persist candidate inside runtime; company journal controls publication.
        import shutil
        candidate = Path(runtime)/'delivery-candidate.xlsx'
        shutil.copyfile(calculated,candidate)
        return {'review':review,'coverage':coverage,
                'audit': {'status':'not_performed','verified':None,'blocking':None,
                          'reason':'user_selected_lean_validation'},
                'validation_profile':'lean', 'recalculation':result,'candidate':str(candidate)}
