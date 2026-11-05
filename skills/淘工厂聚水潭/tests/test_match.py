"""Synthetic-workbook tests only: no business files, network or installation."""
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill, Font, Alignment

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'jst_match.py'
spec = importlib.util.spec_from_file_location('jst_match', MODULE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'merge-input'
        self.root.mkdir()
        self.fine = self.root / '原处罚数据.xlsx'
        self.order = self.root / '聚水潭订单.xlsx'

    def tearDown(self):
        self.tmp.cleanup()

    def book(self, path, rows, sheet='Sheet', extra=False):
        wb = Workbook()
        ws = wb.active
        ws.title = sheet
        for row in rows:
            ws.append(row)
        if extra:
            other = wb.create_sheet('保留页')
            other['A1'] = '=SUM(1,2)'
            other['A1'].fill = PatternFill('solid', fgColor='FF0011')
            other.merge_cells('B2:C2')
            other['B2'] = '保留'
        wb.save(path)
        wb.close()

    def fixtures(self, fine_rows=None, orders=None):
        self.book(self.fine, fine_rows or [
            ['主订单编号', '商品标题', '商品id'], ['000123456789012345678', None, None]
        ], sheet='处罚数据', extra=True)
        self.book(self.order, orders or [
            ['原始线上订单号', '商品名称', '店铺款式编码', '颜色及规格'],
            ['000123456789012345678', '真正的商品名称', '001234567890123456789', '蓝色 大码']
        ])

    def match(self, **kwargs):
        return m.match_fines(self.fine, [self.order], '测试店铺', 'fine-export', **kwargs)

    def test_basic_in_place_product_name_mapping_and_backup(self):
        self.fixtures()
        original = self.fine.read_bytes()
        result = self.match()
        self.assertEqual(Path(result['output']), self.fine)
        self.assertEqual(m.extract_order_ids(self.fine), ['000123456789012345678'])
        self.assertEqual(result['counts']['matched'], 1)
        self.assertEqual(result['counts']['filled_cells'], 2)
        self.assertEqual(result['added_columns'], [])
        self.assertIsNone(result['report_sheet'])
        backup = Path(result['backup'])
        self.assertNotIn(self.root, backup.parents)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['原处罚数据.xlsx', '原处罚数据.xlsx.lock', '聚水潭订单.xlsx'])
        wb = load_workbook(self.fine)
        self.assertEqual(wb.sheetnames, ['处罚数据', '保留页'])
        self.assertEqual(wb['处罚数据'].max_column, 3)
        self.assertEqual(wb['处罚数据']['B2'].value, '真正的商品名称')
        self.assertEqual(wb['处罚数据']['C2'].value, '001234567890123456789')
        self.assertEqual(wb['处罚数据']['C2'].data_type, 's')
        self.assertEqual(wb['保留页']['A1'].value, '=SUM(1,2)')
        self.assertEqual(wb['保留页']['A1'].fill.fgColor.rgb, '00FF0011')
        self.assertEqual(str(next(iter(wb['保留页'].merged_cells.ranges))), 'B2:C2')
        wb.close()

    def test_unique_erp_overwrites_both_nonempty_authoritatively(self):
        self.fixtures(fine_rows=[['主订单编号', '商品标题', '商品id'],
                                ['1', '原商品名', '原商品id']],
                      orders=[['原始线上订单号', '商品名称', '店铺款式编码', '颜色及规格'],
                              ['1', 'ERP商品名', 'ERP商品id', '错误映射的规格']])
        result = self.match()
        self.assertEqual(result['counts']['matched'], 1)
        self.assertEqual(result['counts']['conflict'], 0)
        self.assertEqual(result['counts']['overwritten_cells'], 2)
        wb = load_workbook(self.fine)
        self.assertEqual([wb['处罚数据'][c].value for c in ('B2', 'C2')], ['ERP商品名', 'ERP商品id'])
        wb.close()

    def test_exact_11_columns_rows_styles_formulas_other_zip_members(self):
        headers = ['店铺', '类型', '主订单编号', '商品id', '商品标题', '金额', '日期', '说明', '公式', '状态', '备注']
        self.fixtures(fine_rows=[headers,
                                ['店', '处罚数据', '2', '原2', '原名2', 3, '日期', '说明', '=F2*2', 'a', '末尾'],
                                ['店', '处罚数据', '1', '原1', '原名1', 4, '日期', '说明', '=F3*2', 'b', '末尾'],
                                ['店', '处罚数据', '9', '原9', '原名9', 5, '日期', '说明', '=F4*2', 'c', '末尾']],
                      orders=[['原始线上订单号', '商品名称', '店铺款式编码'],
                              ['1', 'ERP1', '001'], ['2', 'ERP2', '002']])
        wb = load_workbook(self.fine)
        ws = wb['处罚数据']
        ws['D2'].number_format = '0.000'
        ws['D2'].fill = PatternFill('solid', fgColor='123456')
        ws['E2'].font = Font(bold=True, color='ABCDEF')
        ws['E2'].alignment = Alignment(horizontal='right')
        ws.row_dimensions[2].height = 39
        ws.column_dimensions['E'].width = 44
        ws.freeze_panes = 'F2'
        ws.auto_filter.ref = 'A1:K4'
        wb.save(self.fine)
        wb.close()
        original = self.fine.read_bytes()
        result = self.match()
        self.assertEqual(result['counts']['matched'], 2)
        before = load_workbook(io.BytesIO(original), data_only=False)
        after = load_workbook(self.fine, data_only=False)
        self.assertEqual(before.sheetnames, after.sheetnames)
        for old_ws in before:
            new_ws = after[old_ws.title]
            self.assertEqual(old_ws.max_row, new_ws.max_row)
            self.assertEqual(old_ws.max_column, new_ws.max_column)
            for row in old_ws:
                for cell in row:
                    new = new_ws[cell.coordinate]
                    self.assertEqual(cell._style, new._style)
                    if old_ws.title != '处罚数据' or cell.coordinate not in {'D2', 'E2', 'D3', 'E3'}:
                        self.assertEqual((cell.value, cell.data_type), (new.value, new.data_type))
        self.assertEqual([c.value for c in after['处罚数据'][1]], headers)
        self.assertEqual([after['处罚数据'].cell(r, 3).value for r in (2, 3, 4)], ['2', '1', '9'])
        self.assertEqual(after['处罚数据']['D2'].number_format, '0.000')
        before.close()
        after.close()
        with zipfile.ZipFile(io.BytesIO(original)) as old, zipfile.ZipFile(self.fine) as new:
            self.assertEqual(old.namelist(), new.namelist())
            member = m._sheet_member(old, '处罚数据')
            for name in old.namelist():
                if name != member:
                    self.assertEqual(old.read(name), new.read(name), name)
            self.assertIn(b'<f>F2*2</f>', new.read(member))
            # Only the four intended cell nodes differ, all other worksheet XML is exact.
            import re
            strip_targets = lambda data: re.sub(rb'<c\b(?=[^>]*\br="(?:D2|E2|D3|E3)")[^>]*(?:/>|>.*?</c>)', b'', data)
            self.assertEqual(strip_targets(old.read(member)), strip_targets(new.read(member)))

    def test_trigger_mandatory(self):
        for trigger in [None, '', 'order-export', 'ordinary-export']:
            with self.assertRaises(m.MatchError):
                m.match_fines(self.fine, [self.order], '店铺', trigger)
        with self.assertRaises(TypeError):
            m.match_fines(self.fine, [self.order], '店铺')

    def test_unsafe_ids(self):
        for value in [1234567890123456, '=123', '1.234E+18', '12 34', '+123', 1.25, True]:
            with self.subTest(value=value):
                self.book(self.fine, [['主订单编号'], [value]])
                with self.assertRaises(m.MatchError):
                    m.extract_order_ids(self.fine)

    def test_scientific_number_format_rejected(self):
        self.book(self.fine, [['主订单编号'], [123]])
        wb = load_workbook(self.fine)
        wb.active['A2'].number_format = '0.00E+00'
        wb.save(self.fine)
        wb.close()
        with self.assertRaises(m.MatchError):
            m.extract_order_ids(self.fine)

    def test_multiple_ids_exact_and_deduplicated(self):
        self.book(self.fine, [['主订单号'], ['001, 002，003;001'], [12345]])
        self.assertEqual(m.extract_order_ids(self.fine), ['001', '002', '003', '12345'])

    def test_sheet_selection(self):
        self.book(self.fine, [['主订单编号'], ['001']], sheet='A')
        wb = load_workbook(self.fine)
        ws = wb.create_sheet('B')
        ws.append(['主订单号'])
        ws.append(['002'])
        wb.save(self.fine)
        wb.close()
        with self.assertRaises(m.MatchError):
            m.extract_order_ids(self.fine)
        self.assertEqual(m.extract_order_ids(self.fine, 'B'), ['002'])
        wb = load_workbook(self.fine)
        wb['B'].title = '处罚数据'
        wb.save(self.fine)
        wb.close()
        self.assertEqual(m.extract_order_ids(self.fine), ['002'])

    def test_preamble_and_duplicate_headers(self):
        self.book(self.fine, [['导出信息'], [], ['主订单号'], ['001']])
        self.assertEqual(m.extract_order_ids(self.fine), ['001'])
        self.book(self.fine, [['主订单号', '主订单编号'], ['001', '001']])
        with self.assertRaises(m.MatchError):
            m.extract_order_ids(self.fine)

    def test_counts_ambiguity_unmatched_authoritative(self):
        self.fixtures(fine_rows=[['主订单号', '商品标题', '商品ID'],
                                ['1', None, None], ['2', '保留歧义', '保留ID'],
                                ['3', '已有', '旧ID'], ['4', '保留', None],
                                [None, '无订单', None]],
                      orders=[['原始线上订单号', '商品名称', '店铺款式编码'],
                              ['1', '同款', '001'], ['1', '同款', '001'],
                              ['2', '款A', '002'], ['2', '款B', '003'],
                              ['3', '新款', '004']])
        result = self.match()
        self.assertEqual(result['counts'], dict(matched=2, unmatched=2, ambiguous=1, invalid=0,
                                                conflict=0, filled_cells=2, overwritten_cells=2, changed_cells=4))
        wb = load_workbook(self.fine)
        ws = wb['处罚数据']
        self.assertEqual(ws['B3'].value, '保留歧义')
        self.assertEqual(ws['C3'].value, '保留ID')
        self.assertEqual(ws['B4'].value, '新款')
        self.assertEqual(ws['C4'].value, '004')
        self.assertEqual(ws['B5'].value, '保留')
        self.assertEqual(wb.sheetnames, ['处罚数据', '保留页'])
        wb.close()
        self.assertEqual(len(result['report']), 5)

    def test_multivalue_coverage_and_pairs(self):
        self.fixtures(fine_rows=[['主订单编号', '商品标题', '商品id'], ['1,2'], ['1,3'], ['1,9']],
                      orders=[['原始线上订单号', '商品名称', '店铺款式编码'],
                              ['1,2', '同款', '001'], ['3', '另一款', '003']])
        result = self.match()
        self.assertEqual(result['added_columns'], [])
        self.assertEqual(result['counts']['matched'], 1)
        self.assertEqual(result['counts']['ambiguous'], 1)
        self.assertEqual(result['counts']['unmatched'], 1)

    def test_missing_target_columns_rejected_no_changes(self):
        self.fixtures(fine_rows=[['主订单编号'], ['1']])
        original = self.fine.read_bytes()
        with self.assertRaises(m.MatchError):
            self.match()
        self.assertEqual(self.fine.read_bytes(), original)
        self.assertFalse((self.base / '.jst-fine-backups').exists())

    def test_missing_product_name_never_uses_color_spec(self):
        self.fixtures(orders=[['原始线上订单号', '颜色及规格', '店铺款式编码'],
                              ['000123456789012345678', '不要当商品名称', '001']])
        original = self.fine.read_bytes()
        with self.assertRaises(m.MatchError):
            self.match()
        self.assertEqual(self.fine.read_bytes(), original)

    def test_empty_and_invalid_erp_fields_preserve_both_with_report(self):
        invalids = [None, '', '   ', '=1+2', True, 12345678901234567, 1.2, '#VALUE!']
        for bad in invalids:
            for field in (1, 2):
                with self.subTest(bad=bad, field=field):
                    pair = ['1', 'ERP名', '001']
                    pair[field] = bad
                    self.fixtures(fine_rows=[['主订单编号', '商品标题', '商品id'], ['1', '原名', '原ID']],
                                  orders=[['原始线上订单号', '商品名称', '店铺款式编码'], pair])
                    original = self.fine.read_bytes()
                    result = self.match()
                    self.assertEqual(result['counts']['invalid'], 1)
                    self.assertEqual(result['counts']['changed_cells'], 0)
                    self.assertTrue(result['erp_issues'])
                    self.assertIn('preserved', result['report'][0]['reason'])
                    self.assertEqual(self.fine.read_bytes(), original)
                    self.assertIsNone(result['backup'])

    def test_invalid_duplicate_blocks_other_valid_candidate(self):
        self.fixtures(fine_rows=[['主订单编号', '商品标题', '商品id'], ['1', '原名', '原ID']],
                      orders=[['原始线上订单号', '商品名称', '店铺款式编码'],
                              ['1', 'ERP名', '001'], ['1', None, '001']])
        original = self.fine.read_bytes()
        self.assertEqual(self.match()['counts']['invalid'], 1)
        self.assertEqual(original, self.fine.read_bytes())

    def test_generated_values_literal_not_formulas_preserve_number_formats(self):
        self.fixtures(orders=[['原始线上订单号', '商品名称', '店铺款式编码'],
                              ['000123456789012345678', '+SUM(1,2) & <title>\n换行', '@evil']])
        self.match()
        wb = load_workbook(self.fine, data_only=False)
        self.assertEqual(wb['处罚数据']['B2'].value, '+SUM(1,2) & <title>\n换行')
        self.assertEqual(wb['处罚数据']['C2'].value, '@evil')
        self.assertEqual(wb['处罚数据']['B2'].data_type, 's')
        self.assertEqual(wb['处罚数据']['B2'].number_format, 'General')
        wb.close()

    def test_output_compatibility_no_extra_business_file_and_repeat_noop(self):
        self.fixtures()
        for name in ['../escape', 'a/b', 'a\\b', '..', '', ' x', 'x.', 'a\n']:
            with self.subTest(name=name), self.assertRaises(m.MatchError):
                m.match_fines(self.fine, [self.order], name, 'fine-export')
        with self.assertRaises(m.MatchError):
            self.match(output=self.root / '测试店铺_聚水潭.xlsx')
        with self.assertRaises(m.MatchError):
            self.match(output=self.order)
        first = self.match(output=self.fine)
        before = self.fine.read_bytes()
        stamp = self.fine.stat().st_mtime_ns
        backups = list(Path(first['backup']).parent.iterdir())
        second = self.match()
        self.assertFalse(second['changed'])
        self.assertIsNone(second['backup'])
        self.assertEqual(second['counts']['matched'], 1)
        self.assertEqual(second['counts']['changed_cells'], 0)
        self.assertEqual(self.fine.read_bytes(), before)
        self.assertEqual(self.fine.stat().st_mtime_ns, stamp)
        self.assertEqual(list(Path(first['backup']).parent.iterdir()), backups)

    def test_external_backup_configuration_and_unsafe_locations(self):
        self.fixtures()
        for directory in [self.root, self.root / 'backup']:
            with self.assertRaises(m.MatchError):
                self.match(backup_dir=directory)
        directory = self.base / 'external-backup'
        result = self.match(backup_dir=directory)
        self.assertEqual(Path(result['backup']).parent, directory)

    def test_symlink_sources_outputs_backup_and_lock_rejected(self):
        self.fixtures()
        original = self.fine.read_bytes()
        alias = self.root / 'alias.xlsx'
        alias.symlink_to(self.fine)
        with self.assertRaises(m.MatchError):
            m.match_fines(alias, [self.order], '店', 'fine-export')
        with self.assertRaises(m.MatchError):
            self.match(output=alias)
        alias_directory = self.base / 'alias-input'
        alias_directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(m.MatchError):
            m.match_fines(alias_directory / self.fine.name, [self.order], '店', 'fine-export')
        directory = self.base / 'alias-backup'
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(m.MatchError):
            self.match(backup_dir=directory)
        lock = self.fine.with_name(self.fine.name + '.lock')
        lock.symlink_to(self.order)
        with self.assertRaises(m.MatchError):
            self.match()
        self.assertTrue(lock.is_symlink())
        self.assertEqual(self.fine.read_bytes(), original)

    def test_invalid_order_id_prevents_output(self):
        self.fixtures(orders=[['原始线上订单号', '商品名称', '店铺款式编码'],
                              [12345678901234567, '款', '001']])
        original = self.fine.read_bytes()
        with self.assertRaises(m.MatchError):
            self.match()
        self.assertEqual(self.fine.read_bytes(), original)

    def test_validation_failure_cleans_temp_and_lock_original_intact(self):
        self.fixtures()
        original = self.fine.read_bytes()
        real_loader = m.load_workbook
        def fail_temp(path, *args, **kwargs):
            if isinstance(path, Path) and path.name.startswith('.'):
                raise OSError('synthetic reopen failure')
            return real_loader(path, *args, **kwargs)
        with patch.object(m, 'load_workbook', side_effect=fail_temp):
            with self.assertRaisesRegex(OSError, 'synthetic reopen failure'):
                self.match()
        self.assertEqual(self.fine.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['原处罚数据.xlsx', '原处罚数据.xlsx.lock', '聚水潭订单.xlsx'])

    def test_write_and_replace_failure_original_intact(self):
        self.fixtures()
        original = self.fine.read_bytes()
        with patch.object(m.zipfile.ZipFile, 'writestr', side_effect=OSError('synthetic write failure')):
            with self.assertRaisesRegex(OSError, 'synthetic write failure'):
                self.match()
        self.assertEqual(self.fine.read_bytes(), original)
        with patch.object(m.os, 'replace', side_effect=OSError('synthetic replace failure')):
            with self.assertRaisesRegex(OSError, 'synthetic replace failure'):
                self.match()
        self.assertEqual(self.fine.read_bytes(), original)
        backups = list((self.base / '.jst-fine-backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['原处罚数据.xlsx', '原处罚数据.xlsx.lock', '聚水潭订单.xlsx'])

    def test_conflicting_backup_is_never_overwritten(self):
        self.fixtures()
        original = self.fine.read_bytes()
        directory = self.base / 'backups'
        directory.mkdir()
        import hashlib
        backup = directory / f'{self.fine.stem}-{hashlib.sha256(original).hexdigest()}.xlsx'
        backup.write_bytes(b'not the original')
        with self.assertRaises(m.MatchError):
            self.match(backup_dir=directory)
        self.assertEqual(backup.read_bytes(), b'not the original')
        self.assertEqual(self.fine.read_bytes(), original)

    def test_cli_requires_trigger_and_extract_json(self):
        self.fixtures()
        missing = subprocess.run([sys.executable, str(MODULE), 'match', '--fine', str(self.fine),
                                  '--orders', str(self.order), '--shop-name', '店'], capture_output=True)
        self.assertNotEqual(missing.returncode, 0)
        extract = subprocess.run([sys.executable, str(MODULE), 'extract', str(self.fine)], capture_output=True, text=True)
        self.assertEqual(extract.returncode, 0, extract.stderr)
        self.assertIn('000123456789012345678', extract.stdout)
        match = subprocess.run([sys.executable, str(MODULE), 'match', '--fine', str(self.fine),
                                '--orders', str(self.order), '--shop-name', '店', '--trigger', 'fine-export'],
                               capture_output=True, text=True)
        self.assertEqual(match.returncode, 0, match.stderr)
        import json
        result = json.loads(match.stdout)
        self.assertEqual(result['output'], str(self.fine))
        self.assertEqual(result['counts']['matched'], 1)


if __name__ == '__main__':
    unittest.main()
