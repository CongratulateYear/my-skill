import importlib.util,json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).parent/'skills'
SCRIPT=ROOT/'淘工厂资产盘算/scripts/company_run.py'
spec=importlib.util.spec_from_file_location('company_run',SCRIPT);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ChainTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.folder=Path(self.tmp.name)/'公司_dates'
  self.args=SimpleNamespace(company_name='公司',module='all',canonical_module='all',mode='full',order_pay_start='2026-09-01',order_pay_end='2026-09-30',write_off_start='2026-09-01',write_off_end='2026-10-31',resume=False,erp_transport='auto',erp_browser='auto',erp_bridge_url=None,settlement_date=None,output_dir=None)
  self.events=[];self.fail_erp=False;self.status='completed';self.ids=['123'];self.bad_receipt=False
 def loader(self,path,name):
  if name=='chain_match':return SimpleNamespace(extract_order_ids=lambda p:self.ids)
  return SimpleNamespace(snapshot_fine=lambda p:{'snapshot':1},validate_fine=lambda p,s:{'ok':True},validate_inventory=lambda *x:{'ok':True})
 def runner(self,cmd):
  if cmd[0]=='export':
   self.events.append('export');self.folder.mkdir(exist_ok=True)
   (self.folder/'处罚数据.xlsx').write_bytes(b'fine')
   receipt={'run_id':cmd[cmd.index('--run-id')+1],'company':'公司','folder':str(self.folder.resolve()),'dates':{key:getattr(self.args,key) for key in ('order_pay_start','order_pay_end','write_off_start','write_off_end')},'files':{'处罚数据.xlsx':m.digest(self.folder/'处罚数据.xlsx')},'results':{'punishment-data':{'status':self.status}}}
   if self.bad_receipt:receipt['company']='wrong'
   Path(cmd[cmd.index('--result-file')+1]).write_text(json.dumps(receipt));return 0
  if '--fine-file' in cmd:
   self.events.append('erp')
   if self.fail_erp:return 3
   exported=self.folder/'聚水潭订单'/'orders.xlsx';exported.parent.mkdir(exist_ok=True);exported.write_bytes(b'erp')
   proof={'shop':'公司','fine':str((self.folder/'处罚数据.xlsx').resolve()),'order_ids':self.ids,'fine_before_sha256':m.digest(self.folder/'处罚数据.xlsx'),'fine_after_sha256':m.digest(self.folder/'处罚数据.xlsx'),'output_sha256':m.digest(exported)}
   Path(cmd[cmd.index('--result-file')+1]).write_text(json.dumps({'ok':False,'complete':False,'missing_ids':['999'],'matching':{'counts':{'matched':1}},'output':str(exported),'receipt':proof}));return 0
  self.events.append('merge');(self.folder.parent/'公司_2026-09-01_2026-10-31.xlsx').write_bytes(b'main');return 0
 def call(self,merge=True):
  # Template candidate discovery is mocked via an actual tiny placeholder resource tree.
  fake=Path(self.tmp.name)/'skill/scripts';fake.mkdir(parents=True,exist_ok=True);(fake.parent/'template.xlsx').write_bytes(b'template')
  with patch.object(m,'load_module',side_effect=self.loader):
   return m.execute(self.args,self.folder,['export'],['merge'] if merge else None,self.runner,lambda c:(0,{}),['gate'],lambda *x:fake/x[-1])
 def test_order_one_merge_and_resume_no_reexport(self):
  self.assertEqual(self.call(),0);self.assertEqual(self.events,['export','erp','merge'])
  self.args.resume=True;self.events=[];self.assertEqual(self.call(),0);self.assertEqual(self.events,[])
 def test_erp_failure_resume_does_not_export(self):
  self.fail_erp=True;self.assertEqual(self.call(),3);self.assertEqual(self.events,['export','erp'])
  self.fail_erp=False;self.args.resume=True;self.events=[];self.assertEqual(self.call(),0);self.assertEqual(self.events,['erp','merge'])
 def test_skip_does_not_call_erp(self):
  self.status='zero';self.assertEqual(self.call(),0);self.assertEqual(self.events,['export','merge'])
 def test_export_mode_still_erp(self):
  self.assertEqual(self.call(False),0);self.assertEqual(self.events,['export','erp'])
 def test_external_change_blocks_resume(self):
  self.call();self.args.resume=True;(self.folder/'处罚数据.xlsx').write_bytes(b'tamper')
  with self.assertRaisesRegex(ValueError,'外部更改'):self.call()
 def test_no_ids_no_erp(self):
  self.ids=[];self.call();self.assertEqual(self.events,['export','merge'])
 def test_wrong_receipt_blocks(self):
  self.bad_receipt=True
  with self.assertRaisesRegex(ValueError,'身份不符'):self.call()
 def test_existing_no_journal_blocks(self):
  self.folder.mkdir();(self.folder/'old.xlsx').write_bytes(b'old')
  with self.assertRaisesRegex(ValueError,'旧账单'):self.call()
 def test_receipt_checkpoint_recovery_no_erp_login(self):
  self.call();journal_path=self.folder/'.company-run.json';journal=json.loads(journal_path.read_text());journal['stage']='erp_running';journal_path.write_text(json.dumps(journal));self.args.resume=True;self.events=[]
  self.assertEqual(self.call(),0);self.assertEqual(self.events,['merge'])
 def test_changed_product_without_receipt_blocks(self):
  self.fail_erp=True;self.call();self.args.resume=True;(self.folder/'处罚数据.xlsx').write_bytes(b'changed product')
  with self.assertRaisesRegex(ValueError,'无有效ERP'):self.call()
 def test_foreign_top_level_input_blocks(self):
  self.call();self.args.resume=True;(self.folder/'old.xlsx').write_bytes(b'old')
  with self.assertRaisesRegex(ValueError,'非本次账单'):self.call()
 def test_changed_identity_blocks(self):
  self.call();self.args.resume=True;self.args.order_pay_end='2026-09-29'
  with self.assertRaisesRegex(ValueError,'续跑公司'):self.call()

if __name__=='__main__':unittest.main(verbosity=2)
