"""Offline CLI authorization contracts; no sessions, tasks or business edits."""
import io,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import jushuitan as cli

class ScopeCliTests(unittest.TestCase):
    def approval(self,root):
        return dict(approved=True,task_id='123',sha256='a'*64,output=str(root/'orders'),requested_ids=['1'],extra_ids=['2'])

    def test_valid_explicit_authorization_forwarded_only_on_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/'approval.json';value=self.approval(root);p.write_text(json.dumps(value),encoding='utf-8')
            with patch.object(cli,'can_resume_offline',return_value=False),patch.object(cli,'build_session',return_value={'session':None,'headers':{}}),patch.object(cli,'run_export',return_value={}) as run,redirect_stdout(io.StringIO()):
                code=cli.main(['--shop-name','测试','--order-ids','1','--output-dir',str(root/'business'),'--resume','--scope-approval-file',str(p)])
            self.assertEqual(code,0);self.assertEqual(run.call_args.kwargs['scope_approval'],value)

    def test_offline_resume_bypasses_session_and_preserves_safe_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            error=cli.ExportError('PRIVATE signed URL must not appear',code='scope_approval_required',details={'actual_extra_ids':['2'],'scope_approval_eligible':True})
            with patch.object(cli,'can_resume_offline',return_value=True),patch.object(cli,'build_session',side_effect=AssertionError('offline only')),patch.object(cli,'run_export',side_effect=error) as run,redirect_stdout(io.StringIO()) as output:
                code=cli.main(['--shop-name','测试','--order-ids','1','--output-dir',str(root/'business'),'--resume'])
            report=json.loads(output.getvalue())
            self.assertEqual(code,2)
            self.assertIsNone(run.call_args.args[0])
            self.assertEqual(report['error_code'],'scope_approval_required')
            self.assertEqual(report['error_details']['actual_extra_ids'],['2'])
            self.assertNotIn('PRIVATE',output.getvalue())
            self.assertEqual(report['export_recovery']['action'],'explicit-scope-approval')

    def test_authorization_never_creates_task_without_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/'approval.json';p.write_text(json.dumps(self.approval(root)),encoding='utf-8')
            with patch.object(cli,'build_session',side_effect=AssertionError('offline')),patch.object(cli,'run_export',side_effect=AssertionError('no task')),redirect_stdout(io.StringIO()) as output:
                code=cli.main(['--shop-name','测试','--order-ids','1','--scope-approval-file',str(p)])
            self.assertEqual(code,2);self.assertEqual(json.loads(output.getvalue())['error_code'],'scope_approval_invalid')

    def test_unknown_duplicate_unapproved_and_non_decimal_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);p=root/'approval.json';valid=self.approval(root)
            cases=[dict(valid,approved=False),dict(valid,cookie='SECRET'),dict(valid,extra_ids=['bad']),dict(valid,requested_ids=[{}]),dict(valid,extra_ids=['2','2'])]
            for value in cases:
                p.write_text(json.dumps(value),encoding='utf-8')
                with self.assertRaises(cli.ExportError):cli.load_scope_approval(p)
            p.write_text(json.dumps(valid)[:-1]+',"approved":true}',encoding='utf-8')
            with self.assertRaises(cli.ExportError):cli.load_scope_approval(p)

if __name__=='__main__':unittest.main()
