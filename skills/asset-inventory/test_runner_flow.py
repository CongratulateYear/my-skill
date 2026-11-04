import sys,json,unittest,uuid,shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'scripts'))
import multi_shop_runner as r
from inventory_contract import atomic_json
class Flow(unittest.TestCase):
 def test_validation_failure_blocks_delivery_and_zero_module_is_supported(self):
  root=Path(__file__).parent/('flow-'+uuid.uuid4().hex); root.mkdir()
  shop=dict(shop='测试',account='100',accountHint='scm100',orderPayStart='2026-09-01',orderPayEnd='2026-09-30',writeOffStart='2026-09-01',writeOffEnd='2026-10-31')
  opts=SimpleNamespace(module='zero',reuse_downloads=False,browser='auto',transport='bridge',bridge_url='')
  def step(cmd,log):
   if str(r.DOWNLOAD_SCRIPT) in cmd:
    s={'shopName':'测试','dates':{'writeOffTime':{'start':'2026-09-01','end':'2026-10-31'},'orderPayTime':{'start':'2026-09-01','end':'2026-09-30'}},'completedModules':['zero'],'results':{'zero':{'queryRows':0,'outputDir':None,'savedFiles':[]}}}
    atomic_json(root/'测试'/'测试_2026-09-01_2026-10-31.asset-inventory-state.json',s)
   elif str(r.BUILDER_SCRIPT) in cmd:
    Path(cmd[cmd.index('--output')+1]).write_bytes(b'test')
    atomic_json(Path(cmd[cmd.index('--result-json')+1]),dict(schemaVersion=1,product_count=0,allocated_rows=0,unassigned_rows=0,sheet_write_verified=True,presentation_verified=True))
   else:
    atomic_json(Path(cmd[cmd.index('--report')+1]),{'ok':False})
    return 1,''
   return 0,''
  try:
   with patch.object(r,'_modules',return_value={'zero':{'target_sheet':'零'}}),patch.object(r,'run_step',side_effect=step): result=r.run_shop(shop,root,opts)
   self.assertFalse(result['ok']); self.assertNotIn('deliver',result['stages'])
   self.assertFalse(Path(result['deliverDir']).exists())
   self.assertFalse(Path(result['summaryDir']).exists())
  finally: shutil.rmtree(root)

 def test_success_delivers_workbook_to_summary_folder_only(self):
  root=Path(__file__).parent/('flow-'+uuid.uuid4().hex); root.mkdir()
  shop=dict(shop='测试',account='100',accountHint='scm100',orderPayStart='2026-09-01',orderPayEnd='2026-09-30',writeOffStart='2026-09-01',writeOffEnd='2026-10-31')
  opts=SimpleNamespace(module='zero',reuse_downloads=False,browser='auto',transport='bridge',bridge_url='')
  def step(cmd,log):
   if str(r.DOWNLOAD_SCRIPT) in cmd:
    d=root/'测试'/'asset-inventory-zero-1'/'out'; d.mkdir(parents=True,exist_ok=True)
    f=d/'明细.xlsx'; f.write_bytes(b'x')
    s={'shopName':'测试','dates':{'writeOffTime':{'start':'2026-09-01','end':'2026-10-31'},'orderPayTime':{'start':'2026-09-01','end':'2026-09-30'}},'completedModules':['zero'],'results':{'zero':{'queryRows':3,'outputDir':str(d),'savedFiles':[str(f)]}}}
    atomic_json(root/'测试'/'测试_2026-09-01_2026-10-31.asset-inventory-state.json',s)
   elif str(r.BUILDER_SCRIPT) in cmd:
    Path(cmd[cmd.index('--output')+1]).write_bytes(b'wb')
    atomic_json(Path(cmd[cmd.index('--result-json')+1]),dict(schemaVersion=1,product_count=0,allocated_rows=0,unassigned_rows=0,sheet_write_verified=True,presentation_verified=True))
   else:
    atomic_json(Path(cmd[cmd.index('--report')+1]),{'ok':True})
    return 0,''
   return 0,''
  try:
   with patch.object(r,'_modules',return_value={'zero':{'target_sheet':'零'}}),patch.object(r,'run_step',side_effect=step): result=r.run_shop(shop,root,opts)
   self.assertTrue(result['ok'],result)
   folder=root/'资产盘算_2026-09-01_2026-10-31'
   self.assertTrue((folder/'测试_2026-09-01_2026-10-31.xlsx').is_file())
   self.assertTrue((Path(result['deliverDir'])/'零.xlsx').is_file())
   self.assertFalse((Path(result['deliverDir'])/'测试_2026-09-01_2026-10-31.xlsx').exists())
   manifest=json.loads((root/'测试'/'inventory-manifest.json').read_text(encoding='utf-8'))
   self.assertIn(str((folder/'测试_2026-09-01_2026-10-31.xlsx').resolve()),manifest['externalDeliverables'])
   self.assertIn('零.xlsx',' '.join(manifest['deliverables']))
  finally: shutil.rmtree(root)
if __name__=='__main__': unittest.main()
