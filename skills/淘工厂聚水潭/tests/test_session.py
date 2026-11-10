import sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import jst_session as j
import chromium_cookies as c
import browser_bridge as b

class FakeResponse:
    url=j.ORDER_URL
    def __init__(self,text):self.text=text
class Fake:
    def __init__(self,text):self.text=text
    def get(self,*a,**k):return FakeResponse(self.text)

class SessionTests(unittest.TestCase):
    def test_login_url(self):self.assertEqual(j.LOGIN_URL,'https://www.erp321.com/login.aspx')
    def test_html_login_rejected(self):
        with self.assertRaises(j.SessionError):j.validate_session(Fake('<input type=password>'),{})
    def test_cookie_not_identity(self):
        with self.assertRaises(j.SessionError):j.validate_session(Fake('JTable1 __VIEWSTATE'),{},'目标公司')
    def test_server_company(self):
        with patch('jst_export.get_hidden',return_value={}), patch('jst_export._callback',return_value={'datas':[]}):
            self.assertEqual(j.validate_session(Fake('JTable1 __VIEWSTATE "co_name":"测试目标公司"'),{},'测试目标公司')['companyCheck'],'matched')
    def test_domain_filter(self):
        self.assertTrue(c._domain_matches('.erp321.com','www.erp321.com'))
        self.assertFalse(c._domain_matches('.erp321.com.evil.test','www.erp321.com'))
        self.assertFalse(c._domain_matches('erp321.com','www.erp321.com'))
    def test_bridge_domain(self):
        self.assertTrue(b.bridge_host(j.ORDER_URL));self.assertFalse(b.bridge_host('https://e.evil'))
    def test_session_origin_guard(self):
        session=j.Session(None)
        for url in ['http://www.erp321.com/x','https://evil.test/x','https://www.erp321.com@evil.test/','https://www.erp321.com:8443/']:
            with self.assertRaises(j.SessionError):session.get(url)
    def test_auto_uses_playwright_not_native(self):
        with patch.object(j,'_cookie_session',side_effect=AssertionError('no native')), \
             patch.object(j,'_playwright_session',return_value={'transport':'playwright'}) as acquire:
            self.assertEqual(j.build_session()['transport'],'playwright')
            acquire.assert_called_once_with('auto',None)
    def test_bridge_alias_never_calls_bridge(self):
        with patch.object(b,'BridgeClient',side_effect=AssertionError('no bridge')), \
             patch.object(j,'_playwright_session',return_value={'transport':'playwright'}):
            self.assertEqual(j.build_session('bridge')['transport'],'playwright')
        with self.assertRaises(j.SessionError) as cm:
            j.build_session(bridge_url='http://127.0.0.1:8901')
        self.assertEqual(cm.exception.code,'legacy_bridge_disabled')
    def test_playwright_cookie_jar_and_user_agent(self):
        from types import SimpleNamespace
        import http.cookiejar
        fake=SimpleNamespace(acquire_cookies=lambda urls,browser: {'cookies':[], 'userAgent':'test-agent'},
                             cookie_jar=lambda records:http.cookiejar.CookieJar())
        with patch.object(j,'_playwright_adapter',return_value=fake), \
             patch.object(j,'validate_session',return_value={'loggedIn':True}) as check:
            built=j.build_session('playwright',expect_company='目标')
            self.assertEqual(built['transport'],'playwright')
            self.assertEqual(built['headers']['user-agent'],'test-agent')
            self.assertEqual(check.call_args.args[2],'目标')
    def test_adapter_error_sanitized(self):
        from types import SimpleNamespace
        def fail(*a,**k):raise RuntimeError('secret-cookie-value')
        with patch.object(j,'_playwright_adapter',return_value=SimpleNamespace(acquire_cookies=fail)):
            with self.assertRaises(j.SessionError) as cm:j.build_session()
        self.assertNotIn('secret',str(cm.exception))
    def test_company_constraint_stops_without_reacquisition(self):
        with patch.object(j,'_cookie_session',side_effect=j.SessionError('company','company_unverified')), \
             patch.object(j,'_playwright_session',side_effect=AssertionError('no switching')):
            with self.assertRaises(j.SessionError):j.build_session('cookie',expect_company='目标')
    def test_recovery_never_switches_erp_accounts(self):
        recovery=j.recovery_action('company_unverified','目标')
        self.assertEqual(recovery['accountSwitchPolicy'],'reuse-logged-in-erp-never-auto-switch')
        self.assertNotIn('bridgeStartArgv',recovery)
        self.assertNotIn('bridgeAgentSource',recovery)
        self.assertNotIn('expectAccount',recovery)
        self.assertIn('do not compare accounts', ' '.join(recovery['steps']))
    def test_windows_root(self):
        with patch.object(c,'IS_WINDOWS',True),patch.dict(c.os.environ,{'LOCALAPPDATA':'C:/Users/Test/AppData/Local'}):
            self.assertIn('Microsoft',str(c._user_data_root('edge')))
    def test_darwin_root(self):
        with patch.object(c,'IS_WINDOWS',False):self.assertIn('Library/Application Support',c._user_data_root('chrome').as_posix())

if __name__=='__main__':unittest.main()
