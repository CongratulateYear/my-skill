import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[2]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


v = module('inventory_validation', '淘工厂资产盘算/scripts/inventory_validation.py')
m = module('validation_match', '淘工厂聚水潭/scripts/jst_match.py')
b = module('validation_builder', '淘工厂合并/scripts/pan_suan_builder.py')
TEMPLATE = ROOT / '淘工厂合并/assets/盘算模板.xlsx'


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / 'bills'
        self.folder.mkdir()
        self.fine = self.folder / '处罚数据.xlsx'
        wb = load_workbook(TEMPLATE)
        headers = [c.value for c in wb['处罚数据'][1]]
        wb.close()
        wb = Workbook()
        ws = wb.active
        ws.title = '处罚数据'
        ws.append(headers)
        row = {'主订单号': '123456', '主订单编号': '123456', '商品id': '987654',
               '商品标题': 'source title', '处罚金额': 12.5, '店铺': '公司'}
        ws.append([row.get(h) for h in headers])
        wb.save(self.fine)
        wb.close()
        self.main = Path(self.tmp.name) / 'main.xlsx'
        b.merge(TEMPLATE, [self.fine], self.main, strict=True, shop_name='公司')

    def test_builder_english_directory_fallback(self):
        root = Path(self.tmp.name) / 'english-install'
        scripts = root / 'taogongchang-pansuan' / 'scripts'
        scripts.mkdir(parents=True)
        merge_scripts = root / 'taogongchang-merge' / 'scripts'
        merge_scripts.mkdir(parents=True)
        shutil.copyfile(ROOT / '淘工厂合并/scripts/pan_suan_builder.py', merge_scripts / 'pan_suan_builder.py')
        with patch.object(v, '__file__', str(scripts / 'inventory_validation.py')):
            self.assertEqual(v._builder().SUMMARY_REQUIRED_HEADERS, b.SUMMARY_REQUIRED_HEADERS)
            self.assertTrue(v.validate_inventory(self.main, '公司', self.folder, TEMPLATE)['ok'])

    def test_inventory_valid_and_json_safe(self):
        result = v.validate_inventory(self.main, '公司', self.folder, TEMPLATE)
        self.assertEqual(result['fine_rows'], 1)
        json.dumps(result)

    def test_top_level_only_ignores_erp_backups_and_main(self):
        erp = self.folder / '聚水潭订单'
        erp.mkdir()
        (erp / 'bad.xlsx').write_bytes(b'not a workbook')
        (self.folder / '处罚数据.backup.xlsx').write_bytes(b'not a workbook')
        (self.folder / ('处罚数据-' + 'a' * 64 + '.xlsx')).write_bytes(b'not a workbook')
        shutil.copyfile(self.main, self.folder / 'old_main.xlsx')
        result = v.validate_inventory(self.main, '公司', self.folder, TEMPLATE)
        self.assertEqual(result['fine_rows'], 1)

    def test_fine_snapshot_and_backup_path_accept_real_xml_patch(self):
        before = v.snapshot_fine(self.fine)
        json.dumps(before)
        erp = Path(self.tmp.name) / 'erp.xlsx'
        wb = Workbook()
        ws = wb.active
        ws.append(['原始线上订单号', '商品名称', '店铺款式编码'])
        ws.append(['123456', 'ERP title', 'ERP id'])
        wb.save(erp)
        wb.close()
        result = m.match_fines(self.fine, [erp], '公司', 'fine-export')
        self.assertEqual(v.validate_fine(self.fine, before)['changed_cells'], 2)
        self.assertTrue(v.validate_fine(self.fine, result['backup'])['ok'])

    def test_fine_rejects_amount_type_style_sheet_or_row_changes(self):
        before = v.snapshot_fine(self.fine)
        original = self.fine.read_bytes()
        for mutation in ('amount', 'type', 'style', 'sheet', 'row'):
            self.fine.write_bytes(original)
            wb = load_workbook(self.fine)
            ws = wb.active
            col = next(c.column for c in ws[1] if c.value == '处罚金额')
            if mutation == 'amount':
                ws.cell(2, col).value = 99
            elif mutation == 'type':
                ws.cell(2, col).value = '12.5'
            elif mutation == 'style':
                title_col = next(c.column for c in ws[1] if c.value == '商品标题')
                ws.cell(2, title_col).number_format = '0.00'
            elif mutation == 'sheet':
                wb.create_sheet('extra')
            else:
                ws.insert_rows(2)
            wb.save(self.fine)
            wb.close()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                v.validate_fine(self.fine, before)

    def test_inventory_rejects_header_company_total_error_and_fine_mismatch(self):
        original = self.main.read_bytes()
        for mutation in ('header', 'extra_header', 'company', 'total', 'ref', 'error', 'fine', 'extra_fine'):
            self.main.write_bytes(original)
            wb = load_workbook(self.main)
            ws = wb['公司汇总']
            shop_col = next(c.column for c in ws[1] if c.value == '店铺')
            if mutation == 'header':
                ws['A1'] = 'changed'
            elif mutation == 'extra_header':
                ws.cell(1, 31).value = 'extra'
            elif mutation == 'company':
                ws.cell(2, shop_col).value = 'wrong'
            elif mutation == 'total':
                ws.cell(20, shop_col).value = '总计'
            elif mutation == 'ref':
                ws['A20'] = '=#REF!+1'
            elif mutation == 'error':
                ws['A20'] = '#DIV/0!'
            elif mutation == 'fine':
                ws = wb['处罚数据']
                col = next(c.column for c in ws[1] if c.value == '处罚金额')
                ws.cell(2, col).value = 99
            else:
                wb['处罚数据'].cell(100, 1).value = 'unexpected'
            wb.save(self.main)
            wb.close()
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                v.validate_inventory(self.main, '公司', self.folder, TEMPLATE)

    def test_inventory_empty_fine_has_no_company_rows(self):
        wb = load_workbook(self.fine)
        wb.active.delete_rows(2, wb.active.max_row)
        wb.save(self.fine)
        wb.close()
        b.merge(TEMPLATE, [self.fine], self.main, strict=True, shop_name='公司')
        result = v.validate_inventory(self.main, '公司', self.folder, TEMPLATE)
        self.assertEqual(result['fine_rows'], 0)

    def test_inventory_allows_blank_template_padding(self):
        wb = load_workbook(self.main)
        wb['处罚数据'].cell(120, 1).number_format = '@'
        wb.save(self.main)
        wb.close()
        self.assertTrue(v.validate_inventory(self.main, '公司', self.folder, TEMPLATE)['ok'])


if __name__ == '__main__':
    unittest.main()
