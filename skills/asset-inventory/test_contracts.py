import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'scripts'))
from inventory_contract import atomic_json, context, bind, checkpoint, validate_builder, verify_files, validate_download_state
import multi_shop_runner as runner

class Contracts(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
  self.root=Path(self.tmp.name)
  self.shop=dict(shop='测试店',account='100',accountHint='scm100',orderPayStart='2026-09-01',orderPayEnd='2026-09-30',writeOffStart='2026-09-01',writeOffEnd='2026-10-31')
 def tearDown(self): self.tmp.cleanup()
 def test_missing_identity(self):
  with self.assertRaises(ValueError): context(self.shop|{'accountHint':''})
 def test_context_mismatch(self):
  bind(self.root,context(self.shop))
  for change in ({'accountHint':'other'},{'orderPayEnd':'2026-09-29'},{'account':'other'}):
   with self.assertRaises(ValueError): bind(self.root,context(self.shop|change))
 def test_old_unbound_download_rejected(self):
  (self.root/'asset-inventory-test').mkdir()
  with self.assertRaises(ValueError): bind(self.root,context(self.shop))
 def test_tamper(self):
  ctx=context(self.shop); bind(self.root,ctx)
  d=self.root/'asset-inventory-test'; d.mkdir(); f=d/'source.xlsx'; f.write_bytes(b'original')
  checkpoint(self.root,ctx)
  f.write_bytes(b'changed')
  with self.assertRaises(ValueError): bind(self.root,ctx)
 def test_builder_fail_closed(self):
  valid=dict(schemaVersion=1,product_count=0,allocated_rows=0,unassigned_rows=0,sheet_write_verified=True,presentation_verified=True)
  self.assertEqual(validate_builder(valid),valid)
  for change in ({'product_count':None},{'sheet_write_verified':False},{'presentation_verified':1},{'schemaVersion':2},{'skipped_files':['bad']}):
   with self.assertRaises(ValueError): validate_builder(valid|change)
 def test_utf8_separate_streams(self):
  code,out=runner.run_step([sys.executable,'-c',"import sys; print('中文店铺'); print('警告',file=sys.stderr)"],self.root/'test.log')
  self.assertEqual(code,0); self.assertIn('中文店铺',out)
  self.assertEqual((self.root/'test.log').read_text(encoding='utf-8').strip(),'中文店铺')
  self.assertEqual((self.root/'test.stderr.log').read_text(encoding='utf-8').strip(),'警告')
 def test_zero_record_state(self):
  state={'shopName':'测试店','dates':{'writeOffTime':{'start':'2026-09-01','end':'2026-10-31'},'orderPayTime':{'start':'2026-09-01','end':'2026-09-30'}},'completedModules':['zero'],'results':{'zero':{'queryRows':0,'outputDir':None,'savedFiles':[]}}}
  atomic_json(self.root/'测试店_2026-09-01_2026-10-31.asset-inventory-state.json',state)
  self.assertEqual(validate_download_state(self.root,self.shop,['zero'])[1],[])
 def test_incomplete_state(self):
  state={'shopName':'测试店','dates':{'writeOffTime':{'start':'2026-09-01','end':'2026-10-31'},'orderPayTime':{'start':'2026-09-01','end':'2026-09-30'}},'completedModules':[],'results':{}}
  atomic_json(self.root/'测试店_2026-09-01_2026-10-31.asset-inventory-state.json',state)
  with self.assertRaises(ValueError): validate_download_state(self.root,self.shop,['product-loan'])
if __name__=='__main__': unittest.main()
