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
    def test_auto_bridge_fallback(self):
        with patch.object(j,'_cookie_session',side_effect=j.SessionError('no-cookie')),patch.object(b,'BridgeClient',side_effect=RuntimeError('secret')):
            with self.assertRaises(j.SessionError) as ctx:j.build_session()
            self.assertNotIn('secret',str(ctx.exception))
    def test_windows_root(self):
        with patch.object(c,'IS_WINDOWS',True),patch.dict(c.os.environ,{'LOCALAPPDATA':'C:/Users/Test/AppData/Local'}):
            self.assertIn('Microsoft',str(c._user_data_root('edge')))
    def test_darwin_root(self):
        with patch.object(c,'IS_WINDOWS',False):self.assertIn('Library/Application Support',str(c._user_data_root('chrome')))

if __name__=='__main__':unittest.main()
