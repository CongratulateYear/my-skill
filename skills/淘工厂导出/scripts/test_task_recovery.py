"""Offline mock-only task recovery tests; fixtures stay inside this skill."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tmall_bill_download as bill


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dates = {"orderPayTime": {"start": "2026-01-01", "end": "2026-01-31"}}
        self.args = argparse.Namespace(
            resume=False, _task_state={"taskJournalVersion": 1},
            _task_state_file=self.root / "state.json", output_dir=self.root,
            no_pan_suan=True, max_wait_seconds=0, poll_seconds=0)
        # Network is prohibited even if a future regression bypasses a mock.
        self.network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def reload(self):
        self.args._task_state = bill.load_resume_state(self.args._task_state_file)
        self.args.resume = True

    def create(self, response=None):
        return bill.create_tracked_task(self.args, 'product-loan', self.dates, 7,
                                       lambda: response or {'taskId': 'DB_123'})

    def execute(self, key='product-loan'):
        return bill.execute_module(self.args, None, {}, key, self.dates,
                                   self.root / 'unused.xlsx', False, False)

    def test_main_failure_resume_and_completed_publication_contract(self):
        argv = ['download', '--module', 'product-loan', '--shop-name', 'synthetic-company',
                '--expect-company', 'synthetic-company', '--no-pan-suan',
                '--order-pay-start', '2026-01-01', '--order-pay-end', '2026-01-31',
                '--write-off-start', '2026-01-01', '--write-off-end', '2026-01-31',
                '--output-dir', str(self.root), '--state-file', str(self.args._task_state_file)]
        calls = []
        def post(session, url, headers, payload):
            calls.append(url.rsplit('/', 1)[1])
            if url.endswith('/queryBillDetail'):
                return 200, {'paginator': {'total': 7}}
            if url.endswith('/download/download'):
                return 200, {'taskId': 'DB_123'}
            if url.endswith('/pageQueryDownloadTask'):
                return 200, {'taskId': 'DB_123', 'status': 'SUCCESS'}
            if url.endswith('/getDownloadUrl'):
                return 200, {'fileUrl': 'https://offline.invalid/signed'}
            self.fail(url)
        with patch.object(bill.sys, 'argv', argv), patch.object(bill, 'load_browser_headers', return_value=({}, None)), patch.object(bill, 'validate_company'), patch.object(bill, 'post', side_effect=post), patch.object(bill, 'download_and_extract', side_effect=OSError('download interrupted')):
            with self.assertRaises(OSError):
                bill.main()
        disk = bill.load_resume_state(self.args._task_state_file)
        self.assertEqual(disk['remoteTasks']['product-loan']['taskId'], 'DB_123')
        self.assertEqual(disk['completedModules'], [])
        calls.clear()
        file = self.root / 'fixture.xlsx'
        file.write_bytes(b'synthetic')
        with patch.object(bill.sys, 'argv', argv + ['--resume']), patch.object(bill, 'load_browser_headers', return_value=({}, None)), patch.object(bill, 'validate_company'), patch.object(bill, 'post', side_effect=post), patch.object(bill, 'download_and_extract', return_value=([file], [])):
            self.assertEqual(bill.main(), 0)
        self.assertEqual(calls, ['pageQueryDownloadTask', 'getDownloadUrl'])
        disk = bill.load_resume_state(self.args._task_state_file)
        self.assertEqual(disk['completedModules'], ['product-loan'])
        self.assertEqual(disk['results']['product-loan']['status'], 'completed')
        self.assertEqual(disk['results']['product-loan']['publishFiles'], [str(file.resolve())])
        self.assertNotIn('signed', self.args._task_state_file.read_text(encoding='utf-8'))
        completed = copy.deepcopy(disk['results'])
        with patch.object(bill.sys, 'argv', argv + ['--resume']), patch.object(bill, 'load_browser_headers', return_value=({}, None)), patch.object(bill, 'validate_company'), patch.object(bill, 'post', side_effect=AssertionError('completed must not request')):
            self.assertEqual(bill.main(), 0)
        self.assertEqual(bill.load_resume_state(self.args._task_state_file)['results'], completed)
        disk['completedModules'] = []
        disk['remoteTasks']['product-loan']['phase'] = 'creating'
        bill.save_state(self.args._task_state_file, disk)
        with patch.object(bill.sys, 'argv', argv + ['--resume']), patch.object(bill, 'load_browser_headers', side_effect=AssertionError('must not load credentials')), self.assertRaisesRegex(RuntimeError, '禁止自动重建'):
            bill.main()

    def test_task_id_boundary(self):
        for bad in (None, True, 123, '', ' a', '../a', 'a/b', 'a?b', 'a\n', 'a' * 129):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bill.validate_task_id(bad)
        self.assertEqual(bill.find_task_id({'data': {'taskId': 'DB_123'}}), 'DB_123')
        with self.assertRaises(ValueError):
            bill.find_task_id({'taskId': 'a', 'nested': {'taskId': 'b'}})
        self.assertIsNone(bill.find_task_id({'message': 'DB_123'}))
        with self.assertRaises(ValueError):
            bill.download_and_extract('https://offline.invalid', self.root, '../a')

    def test_intent_precedes_create_and_id_immediately_persists(self):
        def create():
            disk = json.loads(self.args._task_state_file.read_text(encoding='utf-8'))
            entry = disk['remoteTasks']['product-loan']
            self.assertEqual(entry['phase'], 'creating')
            from datetime import datetime, timedelta
            self.assertEqual(datetime.fromisoformat(entry['attemptStartedAt']).utcoffset(), timedelta(0))
            self.assertEqual(entry['requestIdentitySha256'], bill.request_identity_digest('product-loan', self.dates, 7))
            return {'taskId': 'DB_123'}
        bill.create_tracked_task(self.args, 'product-loan', self.dates, 7, create)
        self.reload()
        self.assertEqual(bill.resume_task(self.args, 'product-loan', self.dates)['taskId'], 'DB_123')
        self.assertNotIn('https', self.args._task_state_file.read_text(encoding='utf-8'))

    def test_resume_polls_downloads_existing_without_query_or_create(self):
        self.create()
        self.reload()
        def post(session, url, headers, payload):
            if url.endswith('/pageQueryDownloadTask'):
                self.assertEqual(payload['taskId'], 'DB_123')
                return 200, {'taskId': 'DB_123', 'status': 'SUCCESS'}
            if url.endswith('/getDownloadUrl'):
                self.assertEqual(payload['taskIdList'], ['DB_123'])
                return 200, {'fileUrl': 'https://offline.invalid/signed'}
            self.fail('Unexpected query/create: ' + url)
        with patch.object(bill, 'post', side_effect=post), patch.object(bill, 'download_and_extract', return_value=([], [])) as download:
            self.assertEqual(self.execute()['taskId'], 'DB_123')
            self.assertEqual(download.call_args.args[2], 'DB_123')

    def test_timeout_and_failed_poll_keep_id(self):
        self.create()
        self.reload()
        for status, error in [('WAIT', TimeoutError), ('FAILED', RuntimeError)]:
            with self.subTest(status=status), patch.object(bill, 'post', return_value=(200, {'taskId': 'DB_123', 'status': status})), self.assertRaises(error):
                self.execute()
            self.reload()
            self.assertEqual(bill.resume_task(self.args, 'product-loan', self.dates)['taskId'], 'DB_123')

    def test_uncertain_create_blocks_after_response_loss_or_missing_invalid_id(self):
        for effect in [OSError('response lost'), {}, {'taskId': 123}]:
            self.args.resume = False
            self.args._task_state = {'taskJournalVersion': 1}
            def create():
                if isinstance(effect, Exception):
                    raise effect
                return effect
            with self.subTest(effect=effect), self.assertRaises((OSError, RuntimeError, ValueError)):
                bill.create_tracked_task(self.args, 'product-loan', self.dates, 7, create)
            self.reload()
            with patch.object(bill, 'post', side_effect=AssertionError('must not request')), self.assertRaisesRegex(RuntimeError, '禁止自动重建'):
                self.execute()

    def test_checkpoint_failure_retains_uncertain_intent(self):
        save = bill.save_state
        calls = 0
        def fail_second(path, state):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('disk full')
            save(path, state)
        with patch.object(bill, 'save_state', side_effect=fail_second), self.assertRaises(OSError):
            self.create()
        # Same generic failure path used by main must not save an uncheckpointed ID.
        save(self.args._task_state_file, self.args._task_state)
        self.reload()
        with self.assertRaisesRegex(RuntimeError, '禁止自动重建'):
            bill.resume_task(self.args, 'product-loan', self.dates)

    def test_prewrite_failure_does_not_create(self):
        with patch.object(bill, 'save_state', side_effect=OSError('disk full')), patch.object(bill, 'get_json') as create, self.assertRaises(OSError):
            bill.create_tracked_task(self.args, 'product-loan', self.dates, 7, create)
        create.assert_not_called()

    def test_legacy_unfinished_and_changed_parameters_stop(self):
        legacy = {'version': 2, 'completedModules': ['trade-subsidy'], 'results': {'trade-subsidy': {'status': 'zero', 'publishFiles': []}}, 'failedModule': 'product-loan'}
        before = copy.deepcopy(legacy)
        self.args._task_state = legacy
        self.args.resume = True
        with self.assertRaisesRegex(RuntimeError, '旧未完成状态'):
            bill.resume_task(self.args, 'product-loan', self.dates)
        self.assertEqual(legacy, before)
        self.args.resume = False
        self.args._task_state = {'taskJournalVersion': 1}
        self.create()
        self.reload()
        with self.assertRaises(ValueError):
            bill.resume_task(self.args, 'product-loan', {})

    def test_hosting_matches_id_not_newest_record(self):
        key = 'all-item-effects-export'
        bill.create_tracked_task(self.args, key, self.dates, None, lambda: {'taskId': 'HOST_1'})
        self.reload()
        records = {'data': [{'taskId': 'OTHER', 'taskStatus': 'FINISHED', 'fileUrl': 'https://offline.invalid/wrong'}, {'taskId': 'HOST_1', 'taskStatus': 'FINISHED', 'fileUrl': 'https://offline.invalid/right'}]}
        with patch.object(bill, 'get_json', side_effect=AssertionError('must not create')), patch.object(bill, 'post', return_value=(200, records)), patch.object(bill.time, 'sleep'), patch.object(bill, 'download_and_extract', return_value=([], [])) as download:
            self.assertEqual(self.execute(key)['taskId'], 'HOST_1')
            self.assertEqual(download.call_args.args[0], 'https://offline.invalid/right')

    def test_hosting_numeric_ids_and_create_envelope(self):
        record = {'id': 1518343, 'taskStatus': 'FINISHED', 'taskName': 'synthetic'}
        self.assertEqual(bill.hosting_task_id(record), '1518343')
        self.assertIsNone(bill.hosting_task_id({'id': 1518343}))
        self.assertIsNone(bill.hosting_task_id({'nested': record}))
        self.assertIsNone(bill.find_task_id(record))
        for data in (1518343, '1518343', 'HOST_1'):
            self.assertEqual(bill.hosting_created_task_id({'success': True, 'data': data}), str(data))
            self.assertIsNone(bill.hosting_created_task_id({'data': data}))
            self.assertIsNone(bill.hosting_created_task_id({'success': False, 'data': data}))
        for bad in (True, False, 1.0, -1, 0, 2**53, '-1', '0', str(2**53)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bill.hosting_task_id({'id': bad, 'taskStatus': 'FINISHED'})
            with self.subTest(create=bad), self.assertRaises(ValueError):
                bill.hosting_created_task_id({'success': True, 'data': bad})
        with self.assertRaises(ValueError):
            bill.hosting_task_id({**record, 'taskId': 'OTHER'})
        with self.assertRaises(ValueError):
            bill.hosting_created_task_id({'success': True, 'taskId': 'OTHER', 'data': 1518343})
        with self.assertRaises(ValueError):
            bill.find_task_id({'taskId': 1518343})

    def test_hosting_create_compatible_envelopes_and_null_aliases(self):
        fixtures = [
            {'success': True, 'taskId': None, 'data': 1518343},
            {'success': True, 'data': {'id': 1518343, 'taskId': None}},
            {'success': True, 'id': '1518343', 'data': None},
            {'success': True, 'data': {'task_id': '1518343'}},
            {'success': True, 'result': {'data': {'id': 1518343}}},
            {'data': {'success': True, 'data': 1518343}},
            {'success': True, 'taskId': '1518343', 'data': {'id': 1518343}},
        ]
        for response in fixtures:
            with self.subTest(response=response):
                self.assertEqual(bill.hosting_created_task_id(response), '1518343')
        self.assertEqual(bill.hosting_task_id({'id': 1518343, 'taskId': None,
                                              'taskStatus': 'FINISHED'}), '1518343')
        for response in ({'success': True, 'data': None},
                         {'success': True, 'data': {'metadata': {'id': 99}}},
                         {'data': {'id': 1518343}}, {'success': False, 'taskId': 'VALID'}):
            with self.subTest(absent=response):
                self.assertIsNone(bill.hosting_created_task_id(response))
        for response in (
            {'success': True, 'data': {'id': 1518343, 'taskId': ''}},
            {'success': True, 'data': {'id': 1518343, 'taskId': 'OTHER'}},
            {'success': True, 'data': {'success': False, 'id': 1518343}},
            {'success': True, 'data': [1518343]},
            {'success': True, 'result': True},
            {'success': True, 'data': {'data': {'data': {'data': {'id': 1}}}}},
        ):
            with self.subTest(rejected=response), self.assertRaises(ValueError):
                bill.hosting_created_task_id(response)

    def test_hosting_null_alias_create_checkpoint_and_exact_poll(self):
        key = 'all-item-effects-export'
        records = {'data': [{'id': 99, 'taskStatus': 'FINISHED'},
                            {'id': 1518343, 'taskId': None, 'taskStatus': 'FINISHED',
                             'fileUrl': 'https://offline.invalid/right'}]}
        with patch.object(bill, 'hosting_snapshot_ids', return_value=[]), patch.object(bill, 'get_json', return_value={'success': True, 'taskId': None, 'data': {'id': 1518343}}) as create, patch.object(bill, 'post', return_value=(200, records)), patch.object(bill.time, 'sleep'), patch.object(bill, 'download_and_extract', return_value=([], [])) as download:
            self.assertEqual(self.execute(key)['taskId'], '1518343')
            self.assertEqual(create.call_count, 1)
            self.assertEqual(download.call_args.args[0], 'https://offline.invalid/right')
        self.reload()
        self.assertEqual(bill.resume_task(self.args, key, self.dates)['taskId'], '1518343')

    def test_hosting_duplicate_exact_id_never_downloads(self):
        key = 'all-item-effects-export'
        records = {'data': [{'id': 1518343, 'taskStatus': 'FINISHED'},
                            {'taskId': '1518343', 'taskStatus': 'FINISHED'}]}
        with patch.object(bill, 'hosting_snapshot_ids', return_value=[]), patch.object(bill, 'get_json', return_value={'success': True, 'data': 1518343}), patch.object(bill, 'post', return_value=(200, records)), patch.object(bill.time, 'sleep'), patch.object(bill, 'download_and_extract') as download:
            with self.assertRaisesRegex(ValueError, '重复任务编号'):
                self.execute(key)
            download.assert_not_called()

    def test_hosting_ack_without_unique_evidence_stays_uncertain_without_download(self):
        key = 'all-item-effects-export'
        with patch.object(bill, 'hosting_snapshot_ids', return_value=[]), patch.object(bill, 'get_json', return_value={'success': True, 'data': True}) as create, patch.object(bill, 'hosting_bind_new_task', side_effect=RuntimeError('no unique evidence')) as bind, patch.object(bill, 'post') as poll, patch.object(bill, 'download_and_extract') as download:
            with self.assertRaisesRegex(RuntimeError, 'no unique evidence'):
                self.execute(key)
            self.assertEqual(create.call_count, 1)
            bind.assert_called_once()
            poll.assert_not_called()
            download.assert_not_called()
        self.reload()
        self.assertEqual(self.args._task_state['remoteTasks'][key]['phase'], 'creating')
        with self.assertRaises((ValueError, RuntimeError)):
            bill.resume_task(self.args, key, self.dates)

    def test_hosting_safe_id_boundaries(self):
        for value in (1, 2**53 - 1, 'HOST-1_2', '1e3', 'A' * 128):
            self.assertEqual(bill.validate_hosting_task_id(value), str(value))
        for value in (float('nan'), float('inf'), {}, [], 'A' * 129,
                      '../123', '/123', '12/3', ' 123', '123 ', '1.0',
                      '+123', '_123', '-123', '123\n', '１２３'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bill.validate_hosting_task_id(value)
        for success in (None, 0, 1, 'true', 'false'):
            self.assertIsNone(bill.hosting_created_task_id({'success': success, 'data': 123}))

    def test_hosting_numeric_create_persists_and_polls_exact_record(self):
        key = 'all-item-effects-export'
        records = {'data': [{'id': 99, 'taskStatus': 'FINISHED', 'fileUrl': 'https://offline.invalid/wrong'},
                            {'id': 1518343, 'taskStatus': 'FINISHED', 'fileUrl': 'https://offline.invalid/right'}]}
        with patch.object(bill, 'hosting_snapshot_ids', return_value=[]), patch.object(bill, 'get_json', return_value={'success': True, 'data': 1518343}), patch.object(bill, 'post', return_value=(200, records)), patch.object(bill.time, 'sleep'), patch.object(bill, 'download_and_extract', return_value=([], [])) as download:
            self.assertEqual(self.execute(key)['taskId'], '1518343')
            self.assertEqual(download.call_args.args[0], 'https://offline.invalid/right')
        self.reload()
        self.assertEqual(bill.resume_task(self.args, key, self.dates)['taskId'], '1518343')

    def test_verified_hosting_recovery_readonly_bind_and_rejections(self):
        import recover_hosting_task as recovery
        work = self.root / '.work-all'
        work.mkdir()
        path = work / 'fixture.asset-inventory-state.json'
        state = {'scope': 'all', 'expectedCompany': 'synthetic', 'shopName': 'synthetic', 'completedModules': [],
                 'untouched': {'preserve': True}, 'remoteTasks': {recovery.MODULE: {'phase': 'creating', 'dates': self.dates, 'queryRows': None}}}
        bill.save_state(path, state)
        original = path.read_bytes()
        mtime = path.stat().st_mtime
        args = argparse.Namespace(state_file=path, expected_state_sha256=hashlib.sha256(original).hexdigest(),
                                  expect_company='synthetic', task_id='1518343', bind=False, transport='auto', browser='auto',
                                  allow_legacy_mtime=True)
        record = {'id': 1518343, 'taskStatus': 'FINISHED', 'createTime': int(mtime * 1000),
                  'taskName': '投流推广_推广佣金_推广明细_20260101至20260131', 'fileUrl': 'https://offline.invalid/secret'}
        def run(records):
            with patch.object(bill, 'load_browser_headers', return_value=({}, None)), patch.object(bill, 'validate_company') as identity, patch.object(bill, 'post', return_value=(200, {'data': records})) as post:
                result = recovery.recover(args)
                identity.assert_called_once_with('synthetic', {}, None)
                self.assertTrue(post.call_args.args[1].endswith('/pageListDownloadTask'))
                return result
        self.assertFalse(run([record])['bound'])
        self.assertEqual(path.read_bytes(), original)
        for records in ([], [record, record], [{**record, 'taskName': 'wrong'}],
                        [{**record, 'createTime': int((mtime + 121) * 1000)}],
                        [{**record, 'taskStatus': 'WAIT'}], [record, {**record, 'id': 42}]):
            with self.subTest(records=records), self.assertRaises(ValueError):
                run(records)
            self.assertEqual(path.read_bytes(), original)
        args.expected_state_sha256 = '0' * 64
        with patch.object(bill, 'load_browser_headers', side_effect=AssertionError('must fail before credentials')), self.assertRaises(ValueError):
            recovery.recover(args)
        args.expected_state_sha256 = hashlib.sha256(original).hexdigest()
        args.bind = True
        with patch.object(bill, 'load_browser_headers', return_value=({}, None)), patch.object(bill, 'validate_company', side_effect=ValueError('wrong-company')), patch.object(bill, 'post') as post, self.assertRaisesRegex(ValueError, 'wrong-company'):
            recovery.recover(args)
        post.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        def mutate_state(*unused):
            path.write_bytes(original + b'\n')
            return 200, {'data': [record]}
        with patch.object(bill, 'load_browser_headers', return_value=({}, None)), patch.object(bill, 'validate_company'), patch.object(bill, 'post', side_effect=mutate_state), self.assertRaisesRegex(ValueError, 'state changed'):
            recovery.recover(args)
        # Restore the synthetic fixture, then bind it with a fresh timestamp/hash.
        path.write_bytes(original)
        mtime = path.stat().st_mtime
        record['createTime'] = int(mtime * 1000)
        result = run([record])
        self.assertTrue(result['bound'])
        self.assertEqual(Path(result['backup']).read_bytes(), original)
        bound = bill.load_resume_state(path)
        entry = bound['remoteTasks'][recovery.MODULE]
        evidence = entry.pop('recoveryEvidence')
        self.assertNotIn('secret', json.dumps(evidence))
        self.assertEqual(entry.pop('taskId'), '1518343')
        entry['phase'] = 'creating'
        self.assertEqual(bound, state)

    def test_missing_hosting_id_never_falls_back_to_latest(self):
        key = 'all-item-effects-export'
        bill.create_tracked_task(self.args, key, self.dates, None, lambda: {'taskId': 'HOST_1'})
        self.reload()
        with patch.object(bill, 'post', return_value=(200, {'data': [{'taskId': 'OTHER', 'taskStatus': 'FINISHED', 'fileUrl': 'https://offline.invalid/wrong'}]})), patch.object(bill.time, 'sleep'), patch.object(bill, 'download_and_extract') as download, self.assertRaises(TimeoutError):
            self.execute(key)
        download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
