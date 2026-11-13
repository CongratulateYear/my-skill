"""Synthetic only: no browser, credentials or network."""
import json
import subprocess
import unittest
from unittest.mock import Mock, patch
import playwright_session as p

class CleanupTests(unittest.TestCase):
    def test_cleanup_after_success_and_failure(self):
        for result in ({'cookies': [], 'userAgent': 'Chrome/999'}, {'bad': True}):
            client = Mock()
            client.run_code.return_value = result
            with patch.object(p.sys, 'platform', 'darwin'):
                if 'bad' in result:
                    with self.assertRaises(p.MCPError):
                        p._acquire_direct(['https://tgc.tmall.com/'], client=client)
                else:
                    self.assertIs(p._acquire_direct(['https://tgc.tmall.com/'], client=client), result)
            client.close_connection_page.assert_called_once()

    def test_no_secret_error_and_disconnect_tolerated(self):
        client = object.__new__(p.MCPClient)
        client._client_name = 'memory-session-offline'
        client.run_code = Mock(side_effect=RuntimeError('sensitive'))
        self.assertFalse(client.close_connection_page())

    def test_only_current_nonce_owned_extension_page_closed(self):
        client = object.__new__(p.MCPClient)
        client._client_name = 'memory-session-offline'
        scripts = []
        client.run_code = lambda code: scripts.append(code) or {'closed': 1}
        client.close_connection_page()
        code = scripts[0]
        self.assertNotIn('context().pages', code)
        script = '''const fn = CODE;
const base = 'chrome-extension://mmlmfjhmonkocbjadbfplnigmagldckm/connect.html';
function url(name, relay='ws://[::1]:1234/extension/offline-123') {
 const u=new URL(base); u.searchParams.set('client',JSON.stringify({name}));
 u.searchParams.set('mcpRelayUrl',relay);u.searchParams.set('protocolVersion','2');return u.href;
}
(async()=>{for(const [u,expected] of [[url('memory-session-offline'),1],
 [url('memory-session-other'),0],[url('memory-session'),0],
 [url('memory-session-offline','ws://evil.invalid/extension/abc'),0],
 ['https://tgc.tmall.com/',0],['about:blank',0]]) {
 let n=0; const page={url:()=>u,close:async()=>{n++}};
 await fn(page); if(n!==expected)throw Error('ownership boundary failed');
} console.log('OK');})().catch(()=>process.exit(1));'''.replace('CODE', code)
        result = subprocess.run(['node','-e',script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'OK')

if __name__ == '__main__': unittest.main()
