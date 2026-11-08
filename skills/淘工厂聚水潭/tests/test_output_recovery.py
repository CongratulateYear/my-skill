"""Offline contracts: recovery is agent guidance; stdout never replaces receipts."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import jst_session as session
import jushuitan as pipeline
from openpyxl import Workbook


class RecoveryTests(unittest.TestCase):
    def test_failure_instructs_inspection_not_immediate_human_login(self):
        output = io.StringIO()
        with patch.object(session, 'build_session', side_effect=session.SessionError('offline fixture')), redirect_stdout(output):
            self.assertEqual(session.main(['--expect-company', '目标公司']), 3)
        report = json.loads(output.getvalue())
        self.assertIsInstance(report['action'], str)
        self.assertEqual(report['diagnostic'], 'offline fixture')
        self.assertEqual(report['expectCompany'], '目标公司')
        action = report['nextAction']
        self.assertFalse(action['pythonControlsBrowser'])
        self.assertEqual(action['tab'], 'new-own-magpie-tab')
        self.assertEqual(action['prefilledCredentials'], 'preserve-and-submit')
        self.assertEqual(action['credentialPrompt'], 'only-after-form-inspection-if-missing')
        self.assertIn('sms', action['humanRequired'])
        self.assertIn('captcha', action['humanRequired'])
        self.assertIn('server-order-query', action['successCriterion'])
        self.assertIn('never use Taogongchang credentials', ' '.join(action['steps']))

    def test_success_does_not_request_browser_login(self):
        output = io.StringIO()
        with patch.object(session, 'build_session', return_value={'identity': {'loggedIn': True}, 'transport': 'bridge'}), redirect_stdout(output):
            self.assertEqual(session.main([]), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report['loggedIn'])
        self.assertNotIn('nextAction', report)


class OutputTests(unittest.TestCase):
    def test_summary_is_allowlist_and_does_not_mutate_full_report(self):
        report = {'ok': False, 'complete': False, 'requested_orders': 2, 'matched_orders': 1,
                  'missing_ids': ['2'], 'found_ids': ['1'], 'receipt': {'order_ids': ['1', '2']},
                  'matching': {'counts': {'unmatched': 1}, 'reason_counts': {'erp_missing': 1},
                               'report': [{'row': 2}], 'erp_issues': ['fixture']},
                  'unexpected': 'not for stdout'}
        summary = pipeline.stdout_summary(report)
        self.assertEqual(summary['missing_ids'], ['2'])
        self.assertEqual(set(summary['matching']), {'counts', 'reason_counts'})
        for key in ('receipt', 'found_ids', 'unexpected'):
            self.assertNotIn(key, summary)
        self.assertEqual(report['matching']['report'], [{'row': 2}])

    def test_default_and_verbose_keep_full_result_file(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                business = root / 'business'
                business.mkdir()
                fine = business / 'fine.xlsx'
                wb = Workbook()
                wb.active.append(['主订单编号'])
                wb.active.append(['123'])
                wb.save(fine)
                wb.close()
                erp = root / 'source.xlsx'
                erp.write_bytes(b'offline business fixture')
                result_file = root / 'private' / 'result.json'
                report = {'ok': False, 'complete': False, 'output': str(erp),
                          'requested_orders': 2, 'matched_orders': 1,
                          'found_ids': ['123'], 'missing_ids': ['456']}
                matching = {'counts': {'matched': 1}, 'reason_counts': {'matched': 1},
                            'report': [{'row': 2, 'order_ids': ['123'], 'reason_code': 'matched'}],
                            'erp_issues': [{'reason': 'offline fixture'}]}
                args = ['--shop-name', '测试', '--fine-file', str(fine), '--trigger', 'fine-export',
                        '--result-file', str(result_file)] + (['--verbose'] if verbose else [])
                output = io.StringIO()
                with patch.object(pipeline, 'build_session', return_value={'session': None, 'headers': {}}), \
                     patch.object(pipeline, 'run_export', return_value=report), \
                     patch.object(pipeline, 'unpack_export', return_value=[erp]), \
                     patch.object(pipeline, 'publish_orders', return_value=[erp]), \
                     patch.object(pipeline, 'match_fines', return_value=matching), redirect_stdout(output):
                    self.assertEqual(pipeline.main(args), 0)
                stdout = json.loads(output.getvalue())
                full = json.loads(result_file.read_text(encoding='utf-8'))
                self.assertEqual(full['matching'], matching)
                self.assertEqual(full['receipt']['order_ids'], ['123'])
                self.assertIn(str(erp), full['receipt']['outputs_sha256'])
                self.assertEqual(stdout['missing_ids'], ['456'])
                if verbose:
                    self.assertEqual(stdout, full)
                else:
                    self.assertNotIn('report', stdout['matching'])
                    self.assertNotIn('receipt', stdout)
                    self.assertNotIn('found_ids', stdout)


if __name__ == '__main__':
    unittest.main()
