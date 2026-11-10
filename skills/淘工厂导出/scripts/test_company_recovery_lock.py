"""Offline evidence and company-lock tests (no real sessions or business files)."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

import company_lock
import recover_hosting_task as recovery
import tmall_bill_download as bill


class SharedLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)

    @unittest.skipIf(os.name == 'nt', 'POSIX subprocess contention')
    def test_posix_subprocess_same_lock_contention_and_release(self):
        path = self.root / '.company-run.lock'
        script = "from company_lock import locked; import sys\nwith locked(sys.argv[1]): print('acquired')"
        with company_lock.locked(path) as owner:
            owner.require(path)
            result = subprocess.run([sys.executable, '-B', '-c', script, str(path)], cwd=Path(__file__).parent, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('禁止并发', result.stderr.decode())
        with self.assertRaises(ValueError): owner.require(path)
        result = subprocess.run([sys.executable, '-B', '-c', script, str(path)], cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_windows_one_byte_lock_and_unlock_mock(self):
        path = self.root / '.company-run.lock'
        fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock())
        # Select backend without pretending POSIX paths are Windows paths.
        with patch.dict(sys.modules, {'msvcrt': fake}), patch.object(company_lock, '_windows', return_value=True):
            with company_lock.locked(path) as owner:
                owner.require(path)
        self.assertEqual([c.args[1:] for c in fake.locking.call_args_list], [(2, 1), (0, 1)])

    def fixture(self):
        work = self.root / '.work-all'; work.mkdir()
        path = work / 'fixture.asset-inventory-state.json'
        dates = {'orderPayTime': {'start': '2026-01-01', 'end': '2026-01-31'}}
        entry = {'phase': 'creating', 'dates': dates, 'queryRows': None,
                 'attemptStartedAt': '2026-01-31T10:00:00+00:00',
                 'requestIdentitySha256': bill.request_identity_digest(recovery.MODULE, dates, None)}
        state = {'scope': 'all', 'expectedCompany': 'synthetic', 'shopName': 'synthetic', 'remoteTasks': {recovery.MODULE: entry}, 'unchanged': [1, 2]}
        bill.save_state(path, state)
        args = argparse.Namespace(state_file=path, expected_state_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), expect_company='synthetic', task_id='42', bind=False, transport='auto', browser='auto', allow_legacy_mtime=False)
        record = {'id': 42, 'taskStatus': 'FINISHED', 'taskName': '投流推广_推广佣金_推广明细_20260101至20260131', 'createTime': int(datetime.fromisoformat(entry['attemptStartedAt']).timestamp()*1000)}
        return args, state, entry, record

    def test_attempt_not_mtime_legacy_optin_digest_and_wrong_lock(self):
        args, state, entry, record = self.fixture()
        original = args.state_file.read_bytes()
        def refresh():
            bill.save_state(args.state_file, state)
            args.expected_state_sha256 = hashlib.sha256(args.state_file.read_bytes()).hexdigest()
        with patch.object(bill, 'load_browser_headers', return_value=({}, None)), patch.object(bill, 'validate_company'), patch.object(bill, 'post', return_value=(200, {'data':[record]})):
            self.assertEqual(recovery.recover(args)['evidence']['timeBasis'], 'attemptStartedAt')
            self.assertEqual(args.state_file.read_bytes(), original)
            args.company_lock = self.root / 'arbitrary.lock'
            with self.assertRaisesRegex(ValueError, 'does not match'): recovery.recover(args)
            args.company_lock = self.root / '.company-run.lock'
            entry['requestIdentitySha256'] = '0'*64; refresh()
            with self.assertRaisesRegex(ValueError, 'digest mismatch'): recovery.recover(args)
            entry.pop('attemptStartedAt'); refresh()
            with self.assertRaisesRegex(ValueError, 'legacy state'): recovery.recover(args)
            args.allow_legacy_mtime = True
            record['createTime'] = int(args.state_file.stat().st_mtime*1000)
            self.assertFalse(recovery.recover(args)['bound'])
            args.bind = True
            with company_lock.locked(args.company_lock) as owner:
                result = recovery.recover(args, lock_ownership=owner)
            self.assertTrue(result['bound'])
            bound = json.loads(args.state_file.read_bytes())
            self.assertEqual(bound['unchanged'], [1, 2])
            self.assertEqual(bound['remoteTasks'][recovery.MODULE]['taskId'], '42')


if __name__ == '__main__': unittest.main()
