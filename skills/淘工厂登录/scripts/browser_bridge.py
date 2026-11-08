#!/usr/bin/env python3
"""浏览器内 fetch 桥：让 Python 借「已登录的浏览器标签」请求天猫接口。

为什么需要它：Windows 上 Chrome/Edge 用 v20 App-Bound Encryption 保存 cookie，
解密密钥由浏览器进程 + 系统服务共同保护，普通进程读不出来；Edge 运行时还会独占
Cookies 文件（WinError 32 / JET_errFileAccessDenied）。而页面里的
fetch(url, {credentials:"include"}) 由浏览器自己附加 HttpOnly cookie，天然可用。

三步用法：
  1. 起桥（后台常驻）：python scripts/browser_bridge.py --port 8899
  2. 新建自己的 Magpie 标签页（不复用用户/其他 agent 页），已登录直接桥接，未登录才填表；
     read 本 skill 的 scripts/bridge_agent.js 完整源码后 evaluate 直接执行。
  3. 其它脚本加 --transport bridge（默认 auto：先试 cookie 库，失败自动回退到桥）；
     agent started 不是登录成功，以 ensure_login.py 的服务端登录/目标公司验证通过为准。

安全：桥只监听 127.0.0.1；任务与回包只在内存里流转，不落盘；cookie 值不打印。
"""
from __future__ import annotations

import argparse
import base64
import json
import queue
import threading
import time
import uuid
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, build_opener

DEFAULT_PORT = 8899
DEFAULT_HOST = "127.0.0.1"
AGENT_FILE = Path(__file__).resolve().with_name("bridge_agent.js")
# 长轮询：页面没有任务时挂住这么久，避免空转刷日志。
POLL_WAIT_SECONDS = 25
# 单次调用的默认上限；调用方传 timeout 时按 timeout+15 放大。
CALL_TIMEOUT = 300.0
AGENT_READY_SECONDS = 20.0
# 只有这些域走浏览器；签名下载地址（OSS/CDN）直接用 urllib，避免跨域与体积问题。
BRIDGE_HOSTS = ("tmall.com", "taobao.com", "alicdn.com", "alibaba.com")


class BridgeError(RuntimeError):
    """桥不可用或页面代理没有就绪。"""


class BridgeAgentMissing(BridgeError):
    """桥在跑，但没有任何标签注入 bridge_agent.js。"""


AGENT_HINT = (
    "页面代理未就绪，不等于未登录。固定恢复路线：用 magpie-browser 在当前已打开的浏览器中 "
    "tab_new 新建自己的 Magpie 标签页，不复用用户、其他 agent 或旧标签，不新开浏览器或 profile。"
    "打开 https://tgc.tmall.com/ds/page/supplier/home，已登录直接桥接，确认未登录才填表；"
    "同一次恢复继续操作自己新建的同一标签页，不必每步再开。"
    "read 本 skill 的 scripts/bridge_agent.js 完整源码，在自己 tgc.tmall.com 页面用 evaluate "
    "直接执行，不插入 script src；导航或换号后重新注入。"
    "agent started 仅表示注入，不代表成功；重跑 ensure_login.py，保留 --expect-company，"
    "以服务端公司验证通过且退出码 0 为成功标准。凭据不落盘、不回显，验证码人工完成。"
)


def bridge_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == item or host.endswith("." + item) for item in BRIDGE_HOSTS)


class _Task:
    __slots__ = ("spec", "done", "result", "error")

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error = ""


class _Bridge:
    """进程内的任务队列与代理状态（页面 GET /next 取任务，POST /result 回填）。"""

    def __init__(self) -> None:
        self.tasks: queue.Queue[_Task | None] = queue.Queue(maxsize=64)
        self.pending: dict[str, _Task] = {}
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.agent_id = ""
        self.last_seen = 0.0
        self.document_cookie = ""
        self.page_url = ""
        self.done_count = 0
        self.errors: list[str] = []

    def note_agent(self, agent_id: str, session: dict[str, Any]) -> bool:
        if not agent_id or len(agent_id) > 100:
            return False
        with self.changed:
            if self.agent_id and self.agent_id != agent_id and self.agent_attached:
                return False
            if self.agent_id != agent_id:
                # Requests for the old page must never migrate to a new page.
                for task in self.pending.values():
                    task.error = "页面代理已更换；请重新验证账号"
                    task.done.set()
                self.pending.clear()
                while True:
                    try:
                        self.tasks.get_nowait()
                    except queue.Empty:
                        break
                self.agent_id = agent_id
            self.document_cookie = str(session.get("cookie") or "")
            self.page_url = str(session.get("href") or "")
            self.last_seen = time.monotonic()
            self.changed.notify_all()
        return True

    @property
    def agent_attached(self) -> bool:
        return bool(self.agent_id) and (time.monotonic() - self.last_seen) < POLL_WAIT_SECONDS + 15


BRIDGE = _Bridge()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DSHBridge/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 保持 stdout 干净
        return

    # ------------------------------------------------------------ 输出工具
    def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        if body:
            self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        origin = self.headers.get("Origin", "")
        if origin in {"https://tgc.tmall.com", "https://scm.tmall.com"}:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _allowed(self) -> bool:
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "").split(":", 1)[0]
        return host == "127.0.0.1" and (not origin or origin in {
            "https://tgc.tmall.com", "https://scm.tmall.com"})

    def do_OPTIONS(self) -> None:
        self._send(204 if self._allowed() else 403, b"")

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._allowed():
            self._send(403, b"")
            return
        path = urlsplit(self.path).path
        if path == "/ping":
            with BRIDGE.lock:
                self._json(200, {
                    "ok": True,
                    "agentAttached": BRIDGE.agent_attached,
                    "done": BRIDGE.done_count,
                    "page": BRIDGE.page_url, "agentId": BRIDGE.agent_id,
                })
        elif path == "/session":
            with BRIDGE.lock:
                self._json(200, {"cookie": BRIDGE.document_cookie, "page": BRIDGE.page_url,
                                 "agentId": BRIDGE.agent_id})
        elif path == "/agent.js":
            try:
                text = AGENT_FILE.read_text(encoding="utf-8")
            except OSError as exc:
                self._json(500, {"ok": False, "error": str(exc)})
                return
            self._send(200, text.encode("utf-8"), "application/javascript; charset=utf-8")
        elif path == "/next":
            query = parse_qs(urlsplit(self.path).query)
            agent_id = query.get("agentId", [""])[0]
            try:
                wait = min(max(float(query.get("wait", [POLL_WAIT_SECONDS])[0]), 1.0), 60.0)
            except (ValueError, TypeError):
                wait = POLL_WAIT_SECONDS
            deadline = time.monotonic() + wait
            with BRIDGE.changed:
                if not agent_id or agent_id != BRIDGE.agent_id or not BRIDGE.agent_attached:
                    self._json(409, {"ok": False, "error": "代理不匹配"})
                    return
                BRIDGE.last_seen = time.monotonic()
                while True:
                    # Check ownership and dequeue atomically, even after a long poll wakes.
                    if BRIDGE.agent_id != agent_id:
                        self._json(409, {"ok": False, "error": "代理已更换"})
                        return
                    try:
                        task = BRIDGE.tasks.get_nowait()
                    except queue.Empty:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._send(204, b"")
                            return
                        BRIDGE.changed.wait(remaining)
                        continue
                    if task is None:
                        self._send(204, b"")
                        return
                    if (BRIDGE.pending.get(task.spec["taskId"]) is task
                            and task.spec.get("agentId") == agent_id):
                        self._json(200, task.spec)
                        return
        else:
            self._json(404, {"ok": False, "error": "unknown path"})

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._allowed():
            self._send(403, b"")
            return
        path = urlsplit(self.path).path
        payload = self._body()
        if path == "/next":
            self._json(404, {"ok": False})
        elif path == "/result":
            agent_id = str(payload.get("agentId") or "")
            with BRIDGE.lock:
                task_id = str(payload.get("taskId") or "")
                task = BRIDGE.pending.get(task_id)
                if (not agent_id or agent_id != BRIDGE.agent_id or not BRIDGE.agent_attached
                        or task is None or task.spec.get("agentId") != agent_id):
                    self._json(409, {"ok": False, "error": "任务代理不匹配或任务已失效"})
                    return
                BRIDGE.pending.pop(task_id)
                if payload.get("ok"):
                    task.result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
                else:
                    task.error = str(payload.get("error") or "页面执行失败")
                task.done.set()
                BRIDGE.done_count += 1
            self._json(200, {"ok": True})
        elif path == "/session":
            if not BRIDGE.note_agent(str(payload.get("agentId") or ""), payload):
                self._json(409, {"ok": False, "error": "已有另一个页面代理；停止旧代理后重试"})
                return
            self._json(200, {"ok": True})
        elif path == "/log":
            with BRIDGE.lock:
                if (not payload.get("agentId") or payload["agentId"] != BRIDGE.agent_id
                        or not BRIDGE.agent_attached):
                    self._json(409, {"ok": False, "error": "代理不匹配"})
                    return
                message = str(payload.get("message") or "")[:400]
                BRIDGE.errors = (BRIDGE.errors + [message])[-20:]
                BRIDGE.last_seen = time.monotonic()
            self._json(200, {"ok": True})
        elif path == "/call":
            with BRIDGE.changed:
                if not BRIDGE.agent_attached:
                    self._json(503, {"ok": False, "error": AGENT_HINT})
                    return
                if not payload.get("agentId") or payload["agentId"] != BRIDGE.agent_id:
                    self._json(409, {"ok": False, "error": "代理已改变；请重新验证账号"})
                    return
                task = _Task({**payload, "taskId": uuid.uuid4().hex})
                task_id = task.spec["taskId"]
                try:
                    BRIDGE.tasks.put_nowait(task)
                except queue.Full:
                    self._json(503, {"ok": False, "error": "任务队列已满，页面代理可能卡住"})
                    return
                BRIDGE.pending[task_id] = task
                BRIDGE.changed.notify_all()
            timeout = float(payload.get("timeout") or CALL_TIMEOUT)
            finished = task.done.wait(timeout=max(min(timeout, 900.0), 1.0))
            with BRIDGE.lock:
                BRIDGE.pending.pop(task_id, None)
            if not finished:
                self._json(504, {"ok": False, "error": f"页面 {timeout:.0f}s 内没有回包"})
            elif task.error:
                self._json(502, {"ok": False, "error": task.error})
            else:
                self._json(200, {"ok": True, "result": task.result or {}})
        else:
            self._json(404, {"ok": False})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


_SERVERS: dict[int, _Server] = {}
_SERVER_LOCK = threading.Lock()


def ensure_running(port: int = DEFAULT_PORT, quiet: bool = True) -> int:
    """确保本进程内有一个监听端口的桥；返回实际端口。"""
    with _SERVER_LOCK:
        if port in _SERVERS:
            return port
        try:
            server = _Server((DEFAULT_HOST, port), _Handler)
        except OSError as exc:
            raise BridgeError(
                f"无法监听 {DEFAULT_HOST}:{port}（{exc}）。"
                "换一个端口：--bridge-url http://127.0.0.1:8900，或先关掉占用该端口的进程。"
            ) from exc
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _SERVERS[port] = server
        if not quiet:
            print(json.dumps({"ok": True, "port": port, "pid": None}, ensure_ascii=False))
        return port


def ping(port: int = DEFAULT_PORT, timeout: float = 3.0) -> dict[str, Any]:
    """探测桥是否在跑（不依赖本进程内的服务器）。"""
    try:
        with build_opener().open(f"http://{DEFAULT_HOST}:{port}/ping", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (HTTPError, URLError, OSError, ValueError):
        return {}


class BridgeClient:
    """urllib 兼容的 session：open(request) 把请求交给浏览器标签执行。

    非天猫域（签名下载地址等）自动回退到 urllib 原生请求，避免跨域与二进制体积问题。
    """

    mode = "bridge"

    def __init__(self, base: str | None = None, ensure: bool = True) -> None:
        port = DEFAULT_PORT
        if base:
            parsed = urlsplit(base)
            if parsed.port:
                port = parsed.port
        self.port = port
        self.base = f"http://{DEFAULT_HOST}:{port}"
        self._inner = build_opener()
        self.agent_id: str | None = None
        if ensure and not ping(port).get("ok"):
            ensure_running(port)

    # -------------------------------------------------------------- 状态
    def wait_ready(self, seconds: float = AGENT_READY_SECONDS) -> dict[str, Any]:
        deadline = time.monotonic() + max(seconds, 0.0)
        state: dict[str, Any] = {}
        while True:
            state = ping(self.port)
            if not state.get("ok"):
                raise BridgeError(
                    f"桥没有在 {self.base} 应答。先起桥："
                    f"python scripts/browser_bridge.py --port {self.port}"
                )
            if state.get("agentAttached"):
                agent_id = state.get("agentId")
                if not agent_id or (self.agent_id and self.agent_id != agent_id):
                    raise BridgeError("代理已改变；请重新验证账号（协议升级须重启桥并重新注入代理）")
                self.agent_id = agent_id
                return state
            if time.monotonic() >= deadline:
                raise BridgeAgentMissing(AGENT_HINT)
            time.sleep(1.0)

    def session_info(self) -> dict[str, Any]:
        if not self.agent_id:
            self.wait_ready(0)
        with self._inner.open(self.base + "/session", timeout=5) as response:
            info = json.loads(response.read().decode("utf-8"))
            if info.get("agentId") != self.agent_id:
                raise BridgeError("代理已改变；请重新验证账号")
            return info

    def account_hint(self) -> str:
        return account_hint_from_cookie(self.session_info().get("cookie", ""))

    def xsrf_token(self) -> str:
        from urllib.parse import unquote
        for part in self.session_info().get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name in ("XSRF-TOKEN", "X-XSRF-TOKEN") and value:
                return unquote(value)
        return ""

    def headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://tgc.tmall.com",
            "referer": "https://tgc.tmall.com/ds/page/supplier/home",
            "x-requested-with": "XMLHttpRequest",
        }
        token = self.xsrf_token()
        if token:
            headers["x-xsrf-token"] = token
        return headers

    # -------------------------------------------------------------- 请求
    def _call(self, spec: dict[str, Any]) -> dict[str, Any]:
        if not self.agent_id:
            self.wait_ready(0)
        spec = {**spec, "agentId": self.agent_id}
        body = json.dumps(spec, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base}/call",
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        wait = float(spec.get("timeout") or CALL_TIMEOUT) + 30
        try:
            with self._inner.open(request, timeout=wait) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            try:
                parsed = json.loads(detail)
                message = str(parsed.get("error") or detail)
            except ValueError:
                message = detail
            if exc.code == 504:
                raise TimeoutError(f"页面执行超时：{message}") from exc
            raise BridgeError(f"页面执行失败（{exc.code}）：{message}") from exc
        except (URLError, OSError) as exc:
            raise BridgeError(f"桥连接中断：{exc}") from exc
        if not payload.get("ok"):
            raise BridgeError(str(payload.get("error") or "页面执行失败"))
        result = payload.get("result") or {}
        return result if isinstance(result, dict) else {}

    def open(self, request: Any, timeout: float | None = None, data: Any = None) -> Any:  # noqa: A003
        if isinstance(request, str):
            request = Request(request, data=data)
        url = request.full_url
        if not bridge_host(url):
            return self._inner.open(request, timeout=timeout)
        headers = {key.lower(): value for key, value in request.header_items()}
        # 浏览器禁止脚本设置这些头；由浏览器自己补（含 HttpOnly cookie）。
        for key in ("cookie", "host", "origin", "referer", "user-agent",
                    "content-length", "connection", "accept-encoding", "priority"):
            headers.pop(key, None)
        spec: dict[str, Any] = {
            "url": url,
            "method": request.get_method(),
            "headers": headers,
        }
        if request.data is not None:
            spec["body"] = (
                request.data.decode("utf-8", "replace")
                if isinstance(request.data, bytes) else str(request.data)
            )
        spec["timeout"] = float(timeout) if timeout else CALL_TIMEOUT
        result = self._call(spec)
        status = int(result.get("status") or 0)
        raw = (base64.b64decode(result["base64"], validate=True)
               if result.get("base64") else str(result.get("text") or "").encode("utf-8"))
        message = Message()
        if result.get("contentType"):
            message["content-type"] = str(result["contentType"])
        if result.get("disposition"):
            message["content-disposition"] = str(result["disposition"])
        final_url = str(result.get("url") or url)
        if status >= 400:
            raise HTTPError(final_url, status, f"HTTP {status}", message, BytesIO(raw))
        return BridgeResponse(BytesIO(raw), message, final_url, status)


class BridgeResponse:
    """够用的 http.client 响应替身：read/geturl/headers/上下文管理。"""

    def __init__(self, stream: BytesIO, headers: Message, url: str, status: int) -> None:
        self._stream = stream
        self.headers = headers
        self.url = url
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self.url

    def getheader(self, name: str, default: str | None = None) -> Any:
        value = self.headers.get(name)
        return default if value is None else value

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> "BridgeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def account_hint_from_cookie(cookie: str) -> str:
    """从页面 document.cookie 里取非敏感账号提示（tracknick/lgc/unb…）。"""
    values: dict[str, str] = {}
    for part in cookie.split("; "):
        name, _, value = part.partition("=")
        if name and value:
            values[name] = value
    for name in ("tracknick", "lgc", "cookie17", "unb", "_nk_"):
        if values.get(name):
            return values[name]
    return "未知"


def make_session(transport: str = "auto", bridge_url: str | None = None) -> Any:
    """transport=native 用 urllib；bridge 用浏览器；auto 由调用方先试 cookie 库再回退。"""
    if transport == "native":
        return build_opener()
    return BridgeClient(bridge_url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="浏览器内 fetch 桥（只监听 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--print-agent", action="store_true",
                        help="只打印 bridge_agent.js 的内容（用于 magpie-browser evaluate）")
    parser.add_argument("--exit-if-running", action="store_true",
                        help="端口上已有桥时直接返回 0（幂等启动）")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="起桥后最多等这么多秒让代理接入，超时退出码 4；0=立即返回")
    args = parser.parse_args(argv)

    if args.print_agent:
        print(AGENT_FILE.read_text(encoding="utf-8"))
        return 0

    existing = ping(args.port)
    if existing.get("ok"):
        if args.exit_if_running or True:
            print(json.dumps({"ok": True, "port": args.port, "reused": True,
                              "agentAttached": existing.get("agentAttached")},
                             ensure_ascii=False))
            return 0
    try:
        ensure_running(args.port, quiet=True)
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    deadline = time.monotonic() + max(args.seconds, 0.0)
    state: dict[str, Any] = {}
    while True:
        state = ping(args.port)
        if state.get("agentAttached") or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    attached = bool(state.get("agentAttached"))
    print(json.dumps({
        "ok": True,
        "port": args.port,
        "agentAttached": attached,
        "action": "" if attached else AGENT_HINT,
        "agentJs": str(AGENT_FILE),
    }, ensure_ascii=False))
    if not attached and args.seconds > 0:
        return 4
    # 常驻：让调用方用后台任务跑着；Ctrl-C / job_kill 结束。
    if args.seconds <= 0:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
