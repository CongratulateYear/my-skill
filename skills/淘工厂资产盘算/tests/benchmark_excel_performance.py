"""Offline synthetic benchmark; creates only an isolated TemporaryDirectory.
Run before/after changes with the same rows/seed; never reads Downloads.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import random
import statistics
import tempfile
import time
from zipfile import ZipFile

from lxml import etree as ET
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', type=int, default=12000)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--baseline', action='store_true', help='Restore only the measured pre-optimization functions in memory')
    args = parser.parse_args()
    v = module('bench_inventory', ROOT / '淘工厂资产盘算/scripts/inventory_validation.py')
    cache = module('bench_cache', ROOT / '淘工厂合并/scripts/recalc_cache.py')
    if args.baseline:
        from collections import Counter
        def rectangular_errors(wb):
            errors = Counter()
            for ws in wb.worksheets:
                for row in ws:
                    for cell in row:
                        if cell.data_type == 'e' or (isinstance(cell.value, str) and '#REF!' in cell.value.upper()):
                            errors[(ws.title, str(cell.value))] += 1
            return errors
        v._errors = rectangular_errors
        # The production function is otherwise identical, including full audits.
        original_code = (ROOT / '淘工厂合并/scripts/recalc_cache.py').read_text(encoding='utf-8')
        original_code = original_code.replace("if ((f.text or '') != (lf.text or '')\n                        and canonical_formula(f.text or '') != canonical_formula(lf.text or '')):", "if canonical_formula(f.text or '') != canonical_formula(lf.text or ''):")
        exec(compile(original_code, '<baseline-cache>', 'exec'), cache.__dict__)
    builder = v._builder()
    with tempfile.TemporaryDirectory(prefix='offline-excel-performance-') as tmp:
        root = Path(tmp)
        bills = root / 'bills'
        bills.mkdir()
        template = root / 'template.xlsx'
        source = root / 'source.xlsx'
        calc = root / 'calculated.xlsx'
        wb = Workbook()
        ws = wb.active
        ws.title = '公司汇总'
        ws.append(builder.SUMMARY_REQUIRED_HEADERS)
        ws.append(['总计'] + [None] * 29)
        fine = wb.create_sheet('处罚数据')
        fine.append(['处罚内容', '主订单号', '处罚时间', '违规类型', '违规事件', '处罚金额', '商品标题', '商品id'])
        # Widely spaced styled blanks model template padding without losing styles.
        plain = wb.create_sheet('Template padding')
        plain.cell(args.rows * 2, 30).font = Font(bold=True)
        wb.save(template)
        ws.cell(2, 1, '总计')
        data = wb.create_sheet('Details')
        rng = random.Random(1701)
        for r in range(1, args.rows + 1):
            data.append([f'order-{r:018d}', f'title-{r}', r % 13, rng.random()] * 4 + [f'=C{r}+1'])
        wb.save(source)
        wb.close()
        with ZipFile(source) as zin, ZipFile(calc, 'w') as zout:
            for info in zin.infolist():
                raw = zin.read(info.filename)
                if info.filename.startswith('xl/worksheets/'):
                    tree = ET.fromstring(raw)
                    for cell in tree.findall('.//s:c', cache.NS):
                        if cell.find(cache.TAG + 'f') is not None:
                            cell.find(cache.TAG + 'v').text = '5'
                    raw = ET.tostring(tree)
                zout.writestr(info, raw)
        results = {'rows': args.rows, 'repeat': args.repeat, 'seconds': {}}
        for name, action in (
            ('inventory', lambda i: v.validate_inventory(source, '公司', bills, template)),
            ('cache_transfer', lambda i: cache.transplant_caches(source, calc, root / f'out-{i}.xlsx')),
        ):
            times = []
            for i in range(args.repeat):
                start = time.perf_counter()
                result = action(i)
                times.append(time.perf_counter() - start)
            results['seconds'][name] = {'samples': times, 'median': statistics.median(times), 'result': result}
        results['source_bytes'] = source.stat().st_size
        results['output_bytes'] = (root / 'out-0.xlsx').stat().st_size
        print(json.dumps(results, ensure_ascii=False))


if __name__ == '__main__':
    main()
