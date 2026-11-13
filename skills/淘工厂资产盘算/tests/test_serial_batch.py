"""Offline end-to-end serial controller + real company journal, mocked I/O only."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import company_run as c
import serial_batch as b
import pansuan_pipeline as p
import tgc_runtime


class SerialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batch = self.root / 'batch'
        self.calls = []; self.gates = []; self.readiness = {}; self.erp = False
        self.interrupt = False
        def runtime(path):
            import hashlib
            return self.root / 'runtime' / hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()
        self.runtime = runtime
        for module in (b, tgc_runtime):
            mock = patch.object(module, 'runtime_for', side_effect=runtime)
            mock.start(); self.addCleanup(mock.stop)
        original = c.load_module
        def loader(path, name):
            if name == 'inventory_validation':
                return SimpleNamespace(snapshot_fine=lambda _: {}, validate_fine=lambda *a: {'ok': True})
            if name == 'chain_match':
                return SimpleNamespace(extract_order_ids=lambda _: ['123'])
            if name == 'company_erp_preflight':
                return SimpleNamespace(can_resume_offline=lambda *a: False, ExportError=ValueError)
            return original(path, name)
        mock = patch.object(c, 'load_module', side_effect=loader)
        mock.start(); self.addCleanup(mock.stop)

    def argv(self, resume=False):
        result = ['--companies', 'A', 'B', '--batch-folder', str(self.batch), '--mode', 'export']
        for key in ('order-pay-start', 'order-pay-end', 'write-off-start', 'write-off-end'):
            result += ['--' + key, '2026-09-01']
        return result + (['--resume'] if resume else [])

    def company_main(self, argv):
        if '--dry-run' in argv:
            return 0
        company = argv[argv.index('--company-name') + 1]
        a = SimpleNamespace(company_name=company, module='all', canonical_module='all',
            order_pay_start='2026-09-01', order_pay_end='2026-09-01',
            write_off_start='2026-09-01', write_off_end='2026-09-01', mode='export',
            resume='--resume' in argv, erp_transport='playwright', erp_browser='auto', erp_bridge_url=None)
        folder = self.batch / (company + '_2026-09-01_2026-09-01')
        def gate(command):
            service = 'erp' if 'jst_session.py' in str(command) else 'tgc'
            self.gates.append((company, service))
            status = self.readiness.get((company, service))
            return (3, status) if status else (0, {'ok': True, 'loggedIn': True})
        def run(command):
            if '--prepare-only' in command:
                return 0
            if command[0] == 'export':
                self.calls.append((company, 'export'))
                if self.interrupt:
                    raise KeyboardInterrupt()
                fine = folder / '处罚数据.xlsx'; fine.write_bytes(b'synthetic')
                receipt = {'run_id': command[command.index('--run-id') + 1], 'company': company,
                    'folder': str(folder.resolve()), 'dates': {key: getattr(a, key) for key in
                    ('order_pay_start','order_pay_end','write_off_start','write_off_end')},
                    'files': {fine.name: c.digest(fine)},
                    'results': {'punishment-data': {'status': 'completed' if self.erp else 'zero'}}}
                Path(command[command.index('--result-file') + 1]).write_text(json.dumps(receipt))
                return 0
            self.calls.append((company, 'erp'))
            fine = folder / '处罚数据.xlsx'
            output = folder / '聚水潭订单' / 'fixture.xlsx'; output.parent.mkdir(exist_ok=True)
            output.write_bytes(b'offline ERP')
            report = {'ok': False, 'complete': False, 'matching_status': 'partial_match',
                'matching': {'counts': dict(matched=0, unmatched=1, ambiguous=0, invalid=0, conflict=0)},
                'output': str(output), 'receipt': {'shop': company, 'fine': str(fine.resolve()),
                'order_ids': ['123'], 'fine_before_sha256': c.digest(fine), 'fine_after_sha256': c.digest(fine),
                'output_sha256': c.digest(output), 'outputs_sha256': {str(output): c.digest(output)}}}
            Path(command[command.index('--result-file') + 1]).write_text(json.dumps(report))
            return 0
        return c.execute(a, folder, ['export'], None, run, gate, ['tgc'], lambda *a: self.root / 'jushuitan.py')

    def execute(self, argv=None):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            rc = b.execute(argv or self.argv(), self.company_main)
        self.text = stdout.getvalue()
        return rc, json.loads(self.text.splitlines()[-1])['summary']

    def test_first_done_next_wrong_company_then_explicit_resume(self):
        self.readiness[('B', 'tgc')] = {'reason': 'wrong-company', 'raw': 'password=SECRET'}
        rc, summary = self.execute()
        self.assertEqual(rc, 3)
        self.assertEqual(summary['execution_status'], 'awaiting_agent')
        self.assertEqual(self.calls, [('A', 'export')])
        self.assertNotIn('SECRET', self.text)
        # External agent event: browser repair is simulated; Python never calls it.
        self.readiness.clear()
        rc, summary = self.execute(self.argv(True))
        self.assertEqual(rc, 0)
        self.assertEqual(summary['execution_status'], 'completed')
        self.assertEqual(self.calls, [('A', 'export'), ('B', 'export')])
        self.assertEqual(self.gates.count(('A', 'tgc')), 1)

    def test_export_done_erp_expired_resume_no_reexport_and_review_continues(self):
        self.erp = True
        self.readiness[('A', 'erp')] = {'session_status': 'session_expired'}
        rc, summary = self.execute()
        self.assertEqual(summary['execution_status'], 'awaiting_agent')
        self.assertEqual(self.calls, [('A', 'export')])
        self.readiness.clear()
        rc, summary = self.execute(self.argv(True))
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [('A', 'export'), ('A', 'erp'), ('B', 'export'), ('B', 'erp')])
        self.assertEqual(summary['companies'][0]['conclusion'], '流程完成，存在待核查')

    def test_user_wait_stays_wait_and_unknown_exit_is_not_login(self):
        self.readiness[('A', 'tgc')] = {'reason': 'captcha_required'}
        for argv in (self.argv(), self.argv(True)):
            rc, summary = self.execute(argv)
            self.assertEqual(summary['execution_status'], 'awaiting_user')
            self.assertEqual(self.calls, [])
        self.readiness[('A', 'tgc')] = {'reason': 'unknown_error'}
        rc, summary = self.execute(self.argv(True))
        self.assertEqual(summary['execution_status'], 'failed')

    def test_same_cause_bounded_and_no_next_company(self):
        self.readiness[('A', 'tgc')] = {'reason': 'wrong-company'}
        self.assertEqual(self.execute()[1]['execution_status'], 'awaiting_agent')
        result = self.execute(self.argv(True))[1]
        self.assertEqual(result['execution_status'], 'failed')
        self.assertEqual(result['recovery']['agent_attempts'], 2)
        self.assertEqual(self.calls, [])

    def test_interrupt_binding_mismatch_preserves_current_company(self):
        self.interrupt = True
        self.assertEqual(self.execute()[1]['execution_status'], 'failed')
        self.interrupt = False
        for argv in (self.argv(True)[0:1] + ['B', 'A'] + self.argv(True)[3:],
                     [x if x != '2026-09-01' else '2026-09-02' for x in self.argv(True)]):
            with self.assertRaisesRegex(ValueError, 'identical'):
                self.execute(argv)
        self.assertEqual(self.calls, [('A', 'export')])
        rc, summary = self.execute(self.argv(True))
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls[-1], ('B', 'export'))

    def test_completed_verification_is_read_only_and_tamper_stops_next(self):
        self.assertEqual(self.execute()[0], 0)
        path = b.company_paths('A', b.binding(*b.parse(self.argv())))[1]
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertEqual(self.execute(self.argv(True))[0], 0)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.calls, [('A', 'export'), ('B', 'export')])
        folder, _ = b.company_paths('A', b.binding(*b.parse(self.argv())))
        (folder / '处罚数据.xlsx').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, '外部更改'):
            self.execute(self.argv(True))
        self.assertEqual(self.calls, [('A', 'export'), ('B', 'export')])

    def test_empty_business_directory_cannot_adopt_old_internal_journal(self):
        bound = b.binding(*b.parse(self.argv()))
        _, path = b.company_paths('A', bound)
        path.parent.mkdir(parents=True)
        path.write_text('{"stage":"new"}')
        with self.assertRaisesRegex(ValueError, 'cannot adopt'):
            self.execute()
        self.assertEqual(path.read_text(), '{"stage":"new"}')
        self.assertFalse((self.runtime(self.batch) / 'serial-inputs.json').exists())

    def test_cli_dry_run_and_secrets_rejected_without_writes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(p.main(self.argv() + ['--dry-run']), 0)
        self.assertFalse(self.batch.exists())
        for flag in ('--password', '--cookie', '--fetch-file', '--expect-account'):
            with self.assertRaises(ValueError):
                b.parse(self.argv() + [flag, 'SECRET'])
        self.assertFalse((self.root / 'runtime').exists())


if __name__ == '__main__':
    unittest.main()
