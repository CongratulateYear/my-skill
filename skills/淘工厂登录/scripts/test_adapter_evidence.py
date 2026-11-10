"""Offline synthetic cleanup/identity contracts; no browser or remote requests."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import playwright_session as p


class AdapterEvidenceTests(unittest.TestCase):
    def setUp(self):
        p._CLEANUP_EVIDENCE.clear()

    def test_cleanup_statuses_are_bounded_and_value_free(self):
        client = object.__new__(p.MCPClient)
        client._client_name = 'memory-session-SECRET'
        client.timeout = 120
        for answer, expected in [({'closed': 1}, 'closed'), ({'closed': 0}, 'not-owned'),
                                 ({'closed': 2, 'url': 'SECRET'}, 'failed')]:
            client.run_code = Mock(return_value=answer)
            client.close_connection_page()
            self.assertEqual(client.cleanup_status, expected)
            self.assertEqual(client.timeout, 120)
        for exc, status in [(p.MCPDisconnected('SECRET'), 'disconnected'),
                            (RuntimeError('SECRET'), 'failed')]:
            client.run_code = Mock(side_effect=exc)
            self.assertFalse(client.close_connection_page())
            self.assertEqual(client.cleanup_status, status)
        for _ in range(40):
            p._observe_cleanup('SECRET')
        events = p.cleanup_evidence()
        self.assertEqual(len(events), 32)
        self.assertNotIn('SECRET', json.dumps(events))
        events[0]['status'] = 'changed'
        self.assertEqual(p.cleanup_evidence()[0]['status'], 'failed')

    def test_cleanup_disconnect_does_not_replace_cookies_or_acquisition_error(self):
        payload = {'cookies': [], 'userAgent': 'Chrome/999'}
        for exc in (p.MCPDisconnected('SECRET'), RuntimeError('SECRET')):
            client = Mock()
            client.run_code.return_value = payload
            client.close_connection_page.side_effect = exc
            with patch.object(p.sys, 'platform', 'darwin'):
                self.assertIs(p._acquire_direct(p.TMALL_URLS, client=client), payload)
                client.run_code.return_value = {'bad': True}
                with self.assertRaisesRegex(p.MCPError, 'cookie result invalid'):
                    p._acquire_direct(p.TMALL_URLS, client=client)
        self.assertNotIn('SECRET', json.dumps(p.cleanup_evidence()))

    def test_real_cleanup_seam_disconnect_returns_validated_cookies(self):
        payload = {'cookies': [{'name': 'synthetic', 'value': 'offline',
                                'domain': '.tmall.com'}], 'userAgent': 'Chrome/999'}
        client = object.__new__(p.MCPClient)
        client._client_name = 'memory-session-offline'
        client.run_code = Mock(side_effect=[payload, p.MCPDisconnected('SECRET')])
        with patch.object(p.sys, 'platform', 'darwin'):
            self.assertEqual(p.acquire_cookies(p.TMALL_URLS, client=client), payload)
        self.assertEqual(p.cleanup_evidence()[-1]['status'], 'disconnected')

    def test_shim_identity_and_legacy_patch_contract_in_both_import_orders(self):
        login = Path(p.__file__).resolve().parent
        export = login.parents[1] / '淘工厂导出' / 'scripts'
        code = '''import importlib.util, sys
from pathlib import Path
from unittest.mock import patch
first, second = sys.argv[1:]
sys.path.insert(0, first)
import playwright_session as p
before = list(sys.path)
spec = importlib.util.spec_from_file_location('legacy_export', Path(second)/'playwright_session.py')
m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)
assert sys.path == before
assert m.MCPError is p.MCPError
assert m._PROCESS_CACHE is p._PROCESS_CACHE
assert m.SessionScope is p.SessionScope
with patch.object(m, '_acquire_direct', return_value={'cookies': [], 'userAgent':'Chrome/999'}) as mock:
    assert p._acquire_direct is mock
assert m._acquire_direct is p._acquire_direct
print('OK')
'''
        # For the reverse order load export again as the compatibility alias;
        # normal login imports reuse the canonical playwright_session entry.
        for first in (login, export):
            run = subprocess.run([sys.executable, '-B', '-c', code, str(first), str(export)],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), 'OK')


if __name__ == '__main__':
    unittest.main()
