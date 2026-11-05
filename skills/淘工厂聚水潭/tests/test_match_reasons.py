import importlib.util
from pathlib import Path
import tempfile
import unittest
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('jst_match_reasons', ROOT / '淘工厂聚水潭/scripts/jst_match.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MatchReasonsTest(unittest.TestCase):
    def test_stable_reasons_legacy_counts_and_authority(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / 'bills'
            folder.mkdir()
            fine = folder / '处罚数据.xlsx'
            erp = Path(td) / 'erp.xlsx'
            wb = Workbook()
            ws = wb.active
            ws.title = '处罚数据'
            ws.append(['主订单编号', '商品标题', '商品id', '处罚金额'])
            for oid in (None, 'missing', 'amb', 'invalid', 'ok', 'ok,missing'):
                ws.append([oid, 'old title', 'old id', 1.25])
            wb.save(fine)
            wb.close()
            wb = Workbook()
            ws = wb.active
            ws.append(['原始线上订单号', '商品名称', '店铺款式编码'])
            for row in [('amb', 'a', '1'), ('amb', 'b', '2'), ('invalid', None, '3'),
                        ('ok', 'ERP title', 'ERP id'), ('ok', 'ERP title', 'ERP id')]:
                ws.append(row)
            wb.save(erp)
            wb.close()
            result = m.match_fines(fine, [erp], '公司', 'fine-export')
            self.assertEqual([r['reason_code'] for r in result['report']],
                             ['no_order_id', 'erp_missing', 'ambiguous', 'invalid_fields', 'matched', 'erp_missing'])
            self.assertEqual(result['reason_counts'], dict(no_order_id=1, erp_missing=2,
                             ambiguous=1, invalid_fields=1, matched=1))
            self.assertEqual(result['counts'], dict(matched=1, unmatched=3, ambiguous=1,
                             invalid=1, conflict=0, changed_cells=2, filled_cells=0, overwritten_cells=2))
            self.assertTrue(all('reason' in r and 'status' in r for r in result['report']))
            wb = load_workbook(fine)
            self.assertEqual([wb['处罚数据'].cell(6,c).value for c in (2,3)], ['ERP title', 'ERP id'])
            self.assertEqual(wb['处罚数据']['D6'].value, 1.25)
            wb.close()
            again = m.match_fines(fine, [erp], '公司', 'fine-export')
            self.assertFalse(again['changed'])
            self.assertEqual(again['reason_counts'], result['reason_counts'])


if __name__ == '__main__':
    unittest.main()
