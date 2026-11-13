"""Offline-only preparation, failure persistence and coverage contracts."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import jushuitan as cli
import jst_session as session
import browser_bridge
from openpyxl import Workbook
from jst_match import match_fines

class PreparationTests(unittest.TestCase):
    def test_preparation_requires_current_success_and_never_uses_session(self):
        with tempfile.TemporaryDirectory() as root:
            fine = Path(root)/'fine.xlsx'
            wb = Workbook(); wb.active.append(['主订单编号']); wb.active.append(['123']); wb.save(fine); wb.close()
            args = ['--shop-name','测试','--fine-file',str(fine),'--trigger','fine-export','--prepare-only']
            with patch.object(cli,'build_session',side_effect=AssertionError('must be offline')):
                for extra, expected in [([], 'skipped'), (['--fine-export-succeeded'], 'agent_required')]:
                    output = io.StringIO()
                    with redirect_stdout(output): self.assertEqual(cli.main(args+extra),0)
                    result = json.loads(output.getvalue())
                    self.assertEqual(result['preparation_status'],expected)
                    if extra:
                        self.assertFalse(result['recovery_action']['pythonControlsBrowser'])
                self.assertFalse((Path(root)/'聚水潭订单').exists())

    def test_session_failure_persists_complete_failure_and_constraint(self):
        with tempfile.TemporaryDirectory() as root:
            result = Path(root)/'result.json'
            output = io.StringIO()
            with patch.object(cli,'build_session',side_effect=session.SessionError('secret signed URL','extension_not_connected')), redirect_stdout(output):
                code = cli.main(['--shop-name','测试','--order-ids','123','--output-dir',str(Path(root)/'business'),
                                 '--result-file',str(result),'--expect-company','公司','--resume'])
            self.assertEqual(code,3)
            data = json.loads(result.read_text(encoding='utf-8'))
            self.assertEqual(data['execution_status'],'execution_failed')
            self.assertEqual(data['recovery_action']['expectCompany'],'公司')
            self.assertTrue(data['recovery_action']['resumePolicy']['requiredResume'])
            self.assertNotIn('secret',result.read_text(encoding='utf-8'))

    def test_playwright_failures_are_not_login_failure(self):
        for code in ('extension_not_connected','playwright_unavailable','session_scope_expired'):
            with patch.object(session,'_playwright_session',side_effect=session.SessionError('safe',code)):
                with self.assertRaises(session.SessionError) as cm:
                    session.build_session('playwright')
            self.assertEqual(cm.exception.code,code)
            self.assertEqual(session.recovery_action(code)['loginState'],'unverified')

    def test_empty_fine_never_returns_browser_preparation(self):
        with tempfile.TemporaryDirectory() as root:
            fine=Path(root)/'empty.xlsx'
            wb=Workbook(); wb.active.append(['主订单编号']); wb.save(fine); wb.close()
            out=io.StringIO()
            with patch.object(cli,'build_session',side_effect=AssertionError('no ERP')), redirect_stdout(out):
                code=cli.main(['--shop-name','测试','--fine-file',str(fine),'--trigger','fine-export',
                               '--fine-export-succeeded','--prepare-only'])
            self.assertEqual(code,2)
            result=json.loads(out.getvalue())
            self.assertNotIn('bridgeStartArgv',result['recovery_action'])

    def test_export_failure_requires_resume_and_preserves_safe_result(self):
        with tempfile.TemporaryDirectory() as root:
            result=Path(root)/'result.json'
            with patch.object(cli,'build_session',return_value={'session':None,'headers':{}}), \
                 patch.object(cli,'run_export',side_effect=cli.ExportError('uncertain fixture')), redirect_stdout(io.StringIO()):
                code=cli.main(['--shop-name','测试','--order-ids','123','--output-dir',str(Path(root)/'out'),
                               '--result-file',str(result)])
            data=json.loads(result.read_text(encoding='utf-8'))
            self.assertEqual(code,2)
            self.assertEqual(data['stage'],'export')
            self.assertTrue(data['recovery_action']['resumePolicy']['requiredResume'])
            self.assertNotIn('bridgeStartArgv',data['recovery_action'])

    def test_partial_coverage_is_not_execution_failure(self):
        result = cli.result_status({'ok':False,'complete':False,'missing_ids':['2','2'],
                                   'matching':{'counts':{'unmatched':2,'ambiguous':1}}},True)
        self.assertEqual(result['execution_status'],'completed')
        self.assertEqual(result['matching_status'],'partial_match')
        self.assertEqual(result['missing_order_count'],1)
        self.assertEqual(result['review_row_count'],3)
        self.assertFalse(result['ok'])

    def test_review_amount_counts_rows_not_unique_orders(self):
        with tempfile.TemporaryDirectory() as root:
            fine, erp = Path(root)/'fine.xlsx', Path(root)/'erp.xlsx'
            wb=Workbook(); ws=wb.active
            ws.append(['主订单编号','商品标题','商品id','罚款金额'])
            ws.append(['2','','',1.25]); ws.append(['2','','',2]); ws.append(['3','','','=1+1'])
            wb.save(fine); wb.close()
            wb=Workbook(); wb.active.append(['原始线上订单号','商品名称','店铺款式编码'])
            wb.active.append(['1','标题','编码']); wb.save(erp); wb.close()
            report=match_fines(fine,[erp],'测试','fine-export')
            self.assertEqual(report['review_amount'],'3.25')
            self.assertEqual(report['review_amount_known_rows'],2)
            self.assertEqual(report['review_amount_unknown_rows'],1)
            self.assertEqual(report['counts']['unmatched'],3)

if __name__ == '__main__': unittest.main()
