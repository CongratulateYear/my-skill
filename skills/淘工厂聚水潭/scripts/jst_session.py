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
    pass

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
        req = Request(url, data=payload, headers=headers or {}, method=method)
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
        raise SessionError('登录过期或无订单导出权限，请在浏览器登录后重试')
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
        raise SessionError('无法从服务端页面确认目标公司，停止：请核对账号；不要仅凭cookie公司名放行')
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
            except SessionError: continue
    raise SessionError('没有可验证的聚水潭cookie会话；请登录或启用浏览器桥')


def default_headers():
    return {'accept':'*/*', 'content-type':'application/x-www-form-urlencoded; charset=UTF-8', 'x-requested-with':'XMLHttpRequest', 'referer':ORDER_URL, 'origin':ORIGIN}


def build_session(transport='auto', browser='auto', bridge_url=None, expect_company=None):
    if transport not in ('auto', 'cookie', 'bridge'): raise ValueError('transport无效')
    headers = default_headers()
    if transport != 'bridge':
        try:
            session = _cookie_session(browser, expect_company)
        except SessionError:
            if transport == 'cookie': raise
        else:
            info = validate_session(session, headers, expect_company)
            return {'session':session, 'headers':headers, 'transport':'cookie', 'identity':info}
    import browser_bridge
    try:
        client = browser_bridge.BridgeClient(bridge_url, ensure=False)
        client.wait_ready(0)
        page = client.session_info().get('page', '')
        if urlsplit(page).hostname != 'www.erp321.com': raise SessionError('浏览器桥未连接聚水潭页面')
        session = Session(client, 'bridge')
        info = validate_session(session, headers, expect_company)
        return {'session':session, 'headers':headers, 'transport':'bridge', 'identity':info}
    except SessionError: raise
    except Exception:
        raise SessionError('聚水潭浏览器桥不可用；后台启动 browser_bridge.py --port 8901，在已登录ERP的新Magpie标签页注入bridge_agent.js') from None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--transport', choices=['auto','cookie','bridge'], default='auto')
    p.add_argument('--browser', default='auto', choices=['auto',*cookies.BROWSERS])
    p.add_argument('--bridge-url')
    p.add_argument('--expect-company')
    p.add_argument('--wait', type=float, default=0)
    a = p.parse_args()
    deadline = time.monotonic() + max(0, a.wait)
    while True:
        try:
            built = build_session(a.transport, a.browser, a.bridge_url, a.expect_company)
            print(json.dumps({'ok':True, **built['identity'], 'transport':built['transport']},ensure_ascii=False));return 0
        except SessionError as exc:
            if time.monotonic() < deadline: time.sleep(1);continue
            print(json.dumps({'ok':False,'loggedIn':False,'reason':'login-required','action':str(exc),'loginUrl':LOGIN_URL},ensure_ascii=False));return 3
if __name__ == '__main__':
    raise SystemExit(main())
