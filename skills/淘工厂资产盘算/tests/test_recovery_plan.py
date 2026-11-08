"""Synthetic, offline recovery planning; no authorization or export is executed."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import company_run as c


class RecoveryPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = self.root / '.erp-result.json'
        self.state = self.root / 'export.state.json'
        self.ids = ['1234567890123456789']
        self.template = dict(approved=False, task_id=42, sha256='a'*64,
                             output=str(self.root/'orders'), requested_ids=self.ids, extra_ids=['987654321'])
        self.state_data = dict(version=2, phase='candidate', candidate_ids=[42],
                               output=self.template['output'], requested_ids=self.ids,
                               pending={k: self.template[k] for k in ('task_id','sha256','output','requested_ids')})
        self.save()

    def save(self):
        self.report.write_text(json.dumps(dict(error_code='scope_approval_required',
            error_details=dict(approval_required=self.template), raw_response='SECRET')), encoding='utf-8')
        self.state.write_text(json.dumps(self.state_data), encoding='utf-8')

    def plan(self):
        return c.erp_failure_recovery(self.report, self.state, self.root/'jst_scope_approval.py', self.ids)

    def test_exact_paths_no_write_and_no_implicit_approval(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = self.plan()
        self.assertEqual(result['evidence_file'], str(self.report))
        self.assertEqual(result['approval_argv'][5], str(self.report))
        self.assertFalse(result['automatic_approval'])
        self.assertTrue(result['consent_required'])
        self.assertEqual(result['resume_arguments'][0], '--resume')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_mismatched_task_order_hash_output_or_phase_does_not_offer_approval(self):
        for key, value in [('candidate_ids',[43]), ('requested_ids',list(reversed(self.ids))+['99']),
                           ('output','elsewhere'), ('phase','creating'), ('pending',{})]:
            original = dict(self.state_data)
            self.state_data[key] = value
            self.save()
            self.assertNotIn('approval_argv', self.plan(), key)
            self.state_data = original

    def test_pending_hash_output_and_ordered_requests_remain_bound(self):
        for key, value in [('sha256', 'b'*64), ('output', 'changed'),
                           ('requested_ids', ['other'])]:
            old = self.state_data['pending'][key]
            self.state_data['pending'][key] = value
            self.save()
            self.assertNotIn('approval_argv', self.plan(), key)
            self.state_data['pending'][key] = old
        self.template['extra_ids'] = ['changed']
        self.save()
        # This is deliberately only a hint: extras and SHA bytes are revalidated
        # by the original offline generator, never synthesized/approved here.
        result = self.plan()
        self.assertEqual(result.get('evidence_validation'), 'required_by_offline_generator')
        self.assertFalse(result['automatic_approval'])

    def test_missing_or_invalid_evidence_fails_closed(self):
        for content in ('not json', '[]', '{"error_code":"other"}'):
            self.report.write_text(content, encoding='utf-8')
            self.assertNotIn('approval_argv', self.plan())
        self.report.unlink()
        self.assertNotIn('approval_argv', self.plan())

    def test_existing_approval_is_not_overwritten(self):
        approval = self.root/'scope_approval_42.json'
        approval.write_text('sentinel', encoding='utf-8')
        self.assertTrue(self.plan()['approval_file_exists'])
        self.assertEqual(approval.read_text(), 'sentinel')

    def test_string_task_id_protocol_and_invalid_types_fail_closed(self):
        self.template['task_id'] = '42'
        self.state_data['candidate_ids'] = ['42']
        self.state_data['pending']['task_id'] = '42'
        self.save()
        plan = self.plan()
        self.assertTrue(plan.get('approval_argv'))
        self.assertTrue(plan['approval_file'].endswith('scope_approval_42.json'))
        for value in (True, 0, -1, '0', '', ' 42', '٤٢', 42.0, None):
            original = (self.template['task_id'], self.state_data['candidate_ids'],
                        self.state_data['pending']['task_id'])
            self.template['task_id'] = value
            self.state_data['candidate_ids'] = [value]
            self.state_data['pending']['task_id'] = value
            self.save()
            self.assertNotIn('approval_argv', self.plan(), repr(value))
            (self.template['task_id'], self.state_data['candidate_ids'],
             self.state_data['pending']['task_id']) = original

    def test_mixed_task_representations_do_not_rewrite_evidence(self):
        self.template['task_id'] = '42'
        self.save()
        before = (self.report.read_bytes(), self.state.read_bytes())
        self.assertIn('approval_argv', self.plan())
        self.assertEqual(before, (self.report.read_bytes(), self.state.read_bytes()))
        for target, key in ((self.state_data, 'candidate_ids'), (self.state_data['pending'], 'task_id')):
            old = target[key]
            target[key] = [True] if key == 'candidate_ids' else True
            self.save()
            self.assertNotIn('approval_argv', self.plan())
            target[key] = old
        self.state_data['pending']['extra'] = 'unexpected'
        self.save()
        self.assertNotIn('approval_argv', self.plan())

    def test_concentrated_review_retains_unknown_amount(self):
        result = c.delivery_summary({'stage':'complete','erp':{'missing_ids':['12','12','34'],
            'matching':{'counts':dict(matched=2,unmatched=3,ambiguous=0,invalid=0,conflict=0),
                        'reason_counts':dict(erp_missing=3), 'review_amount_unknown_rows':3}}}, self.root)
        self.assertEqual(result['review_focus']['missing_unique_order_count'], 2)
        self.assertEqual(result['review_focus']['review_rows'], 3)
        self.assertIsNone(result['review_focus']['amount'])
        self.assertFalse(result['review_focus']['scope_expanded'])


if __name__ == '__main__':
    unittest.main()
