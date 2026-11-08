"""Offline pending-download, explicit scope approval and adversarial regression."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import jst_export as e
from test_live_export import IDS, LiveSession, Response, envelope, workbook

PARTNER = '323456789012345678'


class MergeSession(LiveSession):
    def __init__(self, **kw):
        super().__init__(content=workbook(IDS + [PARTNER]), **kw)
        self.downloads = 0
        self.polls = 0

    def get(self, url, **kw):
        if url.endswith('/synthetic.xlsx'):
            self.downloads += 1
        return super().get(url, **kw)

    def assert_contract(self, kw):
        assert kw['data']['__CALLBACKID'] == 'JTable1'
        payload = json.loads(kw['data']['__CALLBACKPARAM'])
        assert payload['Args'] == ['11']
        assert payload['CallControl'] == '{page}'
        assert '__VIEWSTATE_DEL' not in kw['data']

    def post(self, url, **kw):
        payload = json.loads(kw['data']['__CALLBACKPARAM'])
        if payload['Method'] == 'CheckAndGetExportData':
            self.polls += 1
            self.assert_contract(kw)
            return envelope('https://www.erp321.com/synthetic.xlsx')
        if url == e.EXPORT_URL and payload['Method'] == 'LoadDataToJSON':
            return envelope(dict(datas=[dict(so_id=','.join(IDS + ['500:' + PARTNER]),
                                                  o_id='100', is_merge=True, order_from='MERGE')],
                                 dp=dict(DataCount=1, PageIndex=1, IsLast=True)))
        return super().post(url, **kw)


class PendingScopeTests(unittest.TestCase):
    def run_it(self, session, root, **kw):
        return e.run_export(session, {}, IDS, root / 'orders', max_polls=1,
                            poll_interval=0, sleep=lambda _: None, **kw)

    def pending(self, root, session=None):
        session = session or MergeSession()
        with self.assertRaises(e.ExportError) as cm:
            self.run_it(session, root)
        self.assertEqual(cm.exception.code, 'scope_approval_required')
        self.assertFalse((root / 'orders.xlsx').exists())
        approval = cm.exception.safe_diagnostic()['details']['approval_required']
        approval['approved'] = True
        return session, approval

    def test_one_download_explicit_authorization_local_resume(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session, approval = self.pending(root)
            self.assertEqual((session.downloads, session.polls), (1, 1))
            self.assertTrue(e.can_resume_offline(root / 'orders.state.json', IDS, root / 'orders'))
            for _ in range(2):
                with self.assertRaises(e.ExportError):
                    self.run_it(None, root, resume=True)
            report = self.run_it(None, root, resume=True, scope_approval=approval)
            self.assertEqual(report['scope_exception'], approval)
            self.assertTrue((root / 'orders.xlsx').exists())
            self.assertEqual((session.downloads, session.polls), (1, 1))
            self.assertEqual(self.run_it(None, root, resume=True)['scope_exception'], approval)

    def test_tamper_every_approval_binding_and_extra_field(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _, approval = self.pending(root)
            for key, value in [('approved', False), ('approved', 1), ('task_id', '12'),
                               ('sha256', '0' * 64), ('output', str(root / 'other')),
                               ('requested_ids', IDS[::-1]), ('extra_ids', ['999']), ('unknown', True)]:
                changed = dict(approval, **{key: value})
                with self.subTest(key=key), self.assertRaises(e.ExportError):
                    self.run_it(None, root, resume=True, scope_approval=changed)
                self.assertFalse((root / 'orders.xlsx').exists())

    def test_corrupted_pending_never_redownloads(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session, approval = self.pending(root)
            path = root / 'orders.state.json.pending'
            path.write_bytes(b'corrupt')
            with self.assertRaises(e.ExportError) as cm:
                self.run_it(None, root, resume=True, scope_approval=approval)
            self.assertEqual(cm.exception.code, 'pending_binding_mismatch')
            self.assertEqual(session.downloads, 1)
            self.assertEqual(path.read_bytes(), b'corrupt')

    def test_unbound_commit_window_blocks_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.pending(root)
            state_path = root / 'orders.state.json'
            state = json.loads(state_path.read_text())
            del state['pending']
            state_path.write_text(json.dumps(state))
            raw = (root / 'orders.state.json.pending').read_bytes()
            with self.assertRaises(e.ExportError) as cm:
                self.run_it(None, root, resume=True)
            self.assertEqual(cm.exception.code, 'pending_unbound')
            self.assertEqual((root / 'orders.state.json.pending').read_bytes(), raw)

    def test_unsafe_download_not_persisted_or_repeated(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session = MergeSession()
            session.content = b'not an archive'
            with self.assertRaises(e.ExportError):
                self.run_it(session, root)
            with self.assertRaises(e.ExportError) as cm:
                self.run_it(None, root, resume=True)
            self.assertEqual(cm.exception.code, 'download_outcome_unknown')
            self.assertEqual(session.downloads, 1)
            self.assertFalse((root / 'orders.state.json.pending').exists())

    def test_foreign_partner_not_proven_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session = LiveSession(content=workbook(IDS + [PARTNER]))
            with self.assertRaises(e.ExportError) as cm:
                self.run_it(session, root)
            self.assertEqual(cm.exception.code, 'scope_not_eligible')
            self.assertNotIn('approval_required', cm.exception.details)
            self.assertFalse((root / 'orders.xlsx').exists())

    def test_legacy_state_has_no_scope_exception_authority(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _, approval = self.pending(root)
            path = root / 'orders.state.json'
            state = json.loads(path.read_text())
            state['version'] = 1
            state.pop('merge_evidence')
            state.pop('internal_order_ids')
            path.write_text(json.dumps(state))
            with self.assertRaises(e.ExportError) as cm:
                self.run_it(None, root, resume=True, scope_approval=approval)
            self.assertEqual(cm.exception.code, 'scope_not_eligible')

    def test_strict_merge_components_and_flags(self):
        for row in [dict(so_id=IDS[0] + ',' + PARTNER, is_merge=True),
                    dict(so_id=IDS[0] + ',' + PARTNER, order_from='MERGE'),
                    dict(so_id=IDS[0] + ',bad:' + PARTNER, is_merge=True, order_from='MERGE'),
                    dict(so_id=IDS[0] + ',1:2:3', is_merge=True, order_from='MERGE'),
                    dict(so_id=IDS[0] + ',notdecimal', is_merge=True, order_from='MERGE')]:
            class S:
                def post(self, *a, **kw):
                    return envelope(dict(datas=[dict(row, o_id='100')], total=1))
            with self.subTest(row=row), self.assertRaises(e.ExportError):
                e.query_orders(S(), {}, IDS, {})

    def test_explicit_unrelated_approval_rejected_on_exact_scope(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session = MergeSession()
            session.content = workbook(IDS)
            # Fail publication after pending is bound, leaving exact-scope local bytes.
            original = e._atomic_create
            def fail_output(path, content):
                if Path(path) == root / 'orders.xlsx':
                    raise e.ExportError('synthetic publication interruption')
                return original(path, content)
            with patch.object(e, '_atomic_create', side_effect=fail_output), self.assertRaises(e.ExportError):
                self.run_it(session, root)
            state = json.loads((root / 'orders.state.json').read_text())
            approval = dict(e._approval_payload(state, state['pending']['sha256'], [PARTNER]), approved=True)
            with self.assertRaises(e.ExportError) as cm:
                self.run_it(None, root, resume=True, scope_approval=approval)
            self.assertEqual(cm.exception.code, 'scope_approval_invalid')
            self.assertFalse((root / 'orders.xlsx').exists())

    def test_poll_interruption_does_not_poll_twice(self):
        class BrokenPoll(MergeSession):
            def post(self, url, **kw):
                payload = json.loads(kw['data']['__CALLBACKPARAM'])
                if payload['Method'] == 'CheckAndGetExportData':
                    self.polls += 1
                    raise TimeoutError('https://sensitive.invalid/')
                return super().post(url, **kw)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session = BrokenPoll()
            with self.assertRaises(e.ExportError):
                self.run_it(session, root)
            with self.assertRaises(e.ExportError) as cm:
                self.run_it(None, root, resume=True)
            self.assertEqual(cm.exception.code, 'download_outcome_unknown')
            self.assertEqual((session.polls, session.downloads), (1, 0))

    def test_orphan_without_state_blocks_before_any_network(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pending = root / 'orders.state.json.pending'
            pending.write_bytes(workbook(IDS))
            with self.assertRaises(e.ExportError) as cm:
                self.run_it(None, root)
            self.assertEqual(cm.exception.code, 'pending_unbound')
            self.assertFalse((root / 'orders.state.json').exists())

    def test_validator_stays_exact_structured(self):
        with self.assertRaises(e.ExportError) as cm:
            e.validate_order_coverage(workbook([IDS[0], PARTNER]), IDS)
        self.assertEqual(cm.exception.code, 'order_coverage_mismatch')
        self.assertEqual(cm.exception.details['actual-extra'], [PARTNER])
        self.assertEqual(cm.exception.details['missing'], IDS[1:])

    def test_safe_diagnostic_drops_sensitive_arbitrary_fields(self):
        error = e.ExportError('https://secret.invalid/?token=private', details={
            'response': 'secret', 'output': 'https://secret.invalid/', 'task_id': 'https://secret.invalid/',
            'requested_ids': ['private-cookie'], 'extra_ids': ['123']})
        diagnostic = error.safe_diagnostic()
        self.assertNotIn('secret', json.dumps(diagnostic))
        self.assertEqual(diagnostic['details'], {'extra_ids': ['123']})


if __name__ == '__main__':
    unittest.main()
