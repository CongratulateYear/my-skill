"""ERP session adapter. Passwords are entered only via the existing browser UI."""
from __future__ import annotations
import argparse
import http.cookiejar
import json
import time
from urllib.parse import urlencode, urlsplit, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPCookieProcessor
from urllib.error import HTTPError
import chromium_cookies as cookies

ORIGIN = 'https://www.erp321.com'
LOGIN_URL = ORIGIN + '/login.aspx'
WORKBENCH_URL = ORIGIN + '/epaas'
ORDER_URL = ORIGIN + '/app/order/export/exportorder.aspx?_c=jst-epaas&epaas=true'

class SessionError(RuntimeError):
    def __init__(self, message, code='session_unverified'):
        super().__init__(message)
        self.code = code

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class Response:
    def __init__(self, raw):
        self.status_code = raw.getcode()
        self.url = raw.geturl()
        self.headers = raw.headers
        self.content = raw.read(64 * 1024 * 1024 + 1)
        if len(self.content) > 64 * 1024 * 1024:
            raise SessionError('响应过大，已停止')
        self.text = self.content.decode('utf-8-sig', 'replace')
    def json(self):
        return json.loads(self.text)

class Session:
    def __init__(self, opener, mode='cookie'):
        self.opener = opener
        self.mode = mode
    def request(self, method, url, headers=None, data=None, timeout=60, params=None, **kwargs):
        if params:
            url += ('&' if '?' in url else '?') + urlencode(params)
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'www.erp321.com' or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise SessionError('认证会话仅允许 www.erp321.com HTTPS；下载外域需独立无凭据连接')
        payload = urlencode(data).encode() if isinstance(data, dict) else data
        if isinstance(payload, str): payload = payload.encode()
        # CookieJar owns domain/path/expiry; a compatibility Cookie header
        # must never override those restrictions.
        safe_headers = {k: v for k, v in (headers or {}).items() if k.lower() != 'cookie'}
        req = Request(url, data=payload, headers=safe_headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as raw:
                response = Response(raw)
        except HTTPError as exc:
            raise SessionError(f'ERP HTTP {exc.code}，请检查登录或权限') from None
        except Exception as exc:
            if isinstance(exc, SessionError): raise
            raise SessionError('ERP连接失败，未显示请求凭据') from None
        if urlsplit(response.url).hostname != 'www.erp321.com':
            raise SessionError('ERP意外跨域跳转')
        return response
    def get(self, url, **kwargs): return self.request('GET', url, **kwargs)
    def post(self, url, **kwargs): return self.request('POST', url, **kwargs)


def company_matches(expected, actual):
    expected = ''.join(str(expected or '').split())
    actual = ''.join(str(actual or '').split())
    return bool(expected and actual and expected == actual)


def validate_session(session, headers, expect_company=None):
    response = session.get(ORDER_URL, headers=headers)
    text = response.text
    # A real ERP order page must contain the expected callback table and state;
    # isLogin cookies alone never count as server validation.
    if 'JTable1' not in text or '__VIEWSTATE' not in text or urlsplit(response.url).path.lower().endswith('login.aspx'):
        raise SessionError('登录过期或无订单导出权限，请在浏览器登录后重试', 'login_or_permission_required')
    # Validate a read-only business callback, not merely static HTML markers.
    from jst_export import get_hidden, _callback, ExportError
    try:
        hidden = get_hidden(session, headers)
        check = _callback(session, headers, ORDER_URL, hidden, 'LoadDataToJSON',
                          ['1', '[{"k":"so_id","v":"__DSH_SESSION_CHECK_NO_ORDER__","c":"@="}]', '{}'])
        rows = check.get('datas')
        if isinstance(rows, str): rows = json.loads(rows)
        if not isinstance(rows, list): raise SessionError('订单查询未返回有效datas，不能确认登录')
    except SessionError: raise
    except Exception:
        raise SessionError('服务端订单查询验证未通过，登录/权限或响应结构需确认') from None
    # Company is server-rendered page evidence, NOT a self-asserted cookie.
    import re, html
    names = []
    for pattern in (r'"(?:co_name|companyName)"\s*:\s*"([^"<>]+)"', r'data-company-name=[\"\x27]([^\"\x27<>]+)'):
        names.extend(html.unescape(x) for x in re.findall(pattern, text))
    company = next((x for x in names if company_matches(expect_company, x)), '') if expect_company else (names[0] if names else '')
    if expect_company and not company:
        raise SessionError('无法从服务端页面确认目标公司，停止：请核对账号；不要仅凭cookie公司名放行', 'company_unverified')
    return {'loggedIn': True, 'company': company, 'companyCheck': 'matched' if expect_company else 'not-requested'}


def _cookie_session(browser='auto', expect_company=None):
    candidates = [browser] if browser != 'auto' else list(cookies.BROWSERS)
    now = int((time.time() + 11644473600) * 1e6)
    for name in candidates:
        files = cookies.cookie_files(name)
        if not files: continue
        try: key = cookies._browser_key(name)
        except (cookies.SessionError, OSError, ValueError): continue
        for path in files:
            try: rows = cookies.load_cookies(path, key)
            except (cookies.SessionError, OSError, ValueError): continue
            rows = [c for c in rows if not c['expiresUtc'] or c['expiresUtc'] > now]
            if not any(c['name'] in ('u_id', 'u_sso_token') and c['value'] for c in rows): continue
            jar = http.cookiejar.CookieJar()
            for c in rows:
                jar.set_cookie(http.cookiejar.Cookie(0, c['name'], c['value'], None, False, c['host'], c['host'].startswith('.'), c['host'].startswith('.'), c['path'], True, c['secure'], None, True, None, None, {}, False))
            session = Session(build_opener(NoRedirect(), HTTPCookieProcessor(jar)))
            try:
                validate_session(session, default_headers(), expect_company)
                return session
            except SessionError as exc:
                if exc.code == 'company_unverified': raise
                continue
    raise SessionError('没有可验证的聚水潭cookie会话；请使用官方Playwright扩展获取Cookie')


def default_headers():
    return {'accept':'*/*', 'content-type':'application/x-www-form-urlencoded; charset=UTF-8', 'x-requested-with':'XMLHttpRequest', 'referer':ORDER_URL, 'origin':ORIGIN}


def _playwright_adapter():
    """Load only the co-installed login skill; never import arbitrary cwd modules."""
    import importlib.util
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for name in ('淘工厂登录', 'taogongchang-login'):
        source = root / name / 'scripts' / 'playwright_session.py'
        if source.is_file():
            key = '_tgc_shared_playwright_session'
            existing = sys.modules.get(key)
            if existing is not None and Path(existing.__file__).resolve() == source.resolve():
                return existing
            spec = importlib.util.spec_from_file_location(key, source)
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(key, None)
                raise SessionError('官方Playwright会话适配器不可用', 'playwright_unavailable') from None
            return module
    raise SessionError('缺少同级淘工厂登录skill的官方Playwright适配器', 'playwright_unavailable')


def _playwright_session(browser='auto', expect_company=None):
    adapter = _playwright_adapter()
    try:
        # The shared execution broker keeps ERP separate from Tmall and reuses
        # this in-memory snapshot across child commands. Never persist cookies.
        snapshot = adapter.acquire_cookies([ORIGIN + '/'], browser=browser)
        jar = adapter.cookie_jar(snapshot['cookies'])
        session = Session(build_opener(NoRedirect(), HTTPCookieProcessor(jar)), 'playwright')
        headers = default_headers()
        if snapshot.get('userAgent'):
            headers['user-agent'] = snapshot['userAgent']
    except SessionError:
        raise
    except Exception as exc:
        safe_codes = {'playwright_unavailable', 'extension_not_connected', 'browser_not_running',
                      'unsupported_browser', 'session_expired', 'session_scope_expired',
                      'broker_unavailable', 'cookie_scope_invalid'}
        code = getattr(exc, 'code', 'playwright_unavailable')
        if code not in safe_codes: code = 'playwright_unavailable'
        raise SessionError('官方Playwright Cookie获取失败；检查现有浏览器及扩展，不输出凭据', code) from None
    info = validate_session(session, headers, expect_company)
    return {'session': session, 'headers': headers, 'transport': 'playwright', 'identity': info}


def build_session(transport='auto', browser='auto', bridge_url=None, expect_company=None):
    if transport not in ('auto', 'cookie', 'playwright', 'bridge'): raise ValueError('transport无效')
    if bridge_url:
        raise SessionError('旧bridge URL已停用；请使用官方Playwright扩展', 'legacy_bridge_disabled')
    # Legacy spelling is an alias only; no bridge process, injection or request.
    if transport == 'bridge': transport = 'playwright'
    headers = default_headers()
    if transport == 'cookie':
        try:
            session = _cookie_session(browser, expect_company)
        except SessionError as exc:
            if transport == 'cookie' or exc.code == 'company_unverified': raise
        else:
            info = validate_session(session, headers, expect_company)
            return {'session':session, 'headers':headers, 'transport':'cookie', 'identity':info}
    return _playwright_session(browser, expect_company)


def recovery_action(reason='session_unverified', expect_company=None, bridge_url=None):
    """Agent-facing plan only: Python cannot inspect or drive Magpie form UI."""
    return {
        'reason': reason,
        'accountSwitchPolicy': 'reuse-logged-in-erp-never-auto-switch',
        'supportedBrowsers': ['windows-edge', 'macos-chrome'],
        'expectCompany': expect_company,
        'resumePolicy': {'sameInputs': True, 'sameOutputDirectory': True,
                         'preserveTaskState': True, 'addResumeAfterExportAttempt': True,
                         'neverRecreateUncertainTask': True},
        'type': 'inspect-own-magpie-tab',
        'executor': 'agent-with-magpie-login-and-playwright-cookies',
        'browser': 'current-existing-browser',
        'tab': 'new-own-magpie-tab',
        'workbenchUrl': WORKBENCH_URL,
        'loginUrl': LOGIN_URL,
        'loginState': 'login-or-permission-required' if reason == 'login_or_permission_required' else 'unverified',
        'steps': [
            'Use the current Windows Edge or macOS Chrome with the official Playwright extension; never start a browser or profile.',
            'Open workbench in a new own Magpie tab; do not touch user/other-agent tabs.',
            'If already signed in to ERP, reuse the session; do not compare accounts, log out or log in again.',
            'If login is needed, inspect the real form refs and only credential presence, never password values.',
            'Preserve prefilled credentials; if both fields are populated, click the real login submit without asking for credentials.',
            'Only fill missing fields using ERP credentials explicitly supplied for this attempt; never use Taogongchang credentials.',
            'Ask once for missing ERP credentials only after inspecting the form; do not extract or persist autofill/passwords.',
            'Stop for human handling of SMS, captcha, slider or risk checks; do not bypass.',
            'After Magpie login, start a fresh execution scope to acquire ERP cookies once with official Playwright, then rerun server validation with the original company constraint.',
            'ERP checks login state only; never infer its account from Taogongchang or automatically switch accounts. An explicit company validation failure must stop without logging out.'
        ],
        'prefilledCredentials': 'preserve-and-submit',
        'credentialPrompt': 'only-after-form-inspection-if-missing',
        'humanRequired': ['sms', 'captcha', 'slider', 'risk-check'],
        'successCriterion': 'server-order-query-and-requested-company-validated',
        'pythonControlsBrowser': False,
    }


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--transport', choices=['auto','cookie','playwright','bridge'], default='auto')
    p.add_argument('--browser', default='auto', choices=['auto',*cookies.BROWSERS])
    p.add_argument('--bridge-url')
    p.add_argument('--expect-company')
    p.add_argument('--wait', type=float, default=0)
    a = p.parse_args(argv)
    deadline = time.monotonic() + max(0, a.wait)
    while True:
        try:
            built = build_session(a.transport, a.browser, a.bridge_url, a.expect_company)
            print(json.dumps({'ok':True, **built['identity'], 'transport':built['transport']},ensure_ascii=False));return 0
        except SessionError as exc:
            if time.monotonic() < deadline: time.sleep(1);continue
            # Preserve legacy fields; failure is unverified, not proof of logout.
            print(json.dumps({'ok':False,'loggedIn':False,'reason':'login-required',
                              'action': '先检查自己的新Magpie标签页；已有预填聚水潭凭据则保留并提交，不立即要求人工。',
                              'diagnostic':str(exc),'loginUrl':LOGIN_URL,
                              'nextAction':recovery_action(exc.code, a.expect_company, a.bridge_url),
                               'recovery_action':recovery_action(exc.code, a.expect_company, a.bridge_url),
                               'session_status':exc.code,
                              'expectCompany':a.expect_company},ensure_ascii=False));return 3
if __name__ == '__main__':
    raise SystemExit(main())
