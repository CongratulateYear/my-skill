"""Offline status/recovery/timing contracts; no live accounts or business files."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import company_run as c


class StatusTests(unittest.TestCase):
    def summary(self, erp=None, stage='complete', mode='full'):
        return c.delivery_summary({'stage': stage, 'identity': {'mode': mode},
                                   'erp': erp or {}}, Path('synthetic'))

    def test_partial_matching_is_not_execution_failure(self):
        result = self.summary({'ok': False, 'complete': False, 'matching_status': 'partial_match',
            'matching': {'counts': {'matched': 3, 'unmatched': 1, 'ambiguous': 0,
                                     'invalid': 0, 'conflict': 0}}})
        self.assertEqual(result['execution_status'], 'completed')
        self.assertEqual(result['conclusion'], '流程完成，存在待核查')
        self.assertEqual(result['punishment_matching']['review_rows'], 1)
        self.assertIn('erp_missing', result['review_guidance'])
        self.assertEqual(result['structural_validation']['status'], 'unknown')

    def test_no_false_complete_match_from_unknown_or_missing_orders(self):
        for status in ('unknown', 'partial_match', 'complete_match'):
            erp = {'matching_status': status}
            if status != 'complete_match':
                erp['matching'] = {'counts': dict(matched=2, unmatched=0, ambiguous=0, invalid=0, conflict=0)}
            self.assertEqual(self.summary(erp)['conclusion'], '流程完成，存在待核查')
        counts = dict(matched=2, unmatched=0, ambiguous=0, invalid=0, conflict=0)
        self.assertEqual(self.summary({'matching_status': 'complete_match', 'matching': {'counts': counts}})
                         ['conclusion'], '流程完成，匹配完整')
        self.assertEqual(self.summary({'skipped': 'confirmed_zero'})['conclusion'], '流程完成，匹配完整')
        self.assertEqual(self.summary({'skipped': 'no_order_id'})['conclusion'], '流程完成，存在待核查')
        self.assertEqual(self.summary(stage='merged')['conclusion'], '流程未完成，需要处理后续跑')

    def test_bridge_recovery_does_not_assume_login_or_request_password(self):
        for code in ('bridge_agent_missing', 'bridge_not_running'):
            result = c.erp_recovery_status({'session_status': code, 'raw_response': 'SECRET'})
            self.assertEqual(result['recovery_kind'], 'bridge_connection')
            self.assertEqual(result['user_input_required'], 'not_established')
            self.assertEqual(result['next_action'], 'recover_own_erp_tab_then_resume')
            self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(c.erp_recovery_status({'reason': 'permission_denied'})['recovery_kind'],
                         'session_inspection')

    def test_elapsed_stage_event_and_duplicate_stage_does_not_reset(self):
        err = io.StringIO()
        with patch.object(c.time, 'monotonic', return_value=10) as clock, contextlib.redirect_stderr(err):
            progress = c.Progress()
            self.assertEqual(progress.interval, 60)
            clock.return_value = 12
            progress.set_stage('delivery_engine')
            clock.return_value = 20
            progress.set_stage('delivery_engine')
            self.assertEqual(progress._stage_started, 12)
            clock.return_value = 22
            progress.set_stage('delivery_integrity')
        records = [json.loads(line) for line in err.getvalue().splitlines()]
        complete = [r for r in records if r['event'] == 'stage_completed']
        self.assertEqual(complete[-1]['stage'], 'delivery_engine')
        self.assertEqual(complete[-1]['stage_elapsed_seconds'], 10)
        self.assertEqual(progress.stage_seconds['delivery_engine'], 10)
        self.assertEqual(len([r for r in records if r['event'] == 'stage' and r['stage'] == 'delivery_engine']), 1)


if __name__ == '__main__':
    unittest.main()
