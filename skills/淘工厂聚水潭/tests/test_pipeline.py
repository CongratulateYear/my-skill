import io,sys,tempfile,unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import jushuitan as j
from openpyxl import Workbook
class PipelineTests(unittest.TestCase):
    def test_dry_no_session_no_writes(self):
        with tempfile.TemporaryDirectory() as root:
            dest=Path(root)/'absent'
            with patch.object(j,'build_session',side_effect=AssertionError('network')),redirect_stdout(io.StringIO()):
                self.assertEqual(j.main(['--order-ids','3316359723289024286,5127637320970018707','--shop-name','测试','--output-dir',str(dest),'--dry-run']),0)
            self.assertFalse(dest.exists())
    def test_trigger_requires_fine(self):
        with self.assertRaises(SystemExit):j.main(['--order-ids','1','--shop-name','测试','--trigger','fine-export'])
    def test_plain_export_no_match(self):
        with tempfile.TemporaryDirectory() as root,patch.object(j,'build_session',return_value={'session':None,'headers':{}}),patch.object(j,'run_export',return_value={'output':'dummy.xlsx'}),patch.object(j,'match_fines',side_effect=AssertionError('unexpected')),redirect_stdout(io.StringIO()):
            self.assertEqual(j.main(['--order-ids','1','--shop-name','测试','--output-dir',root]),0)
    def test_fine_explicit_calls_match(self):
        with tempfile.TemporaryDirectory() as root:
            fine=Path(root)/'fine.xlsx';wb=Workbook();wb.active.append(['主订单编号']);wb.active.append(['3316359723289024286']);wb.save(fine)
            with patch.object(j,'build_session',return_value={'session':None,'headers':{}}),patch.object(j,'run_export',return_value={'output':'new.xlsx'}),patch.object(j,'unpack_export',return_value=['new.xlsx']),patch.object(j,'match_fines',return_value={'counts':{}}) as m,redirect_stdout(io.StringIO()):
                self.assertEqual(j.main(['--fine-file',str(fine),'--shop-name','测试','--trigger','fine-export']),0)
                self.assertEqual(m.call_args.args[3],'fine-export')
    def test_resume_and_partial_options(self):
        with tempfile.TemporaryDirectory() as root,patch.object(j,'build_session',return_value={'session':None,'headers':{}}),patch.object(j,'run_export',return_value={'complete':False,'missing_ids':['2']}) as e,redirect_stdout(io.StringIO()):
            self.assertEqual(j.main(['--order-ids','1,2','--shop-name','测试','--output-dir',root,'--resume']),0)
            self.assertTrue(e.call_args.kwargs['resume']);self.assertTrue(e.call_args.kwargs['allow_missing'])
    def test_fine_download_separate_and_backup_passed(self):
        with tempfile.TemporaryDirectory() as root:
            fine=Path(root)/'处罚数据.xlsx';w=Workbook();w.active.append(['主订单号']);w.active.append(['123']);w.save(fine)
            with patch.object(j,'build_session',return_value={'session':None,'headers':{}}),patch.object(j,'run_export',return_value={'output':'new.xlsx'}) as e,patch.object(j,'unpack_export',return_value=['new.xlsx']),patch.object(j,'match_fines',return_value={}) as m,redirect_stdout(io.StringIO()):
                self.assertEqual(j.main(['--fine-file',str(fine),'--shop-name','测试','--trigger','fine-export','--backup-dir',str(Path(root).parent/'backups')]),0)
                self.assertEqual(e.call_args.args[3].parent,(fine.parent/'聚水潭订单').resolve());self.assertIn('backup_dir',m.call_args.kwargs)
if __name__=='__main__':unittest.main()
