"""Offline progress and response isolation tests; no export/network."""
import contextlib
import io
import json
from pathlib import Path
import sys
import threading
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import company_run as c
import pansuan_pipeline as p


class PreflightTests(unittest.TestCase):
    def test_preflight_writable_probe_is_ephemeral_and_checks_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            company, output = root/'company', root/'output'
            company.mkdir(); output.mkdir()
            with patch('tgc_runtime.runtime_for', return_value=root/'internal'), \
                 patch.object(p, 'resolve_skill_script', return_value=root/'jushuitan.py'):
                (root/'jushuitan.py').touch()
                (root/'jst_export.py').touch()
                (root/'jst_scope_approval.py').touch()
                p.startup_preflight(company, output, merge_required=False)
                self.assertEqual(list(company.iterdir()), [])
                self.assertEqual(list(output.iterdir()), [])

    def test_full_preflight_checks_template_engine_and_write_denial(self):
        from openpyxl import Workbook
        import delivery_enrichment as delivery
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ('jushuitan.py', 'jst_export.py', 'jst_scope_approval.py',
                         'builder.py', 'template_compat.py', 'node', 'cli'):
                (root/name).touch()
            helper = root/'淘工厂合并'/'scripts'/'recalc_cache.py'
            helper.parent.mkdir(parents=True); helper.touch()
            template = root/'template.xlsx'
            workbook = Workbook(); workbook.active.title = '公司汇总'
            workbook.create_sheet('所有商品效果数据导出'); workbook.save(template); workbook.close()
            with patch('tgc_runtime.runtime_for', return_value=root/'internal'), \
                 patch.object(p, 'resolve_skill_script', return_value=root/'jushuitan.py'), \
                 patch.object(p, 'SKILLS_ROOT', root), \
                 patch.object(c, 'load_module', return_value=SimpleNamespace(
                     PAN_SUAN_BUILDER=root/'builder.py', PAN_SUAN_TEMPLATE=template)), \
                 patch.dict('os.environ', {'TGC_RECALC_NODE': str(root/'node'), 'TGC_RECALC_CLI': str(root/'cli')}):
                p.startup_preflight(root/'company', root/'output', merge_required=True)
                self.assertFalse((root/'company').exists())
                with patch('tempfile.TemporaryFile', side_effect=PermissionError('denied')):
                    with self.assertRaises(PermissionError):
                        p.startup_preflight(root/'company', root/'output', merge_required=True)
                with patch.dict('os.environ', {'TGC_RECALC_NODE': str(root/'missing-node')}):
                    with self.assertRaisesRegex(ValueError, '依赖或模板缺失'):
                        p.startup_preflight(root/'company', root/'output', merge_required=True)
                workbook = Workbook(); workbook.save(template); workbook.close()
                with self.assertRaisesRegex(ValueError, '模板结构无效'):
                    p.startup_preflight(root/'company', root/'output', merge_required=True)

    def test_cli_dry_run_never_probes_writes_or_runs_business(self):
        argv = ['pansuan_pipeline.py', '--company-name', 'test', '--mode', 'export',
                '--order-pay-start', '2026-09-01', '--order-pay-end', '2026-09-30',
                '--write-off-start', '2026-10-01', '--write-off-end', '2026-10-31', '--dry-run']
        with patch.object(sys, 'argv', argv), patch.object(p, 'startup_preflight') as preflight, \
             patch.object(c, 'execute') as execute, patch.object(p, 'run') as run, \
             patch.object(Path, 'mkdir', side_effect=AssertionError('dry-run wrote')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(p.main(), 0)
        preflight.assert_not_called()
        execute.assert_not_called()
        run.assert_not_called()

    def test_cli_preflight_failure_never_creates_journal_or_business_task(self):
        argv = ['pansuan_pipeline.py', '--company-name', 'test', '--mode', 'export',
                '--order-pay-start', '2026-09-01', '--order-pay-end', '2026-09-30',
                '--write-off-start', '2026-10-01', '--write-off-end', '2026-10-31']
        with patch.object(sys, 'argv', argv), \
             patch.object(p, 'startup_preflight', side_effect=PermissionError('blocked')) as preflight, \
             patch.object(c, 'execute') as execute, patch.object(p, 'run_gate') as gate, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(p.main(), 2)
        preflight.assert_called_once()
        execute.assert_not_called()
        gate.assert_not_called()

    def test_preflight_missing_dependency_fails_before_execute(self):
        with patch.object(p, 'resolve_skill_script', return_value=Path('missing-jushuitan.py')):
            with self.assertRaisesRegex(ValueError, '启动预检'):
                p.startup_preflight(Path('.'), Path('.'), merge_required=False)


class FlushedStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


class ProgressTests(unittest.TestCase):
    def setUp(self):
        ping = patch('orchestration_policy.bridge_precheck',
                     return_value={'status': 'unavailable', 'login_state': 'unverified'})
        ping.start()
        self.addCleanup(ping.stop)

    def test_heartbeat_during_synchronous_work_and_shutdown(self):
        err, out = FlushedStream(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            with c.Progress(.01) as progress:
                progress.call('delivery_recalculation', time.sleep, .055)
            finished = err.getvalue()
            time.sleep(.025)
            self.assertEqual(err.getvalue(), finished)
        records = [json.loads(line) for line in finished.splitlines()]
        self.assertTrue(any(r['event'] == 'heartbeat' and r['stage'] == 'delivery_recalculation' for r in records))
        self.assertTrue(all(set(r) == {'type','event','stage','elapsed_seconds','stage_elapsed_seconds'} for r in records))
        self.assertEqual(records[-1]['event'], 'finished')
        self.assertGreaterEqual(err.flushes, len(records))
        self.assertEqual(out.getvalue(), '')
        self.assertFalse(any(t.name == 'pipeline-progress' for t in threading.enumerate()))

    def test_delivery_substeps_are_static_and_no_sensitive_values(self):
        stages = ('delivery_review','delivery_coverage','delivery_audit_preparation',
                  'delivery_engine','delivery_cache_transfer','delivery_integrity','delivery_amount_audit')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with c.Progress(.01) as progress:
                for stage in stages:
                    progress.set_stage(stage)
                with self.assertRaises(ValueError):
                    progress.set_stage('Cookie SECRET /company/path')
        records = [json.loads(line) for line in err.getvalue().splitlines()]
        self.assertTrue(set(stages).issubset({r['stage'] for r in records}))
        self.assertNotIn('SECRET',err.getvalue())
        self.assertTrue(all(set(r) == {'type','event','stage','elapsed_seconds','stage_elapsed_seconds'} for r in records))

    def test_failure_and_invalid_configuration(self):
        for value in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                c.Progress(value)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaisesRegex(RuntimeError, 'SECRET'):
                with c.Progress(.01) as progress:
                    progress.call('export', lambda: (_ for _ in ()).throw(RuntimeError('SECRET')))
        self.assertNotIn('SECRET', err.getvalue())
        self.assertEqual(json.loads(err.getvalue().splitlines()[-1])['event'], 'failed')
        with self.assertRaises(ValueError):
            c.Progress().set_stage('secret argument')

    def test_subprocess_wait_heartbeat_and_no_raw_streams(self):
        err, out = io.StringIO(), io.StringIO()
        def child(*args, **kwargs):
            self.assertTrue(kwargs['capture_output'])
            time.sleep(.045)
            return SimpleNamespace(returncode=0, stdout='TOKEN secret response', stderr='Cookie: secret')
        with patch.object(p.subprocess, 'run', side_effect=child), contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            with c.Progress(.01) as progress:
                self.assertEqual(progress.call('export', p.run, ['secret-command', 'secret-arg']), 0)
        self.assertIn('heartbeat', err.getvalue())
        self.assertNotIn('secret', err.getvalue() + out.getvalue())
        self.assertEqual(out.getvalue(), '')

    def test_safe_failure_diagnostic_and_gate_state(self):
        response = {'ok':False, 'error_code':'scope-mismatch', 'recovery_action':'resume',
            'cookie':'SECRET', 'error_details':{'raw_response':'SECRET','extra_ids':['123'],
                'approval_required':{'approved':False,'task_id':'456','sha256':'abc','output':'local.xlsx',
                    'requested_ids':['1'],'extra_ids':['123'],'headers':'SECRET'}}}
        out, err = io.StringIO(), io.StringIO()
        with patch.object(p.subprocess, 'run', return_value=SimpleNamespace(returncode=2,stdout=json.dumps(response),stderr='SECRET')), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(p.run(['SECRET']), 2)
            rc, state = p.run_gate(['SECRET'])
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(rc, 2)
        self.assertEqual(state['error_details']['extra_ids'], ['123'])
        self.assertEqual(records[0], state)
        self.assertNotIn('SECRET', out.getvalue() + err.getvalue())

    def test_real_export_error_diagnostic_roundtrip(self):
        path = Path(c.__file__).parents[2]/'淘工厂聚水潭'/'scripts'
        sys.path.insert(0, str(path))
        try:
            import jst_export
            raw = {'actual_extra_ids':['2'], 'missing_ids':['3'], 'requested_ids':['1'],
                'scope_approval_eligible':True, 'task_id':'123','sha256':'a'*64,
                'output':str(Path(c.__file__).resolve()),
                'approval_required':{'approved':False,'task_id':'123','sha256':'a'*64,
                    'output':str(Path(c.__file__).resolve()),'requested_ids':['1'],'extra_ids':['2']}}
            diagnostic = jst_export.export_error_diagnostic(jst_export.ExportError('offline',
                code='scope_approval_required', details=raw))
        finally:
            sys.path.remove(str(path))
        envelope = {'ok':False,'error_code':diagnostic['code'],
            'error_details':diagnostic['details'],'export_recovery':diagnostic['recovery']}
        self.assertEqual(p.safe_diagnostic(envelope), envelope)
        rejected = p.safe_diagnostic({'export_recovery':{'action':'https://x/?SECRET',
            'requiredResume':'SECRET','unknown':'SECRET'},'error_details':{
            'actual_extra_ids':['SECRET'],'scope_approval_eligible':'SECRET'}})
        self.assertNotIn('SECRET',json.dumps(rejected))

    def test_real_session_recovery_protocol_and_rejects_unknown_values(self):
        import importlib.util
        path = Path(c.__file__).parents[2]/'淘工厂聚水潭'/'scripts'
        sys.path.insert(0, str(path))
        try:
            spec = importlib.util.spec_from_file_location('offline_jst_session_protocol',path/'jst_session.py')
            session = importlib.util.module_from_spec(spec);spec.loader.exec_module(session)
            recovery = session.recovery_action('bridge_not_running','测试公司','http://127.0.0.1:8901')
        finally:
            sys.path.remove(str(path))
        state = p.safe_diagnostic({'ok':False,'reason':'login-required','company':'测试公司',
            'accountHint':'138****1234','recovery_action':recovery})
        self.assertEqual(state['recovery_action'], recovery)
        self.assertEqual(state['company'], '测试公司')
        self.assertEqual(state['accountHint'], '138****1234')
        unsafe = p.safe_diagnostic({'ok':False,'reason':'SECRET','error_code':'https://x/?token=secret',
            'action':'https://x/?signature=SECRET','raw_response':'SECRET',
            'recovery_action':{'reason':'SECRET','unknown':'SECRET','workbenchUrl':'https://x/?secret=x',
                'steps':['SECRET'],'bridgeStartArgv':['python','https://x/browser_bridge.py','--port','8901']},
            'error_details':{'approval_required':{'task_id':'SECRET','sha256':'not-a-hash','output':'https://x/SECRET'}}})
        self.assertNotIn('SECRET',json.dumps(unsafe))
        self.assertNotIn('https://x',json.dumps(unsafe))
        approved = p.safe_diagnostic({'error_details':{'approval_required':{'approved':False,
            'task_id':'123','sha256':'a'*64,'output':str(Path(c.__file__).resolve()),
            'requested_ids':['1'],'extra_ids':['2']}}})
        self.assertEqual(len(approved['error_details']['approval_required']),6)

    def test_scope_approval_is_resume_only_and_not_task_identity(self):
        import tempfile
        import tgc_runtime
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            folder, state = root/'business', root/'runtime'
            args = SimpleNamespace(company_name='test',module='all',canonical_module='all',
                order_pay_start='2026-01-01',order_pay_end='2026-01-31',
                write_off_start='2026-01-01',write_off_end='2026-01-31',
                mode='export',resume=False,erp_transport='auto',erp_browser='auto',
                erp_bridge_url=None,erp_scope_approval_file=None)
            calls = []
            def runner(command):
                calls.append(command[:])
                if command[0] == 'export':
                    fine = folder/'处罚数据.xlsx';fine.write_bytes(b'offline')
                    receipt = {'run_id':command[command.index('--run-id')+1],
                        'company':'test','folder':str(folder.resolve()),
                        'dates':{key:getattr(args,key) for key in ('order_pay_start','order_pay_end','write_off_start','write_off_end')},
                        'files':{fine.name:c.digest(fine)},'results':{'punishment-data':{'status':'completed'}}}
                    Path(command[command.index('--result-file')+1]).write_text(json.dumps(receipt),encoding='utf-8')
                    return 0
                return 0 if '--prepare-only' in command else 2
            original_loader = c.load_module
            real_export_script = Path(c.__file__).parents[2]/'淘工厂聚水潭'/'scripts'/'jst_export.py'
            def loader(path,name):
                if name == 'company_erp_preflight':
                    return original_loader(real_export_script,name)
                if name == 'inventory_validation':
                    return SimpleNamespace(snapshot_fine=lambda _: {},validate_fine=lambda *a: {})
                return SimpleNamespace(extract_order_ids=lambda _: ['123'])
            with patch.object(tgc_runtime,'runtime_for',return_value=state), patch.object(c,'load_module',side_effect=loader), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(c.execute(args,folder,['export'],None,runner,lambda _: (0,{'ok':True}),None,lambda *a:root/'jushuitan.py'),2)
                args.erp_scope_approval_file = root/'approval.json'
                with self.assertRaises(ValueError):
                    c.execute(args,folder,['export'],None,runner,None,None,None)
                args.resume = True
                # Actual core preflight accepts a validated state with an uncertain local download.
                export_state = {'version':1,'phase':'candidate','requested_ids':['123'],
                    'found_ids':['123'],'missing_ids':[],'internal_ids':['9'],
                    'baseline_ids':[],'candidate_ids':['456'],'export_type':'xlsx',
                    'output':str((state/'orders').absolute()),'download_attempted':True}
                (state/'export.state.json').write_text(json.dumps(export_state),encoding='utf-8')
                self.assertEqual(c.execute(args,folder,['export'],None,runner,
                    lambda _: self.fail('offline recovery must not gate session'),None,lambda *a:root/'jushuitan.py'),2)
                before = len(calls)
                (state/'export.state.json').write_text('{}',encoding='utf-8')
                with contextlib.redirect_stdout(io.StringIO()) as diagnostic:
                    self.assertEqual(c.execute(args,folder,['export'],None,runner,
                        lambda _: self.fail('invalid state must fail closed before session'),None,lambda *a:root/'jushuitan.py'),2)
                records = [json.loads(line) for line in diagnostic.getvalue().splitlines()]
                self.assertEqual(records[0]['error_code'], 'state_invalid')
                self.assertEqual(records[-1]['summary']['execution_status'], 'failed')
                self.assertEqual(len(calls),before+1)  # offline prepare only, no actual ERP task
            self.assertEqual(sum(cmd[0] == 'export' for cmd in calls),1)
            self.assertNotIn('--scope-approval-file', next(cmd for cmd in reversed(calls) if '--prepare-only' in cmd))
            self.assertIn('--scope-approval-file',next(cmd for cmd in reversed(calls) if '--prepare-only' not in cmd))
            journal = json.loads((state/'.company-run.json').read_text(encoding='utf-8'))
            self.assertNotIn('erp_scope_approval_file',journal['identity'])

    def test_cli_rejects_invalid_config_before_dependencies(self):
        base = ['pansuan_pipeline.py','--company-name','test']
        for extra in (['--heartbeat-interval','nan'], ['--heartbeat-interval','0'], ['--erp-scope-approval-file','local.json']):
            with patch.object(sys,'argv',base + extra), patch.object(p,'check_dependencies') as check, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    p.main()
                self.assertEqual(raised.exception.code, 2)
                check.assert_not_called()


if __name__ == '__main__':
    unittest.main()
