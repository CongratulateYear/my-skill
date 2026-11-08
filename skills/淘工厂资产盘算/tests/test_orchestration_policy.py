"""Offline control-flow tests; no browser, business export or real HTTP."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import company_run as c
import orchestration_policy as p


class PolicyTests(unittest.TestCase):
    def test_continuation_and_heartbeat_never_poll_again(self):
        for event in (None, 'heartbeat', 'automatic_continuation', 'stage'):
            self.assertEqual(p.wait_decision(job_id='job1', last_read_job='job1', event=event),
                             'yield_until_host_event')
        self.assertEqual(p.wait_decision(job_id='job1'), 'await_host_notification')
        for event in ('completed', 'failed'):
            self.assertEqual(p.wait_decision(job_id='job1', last_read_job='job1', event=event), 'collect_once')
        self.assertEqual(p.wait_decision(job_id='job1', last_read_job='job1', diagnostic=True), 'collect_once')
        self.assertEqual(p.wait_decision(job_id='job1', last_read_job='job1',
                                         event='completed', event_consumed=True),
                         'yield_until_host_event')
        self.assertEqual(p.wait_decision(job_id='job1', last_read_job='job1',
                                         event='failed', event_consumed=True),
                         'yield_until_host_event')

    def test_waiting_user_rounds_do_not_repeat_read_or_prompt(self):
        key = 'company-batch-job-missing-token'
        self.assertEqual(p.input_wait_decision(waiting_key=key), 'ask_once')
        self.assertEqual(p.input_wait_decision(waiting_key=key, last_prompt_key=key),
                         'yield_until_new_input')
        self.assertEqual(p.input_wait_decision(waiting_key=key, last_prompt_key=key,
                                              new_input=True), 'inspect_original_stop_point')
        self.assertEqual(p.input_wait_decision(waiting_key=key, last_prompt_key=key,
                                              new_session_evidence=True), 'inspect_original_stop_point')
        self.assertEqual(p.input_wait_decision(waiting_key=None), 'not_waiting')

    def test_precheck_rejects_external_credentials_paths_without_http(self):
        with patch.object(p, 'build_opener') as factory:
            for url in ('https://evil.test', 'http://evil.test:8901', 'http://user:secret@localhost:8901',
                        'http://localhost:8901/session', 'http://localhost:8901?secret=x'):
                self.assertEqual(p.bridge_precheck(url)['status'], 'skipped_nonlocal_or_invalid')
            factory.assert_not_called()

    def test_ping_only_bounded_whitelist_and_never_login_proof(self):
        opener = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(dict(ok=True, agentAttached=True, cookie='SECRET', page='SECRET')).encode()
        opener.open.return_value = response
        with patch.object(p, 'build_opener', return_value=opener):
            result = p.bridge_precheck()
        self.assertEqual(result, dict(status='attached', login_state='unverified'))
        self.assertEqual(opener.open.call_args.args[0].full_url, 'http://127.0.0.1:8901/ping')
        response.read.assert_called_once_with(4097)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_precheck_failure_is_advisory_sanitized(self):
        with patch.object(p, 'build_opener', side_effect=OSError('SECRET')):
            self.assertEqual(p.bridge_precheck(), dict(status='unavailable', login_state='unverified'))


class RecoveryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.plan = dict(next_action='validate_scope_evidence_under_existing_consent_then_resume',
                         approval_file_exists=False, approval_file='new.json', approval_argv=['generator'])
        self.progress = Mock()
        self.progress.call.side_effect = lambda stage, fn, argv: fn(argv)
        self.run = Mock(return_value=0)
        self.command = ['python', 'erp', '--fine-file', 'original.xlsx']

    def execute(self, **kw):
        return c.resume_proven_partner(self.plan, self.command, consent=kw.get('consent', True),
            explicit_approval=kw.get('explicit_approval', None), run=self.run, progress=self.progress)

    def test_opt_in_generator_then_exact_resume_once(self):
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.run.call_args_list[0].args[0], ['generator'])
        self.assertEqual(self.run.call_args_list[1].args[0], self.command + ['--resume', '--scope-approval-file', 'new.json'])
        self.assertEqual(self.command, ['python', 'erp', '--fine-file', 'original.xlsx'])

    def test_no_consent_existing_or_invalid_plan_no_execution(self):
        self.assertIsNone(self.execute(consent=False))
        self.assertIsNone(self.execute(explicit_approval='existing.json'))
        self.plan['approval_file_exists'] = True
        self.assertIsNone(self.execute())
        self.plan['approval_file_exists'] = False
        self.plan['next_action'] = 'inspect_current_erp_result_then_resume'
        self.assertIsNone(self.execute())
        self.run.assert_not_called()

    def test_generator_reject_stops_without_export_retry(self):
        self.run.return_value = 2
        self.assertIsNone(self.execute())
        self.run.assert_called_once_with(['generator'])

    def test_failed_resume_is_returned_not_looped(self):
        self.run.side_effect = [0, 2]
        self.assertEqual(self.execute(), 2)
        self.assertEqual(self.run.call_count, 2)

    def test_resume_flag_not_duplicated(self):
        self.command.append('--resume')
        self.execute()
        self.assertEqual(self.run.call_args.args[0].count('--resume'), 1)


if __name__ == '__main__':
    unittest.main()
