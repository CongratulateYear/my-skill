import sys
from pathlib import Path
import tempfile
import unittest
from openpyxl import Workbook, load_workbook
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from delivery_coverage import add_coverage, MODULES

class CoverageTests(unittest.TestCase):
    def test_five_outputs_not_seventeen_successful_files(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.xlsx';b=Path(td)/'b.xlsx';Workbook().save(a)
            results={k:{'status':'completed' if i<5 else 'zero'} for i,k in enumerate(MODULES)}
            result=add_coverage(a,b,{'company':'=literal','results':results})
            self.assertEqual(result['counts'],{'completed':5,'zero':12})
            wb=load_workbook(b);ws=wb['执行说明']
            self.assertEqual(ws.max_row,22);self.assertEqual(ws['B1'].data_type,'s')
            self.assertEqual(ws['C6'].value,'未记录');self.assertEqual(ws['C11'].value,0);wb.close()
    def test_missing_receipt_not_zero(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.xlsx';b=Path(td)/'b.xlsx';Workbook().save(a)
            self.assertEqual(add_coverage(a,b,None)['counts'],{'not_executed':17})
    def test_explicit_skip_and_same_path_are_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.xlsx';b=Path(td)/'b.xlsx';Workbook().save(a)
            key=next(iter(MODULES))
            result=add_coverage(a,b,{'skipped':[key]})
            self.assertEqual(result['counts'],{'skipped':1,'not_executed':16})
            with self.assertRaisesRegex(ValueError,'separate'):
                add_coverage(a,a,None)
    def test_append_uses_owned_workbook_and_rejects_duplicate(self):
        from unittest.mock import patch
        import delivery_coverage as coverage
        w=Workbook()
        try:
            with patch.object(coverage,'load_workbook',side_effect=AssertionError('unexpected reload')):
                self.assertEqual(coverage._append_coverage(w,None)['counts'],{'not_executed':17})
                with self.assertRaisesRegex(ValueError,'already exists'):
                    coverage._append_coverage(w,None)
        finally:
            w.close()

    def test_existing_destination_not_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.xlsx';b=Path(td)/'b.xlsx';Workbook().save(a)
            b.write_bytes(b'keep existing')
            with self.assertRaisesRegex(ValueError,'separate'):
                add_coverage(a,b,None)
            self.assertEqual(b.read_bytes(),b'keep existing')

    def test_zero_with_count_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            a=Path(td)/'a.xlsx';b=Path(td)/'b.xlsx';Workbook().save(a)
            with self.assertRaisesRegex(ValueError,'nonzero'):
                add_coverage(a,b,{'results':{next(iter(MODULES)):{'status':'zero','queryRows':2}}})

if __name__=='__main__':unittest.main()
