import sys, unittest, tempfile
from pathlib import Path
from openpyxl import load_workbook
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from template_compat import rebuild_legacy_helpers
from recalc_cache import canonical_formula, transplant_caches

TEMPLATE=Path(__file__).resolve().parents[1]/'assets'/'盘算模板.xlsx'
class CompatibilityTests(unittest.TestCase):
 def test_empty_template_clears_arrays_and_legacy_error(self):
  w=load_workbook(TEMPLATE); rebuild_legacy_helpers(w)
  self.assertFalse(any(c.data_type=='e' or hasattr(c.value,'text') for s in w for row in s for c in row))
 def test_live_dependency_refused(self):
  w=load_workbook(TEMPLATE);w['使用说明']['A18']='=中间表!B2'
  with self.assertRaisesRegex(ValueError,'真实依赖'):rebuild_legacy_helpers(w)
 def test_id_is_text_and_penalty_uses_correct_columns(self):
  w=load_workbook(TEMPLATE);w['处罚数据']['H2']='=evil';w['处罚数据']['K2']=5
  rebuild_legacy_helpers(w)
  self.assertEqual(w['中间表']['AQ2'].data_type,'s')
  self.assertIn('$K$2',w['中间表']['AR2'].value);self.assertIn('$H$2',w['中间表']['AR2'].value)
 def test_only_optional_reference_normalization(self):
  self.assertEqual(canonical_formula("SUM('商品货款'!$E$2:$E$2)"),canonical_formula('SUM(商品货款!$E$2)'))
  self.assertNotEqual(canonical_formula('SUM(A1)'),canonical_formula('-SUM(A1)'))
 def test_string_literals_not_normalized(self):
  self.assertNotEqual(canonical_formula('IF(A1="\'商品货款\'!B1",1,0)'),canonical_formula('IF(A1="商品货款!B1",1,0)'))
 def test_business_amount_change_refused(self):
  from openpyxl import Workbook
  with tempfile.TemporaryDirectory(dir=r'D:\deepseek') as d:
   d=Path(d); w=Workbook();w.active['A1']=100;w.active['B1']='=SUM(A1)';w.save(d/'source.xlsx')
   w.active['A1']=200;w.save(d/'lo.xlsx')
   with self.assertRaisesRegex(ValueError,'Calculation input changed'):
    transplant_caches(d/'source.xlsx',d/'lo.xlsx',d/'result.xlsx')
 def test_real_cache_transplant(self):
  source=Path(r'D:\deepseek\治发-兼容候选.xlsx');calc=Path(r'D:\deepseek\治发-兼容重算.xlsx')
  with tempfile.TemporaryDirectory(dir=r'D:\deepseek') as d:
   result=transplant_caches(source,calc,Path(d)/'test.xlsx')
   self.assertEqual(result['formula_caches'],2417)
   self.assertEqual(result['empty_string_caches'],11)
if __name__=='__main__':unittest.main()
