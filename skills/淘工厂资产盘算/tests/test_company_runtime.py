"""Offline journal layout/resume tests; no session or remote task creation."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import company_run as c
import tgc_runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.ping = patch('orchestration_policy.bridge_precheck',
                          return_value={'status': 'unavailable', 'login_state': 'unverified'})
        self.ping.start()
        self.addCleanup(self.ping.stop)

    def args(self, resume=False):
        return SimpleNamespace(company_name='测试', module='all', canonical_module='all',
            order_pay_start='2026-09-01', order_pay_end='2026-09-30',
            write_off_start='2026-09-01', write_off_end='2026-10-31',
            mode='export', resume=resume)

    def test_clean_delivery_and_completed_resume(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / 'business'
            state = Path(td) / 'internal'
            calls = []
            def run(command):
                calls.append(command)
                self.assertEqual(command[command.index('--state-dir')+1], str(state))
                output = folder / '交易佣金.xlsx'
                output.write_bytes(b'offline fixture')
                receipt = {'run_id':command[command.index('--run-id')+1],
                    'company':'测试', 'folder':str(folder.resolve()),
                    'dates':{k:getattr(self.args(), k) for k in ('order_pay_start','order_pay_end','write_off_start','write_off_end')},
                    'files':{output.name:hashlib.sha256(output.read_bytes()).hexdigest()},
                    'results':{'punishment-data':{'status':'zero'}}}
                Path(command[command.index('--result-file')+1]).write_text(json.dumps(receipt), encoding='utf-8')
                return 0
            import contextlib, io
            stdout = io.StringIO()
            with patch.object(tgc_runtime, 'runtime_for', return_value=state), contextlib.redirect_stdout(stdout):
                self.assertEqual(c.execute(self.args(), folder, ['export'], None, run, None, None, None), 0)
                self.assertEqual(c.execute(self.args(True), folder, ['export'], None, run, None, None, None), 0)
                responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
                self.assertEqual(responses[0]['summary'], responses[1]['summary'])
                self.assertEqual(responses[0]['summary']['modules']['counts']['zero'],1)
                self.assertIsNone(responses[0]['summary']['main_workbook'])
                self.assertEqual(responses[0]['summary']['amount_audit']['status'],'not_applicable')
                self.assertEqual(len(calls), 1)
                self.assertEqual([p.name for p in folder.iterdir()], ['交易佣金.xlsx'])
                self.assertTrue((state/'.company-run.json').is_file())
                (folder/'交易佣金.xlsx').write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, '外部更改'):
                    c.execute(self.args(True), folder, ['export'], None, run, None, None, None)

    def test_legacy_blocks_without_creating_task(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / 'business'; folder.mkdir()
            (folder/'.company-run.json').write_text('{}')
            with patch.object(tgc_runtime, 'runtime_for', return_value=Path(td)/'internal'):
                with self.assertRaisesRegex(ValueError, '旧版'):
                    c.execute(self.args(True), folder, ['export'], None, lambda _: self.fail('must not run'), None, None, None)

    def test_session_gate_preserves_erp_success_json(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('pipeline_gate_test',Path(c.__file__).with_name('pansuan_pipeline.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='{"ok":true,"company":"ERP"}',stderr='')):
            rc,state=module.run_gate(['offline-session'])
        self.assertEqual(rc,0);self.assertTrue(state['ok'])

    def test_erp_readiness_failure_never_starts_export_or_reexports(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'business'; state=Path(td)/'internal'; calls=[]; sessions=[]
            args=self.args();args.erp_transport='bridge';args.erp_browser='auto';args.erp_bridge_url='http://127.0.0.1:8901'
            def run(command):
                calls.append(command)
                if '--prepare-only' in command:
                    self.assertNotIn('--result-file',command)
                    return 0
                self.assertEqual(command[0],'export')
                fine=folder/'处罚数据.xlsx';fine.write_bytes(b'offline')
                receipt={'run_id':command[command.index('--run-id')+1],'company':'测试','folder':str(folder.resolve()),
                    'dates':{k:getattr(args,k) for k in ('order_pay_start','order_pay_end','write_off_start','write_off_end')},
                    'files':{fine.name:c.digest(fine)},'results':{'punishment-data':{'status':'completed'}}}
                Path(command[command.index('--result-file')+1]).write_text(json.dumps(receipt),encoding='utf-8');return 0
            original=c.load_module
            def loader(path,name):
                if name=='inventory_validation':return SimpleNamespace(snapshot_fine=lambda _: {},validate_fine=lambda *a: {})
                if name=='chain_match':return SimpleNamespace(extract_order_ids=lambda _: ['123'])
                return original(path,name)
            def gate(command):
                sessions.append(command);return 3,{'session_status':'bridge_not_running'}
            with patch.object(tgc_runtime,'runtime_for',return_value=state),patch.object(c,'load_module',side_effect=loader):
                for resume in (False,True):
                    args.resume=resume
                    self.assertEqual(c.execute(args,folder,['export'],None,run,gate,None,lambda *a:Path(td)/'jushuitan.py'),3)
            self.assertEqual(sum(cmd[0]=='export' for cmd in calls),1)
            self.assertEqual(len(sessions),2)
            self.assertEqual(sum('--prepare-only' in cmd for cmd in calls),2)
            self.assertEqual(json.loads((state/'.company-run.json').read_text(encoding='utf-8'))['stage'],'erp_running')

    def test_all_erp_shards_are_verified(self):
        with tempfile.TemporaryDirectory() as td:
            files = [Path(td)/'erp.xlsx', Path(td)/'erp_2.xlsx']
            for file in files:
                file.write_bytes(b'fixture')
            hashes = {str(file):c.digest(file) for file in files}
            report = {'output':str(files[0]), 'receipt':{
                'output_sha256':hashes[str(files[0])], 'outputs_sha256':hashes}}
            self.assertTrue(c.valid_erp_outputs(report))
            files[1].write_bytes(b'external change')
            self.assertFalse(c.valid_erp_outputs(report))
            self.assertFalse(c.valid_erp_outputs({'output':str(files[0]), 'receipt':{}}))

    def test_delivery_prepared_resume_after_publish_and_tamper(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'business';folder.mkdir();state=Path(td)/'runtime';state.mkdir()
            args=self.args(True);args.mode='full';args.settlement_date=None;args.output_dir=None
            output=folder.parent/'测试_2026-09-01_2026-10-31.xlsx';output.write_bytes(b'published')
            candidate=state/'delivery-candidate.xlsx';candidate.write_bytes(b'published')
            # Build the normal journal identity via a harmless export fixture first.
            a=self.args()
            def exporter(command):
                receipt={'run_id':command[command.index('--run-id')+1], 'company':'测试','folder':str(folder.resolve()),
                    'dates':{k:getattr(a,k) for k in ('order_pay_start','order_pay_end','write_off_start','write_off_end')},
                    'files':{},'results':{'punishment-data':{'status':'zero'}}}
                Path(command[command.index('--result-file')+1]).write_text(json.dumps(receipt),encoding='utf-8');return 0
            with patch.object(tgc_runtime,'runtime_for',return_value=state):
                c.execute(a,folder,['export'],None,exporter,None,None,None)
                path=state/'.company-run.json';journal=json.loads(path.read_text(encoding='utf-8'))
                # Identity mirrors the requested full pipeline command.
                journal['identity']['mode']='full'
                journal['identity']['merge_command']=['merge']
                journal.update(stage='delivery_prepared',merged_sha256='original-before-publish',
                    candidate_sha256=c.digest(output),delivery={'candidate':str(candidate)})
                path.write_text(json.dumps(journal),encoding='utf-8')
                template=state/'盘算模板.xlsx';template.write_bytes(b'template')
                fake_script=state/'scripts'/'merge_excel.py';fake_script.parent.mkdir()
                # resolve assumes template in script.parent.parent/assets
                assets=state/'assets';assets.mkdir();(assets/'盘算模板.xlsx').write_bytes(b'template')
                validator=SimpleNamespace(validate_inventory=lambda *a:{'ok':True})
                with patch.object(c,'load_module',return_value=validator):
                    # Prepared but unpublished: external edits block before replacement.
                    output.write_bytes(b'person-edited-merged')
                    with self.assertRaisesRegex(ValueError,'外部更改'):
                        c.execute(args,folder,['export'],['merge'],lambda _:self.fail('must not run'),None,None,lambda *a:fake_script)
                    # Simulate publication succeeding immediately before checkpoint.
                    output.write_bytes(b'published')
                    self.assertEqual(c.execute(args,folder,['export'],['merge'],lambda _:self.fail('must not re-export/merge'),None,None,lambda *a:fake_script),0)
                output.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError,'外部更改'):
                    c.execute(args,folder,['export'],['merge'],lambda _:self.fail('must not run'),None,None,lambda *a:fake_script)

    def test_summary_uses_only_recorded_evidence_without_mutation(self):
        import copy
        journal = {'identity': {'company':'测试', 'mode':'full', 'dates':{'write_off_start':'2026-09-01'}},
            'receipt': {'files':dict.fromkeys([str(i)+'.xlsx' for i in range(17)], 'hash'),
                'results':{'product-loan':{'status':'completed'}, 'punishment-data':{'status':'zero'},
                           'trade-subsidy':{'status':'failed'}}, 'skipped':['trade-commission']},
            'erp':{'matching_status':'partial_match','missing_ids':['123'], 'matching':{
                'counts':{'matched':2,'unmatched':1,'ambiguous':0,'invalid':0,'conflict':0},
                'reason_counts':{'matched':2,'erp_missing':1}, 'review_amount':'8.50',
                'review_amount_known_rows':1,'review_amount_unknown_rows':0}},
            'delivery':{'recalculation':{'ok':True},'audit':{'status':'passed','verified':True,'blocking':False}},
            'validation':{'ok':True}}
        before = copy.deepcopy(journal)
        with patch('openpyxl.load_workbook',side_effect=AssertionError('no workbook reads')):
            summary = c.delivery_summary(journal,Path('source'),Path('main.xlsx'))
        self.assertEqual(journal,before)
        self.assertEqual(summary['modules']['counts'],dict(completed=1,zero=1,failed=1,skipped=1,not_executed=0,unknown=13))
        self.assertEqual(summary['punishment_matching']['review_amount'],'8.50')
        self.assertEqual(summary['punishment_matching']['review_amount_unknown_rows'],0)
        self.assertEqual(summary['punishment_matching']['total_rows'],3)
        self.assertEqual(summary['punishment_matching']['review_rows'],1)
        self.assertEqual(summary['punishment_matching']['missing_ids'],['123'])
        self.assertEqual(summary['recalculation']['status'],'passed')
        self.assertEqual(summary['structural_validation']['status'],'passed')
        self.assertTrue(summary['amount_audit']['verified'])

    def test_old_complete_unknown_and_export_not_applicable(self):
        journal = {'stage':'complete','identity':{'mode':'full'}, 'receipt':{}}
        summary = c.delivery_summary(journal,Path('source'),Path('main.xlsx'))
        self.assertEqual(summary['modules']['counts']['unknown'],17)
        self.assertEqual(summary['recalculation'],{'status':'unknown','ok':None})
        self.assertEqual(summary['structural_validation']['status'],'unknown')
        self.assertEqual(summary['amount_audit']['status'],'unknown')
        for key in ('missing_ids','review_amount','review_amount_unknown_rows','review_rows'):
            self.assertIsNone(summary['punishment_matching'][key])
        journal['identity']['mode'] = 'export'
        journal['erp'] = {'skipped':'confirmed_zero','matching_status':'not_applicable'}
        summary = c.delivery_summary(journal,Path('source'),Path('must-not-appear.xlsx'))
        self.assertIsNone(summary['main_workbook'])
        self.assertEqual(summary['amount_audit']['status'],'not_applicable')
        self.assertIsNone(summary['amount_audit']['verified'])
        self.assertEqual(summary['punishment_matching']['skipped'],'confirmed_zero')

    def test_runtime_stable_and_outside_business(self):
        with tempfile.TemporaryDirectory() as td:
            folder=Path(td)/'business'
            self.assertEqual(tgc_runtime.runtime_for(folder), tgc_runtime.runtime_for(folder/'.'))
            self.assertNotIn(folder.resolve(), tgc_runtime.runtime_for(folder).parents)

if __name__ == '__main__':
    unittest.main()
