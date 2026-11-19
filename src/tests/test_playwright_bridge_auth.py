"""Exercise the actual Node request gate without attaching to a browser."""

import http.client
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from src.mcp.playwright_bridge import _write_token, bridge_headers, ensure_playwright_bridge


class BridgeAuthTests(unittest.TestCase):
    def test_oryn_sends_token_only_to_its_local_bridge(self):
        from src.mcp.client import MCPClient
        from src.mcp.discovery import MCPServerConfig
        from src.mcp.playwright_bridge import BRIDGE_URL

        client = MCPClient([])
        with patch("src.mcp.client.ensure_playwright_bridge") as ensure, \
             patch("src.mcp.client.bridge_headers", return_value={"Authorization": "Bearer private"}), \
             patch.object(client, "_connect_one", new_callable=AsyncMock, return_value=22) as connect:
            asyncio.run(client._connect_config(MCPServerConfig("playwright", url=BRIDGE_URL), None, None))
            self.assertEqual(connect.call_args.args[2].headers, {"Authorization": "Bearer private"})
            ensure.assert_called_once_with()
            connect.reset_mock()
            result = asyncio.run(client._connect_config(
                MCPServerConfig("playwright", url="http://example.com/mcp"), None, None))
            self.assertIn("local bridge", result)
            connect.assert_not_called()

    def test_token_is_private_and_survives_client_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "token"
            with patch("src.mcp.playwright_bridge._TOKEN_PATH", path):
                _write_token()
                headers = bridge_headers()
                self.assertEqual(headers, bridge_headers())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                path.chmod(0o644)
                with self.assertRaisesRegex(RuntimeError, "owner-only"):
                    bridge_headers()
                path.unlink()
                target = Path(folder) / "target"
                target.write_text("a" * 64)
                target.chmod(0o600)
                path.symlink_to(target)
                with self.assertRaisesRegex(RuntimeError, "regular file"):
                    bridge_headers()

    def test_old_unprotected_service_is_replaced_once(self):
        endpoint = "ws://127.0.0.1:9222/devtools/browser/a"
        with patch("src.mcp.playwright_bridge._active", return_value=True), \
             patch("src.mcp.playwright_bridge._stored_endpoint", return_value=endpoint), \
             patch("src.mcp.playwright_bridge._port_accepting", return_value=True), \
             patch("src.mcp.playwright_bridge._authenticated", side_effect=[False, True]), \
             patch("src.mcp.playwright_bridge._start_service") as start, \
             patch("src.mcp.playwright_bridge._stop_service") as stop:
            ensure_playwright_bridge(endpoint)
        start.assert_called_once_with(endpoint)
        stop.assert_called_once_with()

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the Playwright HTTP gate")
    def test_gate_blocks_missing_wrong_and_browser_origin_credentials(self):
        gate = Path(__file__).resolve().parents[1] / "mcp" / "playwright_http_auth.cjs"
        script = """
const http = require('node:http');
const server = http.createServer((req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.end(JSON.stringify({method: req.method, session: req.headers['mcp-session-id']}));
});
server.listen(0, '127.0.0.1', () => console.log(server.address().port));
"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "token"
            with patch("src.mcp.playwright_bridge._TOKEN_PATH", path):
                _write_token()
                headers = bridge_headers()
            env = {**os.environ, "ORYN_PLAYWRIGHT_TOKEN_FILE": str(path)}
            process = subprocess.Popen([shutil.which("node"), "--require", str(gate), "-e", script],
                                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                port = int(process.stdout.readline())
                for method in ("GET", "POST", "DELETE", "OPTIONS"):
                    for supplied, expected in (({}, 401), ({"Authorization": "Bearer wrong"}, 401),
                                               ({**headers, "Origin": "https://example.com"}, 403),
                                               (headers, 200)):
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                        try:
                            connection.request(method, "/mcp", headers={**supplied, "Mcp-Session-Id": "session"})
                            response = connection.getresponse()
                            body = response.read()
                            self.assertEqual(response.status, expected)
                            if expected == 200:
                                self.assertEqual(json.loads(body), {"method": method, "session": "session"})
                        finally:
                            connection.close()
            finally:
                process.terminate()
                process.communicate(timeout=5)
