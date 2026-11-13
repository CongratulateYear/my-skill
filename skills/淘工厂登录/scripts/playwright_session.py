"""Official Playwright MCP stdio client. All tool results remain in memory.

No browser launch, CDP, profile access, cookie DB, or page bridge. Each process
connects to the already running supported browser through Playwright Extension.
"""
from __future__ import annotations

import atexit
import http.cookiejar
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
import logging
from collections import deque
from urllib.parse import urlparse

# A single module identity across ordinary imports and the export compatibility
# shim: caches, exception classes and legacy patch targets must not fork.
_CANONICAL_MODULE = "_tgc_canonical_playwright_session"
if __name__ in sys.modules:
    sys.modules.setdefault(_CANONICAL_MODULE, sys.modules[__name__])

_CLEANUP_EVIDENCE = deque(maxlen=32)
_CLEANUP_LOCK = threading.RLock()
_LOG = logging.getLogger("tgc.playwright_session")


def cleanup_evidence():
    """Return at most 32 value-free cleanup observations (never auth data)."""
    with _CLEANUP_LOCK:
        return [dict(item) for item in _CLEANUP_EVIDENCE]


def _observe_cleanup(status):
    if status not in {"closed", "not-owned", "disconnected", "failed"}:
        status = "failed"
    with _CLEANUP_LOCK:
        _CLEANUP_EVIDENCE.append({"event": "connection_page_cleanup", "status": status})
    _LOG.info("connection_page_cleanup status=%s", status)

MCP_VERSION = "0.0.84"  # discovered in locally installed @playwright/mcp/package.json


class MCPError(RuntimeError):
    """Sanitized diagnostic: never contains tool output, URLs, or input code."""


class MCPDisconnected(MCPError):
    """Transport ended; no exception text is used as diagnostic evidence."""


def browser_channel(browser="auto"):
    supported = "chrome" if sys.platform == "darwin" else "edge" if sys.platform == "win32" else None
    if supported is None:
        raise MCPError("Only macOS Chrome and Windows Edge are supported.")
    if browser not in (None, "auto", supported, "msedge" if supported == "edge" else "chrome"):
        raise MCPError("Browser unsupported on this OS; use macOS Chrome or Windows Edge.")
    return "msedge" if supported == "edge" else "chrome"


def require_running_browser():
    """Guard official extension's connect-page opener against cold browser launch."""
    try:
        if sys.platform == "darwin":
            result = subprocess.run(["/usr/bin/pgrep", "-x", "Google Chrome"],
                                    capture_output=True, timeout=5)
            running = result.returncode == 0
        elif sys.platform == "win32":
            result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
                                    capture_output=True, text=True, timeout=5)
            running = result.returncode == 0 and '"msedge.exe"' in result.stdout.lower()
        else:
            running = False
    except Exception:
        running = False
    if not running:
        raise MCPError("Supported browser is not already running; open your existing Chrome/macOS or Edge/Windows manually, then authorize Playwright Extension. No browser was launched.")


class MCPClient:
    def __init__(self, browser="auto", timeout=120, *, command=None):
        channel = browser_channel(browser)
        self.timeout = timeout
        self._lock = threading.RLock()
        self._next = 0
        self._closed = False
        self._messages = queue.Queue()
        self._production = command is None
        self._client_name = 'memory-session-' + uuid.uuid4().hex
        # command is an injection seam for offline synthetic tests, not a CLI option.
        if command is None:
            node = shutil.which("node")
            npx = shutil.which("npx.cmd" if sys.platform == "win32" else "npx")
            if not npx or not node:
                raise MCPError("Node.js/npx missing; install Node and pinned @playwright/mcp@0.0.84 first.")
            from pathlib import Path
            if sys.platform == "win32":
                cli = Path(npx).parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
                if not cli.is_file():
                    raise MCPError("Cannot resolve npm npx-cli.js beside Node; repair Node/npm installation.")
                launcher = [node, str(cli)]
            else:
                launcher = [npx]
            require_running_browser()
            command = [*launcher, "--offline", "--yes", f"@playwright/mcp@{MCP_VERSION}",
                       "--extension", "--browser", channel, "--snapshot-mode", "none", "--codegen", "none", "--no-webmcp"]
        # Only the explicitly configured extension credential may cross this boundary.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PLAYWRIGHT_", "DEBUG", "PWDEBUG", "TGC_SESSION_BROKER_"))}
        if self._production:
            token = os.environ.get("PLAYWRIGHT_MCP_EXTENSION_TOKEN", "").strip()
            if not token:
                from pathlib import Path
                token_file = Path.home() / ".config" / "tgc-playwright" / "extension-token"
                if token_file.exists():
                    if token_file.is_symlink() or (os.name != "nt" and token_file.stat().st_mode & 0o077):
                        raise MCPError("Extension credential file must be private and not a symlink.")
                    token = token_file.read_text(encoding="utf-8").strip()
            if token:
                if any(c.isspace() for c in token):
                    raise MCPError("Extension credential format invalid.")
                env["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = token
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env)
        except Exception:
            raise MCPError("Could not start pinned official MCP server; check Node/npm installation.") from None
        threading.Thread(target=self._read, daemon=True).start()
        atexit.register(self.close)
        try:
            self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                       "clientInfo": {"name": self._client_name, "version": "1"}})
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.tools = {}
            cursor = None
            while True:
                result = self.request("tools/list", {"cursor": cursor} if cursor else {})
                self.tools.update({t["name"]: t for t in result.get("tools", [])})
                cursor = result.get("nextCursor")
                if not cursor:
                    break
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self._messages.put(json.loads(line))
                except ValueError:
                    self._messages.put(None)
        finally:
            self._messages.put(None)

    def _send(self, payload):
        try:
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except Exception:
            raise MCPDisconnected("MCP connection closed; reconnect and authorize Playwright Extension.") from None

    def request(self, method, params):
        import time
        with self._lock:
            self._next += 1
            ident = self._next
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    msg = self._messages.get(timeout=max(0.001, deadline - time.monotonic()))
                except queue.Empty:
                    raise MCPError("MCP timed out; authorize the extension in the already running browser and retry.") from None
                if msg is None:
                    raise MCPDisconnected("MCP stream ended or invalid response; verify pinned package and extension.")
                if "method" in msg:
                    if "id" in msg:
                        response = {"jsonrpc": "2.0", "id": msg["id"]}
                        if msg["method"] == "ping":
                            response["result"] = {}
                        else:
                            response["error"] = {"code": -32601, "message": "Unsupported method"}
                        self._send(response)
                    continue
                if msg.get("id") != ident:
                    continue
                if "error" in msg:
                    raise MCPError("MCP request failed; check extension authorization and installed version.")
                return msg.get("result", {})

    def call_tool(self, name, arguments):
        if self._production:
            require_running_browser()
        if name not in self.tools:
            raise MCPError("Required tool missing from tools/list; check official MCP version.")
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise MCPError("Playwright MCP tool failed; inspect browser/extension authorization without logging responses.")
        return result

    def run_code(self, code):
        name = next((n for n in ("browser_run_code_unsafe", "browser_run_code") if n in self.tools), None)
        if not name:
            raise MCPError("Official MCP exposes neither browser_run_code_unsafe nor browser_run_code.")
        result = self.call_tool(name, {"code": code})
        # Official tool wraps its return value under '### Result'. Ignore code,
        # snapshots and diagnostics, which may contain arbitrary page content.
        for part in result.get("content", []):
            text = part.get("text", "") if part.get("type") == "text" else ""
            candidate = text.split("### Result\n", 1)[-1].strip()
            try:
                value, _ = json.JSONDecoder().raw_decode(candidate)
                return value
            except ValueError:
                continue
        raise MCPError("MCP code result format unsupported; no response contents were logged.")

    def close_connection_page(self):
        """Close only this client's nonce-owned extension connect page, never a browser."""
        code = """async (page) => {
          const expected = CLIENT_NAME;
          let closed = 0;
          for (const candidate of [page]) {
            let u, owner, relay;
            try {
              u = new URL(candidate.url());
              if (u.protocol !== 'chrome-extension:' ||
                  u.hostname !== 'mmlmfjhmonkocbjadbfplnigmagldckm' ||
                  u.pathname !== '/connect.html' ||
                  u.searchParams.get('protocolVersion') !== '2') continue;
              owner = JSON.parse(u.searchParams.get('client') || 'null');
              if (!owner || owner.name !== expected) continue;
              relay = new URL(u.searchParams.get('mcpRelayUrl') || '');
              if (relay.protocol !== 'ws:' ||
                  !['[::1]', '127.0.0.1', 'localhost'].includes(relay.hostname) ||
                  !/^\\/extension\\/[a-zA-Z0-9-]+$/.test(relay.pathname)) continue;
            } catch { continue; }
            await candidate.close();
            closed++;
          }
          return {closed};
        }""".replace('CLIENT_NAME', json.dumps(self._client_name))
        # Cleanup is best-effort and bounded independently of acquisition.
        lock = getattr(self, '_lock', threading.RLock())
        with lock:
            previous = getattr(self, 'timeout', 120)
            self.timeout = min(previous, 5)
            try:
                result = self.run_code(code)
                count = result.get('closed') if isinstance(result, dict) else None
                status = ('closed' if type(count) is int and count == 1 else
                          'not-owned' if type(count) is int and count == 0 else 'failed')
            except (MCPDisconnected, ConnectionError, EOFError, BrokenPipeError):
                # Closing the owned tab can sever the transport before its ACK.
                # This is evidence of disconnect, NOT proof the tab was closed.
                status = 'disconnected'
            except Exception:
                status = 'failed'
            finally:
                self.timeout = previous
        self.cleanup_status = status
        _observe_cleanup(status)
        return status == 'closed'

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.process.stdin.close()
            self.process.wait(timeout=3)
        except Exception:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()
                self.process.wait()
        finally:
            self.process.stdout.close()
            atexit.unregister(self.close)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _acquire_direct(urls: list[str], browser="auto", *, client=None):
    browser_channel(browser)
    if not urls or any(urlparse(u).scheme != "https" or not urlparse(u).hostname for u in urls):
        raise MCPError("Cookie acquisition requires explicit HTTPS target URLs.")
    code = "async (page) => ({cookies: await page.context().cookies(" + json.dumps(urls) + "), userAgent: await page.evaluate(() => navigator.userAgent)})"
    if client is None:
        with MCPClient(browser) as own:
            return _acquire_direct(urls, browser, client=own)
    try:
        result = client.run_code(code)
        if not isinstance(result, dict) or not isinstance(result.get("cookies"), list) or not isinstance(result.get("userAgent"), str):
            raise MCPError("MCP cookie result invalid.")
        ua = result["userAgent"]
        if (sys.platform == "darwin" and ("Chrome/" not in ua or "Edg/" in ua)) or (sys.platform == "win32" and "Edg/" not in ua):
            raise MCPError("Connected browser does not match macOS Chrome / Windows Edge policy.")
        return result
    finally:
        cleanup = getattr(client, 'close_connection_page', None)
        if cleanup is not None:
            try:
                cleanup()
            except (MCPDisconnected, ConnectionError, EOFError, BrokenPipeError):
                _observe_cleanup('disconnected')
            except Exception:
                _observe_cleanup('failed')


def cookie_jar(cookies):
    policy = http.cookiejar.DefaultCookiePolicy(strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrictNonDomain)
    jar = http.cookiejar.CookieJar(policy)
    for c in cookies:
        domain = c.get("domain", c.get("host", ""))
        if not domain or not c.get("name") or "partitionKey" in c:
            # Partitioned cookies cannot be faithfully replayed by urllib.
            if "partitionKey" in c:
                raise MCPError("Partitioned cookies require browser-only requests; refusing lossy replay.")
            raise MCPError("Cookie record invalid.")
        expiry = c.get("expires", -1)
        rest = {"HttpOnly": None} if c.get("httpOnly") else {}
        jar.set_cookie(http.cookiejar.Cookie(0, c["name"], c["value"], None, False,
                       domain, domain.startswith("."), domain.startswith("."), c.get("path", "/"), True,
                       bool(c.get("secure")), int(expiry) if expiry is not None and expiry >= 0 else None,
                       expiry is None or expiry < 0, None, None, rest, False))
    return jar


# Execution-scoped secret transport: loopback HTTP only, authenticated with an
# unguessable capability inherited by child processes. Never persist this state.
import copy
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor, HTTPHandler, HTTPSHandler

TMALL_URLS = ["https://tgc.tmall.com/", "https://scm.tmall.com/"]
ERP_URLS = ["https://www.erp321.com/"]
_BROKER_URL = "TGC_SESSION_BROKER_URL"
_BROKER_TOKEN = "TGC_SESSION_BROKER_TOKEN"
_SCOPE_LOCK = threading.RLock()
_PROCESS_LOCK = threading.RLock()
_PROCESS_CACHE = {}
MAX_CACHE_SECONDS = 1800


def _service(urls):
    if not isinstance(urls, (list, tuple)) or not urls:
        raise MCPError("Explicit allowed service URLs required.")
    origins = set()
    for url in urls:
        u = urlparse(url)
        if u.scheme != "https" or u.username or u.password or u.port not in (None, 443) or u.path not in ("", "/") or u.query or u.fragment:
            raise MCPError("Cookie acquisition URL is outside fixed service scope.")
        origins.add("https://" + str(u.hostname) + "/")
    if origins <= set(TMALL_URLS):
        return "tmall", TMALL_URLS
    if origins <= set(ERP_URLS):
        return "erp", ERP_URLS
    raise MCPError("Cookie acquisition URL is outside fixed service scope.")


def _validate_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("cookies"), list) or not isinstance(payload.get("userAgent"), str):
        raise MCPError("Cookie payload invalid.")
    cookie_jar(payload["cookies"])
    if not payload["cookies"]:
        raise MCPError("No cookies available; inspect current browser login state.")
    return copy.deepcopy(payload)


def _cached(cache, lock, service, browser, source, provider):
    key = (service, browser, source)
    with lock:
        if key not in cache:
            try:
                value = _validate_payload(provider())
                expiries = [float(c.get("expires", -1)) for c in value["cookies"] if c.get("expires") is not None and float(c.get("expires", -1)) >= 0]
                deadline = min([time.time() + MAX_CACHE_SECONDS] + expiries)
                cache[key] = (value, deadline, None)
            except Exception:
                cache[key] = (None, 0, "Cookie acquisition failed; inspect browser/extension and start a new execution scope.")
        value, deadline, error = cache[key]
        if error:
            raise MCPError(error)
        if time.time() >= deadline:
            raise MCPError("Cached cookies expired; stop and start a new execution scope after login recovery.")
        return copy.deepcopy(value)


def _provider(service, browser, source):
    if source == "playwright":
        return _acquire_direct(TMALL_URLS if service == "tmall" else ERP_URLS, browser)
    raise MCPError("Unsupported cookie source.")


def _remote(payload):
    url, token = os.environ.get(_BROKER_URL), os.environ.get(_BROKER_TOKEN)
    if not url or not token:
        raise MCPError("Incomplete memory broker reference; refusing fallback.")
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path != "/session" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MCPError("Invalid memory broker reference; refusing fallback.")
    request = Request(url, data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
            result = json.loads(response.read(8 * 1024 * 1024))
    except Exception:
        raise MCPError("Execution memory broker unavailable; refusing new browser acquisition. Restart the execution scope.") from None
    if not result.get("ok"):
        raise MCPError("Execution memory session unavailable or expired; restart scope after recovery.")
    return _validate_payload(result["payload"])


def acquire_cookies(urls: list[str], browser="auto", *, client=None, source="playwright"):
    service, allowed = _service(urls)
    channel = browser_channel(browser)
    normalized = "edge" if channel == "msedge" else "chrome"
    if source != "playwright":
        raise MCPError("Unsupported cookie source.")
    if client is not None:
        return _validate_payload(_acquire_direct(allowed, normalized, client=client))
    if _BROKER_URL in os.environ or _BROKER_TOKEN in os.environ:
        return _remote({"service": service, "browser": normalized, "source": source})
    return _cached(_PROCESS_CACHE, _PROCESS_LOCK, service, normalized, source,
                   lambda: _provider(service, normalized, source))


class SessionScope:
    """One company/execution RAM scope; children inherit only URL + capability.

    A completed scope cannot be reused by a later standalone CLI. Nested scopes
    borrow the outer capability; switching companies requires a fresh scope.
    """
    def __init__(self, *, provider=None):
        self.provider = provider or _provider
        self.cache = {}
        self.lock = threading.RLock()
        self.server = None

    def __enter__(self):
        _SCOPE_LOCK.acquire()
        self.previous = {key: os.environ.get(key) for key in (_BROKER_URL, _BROKER_TOKEN)}
        if any(value is not None for value in self.previous.values()):
            self.borrowed = True
            return self
        self.borrowed = False
        token = secrets.token_urlsafe(48)
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                if self.path != "/session" or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 1024:
                        raise ValueError()
                    body = json.loads(self.rfile.read(size))
                    service, browser, source = body["service"], body["browser"], body["source"]
                    if service not in ("tmall", "erp") or browser not in ("chrome", "edge") or source != "playwright":
                        raise ValueError()
                    payload = _cached(owner.cache, owner.lock, service, browser, source,
                                      lambda: owner.provider(service, browser, source))
                    answer = {"ok": True, "payload": payload}
                except Exception:
                    answer = {"ok": False}
                raw = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self.server.daemon_threads = True
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            os.environ[_BROKER_URL] = "http://127.0.0.1:%d/session" % self.server.server_port
            os.environ[_BROKER_TOKEN] = token
            return self
        except BaseException:
            _SCOPE_LOCK.release()
            raise

    def __exit__(self, *args):
        try:
            if not self.borrowed:
                for key, value in self.previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(timeout=2)
                self.cache.clear()
        finally:
            _SCOPE_LOCK.release()


class _StripGlobalCookie(HTTPHandler):
    def http_request(self, request):
        request.remove_header("Cookie")
        return request
    https_request = http_request
    handler_order = 100


def scoped_opener(cookies):
    """Apply per-host/path/expiry jar after stripping compatibility Cookie headers."""
    return build_opener(_StripGlobalCookie(), HTTPCookieProcessor(cookie_jar(cookies)))
