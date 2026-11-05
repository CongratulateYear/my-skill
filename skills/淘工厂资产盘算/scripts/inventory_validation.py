#!/usr/bin/env python3
"""Read-only local guards for ERP fine enrichment and inventory publication.

No network, recalculation, workbook writes, recursive source discovery or CLI.
All successful public calls return JSON-safe dictionaries; unsafe input raises
ValueError. validate_fine also accepts an immutable backup path as its snapshot.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _fine_snapshot(path):
    wb = load_workbook(path, data_only=False)
    try:
        candidates = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(max_row=min(50, ws.max_row)):
                labels = [c.value.strip() if isinstance(c.value, str) else c.value for c in row]
                if (any(x in labels for x in ('主订单号', '主订单编号'))
                        and '商品标题' in labels and any(x in labels for x in ('商品id', '商品ID'))):
                    cols = [i + 1 for i, x in enumerate(labels) if x in ('商品标题', '商品id', '商品ID')]
                    if len(cols) != 2:
                        raise ValueError('Ambiguous fine product headers')
                    candidates.append((ws, row[0].row, cols))
        preferred = [c for c in candidates if c[0].title == '处罚数据']
        candidates = preferred or candidates
        if len(candidates) != 1:
            raise ValueError('Expected one fine sheet/header')
        ws, header, cols = candidates[0]
        allowed = {f'{get_column_letter(c)}{r}' for r in range(header + 1, ws.max_row + 1) for c in cols}
        values = {coord: [ws[coord].data_type, repr(ws[coord].value)] for coord in sorted(allowed)}
        sheetnames = list(wb.sheetnames)
        title = ws.title
    finally:
        wb.close()
    with zipfile.ZipFile(path) as z:
        if len(z.namelist()) != len(set(z.namelist())):
            raise ValueError('Duplicate ZIP entries')
        book = ET.fromstring(z.read('xl/workbook.xml'))
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        sheet = next(s for s in book.find(f'{{{_NS}}}sheets') if s.attrib['name'] == title)
        target = next(r.attrib['Target'] for r in rels if r.attrib['Id'] == sheet.attrib[f'{{{_R}}}id'])
        member = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
        entries = {}
        styles = {}
        for name in z.namelist():
            data = z.read(name)
            if name == member:
                root = ET.fromstring(data)
                for row in root.iter(f'{{{_NS}}}row'):
                    for cell in list(row):
                        if cell.tag == f'{{{_NS}}}c' and cell.attrib.get('r') in allowed:
                            attrs = {k: v for k, v in cell.attrib.items() if k not in ('r', 't')}
                            if attrs:
                                styles[cell.attrib['r']] = attrs
                            row.remove(cell)
                data = ET.tostring(root, encoding='utf-8')
            entries[name] = _digest(data)
    return {'version': 1, 'sheet': title, 'sheetnames': sheetnames,
            'entries': entries, 'target_styles': styles, 'target_values': values}


def snapshot_fine(path) -> dict:
    """Capture JSON-safe exact non-product OOXML fingerprints before matching."""
    try:
        return _fine_snapshot(path)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f'Cannot snapshot fine workbook: {exc}') from exc


def validate_fine(path, snapshot) -> dict:
    """Only product id/title values may change; styles and all else stay exact."""
    before = snapshot_fine(snapshot) if isinstance(snapshot, (str, Path)) else snapshot
    after = snapshot_fine(path)
    if not isinstance(before, dict) or before.get('version') != 1:
        raise ValueError('Invalid fine snapshot')
    for key in ('sheet', 'sheetnames', 'entries', 'target_styles'):
        if before.get(key) != after[key]:
            raise ValueError(f'Fine invariant changed: {key}')
    old = before.get('target_values', {})
    new = after['target_values']
    if old.keys() != new.keys():
        raise ValueError('Fine row structure changed')
    changed = [c for c in old if old[c] != new[c]]
    if any(new[c][0] != 's' for c in changed):
        raise ValueError('Changed fine product field must be text')
    return {'ok': True, 'sheet': after['sheet'], 'changed_cells': len(changed),
            'preserved_non_product_xml': True}


def _builder():
    roots = (Path(__file__).resolve().parents[2],
             Path.home() / '.agents' / 'skills', Path.home() / '.dsh' / 'skills')
    path = next((root / name / 'scripts' / 'pan_suan_builder.py'
                 for root in roots for name in ('淘工厂合并', 'taogongchang-merge')
                 if (root / name / 'scripts' / 'pan_suan_builder.py').is_file()), None)
    if path is None:
        raise ValueError('Missing local merge builder (淘工厂合并/taogongchang-merge)')
    spec = importlib.util.spec_from_file_location('_inventory_validation_builder', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _errors(wb):
    errors = Counter()
    for ws in wb.worksheets:
        for row in ws:
            for cell in row:
                if cell.data_type == 'e' or (isinstance(cell.value, str) and '#REF!' in cell.value.upper()):
                    errors[(ws.title, str(cell.value))] += 1
    return errors


def _row_key(row):
    # Dates and Excel scalar types are made comparable without equating bool/int.
    return json.dumps([[type(v).__name__, v.isoformat() if hasattr(v, 'isoformat') else v]
                       for v in row], ensure_ascii=False, default=str)


def _top_sources(folder, main_path, template_path):
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError('Source folder does not exist')
    excluded = {Path(main_path).resolve(), Path(template_path).resolve()}
    result = []
    for p in sorted(folder.iterdir()):
        lower = p.name.lower()
        if (p.is_symlink() or not p.is_file() or p.suffix.lower() != '.xlsx'
                or p.resolve() in excluded or lower.startswith(('~$', '.'))
                or re.search(r'-[0-9a-f]{64}\.xlsx$', lower)
                or any(token in lower for token in ('backup', '.bak', '备份', '聚水潭'))):
            continue
        result.append(p)
    return result


def validate_inventory(main_path, company, source_folder, template_path) -> dict:
    """Validate fixed headers, company/total, error growth, and fine source rows.

    Only top-level bill workbooks are read. Fine rows use the builder's existing
    identifier normalization/exact-dedup semantics; trailing blank or unchanged
    template padding is permitted, but never extra business rows.
    """
    opened = []
    try:
        if not isinstance(company, str) or not company.strip():
            raise ValueError('Company must be nonempty')
        builder = _builder()
        main = load_workbook(main_path, data_only=False)
        opened.append(main)
        template = load_workbook(template_path, data_only=False)
        opened.append(template)
        if '公司汇总' not in main.sheetnames or '公司汇总' not in template.sheetnames:
            raise ValueError('Missing 公司汇总')
        summary = main['公司汇总']
        headers = [summary.cell(1, c).value for c in range(1, 31)]
        expected = [template['公司汇总'].cell(1, c).value for c in range(1, 31)]
        if headers != expected or headers != builder.SUMMARY_REQUIRED_HEADERS:
            raise ValueError('公司汇总 fixed 30-column header mismatch')
        if any(summary.cell(1, c).value is not None for c in range(31, summary.max_column + 1)):
            raise ValueError('公司汇总 has extra headers')
        shop_col = headers.index('店铺') + 1
        names = [summary.cell(r, shop_col).value for r in range(2, summary.max_row + 1)]
        if names.count('总计') != 1:
            raise ValueError('公司汇总 must contain exactly one 总计')
        actual_names = {n for n in names if n not in (None, '', '总计')}
        # A valid zero-product workbook has no company-bearing product rows.
        product_col = headers.index('商品ID') + 1
        has_products = any(summary.cell(r, product_col).value not in (None, '') for r in range(2, summary.max_row + 1))
        if actual_names != {company} and (actual_names or has_products):
            raise ValueError('公司汇总 company mismatch')
        for r in range(2, summary.max_row + 1):
            if summary.cell(r, product_col).value not in (None, '') and summary.cell(r, shop_col).value != company:
                raise ValueError(f'公司汇总 missing/wrong company at row {r}')
        baseline = _errors(template)
        fine_paths = []
        fine_rows = []
        sources = _top_sources(source_folder, main_path, template_path)
        for p in sources:
            source = load_workbook(p, data_only=False)
            opened.append(source)
            if '公司汇总' in source.sheetnames:
                source.close()
                continue
            baseline.update(_errors(source))
            if builder.detect_target(p, source) == '处罚数据':
                if '处罚数据' not in template.sheetnames:
                    raise ValueError('Template missing 处罚数据')
                fine_headers = [str(c.value or '') for c in template['处罚数据'][1]]
                rows, _ = builder.read_bill_rows(p, fine_headers)
                fine_rows.extend(rows)
                fine_paths.append(str(p))
            source.close()
        if _errors(main) - baseline:
            raise ValueError('Inventory contains new Excel errors or #REF!')
        if '处罚数据' not in main.sheetnames or '处罚数据' not in template.sheetnames:
            raise ValueError('Missing 处罚数据 sheet')
        ws, tws = main['处罚数据'], template['处罚数据']
        fine_headers = [str(c.value or '') for c in tws[1]]
        if [str(c.value or '') for c in ws[1]] != fine_headers:
            raise ValueError('处罚数据 header mismatch')
        fine_rows, duplicates = builder.dedupe_rows(fine_rows, fine_headers)
        for i, row in enumerate(fine_rows, 2):
            actual = [ws.cell(i, c).value for c in range(1, len(fine_headers) + 1)]
            if _row_key(actual) != _row_key(row):
                raise ValueError(f'处罚数据 differs from sources at row {i}')
        for r in range(len(fine_rows) + 2, ws.max_row + 1):
            row = [ws.cell(r, c).value for c in range(1, len(fine_headers) + 1)]
            padding = [tws.cell(r, c).value for c in range(1, len(fine_headers) + 1)] if r <= tws.max_row else [None] * len(fine_headers)
            if any(v not in (None, '') for v in row) and _row_key(row) != _row_key(padding):
                raise ValueError(f'Unexpected 处罚数据 row {r}')
        return {'ok': True, 'company': company, 'summary_columns': 30,
                'total_rows': 1, 'fine_rows': len(fine_rows),
                'fine_source_files': fine_paths, 'fine_duplicates_removed': duplicates,
                'top_level_sources': len(sources), 'new_errors': 0}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f'Inventory validation failed: {exc}') from exc
    finally:
        for wb in opened:
            wb.close()
