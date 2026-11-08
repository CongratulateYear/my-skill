"""Preserve the source XLSX package and transplant verified LibreOffice caches only."""
from copy import deepcopy
from pathlib import Path
import re
import zipfile
from lxml import etree as ET
from openpyxl.formula.tokenizer import Tokenizer

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
TAG = '{'+NS['s']+'}'

def canonical_formula(text):
    """Only remove optional sheet quoting and collapse identical endpoint ranges.

    Tokenization protects string literals (including strings containing sheet-like text).
    No arithmetic, sign, function, reference or order rewriting is accepted.
    """
    tokens = Tokenizer('='+text).items
    result = []
    for token in tokens:
        value = token.value
        if token.type == 'OPERAND' and token.subtype == 'RANGE':
            value = re.sub(r"'([^']+)'!", lambda m: m.group(1)+'!' if re.fullmatch(r'[\w\u0080-\uffff]+', m.group(1)) else m.group(0), value)
            value = re.sub(r'(\$?[A-Z]+\$?\d+):\1(?![\dA-Z])', r'\1', value)
        result.append(value)
    return ''.join(result)

def transplant_caches(source, calculated, output):
    source, calculated, output = map(Path, (source, calculated, output))
    if output.resolve() in {source.resolve(), calculated.resolve()}:
        raise ValueError('Cache transplant output must be a separate file')
    # Validate every nonformula calculation input, including added nonempty cells.
    # XLSX writers may shorten insignificant binary-float decimal tails only.
    from openpyxl import load_workbook
    import math
    before_wb = load_workbook(source, read_only=True, data_only=False)
    lo_wb = load_workbook(calculated, read_only=True, data_only=False)
    try:
        if before_wb.sheetnames != lo_wb.sheetnames:
            raise ValueError('Calculation input sheet order/names changed')
        for sheet in before_wb:
            other = lo_wb[sheet.title]
            old_cells = {c.coordinate: c for row in sheet for c in row if c.value is not None}
            new_cells = {c.coordinate: c for row in other for c in row if c.value is not None}
            for coord in old_cells.keys() | new_cells.keys():
                old = old_cells.get(coord); new = new_cells.get(coord)
                if old is not None and old.data_type == 'f':
                    continue
                x = old.value if old is not None else None
                y = new.value if new is not None else None
                if x in (None, '') and y in (None, ''):
                    continue
                if type(x) in (int, float) and type(y) in (int, float):
                    same = math.isfinite(x) and math.isfinite(y) and abs(x-y) <= max(1e-12, 8*math.ulp(float(x)))
                else:
                    same = type(x) is type(y) and x == y
                if not same:
                    raise ValueError(f'Calculation input changed: {sheet.title}!{coord}: {x!r} -> {y!r}')
    finally:
        before_wb.close(); lo_wb.close()
    counts = {'formula_caches': 0, 'empty_string_caches': 0, 'lo_array_wrappers': 0}
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(calculated) as lo:
        replacements = {}
        # Match workbook sheet relationships rather than assuming sheet order survives.
        def sheet_paths(archive):
            book = ET.fromstring(archive.read('xl/workbook.xml'))
            rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
            targets = {r.get('Id'): r.get('Target') for r in rels}
            result = {}
            for sheet in book.find(TAG+'sheets'):
                target = targets[sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')]
                result[sheet.get('name')] = target.lstrip('/') if target.startswith('/') else 'xl/'+target
            return result
        source_paths, lo_paths = sheet_paths(original), sheet_paths(lo)
        if source_paths.keys() != lo_paths.keys():
            raise ValueError('Recalculated sheet names differ')
        for sheet, path in source_paths.items():
            root = ET.fromstring(original.read(path))
            lo_root = ET.fromstring(lo.read(lo_paths[sheet]))
            cells = {c.get('r'): c for c in root.findall('.//s:sheetData/s:row/s:c', NS)}
            lo_cells = {c.get('r'): c for c in lo_root.findall('.//s:sheetData/s:row/s:c', NS)}
            for coord, cell in cells.items():
                if cell.get('t') == 'e':
                    raise ValueError(f'Source candidate contains error: {sheet}!{coord}')
            source_formulas = {k for k,c in cells.items() if c.find(TAG+'f') is not None}
            lo_formulas = {k for k,c in lo_cells.items() if c.find(TAG+'f') is not None}
            if source_formulas != lo_formulas:
                raise ValueError(f'Formula coordinates changed: {sheet}')
            for coord in source_formulas:
                c, lc = cells[coord], lo_cells[coord]
                f, lf = c.find(TAG+'f'), lc.find(TAG+'f')
                # Identical text is already the strictest equivalence proof;
                # tokenize only actual LO rewrites, never twice per unchanged
                # formula across large summary/helper sheets.
                if ((f.text or '') != (lf.text or '')
                        and canonical_formula(f.text or '') != canonical_formula(lf.text or '')):
                    raise ValueError(f'Unapproved formula rewrite: {sheet}!{coord}: {f.text} -> {lf.text}')
                # Approved source candidates use ordinary formulas exclusively. Never
                # infer spill cells or overwrite original nonformula bill data.
                if f.get('t') in {'array', 'shared', 'dataTable'}:
                    raise ValueError(f'Source candidate must rewrite legacy array/shared formula first: {sheet}!{coord}')
                if lf.get('t') == 'array':
                    if lf.get('ref') != coord:
                        raise ValueError(f'LO expanded an array range: {sheet}!{coord}')
                    counts['lo_array_wrappers'] += 1
                elif lf.get('t') not in (None, 'normal'):
                    raise ValueError(f'Unexpected LO formula type: {sheet}!{coord}')
                cached = lc.find(TAG+'v')
                if lc.get('t') == 'e':
                    raise ValueError(f'Calculated error: {sheet}!{coord}: {cached.text if cached is not None else None}')
                if cached is None:
                    raise ValueError(f'Calculated cache missing: {sheet}!{coord}')
                if cached.text is None and lc.get('t') != 'str':
                    raise ValueError(f'Unexpected empty cache: {sheet}!{coord}')
                if lc.get('t') == 's':
                    raise ValueError(f'Shared-string formula cache unsupported: {sheet}!{coord}')
                for old in list(c):
                    if old.tag in {TAG+'v', TAG+'is'}:
                        c.remove(old)
                if lc.get('t') is None:
                    c.attrib.pop('t', None)
                else:
                    c.set('t', lc.get('t'))
                c.append(deepcopy(cached))
                counts['formula_caches'] += 1
                if cached.text is None:
                    counts['empty_string_caches'] += 1
            # All error cells, including nonformula leftovers, are fatal.
            for coord, c in lo_cells.items():
                if c.get('t') == 'e':
                    raise ValueError(f'Workbook contains error: {sheet}!{coord}')
            if source_formulas:
                replacements[path] = ET.tostring(root, encoding='utf-8')
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as dest:
            for info in original.infolist():
                data = replacements[info.filename] if info.filename in replacements else original.read(info.filename)
                dest.writestr(info, data)
    # Verify exact original formulas, exact untouched package entries/nonformula cells,
    # and every resulting cache against LO. Formula comparison is deliberately strict.
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(calculated) as lo, zipfile.ZipFile(output) as final:
        for name in original.namelist():
            if name not in replacements:
                if original.read(name) != final.read(name):
                    raise ValueError(f'Unchanged package entry differs: {name}')
        for sheet, path in source_paths.items():
            old = ET.fromstring(original.read(path)); new = ET.fromstring(final.read(path)); calc = ET.fromstring(lo.read(lo_paths[sheet]))
            maps = [{c.get('r'):c for c in root.findall('.//s:sheetData/s:row/s:c',NS)} for root in (old,new,calc)]
            for coord, before in maps[0].items():
                after = maps[1][coord]
                if before.find(TAG+'f') is None:
                    if ET.tostring(before) != ET.tostring(after):
                        raise ValueError(f'Nonformula cell changed: {sheet}!{coord}')
                else:
                    if ET.tostring(before.find(TAG+'f')) != ET.tostring(after.find(TAG+'f')):
                        raise ValueError(f'Original formula changed: {sheet}!{coord}')
                    expected = maps[2][coord]
                    if after.get('t') != expected.get('t') or ET.tostring(after.find(TAG+'v')) != ET.tostring(expected.find(TAG+'v')):
                        # Namespace declarations differ between packages; compare data.
                        if after.get('t') != expected.get('t') or after.find(TAG+'v').text != expected.find(TAG+'v').text:
                            raise ValueError(f'Cache mismatch: {sheet}!{coord}')
    return counts
