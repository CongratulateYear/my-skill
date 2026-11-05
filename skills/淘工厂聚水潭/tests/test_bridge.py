"""Synthetic loopback protocol tests: never touch browser cookies or live ERP."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('jst_test_bridge', SCRIPTS / 'browser_bridge.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        b.BRIDGE = b._Bridge()
        self.server = b._Server(('127.0.0.1', 0), b._Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, obj):
        request = Request(self.base + path, data=json.dumps(obj).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_second_agent_rejected_and_cookie_not_retained(self):
        self.post('/session', {'agentId': 'a', 'href': 'https://www.erp321.com/epaas', 'cookie': 'secret'})
        with self.assertRaises(HTTPError) as caught:
            self.post('/session', {'agentId': 'b', 'href': 'https://www.erp321.com/epaas'})
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(b.BRIDGE.agent_id, 'a')
        self.assertEqual(b.BRIDGE.document_cookie, '')

    def test_client_locked_to_agent(self):
        self.post('/session', {'agentId': 'a', 'href': 'https://www.erp321.com/epaas'})
        client = b.BridgeClient(self.base, ensure=False)
        client.wait_ready(0)
        self.assertEqual(client.session_info()['agentId'], 'a')
        b.BRIDGE.last_seen = 0
        self.post('/session', {'agentId': 'b', 'href': 'https://www.erp321.com/epaas'})
        with self.assertRaises(b.BridgeError):
            client.wait_ready(0)
        with self.assertRaises(b.BridgeError):
            client.session_info()
        with self.assertRaises(b.BridgeError):
            client._call({'url': 'https://www.erp321.com/', 'timeout': 1})

    def test_task_and_result_bound_to_same_agent(self):
        self.post('/session', {'agentId': 'a', 'href': 'https://www.erp321.com/epaas'})
        task = b._Task({'url': 'https://www.erp321.com/', 'agentId': 'a'})
        b.BRIDGE.pending[str(id(task))] = task
        b.BRIDGE.tasks.put(task)
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + '/next?agentId=b', timeout=2)
        self.assertEqual(caught.exception.code, 409)
        with urlopen(self.base + '/next?agentId=a', timeout=2) as response:
            received = json.load(response)
        self.assertEqual(received['agentId'], 'a')
        with self.assertRaises(HTTPError):
            self.post('/result', {'agentId': 'b', 'taskId': id(task), 'ok': True})
        self.assertFalse(task.done.is_set())
        self.post('/result', {'agentId': 'a', 'taskId': id(task), 'ok': True, 'result': {'status': 200}})
        self.assertTrue(task.done.is_set())
        self.assertEqual(task.result['status'], 200)

    def test_expired_agent_fails_pending_tasks(self):
        b.BRIDGE.note_agent('a')
        task = b._Task({'agentId': 'a'})
        b.BRIDGE.pending['task'] = task
        b.BRIDGE.last_seen = 0
        self.assertTrue(b.BRIDGE.note_agent('b'))
        self.assertTrue(task.done.is_set())
        self.assertTrue(task.error)
        self.assertEqual(b.BRIDGE.pending, {})

    def test_body_limit_and_origin(self):
        request = Request(self.base + '/session', data=b'x', headers={'Content-Length': str(b.MAX_BODY + 1)})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 413)
        request = Request(self.base + '/ping', headers={'Origin': 'https://evil.example'})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=2)
        self.assertEqual(caught.exception.code, 403)

    def test_seconds_success_keeps_server_resident(self):
        from unittest.mock import patch
        states = iter([{}, {'agentAttached': True}])
        with patch.object(b, 'ping', side_effect=lambda port: next(states)), \
             patch.object(b, 'ensure_running'), \
             patch.object(b.time, 'sleep', side_effect=KeyboardInterrupt) as sleep:
            self.assertEqual(b.main(['--port', '8999', '--seconds', '1']), 0)
            sleep.assert_called_once_with(3600)

    def test_js_stream_and_epaas_contract(self):
        source = (SCRIPTS / 'bridge_agent.js').read_text()
        self.assertIn('crypto.randomUUID()', source)
        self.assertIn('response.body.getReader()', source)
        self.assertNotIn('response.arrayBuffer()', source)
        self.assertIn('/epaas', source)
        self.assertNotIn('/ds/page/supplier/home', source)
        self.assertIn('task.agentId !== AGENT_ID', source)


if __name__ == '__main__':
    unittest.main()
