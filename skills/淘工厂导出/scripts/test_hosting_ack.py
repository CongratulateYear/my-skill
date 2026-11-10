"""Fully mocked Hosting ACK/snapshot association; no browser or network."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tmall_bill_download as b


class HostingAckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = argparse.Namespace(resume=False, _task_state={'taskJournalVersion': 1},
            _task_state_file=Path(self.temp.name) / 'state.json')
        self.key = 'all-item-effects-export'
        self.dates = {'orderPayTime': {'start': '2026-09-01', 'end': '2026-09-30'}}

    def record(self, task_id='NEW', name=None, seconds=0):
        entry = self.args._task_state['remoteTasks'][self.key]
        moment = b.datetime.fromisoformat(entry['attemptStartedAt']).timestamp()
        return {'id': task_id, 'taskName': name or '投流推广_推广佣金_推广明细_20260901至20260930',
                'createTime': int((moment + seconds) * 1000), 'taskStatus': 'FINISHED'}

    def invoke(self, response, records=None):
        # Every generation deliberately uses pageSize caps of one record and an
        # explicit empty last page to exercise pagination, not list length.
        created = False
        def create():
            nonlocal created
            created = True
            return response
        def post(session, url, headers, payload):
            page = payload['pageNo']
            if not created:
                items = [{'id': 'OLD', 'taskStatus': 'FINISHED'}] if page == 1 else []
            else:
                items = records() if records else [self.record()]
                items = items[page - 1:page]
            return 200, {'success': True, 'data': items}
        with patch.object(b, 'post', side_effect=post), patch.object(b.time, 'sleep'), patch.object(b, 'HOSTING_MAX_POLLS', 2):
            return b.create_hosting_tracked_task(self.args, self.key, self.dates, object(), {}, create)

    def test_ack_variants_bind_unique_new_task(self):
        for response in (True, False, None, {'success': True, 'data': True},
                         {'success': True, 'data': None}, {'success': True, 'data': False},
                         {'success': True, 'data': {'taskId': '../bad'}},
                         {'success': True, 'data': {'taskId': 1.2}},
                         {'success': True, 'data': {'taskId': ''}}):
            with self.subTest(response=response):
                self.args._task_state = {'taskJournalVersion': 1}
                self.assertEqual(self.invoke(response), 'NEW')
                saved = json.loads(self.args._task_state_file.read_text())
                entry = saved['remoteTasks'][self.key]
                self.assertEqual(entry['hostingSnapshotTaskIds'], ['OLD'])
                self.assertEqual(entry['phase'], 'created')
                self.assertNotEqual(entry['taskId'], '1')
                self.assertEqual(entry['hostingAssociation'], 'verified_snapshot')
                self.assertIn('hostingResponseShape', entry)

    def test_valid_response_id_does_not_need_association(self):
        for response in ({'success': True, 'data': 123}, {'taskId': '123'},
                         {'success': True, 'data': {'id': 123, 'taskId': None}},
                         {'success': True, 'taskId': '../bad', 'data': 123}):
            self.args._task_state = {'taskJournalVersion': 1}
            self.assertEqual(self.invoke(response), '123')
            entry = self.args._task_state['remoteTasks'][self.key]
            self.assertEqual(entry['hostingAssociation'], 'direct_id')

    def test_conflicting_real_ids_cannot_fallback_even_with_bad_alias(self):
        for response in ({'success': True, 'taskId': 'ONE', 'data': {'id': 'TWO'}},
                         {'success': True, 'taskId': '../bad', 'data': {'taskId': 'ONE', 'id': 'TWO'}},
                         {'success': False, 'data': True}):
            self.args._task_state = {'taskJournalVersion': 1}
            with patch.object(b, 'hosting_bind_new_task') as bind:
                with self.assertRaises(b.HostingCreateConflict):
                    self.invoke(response)
                bind.assert_not_called()
            self.assertFalse(self.args._task_state['remoteTasks'][self.key]['hostingFallbackAllowed'])

    def test_uniqueness_requires_all_pages(self):
        with self.assertRaisesRegex(ValueError, '不唯一'):
            self.invoke(True, lambda: [self.record('ONE'), self.record('TWO')])

    def test_wrong_name_old_ids_and_time_are_never_selected(self):
        for make in (lambda: [self.record('OLD')], lambda: [self.record(name='wrong')],
                     lambda: [self.record(seconds=121)], lambda: []):
            self.args._task_state = {'taskJournalVersion': 1}
            with self.assertRaises(RuntimeError):
                self.invoke(True, make)
            self.assertEqual(self.args._task_state['remoteTasks'][self.key]['phase'], 'creating')

    def test_incomplete_snapshot_never_creates(self):
        for response in ({'data': {}}, {'data': [{'id': 'OLD', 'taskStatus': 'FINISHED'}]}):
            self.args._task_state = {'taskJournalVersion': 1}
            with patch.object(b, 'post', return_value=(200, response)), patch.object(b, 'get_json') as create:
                with self.assertRaises(ValueError):
                    b.create_hosting_tracked_task(self.args, self.key, self.dates, object(), {}, create)
                create.assert_not_called()
            self.assertNotIn('remoteTasks', self.args._task_state)

    def test_resume_snapshot_ack_only_reads_never_recreates(self):
        with self.assertRaises(RuntimeError):
            self.invoke(True, lambda: [])
        self.args.resume = True
        with patch.object(b, 'hosting_complete_records', side_effect=lambda *a: [self.record()]), patch.object(b, 'get_json') as create:
            self.assertEqual(b.create_hosting_tracked_task(self.args, self.key, self.dates, object(), {}, create), 'NEW')
            create.assert_not_called()
        self.assertEqual(self.args._task_state['remoteTasks'][self.key]['hostingAssociation'], 'verified_snapshot')

    def test_legacy_creating_still_requires_explicit_recovery(self):
        self.args.resume = True
        self.args._task_state['remoteTasks'] = {self.key: {'phase': 'creating', 'dates': self.dates}}
        with patch.object(b, 'post') as post, patch.object(b, 'get_json') as create:
            with self.assertRaises(RuntimeError):
                b.create_hosting_tracked_task(self.args, self.key, self.dates, object(), {}, create)
            post.assert_not_called()
            create.assert_not_called()

    def test_snapshot_is_durable_before_single_create(self):
        def create():
            saved = json.loads(self.args._task_state_file.read_text())
            entry = saved['remoteTasks'][self.key]
            self.assertEqual(entry['phase'], 'creating')
            self.assertEqual(entry['hostingSnapshotTaskIds'], ['OLD'])
            raise TimeoutError('synthetic network failure')
        with patch.object(b, 'hosting_snapshot_ids', return_value=['OLD']), patch.object(b, 'hosting_bind_new_task') as bind:
            with self.assertRaises(TimeoutError):
                b.create_hosting_tracked_task(self.args, self.key, self.dates, object(), {}, create)
            bind.assert_not_called()
        self.args.resume = True
        with self.assertRaises(RuntimeError):
            b.create_hosting_tracked_task(self.args, self.key, self.dates, object(), {}, create)

    def test_precreate_checkpoint_failure_never_creates(self):
        with patch.object(b, 'hosting_snapshot_ids', return_value=[]), patch.object(b, 'save_state', side_effect=OSError('fixture')), patch.object(b, 'get_json') as create:
            with self.assertRaises(OSError):
                b.create_hosting_tracked_task(self.args, self.key, self.dates, object(), {}, create)
            create.assert_not_called()

    def test_create_checkpoint_failure_does_not_promise_recoverability(self):
        original = b.save_state
        calls = 0
        def save(path, state):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError('fixture checkpoint')
            original(path, state)
        with patch.object(b, 'hosting_snapshot_ids', return_value=[]), patch.object(b, 'save_state', side_effect=save):
            with self.assertRaises(OSError):
                b.create_hosting_tracked_task(self.args, self.key, self.dates, object(), {}, lambda: {'taskId': 'ONE'})
        entry = self.args._task_state['remoteTasks'][self.key]
        self.assertEqual(entry['phase'], 'creating')
        self.assertNotIn('taskId', entry)
        self.assertFalse(entry['hostingFallbackAllowed'])

    def test_postcreate_duplicate_pages_and_invalid_time_reject(self):
        for records in (lambda: [self.record('ONE'), self.record('ONE')],
                        lambda: [{**self.record(), 'createTime': True}]):
            self.args._task_state = {'taskJournalVersion': 1}
            with self.assertRaises(ValueError):
                self.invoke(True, records)

    def test_shape_contains_no_payload_or_arbitrary_keys(self):
        response = {'success': True, 'taskId': 'https://secret.invalid/?cookie=SECRET',
                    'cookie': 'SECRET', 'data': {'authorization': 'SECRET', 'id': True}}
        text = json.dumps(b.hosting_response_shape(response))
        self.assertNotIn('SECRET', text)
        self.assertNotIn('https', text)
        self.assertNotIn('authorization', text)
        self.assertEqual(b.hosting_response_shape(response)['fields']['data']['fields']['id'], {'type': 'bool'})

    def test_request_hash_mismatch_never_binds_or_recreates(self):
        with self.assertRaises(RuntimeError):
            self.invoke(True, lambda: [])
        self.args.resume = True
        self.args._task_state['remoteTasks'][self.key]['requestIdentitySha256'] = 'tampered'
        with patch.object(b, 'hosting_complete_records') as records, patch.object(b, 'get_json') as create:
            with self.assertRaisesRegex(ValueError, '摘要'):
                b.create_hosting_tracked_task(self.args, self.key, self.dates, object(), {}, create)
            records.assert_not_called()
            create.assert_not_called()

    def test_ack_secret_values_never_reach_checkpoint(self):
        secret = 'https://auth.invalid/?token=SECRET&cookie=SECRET'
        self.assertEqual(self.invoke({'success': True, 'taskId': secret,
                                     'cookie': secret, 'data': {'authorization': secret}}), 'NEW')
        saved = self.args._task_state_file.read_text()
        self.assertNotIn('SECRET', saved)
        self.assertNotIn('auth.invalid', saved)
        self.assertNotIn('authorization', saved)
        self.assertEqual(b.hosting_response_shape(type('SECRET', (), {})()), {'type': 'other'})

    def test_arbitrary_nested_ids_are_not_response_candidates(self):
        self.assertEqual(b.hosting_create_identity({'success': True, 'metadata': {'id': 'SECRET'}}), (None, False))
        self.assertEqual(b.hosting_create_identity({'success': True, 'data': True}), (None, True))


if __name__ == '__main__':
    unittest.main()
