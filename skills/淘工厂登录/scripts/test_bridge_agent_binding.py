"""Offline bridge protocol tests; loopback ephemeral ports, synthetic sessions only.
Run: python -B test_bridge_agent_binding.py
No live bridge, browser, credential store, or external host is accessed.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = [ROOT / name / "scripts" for name in ("淘工厂登录", "淘工厂导出")]


class BindingTests:
    scripts: Path

    def setUp(self):
        spec = importlib.util.spec_from_file_location("isolated_bridge", self.scripts / "browser_bridge.py")
        self.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bridge)
        self.server = self.bridge._Server(("127.0.0.1", 0), self.bridge._Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.pool = ThreadPoolExecutor(max_workers=4)

    def tearDown(self):
        self.pool.shutdown(wait=True)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(self.base + path, data=body, headers={"content-type": "application/json"})
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            return response.code, json.loads(raw) if raw else None

    def attach(self, agent="A"):
        return self.request("/session", {"agentId": agent, "cookie": "synthetic=offline", "href": "https://tgc.tmall.com/offline"})

    def call(self, agent="A", pending_count=1):
        future = self.pool.submit(self.request, "/call", {"agentId": agent, "url": "https://tgc.tmall.com/offline", "timeout": 3})
        with self.bridge.BRIDGE.changed:
            self.assertTrue(self.bridge.BRIDGE.changed.wait_for(lambda: len(self.bridge.BRIDGE.pending) >= pending_count, 2))
        return future

    def expire(self):
        with self.bridge.BRIDGE.lock:
            self.bridge.BRIDGE.last_seen = time.monotonic() - 100

    def result(self, task, agent="A"):
        return self.request("/result", {"taskId": task["taskId"], "agentId": agent, "ok": True, "result": {"status": 200, "text": "offline"}})

    def test_single_page_and_tgc_behavior(self):
        self.assertFalse(self.request("/ping")[1]["agentAttached"])
        self.assertEqual(self.attach()[0], 200)
        client = self.bridge.BridgeClient(self.base, ensure=False)
        self.assertEqual(client.wait_ready(0)["agentId"], "A")
        self.assertEqual(client.session_info()["cookie"], "synthetic=offline")
        self.assertTrue(self.bridge.bridge_host("https://sub.tmall.com/path"))
        self.assertTrue(self.bridge.bridge_host("https://a.taobao.com/path"))
        self.assertFalse(self.bridge.bridge_host("https://tmall.com.invalid/path"))
        future = self.pool.submit(client._call, {"url": "https://tgc.tmall.com/offline", "timeout": 3})
        code, task = self.request("/next?agentId=A&wait=1")
        self.assertEqual(code, 200)
        self.assertEqual(task["agentId"], "A")
        self.assertEqual(self.result(task)[0], 200)
        self.assertEqual(future.result(4)["text"], "offline")
        self.assertEqual(self.request("/next?agentId=A&wait=1")[0], 204)

    def test_two_agents_cannot_steal_or_overwrite_session(self):
        self.attach()
        self.assertEqual(self.attach("B")[0], 409)
        self.assertEqual(self.request("/session")[1]["agentId"], "A")
        future = self.call()
        self.assertEqual(self.request("/next?agentId=B&wait=1")[0], 409)
        self.assertEqual(self.request("/log", {"agentId": "B", "message": "offline"})[0], 409)
        code, task = self.request("/next?agentId=A&wait=1")
        self.assertEqual(code, 200)
        self.result(task)
        self.assertEqual(future.result(4)[0], 200)

    def test_wrong_agent_and_duplicate_result_are_rejected(self):
        self.attach()
        future = self.call()
        task = self.request("/next?agentId=A&wait=1")[1]
        self.assertEqual(self.result(task, "B")[0], 409)
        self.assertFalse(future.done())
        self.assertEqual(self.result(task)[0], 200)
        self.assertEqual(future.result(4)[0], 200)
        self.assertEqual(self.result(task)[0], 409)
        self.assertEqual(self.request("/ping")[1]["done"], 1)

    def test_expired_takeover_invalidates_queued_and_inflight_pending(self):
        self.attach()
        inflight = self.call()
        task = self.request("/next?agentId=A&wait=1")[1]
        queued = self.call(pending_count=2)
        self.expire()
        self.assertEqual(self.attach("B")[0], 200)
        self.assertEqual(inflight.result(4)[0], 502)
        self.assertEqual(queued.result(4)[0], 502)
        self.assertFalse(self.bridge.BRIDGE.pending)
        self.assertTrue(self.bridge.BRIDGE.tasks.empty())
        self.assertEqual(self.result(task)[0], 409)
        self.assertEqual(self.result(task, "B")[0], 409)
        self.assertEqual(self.request("/next?agentId=A&wait=1")[0], 409)
        future = self.call("B")
        new_task = self.request("/next?agentId=B&wait=1")[1]
        self.assertNotEqual(new_task["taskId"], task["taskId"])
        self.result(new_task, "B")
        self.assertEqual(future.result(4)[0], 200)

    def test_old_long_poll_cannot_consume_new_agent_task(self):
        self.attach()
        waiting = threading.Event()
        condition = self.bridge.BRIDGE.changed
        original_wait = condition.wait

        def marked_wait(timeout=None):
            waiting.set()
            return original_wait(timeout)

        condition.wait = marked_wait
        old_poll = self.pool.submit(self.request, "/next?agentId=A&wait=3")
        self.assertTrue(waiting.wait(2))
        self.expire()
        self.assertEqual(self.attach("B")[0], 200)
        future = self.call("B")
        self.assertEqual(old_poll.result(3)[0], 409)
        code, task = self.request("/next?agentId=B&wait=1")
        self.assertEqual(code, 200)
        self.result(task, "B")
        self.assertEqual(future.result(4)[0], 200)

    def test_old_js_and_pinned_python_client_fail_closed(self):
        self.assertEqual(self.request("/session", {"cookie": "synthetic=offline"})[0], 409)
        self.attach()
        self.assertEqual(self.request("/next?wait=1")[0], 409)
        self.assertEqual(self.request("/call", {"url": "https://tgc.tmall.com/offline"})[0], 409)
        client = self.bridge.BridgeClient(self.base, ensure=False)
        client.wait_ready(0)
        self.expire()
        self.attach("B")
        with self.assertRaises(self.bridge.BridgeError):
            client.wait_ready(0)
        with self.assertRaises(self.bridge.BridgeError):
            client.session_info()
        with self.assertRaises(self.bridge.BridgeError):
            client._call({"url": "https://tgc.tmall.com/offline", "timeout": 1})


for index, scripts in enumerate(SCRIPTS):
    globals()[f"BridgeCopy{index}Tests"] = type(f"BridgeCopy{index}Tests", (BindingTests, unittest.TestCase), {"scripts": scripts})


class JavascriptTests(unittest.TestCase):
    def test_agent_protocol_with_mocked_fetch(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable for offline JS execution")
        # A VM page and synthetic fetches only; no browser or real cookie access.
        program = r'''
const vm = require("node:vm");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const sources = JSON.parse(fs.readFileSync(0, "utf8"));
async function run(source, conflictAt) {
  const calls = [];
  let business = 0;
  const context = {window: {}, location: {origin: "https://tgc.tmall.com", href: "https://tgc.tmall.com/offline"},
    document: {cookie: "XSRF-TOKEN=synthetic"}, crypto: {randomUUID: () => "offline-agent"},
    URL, AbortController, TextDecoder, Uint8Array, setTimeout, clearTimeout,
    fetch: async (url, options = {}) => {
      if (!url.startsWith("http://127.0.0.1:")) {
        business++;
        assert.equal(options.credentials, "include");
        assert.equal(options.redirect, "follow");
        assert.equal(options.headers["x-xsrf-token"], "synthetic");
        return {status: 200, url, headers: {get: () => "application/json"}, arrayBuffer: async () => new TextEncoder().encode("{}").buffer};
      }
      calls.push({url, options});
      if (options.body) assert.equal(JSON.parse(options.body).agentId, "offline-agent");
      if (url.includes(conflictAt) && (conflictAt !== "/session" || calls.length > 1)) return {status: 409, ok: false};
      if (url.includes("/next")) {
        assert.ok(url.includes("agentId=offline-agent"));
        return {status: 200, ok: true, json: async () => ({taskId: "task", agentId: "offline-agent", url: "https://tgc.tmall.com/offline"})};
      }
      return {status: 200, ok: true};
    }};
  vm.createContext(context);
  assert.equal(vm.runInContext(source, context), "bridge agent started");
  for (let i = 0; i < 20 && context.window.__dshBridgeRunning; i++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(context.window.__dshBridgeRunning, false);
  assert.equal(business, conflictAt === "/next" ? 0 : 1);
  const count = calls.length;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, count, "409 must stop polling");
}
(async () => { for (const source of sources) {await run(source, "/next"); await run(source, "/result"); await run(source, "/session");} })().catch(error => {console.error(error); process.exitCode = 1;});
'''
        result = subprocess.run([node, "-e", program], input=json.dumps([(path / "bridge_agent.js").read_text(encoding="utf-8") for path in SCRIPTS]), text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
