"""Offline only: synthetic cookies, no browser or business endpoints."""
import concurrent.futures
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from urllib.request import Request
import playwright_session as p


def payload(domain='.tmall.com', expires=-1):
    return {'cookies': [{'name': 'synthetic', 'value': 'offline-only', 'domain': domain,
                         'path': '/private', 'secure': True, 'expires': expires}],
            'userAgent': 'Chrome/999'}


class MemoryScopeTests(unittest.TestCase):
    def setUp(self):
        p._PROCESS_CACHE.clear()
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.old = {k: os.environ.pop(k, None) for k in (p._BROKER_URL, p._BROKER_TOKEN)}

    def tearDown(self):
        for key, value in self.old.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value
        self.env.stop()

    def test_scope_acquires_once_concurrent_and_services_separate(self):
        calls = []
        def provider(service, browser, source):
            calls.append((service, browser, source))
            return payload('.tmall.com' if service == 'tmall' else 'www.erp321.com')
        with p.SessionScope(provider=provider):
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                list(pool.map(lambda _: p.acquire_cookies(p.TMALL_URLS), range(12)))
            p.acquire_cookies(p.ERP_URLS)
        self.assertEqual(len(calls), 2)
        self.assertNotIn(p._BROKER_TOKEN, os.environ)

    def test_child_inherits_reference_not_cookie_and_reuses(self):
        calls = []
        def provider(*args):
            calls.append(args)
            return payload()
        with p.SessionScope(provider=provider):
            p.acquire_cookies(p.TMALL_URLS)
            code = 'import playwright_session as p; p.acquire_cookies(p.TMALL_URLS); print("OK")'
            result = subprocess.run([sys.executable, '-c', code], cwd=os.path.dirname(p.__file__),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'OK')
            self.assertNotIn('offline-only', json.dumps(dict(os.environ)))
        self.assertEqual(len(calls), 1)

    def test_dead_broker_never_falls_back(self):
        with p.SessionScope(provider=lambda *args: payload()):
            refs = {key: os.environ[key] for key in (p._BROKER_URL, p._BROKER_TOKEN)}
        with patch.dict(os.environ, refs), patch.object(p, '_acquire_direct') as direct:
            with self.assertRaises(p.MCPError):
                p.acquire_cookies(p.TMALL_URLS)
            direct.assert_not_called()

    def test_failure_cached_no_retry_storm(self):
        count = []
        def fail(*args):
            count.append(1)
            raise RuntimeError('sensitive diagnostic')
        with p.SessionScope(provider=fail):
            for _ in range(3):
                with self.assertRaises(p.MCPError) as error:
                    p.acquire_cookies(p.TMALL_URLS)
                self.assertNotIn('sensitive', str(error.exception))
        self.assertEqual(len(count), 1)

    def test_expiry_rejects_without_refresh(self):
        count = []
        def provider(*args):
            count.append(1)
            return payload(expires=time.time() - 2)
        with p.SessionScope(provider=provider):
            for _ in range(2):
                with self.assertRaises(p.MCPError):
                    p.acquire_cookies(p.TMALL_URLS)
        self.assertEqual(len(count), 1)

    def test_arbitrary_urls_rejected(self):
        for urls in [['https://evil.invalid/'], ['http://tgc.tmall.com/'],
                     ['https://tgc.tmall.com/private'], ['https://tgc.tmall.com:444/'],
                     p.TMALL_URLS + p.ERP_URLS]:
            with self.assertRaises(p.MCPError):
                p.acquire_cookies(urls)

    def test_cookie_scope_and_compatibility_header_stripping(self):
        opener = p.scoped_opener(payload()['cookies'])
        strip = next(h for h in opener.handlers if isinstance(h, p._StripGlobalCookie))
        processor = next(h for h in opener.handlers if isinstance(h, p.HTTPCookieProcessor))
        for url, expected in [('https://tgc.tmall.com/private/a', True),
                              ('https://tgc.tmall.com/public', False),
                              ('https://evil.invalid/private', False),
                              ('http://tgc.tmall.com/private', False)]:
            request = Request(url, headers={'Cookie': 'global=unsafe'})
            strip.http_request(request)
            processor.http_request(request)
            self.assertNotIn('global', request.get_header('Cookie', ''))
            self.assertEqual('synthetic' in request.get_header('Cookie', ''), expected)

    def test_broker_authentication_and_no_secret_error_output(self):
        with p.SessionScope(provider=lambda *args: payload()):
            with patch.dict(os.environ, {p._BROKER_TOKEN: 'wrong-token'}):
                with self.assertRaises(p.MCPError) as error:
                    p.acquire_cookies(p.TMALL_URLS)
            self.assertNotIn('offline-only', str(error.exception))

    def test_default_transport_and_identity_unchanged(self):
        import tmall_session as t
        with patch.object(t, 'build_playwright_session', return_value=({}, object())) as extension, \
             patch.object(t, 'build_headers') as native:
            self.assertEqual(t.build_session()['transport'], 'playwright')
            extension.assert_called_once()
            native.assert_not_called()
        self.assertTrue(t.company_matches('目标', '目标有限公司'))
        self.assertEqual(t.account_verdict('', '', ''), 'ok')

    def test_process_cache_and_failure_no_new_cli_promise(self):
        with patch.object(p, '_acquire_direct', return_value=payload()) as direct:
            p.acquire_cookies(p.TMALL_URLS)
            p.acquire_cookies(p.TMALL_URLS)
            self.assertEqual(direct.call_count, 1)

    def test_nested_scope_borrows_and_restores(self):
        calls = []
        with p.SessionScope(provider=lambda *args: calls.append(args) or payload()):
            token = os.environ[p._BROKER_TOKEN]
            with p.SessionScope():
                p.acquire_cookies(p.TMALL_URLS)
            self.assertEqual(token, os.environ[p._BROKER_TOKEN])
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
