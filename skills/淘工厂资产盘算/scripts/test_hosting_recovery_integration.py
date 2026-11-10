"""Offline parent-lock recovery integration; never starts a real exporter."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import company_run as company


class IntegrationTests(unittest.TestCase):
    def test_readonly_plan_then_explicit_binding_uses_parent_lock_and_same_hash(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            work = root / '.work-all'; work.mkdir()
            path = work / 'fixture.asset-inventory-state.json'
            args = argparse.Namespace(canonical_module='all', company_name='synthetic', order_pay_start='2026-01-01', order_pay_end='2026-01-31', transport='auto', browser='auto', recover_hosting_task_id=None)
            state = {'scope':'all', 'shopName':'synthetic', 'expectedCompany':'synthetic', 'remoteTasks':{'all-item-effects-export':{'phase':'creating','dates':{'orderPayTime':{'start':args.order_pay_start,'end':args.order_pay_end}}}}}
            path.write_text(json.dumps(state))
            original = path.read_bytes()
            company.export_support()
            import recover_hosting_task as recovery
            with company.locked(root / '.company-run.lock') as owner:
                with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(recovery, 'recover', side_effect=AssertionError('no automatic binding')):
                    self.assertFalse(company.hosting_recovery(args, root, owner))
                plan = json.loads(output.getvalue())['recovery']
                self.assertEqual(plan['expected_state_sha256'], hashlib.sha256(original).hexdigest())
                self.assertEqual(path.read_bytes(), original)
                entry = state['remoteTasks']['all-item-effects-export']
                entry.update(hostingFallbackAllowed=True, hostingSnapshotTaskIds=[])
                path.write_text(json.dumps(state))
                with patch.object(recovery, 'recover', side_effect=AssertionError('kernel must verify new evidence')):
                    self.assertTrue(company.hosting_recovery(args, root, owner))
                self.assertEqual(json.loads(path.read_text()), state)
                args.recover_hosting_task_id = '42'
                with contextlib.redirect_stdout(io.StringIO()), patch.object(recovery, 'recover', return_value={'bound':True}) as recover:
                    self.assertTrue(company.hosting_recovery(args, root, owner))
                self.assertIs(recover.call_args.kwargs['lock_ownership'], owner)
                self.assertEqual(recover.call_args.args[0].expected_state_sha256, hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertTrue(recover.call_args.args[0].bind)
                state['remoteTasks']['all-item-effects-export']['dates']['orderPayTime']['end']='2026-02-01'
                path.write_text(json.dumps(state))
                with self.assertRaisesRegex(ValueError,'dates mismatch'):
                    company.hosting_recovery(args, root, owner)

    def test_first_export_failure_emits_plan_without_binding_or_retry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            runtime = root / 'runtime'
            folder = root / 'company'
            args = argparse.Namespace(company_name='synthetic', module='all', canonical_module='all',
                mode='export', resume=False, order_pay_start='2026-01-01', order_pay_end='2026-01-31',
                write_off_start='2026-01-01', write_off_end='2026-01-31', recover_hosting_task_id=None)
            calls = []
            def run(command):
                calls.append(command)
                work = runtime / '.work-all'; work.mkdir()
                state = {'scope':'all','shopName':'synthetic','expectedCompany':'synthetic',
                    'remoteTasks':{'all-item-effects-export':{'phase':'creating','dates':
                    {'orderPayTime':{'start':args.order_pay_start,'end':args.order_pay_end}}}}}
                (work / 'fixture.asset-inventory-state.json').write_text(json.dumps(state))
                return 1
            company.export_support()
            import recover_hosting_task as recovery
            import tgc_runtime
            with patch.object(tgc_runtime, 'runtime_for', return_value=runtime), patch.object(recovery, 'recover', side_effect=AssertionError('must not bind')), contextlib.redirect_stdout(io.StringIO()) as output:
                progress = type('Progress', (), {'set_stage':lambda *x:None, 'call':lambda self,stage,fn,*a:fn(*a)})()
                rc = company._execute(args, folder, ['offline-export'], [], run, None, [], None, progress)
            self.assertEqual(rc, 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(output.getvalue())['stage'], 'hosting_recovery_required')

    def test_explicit_recovery_requires_resume_before_any_execution(self):
        args = argparse.Namespace(resume=False, recover_hosting_task_id='42')
        with self.assertRaisesRegex(ValueError, 'requires explicit --resume'):
            company.execute(args, None, [], [], None, None, [], None)


if __name__ == '__main__': unittest.main()
