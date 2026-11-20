import asyncio
from concurrent.futures import Future
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx2

from src.agent.conversation_loop import run_turn
from src.mcp.adapter import provider_tool, result_text
from src.mcp.client import MCPClient
from src.mcp.discovery import (
    MCPServerConfig,
    SERVER_NAMES,
    load_enabled_servers,
    save_enabled_servers,
    server_configs,
)
from src.providers.types import ModelResponse, ToolCall
from src.tools.registry import tool_schemas


class MCPTests(unittest.TestCase):
    def test_playwright_preset_attaches_to_local_brave_and_is_not_isolated(self):
        with patch("src.mcp.discovery.local_secret", return_value=""):
            with patch("src.mcp.discovery.brave_cdp_endpoint", return_value="ws://127.0.0.1:9222/devtools/browser/test"):
                configs = {c.name: c for c in server_configs(set())}
                local = {c.name: c for c in server_configs({"playwright"})}["playwright"]
                with patch("src.mcp.discovery.brave_cdp_endpoint", side_effect=RuntimeError("enable debugging")):
                    unavailable = {c.name: c for c in server_configs({"playwright"})}["playwright"]
        self.assertFalse(configs["playwright"].enabled)
        self.assertTrue(local.enabled)
        self.assertEqual(local.url, "http://127.0.0.1:8931/mcp")
        self.assertEqual(local.command, "")
        from src.mcp.playwright_brave import PLAYWRIGHT_MCP_VERSION
        self.assertEqual(PLAYWRIGHT_MCP_VERSION, "0.0.85")
        self.assertEqual(local.args, ())
        self.assertIn("existing Brave session", local.access)
        self.assertIn("token", local.access)
        self.assertNotIn("browser_use_local", SERVER_NAMES)
        self.assertEqual(local.disabled_reason, "")
        self.assertIn("enable debugging", unavailable.disabled_reason)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"enabled_servers": ["playwright"], "known_servers": ["playwright"]}))
            self.assertEqual(load_enabled_servers(path), {"playwright"})
            save_enabled_servers({"playwright"}, path)
            self.assertEqual(load_enabled_servers(path), {"playwright"})

    def test_local_playwright_action_policy_and_environment(self):
        client = MCPClient(configs=[])
        for tool in ("browser_click", "browser_snapshot", "browser_navigate"):
            client._bindings[f"mcp__playwright__{tool}"] = ("playwright", tool, Mock())
        self.assertTrue(client.requires_approval("mcp__playwright__browser_click"))
        self.assertFalse(client.requires_approval("mcp__playwright__browser_snapshot"))
        self.assertEqual(client.tool_environment("mcp__playwright__browser_snapshot"), "local_desktop")

    def test_local_playwright_tab_policy_approves_only_reads_and_safe_new_urls(self):
        client = MCPClient(configs=[])
        name = "mcp__playwright__browser_tabs"
        client._bindings[name] = ("playwright", "browser_tabs", Mock())

        self.assertFalse(client.requires_approval(name, {"action": "list"}))
        self.assertFalse(client.requires_approval(name, {
            "action": "new", "url": "https://8.8.8.8/",
        }))
        self.assertTrue(client.requires_approval(name, {
            "action": "new", "url": "http://127.0.0.1:8931/",
        }))
        self.assertTrue(client.requires_approval(name, {"action": "new"}))
        self.assertTrue(client.requires_approval(name, {"action": "select", "index": 0}))
        self.assertTrue(client.requires_approval(name, {"action": "close", "index": 0}))
        self.assertTrue(client.requires_approval(name, {"action": "unexpected"}))

    def test_brave_endpoint_uses_current_devtools_websocket_and_fails_closed(self):
        from src.mcp.playwright_brave import brave_cdp_endpoint

        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            (profile / "DevToolsActivePort").write_text("9222\n/devtools/browser/current-session\n")
            self.assertEqual(
                brave_cdp_endpoint(profile),
                "ws://127.0.0.1:9222/devtools/browser/current-session",
            )
            (profile / "DevToolsActivePort").write_text("bad\n/not-a-browser-path\n")
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                brave_cdp_endpoint(profile)
            (profile / "DevToolsActivePort").write_text("99999\n/devtools/browser/current-session\n")
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                brave_cdp_endpoint(profile)

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "remote debugging is not active"):
                brave_cdp_endpoint(Path(folder))

    def test_playwright_bridge_reuses_the_existing_service_for_the_same_brave_session(self):
        from src.mcp.playwright_bridge import BRIDGE_URL, ensure_playwright_bridge

        with patch("src.mcp.playwright_bridge._authenticated", return_value=True), \
             patch("src.mcp.playwright_bridge._active", return_value=True), \
             patch("src.mcp.playwright_bridge._stored_endpoint", return_value="ws://127.0.0.1:9222/devtools/browser/a"), \
             patch("src.mcp.playwright_bridge._port_accepting", return_value=True), \
             patch("src.mcp.playwright_bridge._start_service") as start, \
             patch("src.mcp.playwright_bridge._stop_service") as stop:
            url = ensure_playwright_bridge("ws://127.0.0.1:9222/devtools/browser/a")
        self.assertEqual(url, BRIDGE_URL)
        start.assert_not_called()
        stop.assert_not_called()

    def test_playwright_bridge_starts_once_without_opening_a_cdp_websocket(self):
        from src.mcp.playwright_bridge import BRIDGE_URL, ensure_playwright_bridge

        endpoint = "ws://127.0.0.1:9222/devtools/browser/a"
        with patch("src.mcp.playwright_bridge._authenticated", return_value=True), \
             patch("src.mcp.playwright_bridge._active", side_effect=[False, True]), \
             patch("src.mcp.playwright_bridge._stored_endpoint", return_value=endpoint), \
             patch("src.mcp.playwright_bridge._port_accepting", side_effect=[True, False, True]), \
             patch("src.mcp.playwright_bridge._start_service") as start, \
             patch("src.mcp.playwright_bridge._stop_service") as stop:
            self.assertEqual(ensure_playwright_bridge(endpoint), BRIDGE_URL)
        start.assert_called_once_with(endpoint)
        stop.assert_not_called()

    def test_playwright_bridge_replaces_once_when_brave_endpoint_changes(self):
        from src.mcp.playwright_bridge import ensure_playwright_bridge

        old = "ws://127.0.0.1:9222/devtools/browser/a"
        new = "ws://127.0.0.1:9333/devtools/browser/b"
        with patch("src.mcp.playwright_bridge._authenticated", return_value=True), \
             patch("src.mcp.playwright_bridge._active", side_effect=[True, True]), \
             patch("src.mcp.playwright_bridge._stored_endpoint", side_effect=[old, new]), \
             patch("src.mcp.playwright_bridge._port_accepting", return_value=True), \
             patch("src.mcp.playwright_bridge._start_service") as start, \
             patch("src.mcp.playwright_bridge._stop_service") as stop:
            ensure_playwright_bridge(new)
        stop.assert_called_once_with()
        start.assert_called_once_with(new)

    def test_playwright_bridge_fails_before_start_when_brave_listener_is_missing(self):
        from src.mcp.playwright_bridge import ensure_playwright_bridge

        with patch("src.mcp.playwright_bridge._port_accepting", return_value=False), \
             patch("src.mcp.playwright_bridge._start_service") as start:
            with self.assertRaisesRegex(RuntimeError, "not listening"):
                ensure_playwright_bridge("ws://127.0.0.1:9222/devtools/browser/a")
        start.assert_not_called()

    def test_playwright_bridge_refuses_an_unmanaged_local_listener(self):
        from src.mcp.playwright_bridge import ensure_playwright_bridge

        with patch("src.mcp.playwright_bridge._active", return_value=False), \
             patch("src.mcp.playwright_bridge._port_accepting", side_effect=[True, True]), \
             patch("src.mcp.playwright_bridge._start_service") as start:
            with self.assertRaisesRegex(RuntimeError, "unknown local process"):
                ensure_playwright_bridge("ws://127.0.0.1:9222/devtools/browser/a")
        start.assert_not_called()

    def test_playwright_service_does_not_expose_cdp_endpoint_in_process_arguments(self):
        from src.mcp.playwright_bridge import _start_service

        endpoint = "ws://127.0.0.1:9222/devtools/browser/private-session-id"
        with tempfile.TemporaryDirectory() as folder:
            endpoint_file = Path(folder) / "playwright-bridge-endpoint"
            with patch("src.mcp.playwright_bridge._STATE_PATH", endpoint_file), \
                 patch("src.mcp.playwright_bridge._TOKEN_PATH", Path(folder) / "token"), \
                 patch("src.mcp.playwright_bridge.shutil.which", return_value="/usr/bin/systemd-run"), \
                 patch("src.mcp.playwright_bridge.subprocess.run", return_value=SimpleNamespace(
                     returncode=0, stderr="", stdout="",
                 )) as run:
                _start_service(endpoint)
            command = run.call_args.args[0]
            self.assertNotIn(endpoint, command)
            self.assertFalse(any("ORYN_BRAVE_CDP_ENDPOINT" in arg for arg in command))
            self.assertTrue(command[-2].endswith("/playwright_bridge_service.py"))
            self.assertEqual(endpoint_file.read_text(encoding="utf-8").strip(), endpoint)
            self.assertEqual(endpoint_file.stat().st_mode & 0o777, 0o600)

    def test_persistent_bridge_anchor_holds_one_read_only_tabs_client(self):
        from src.mcp.playwright_bridge_service import _connect_anchor

        anchor = Mock()
        anchor.diagnostic_snapshot.return_value = {"connected_count": 1}
        anchor.call_tool.return_value = "one tab"
        stop = threading.Event()
        with patch("src.mcp.client.MCPClient", return_value=anchor) as make_client:
            returned = _connect_anchor(stop)
        self.assertIs(returned, anchor)
        self.assertEqual(make_client.call_args.args[0][0].name, "playwright")
        self.assertEqual(anchor.call_tool.call_args.args[:2], (
            "mcp__playwright__browser_tabs", {"action": "list"},
        ))
        anchor.close.assert_not_called()

    def test_persistent_bridge_anchor_rejects_playwright_error_results(self):
        from src.mcp.playwright_bridge_service import _connect_anchor

        anchor = Mock()
        anchor.diagnostic_snapshot.return_value = {"connected_count": 1}
        for result in ("### Error\nCDP connection failed", "MCP server reported a tool error:\n### Error\nTimeout"):
            anchor.reset_mock()
            anchor.call_tool.return_value = result
            with patch("src.mcp.client.MCPClient", return_value=anchor):
                with self.assertRaisesRegex(RuntimeError, "tab check failed"):
                    _connect_anchor(threading.Event())
            anchor.close.assert_called_once_with()

    def test_disabling_playwright_stops_the_persistent_service(self):
        client = MCPClient([MCPServerConfig("playwright", url="http://127.0.0.1:8931/mcp", enabled=False)])
        with patch("src.mcp.client.stop_playwright_bridge") as stop:
            asyncio.run(client._reconfigure({"playwright"}))
        stop.assert_called_once_with()

    def test_local_mcp_and_remote_browser_cannot_silently_replace_each_other(self):
        local_name = "mcp__playwright__browser_click"
        for local_first in (True, False):
            with self.subTest(local_first=local_first), tempfile.TemporaryDirectory() as folder:
                mcp = SimpleNamespace(
                    tool_directory=Mock(return_value=[{"server": "playwright", "description": "Local Brave", "state": "connected", "tool_count": 1}]),
                    tool_schemas=Mock(return_value=[{"name": local_name, "description": "Read local tab", "parameters": {"type": "object", "properties": {}}}]),
                    diagnostic_snapshot=Mock(return_value={}), requires_approval=Mock(return_value=False),
                    tool_environment=lambda name: "local_desktop", call_tool=Mock(return_value="LOCAL TAB"),
                )
                local = ToolCall("local", local_name, {})
                remote = ToolCall("remote", "browser_open", {"url": "https://example.com"})
                responses = [ModelResponse(tool_calls=[ToolCall("load", "load_mcp_tools", {"server": "playwright"})])]
                responses += [ModelResponse(tool_calls=[c]) for c in ((local, remote) if local_first else (remote, local))]
                responses.append(ModelResponse("Stopped at environment boundary."))
                history = [{"role": "user", "content": "Inspect the requested browser."}]
                with patch("src.agent.conversation_loop.execute_tool", return_value="REMOTE TAB") as execute:
                    run_turn(history, Mock(side_effect=responses), tool_schemas(), Path(folder), Mock(), Mock(), mcp_client=mcp)
                outputs = [m["content"] for m in history if m.get("role") == "tool"]
                self.assertIn("Tool error:", outputs[-1])
                self.assertEqual(mcp.call_tool.call_count, int(local_first))
                self.assertEqual(execute.call_count, int(not local_first))

    def test_uncertain_local_browser_call_still_binds_the_ui_route(self):
        local_name = "mcp__playwright__browser_click"
        mcp = SimpleNamespace(
            tool_directory=Mock(return_value=[{"server": "playwright", "description": "Local Brave", "state": "connected", "tool_count": 1}]),
            tool_schemas=Mock(return_value=[{"name": local_name, "description": "Click local tab", "parameters": {"type": "object", "properties": {}}}]),
            diagnostic_snapshot=Mock(return_value={}), requires_approval=Mock(return_value=False),
            tool_environment=lambda name: "local_desktop", call_tool=Mock(side_effect=ConnectionResetError("Input may have run")),
        )
        replies = [
            ModelResponse(tool_calls=[ToolCall("load", "load_mcp_tools", {"server": "playwright"})]),
            ModelResponse(tool_calls=[ToolCall("local", local_name, {})]),
            ModelResponse(tool_calls=[ToolCall("remote", "browser_open", {"url": "https://example.com"})]),
            ModelResponse("Inspect local state before continuing."),
        ]
        history = [{"role": "user", "content": "Use my local browser."}]
        with tempfile.TemporaryDirectory() as folder, patch("src.agent.conversation_loop.execute_tool") as execute:
            run_turn(history, Mock(side_effect=replies), tool_schemas(), Path(folder), Mock(), Mock(), mcp_client=mcp)
        execute.assert_not_called()
        self.assertIn("bound to the live local desktop", history[-1]["content"])

    def test_transient_reads_retry_but_mutations_permanent_errors_and_cancelled_calls_do_not(self):
        request = httpx2.Request("POST", "https://example.com/mcp")
        unauthorized = httpx2.HTTPStatusError("Unauthorized", request=request,
                                             response=httpx2.Response(401, request=request))
        unavailable = httpx2.HTTPStatusError("Unavailable", request=request,
                                            response=httpx2.Response(503, headers={"Retry-After": "60"}, request=request))
        certificate = httpx2.ConnectError("Invalid certificate", request=request)
        import ssl
        certificate.__cause__ = ssl.SSLCertVerificationError("Invalid certificate")

        def ready(value):
            future = Future()
            if isinstance(value, BaseException):
                future.set_exception(value)
            else:
                future.set_result(value)
            return future

        scenarios = [
            ("github", False, ConnectionResetError("Reset"), True),
            ("linear", True, ConnectionResetError("Reset"), True),
            ("linear", False, ConnectionResetError("Uncertain write"), False),
            ("playwright", True, ConnectionResetError("Uncertain browser state"), False),
            ("github", False, unauthorized, False),
            ("github", False, unavailable, True),
            ("github", False, certificate, False),
            ("github", False, httpx2.UnsupportedProtocol("Bad URL"), False),
        ]
        for server, hinted_read, failure, retries in scenarios:
            client = MCPClient(configs=[])
            client.start = Mock()
            client._call_local_browser = Mock()
            client._loop = Mock()
            name = f"mcp__{server}__action"
            client._bindings[name] = (server, "action", Mock())
            if hinted_read:
                client._read_only_tools.add(name)
            stop = threading.Event()
            notices = []
            try:
                with patch("src.mcp.client.asyncio.run_coroutine_threadsafe",
                           side_effect=[ready(failure), ready("result")]) as schedule, \
                     patch("src.mcp.client.result_text", return_value="Read result"), \
                     patch.object(stop, "wait", return_value=False) as wait:
                    if retries:
                        self.assertEqual(client.call_tool(name, {}, stop, lambda n, d: notices.append((n, d))),
                                         "Read result")
                        self.assertEqual(schedule.call_count, 2)
                        self.assertEqual(len(notices), 1)
                        self.assertLessEqual(wait.call_args.args[0], 30)
                    else:
                        with self.assertRaises(type(failure)):
                            client.call_tool(name, {}, stop, lambda n, d: notices.append((n, d)))
                        schedule.assert_called_once()
                        self.assertEqual(notices, [])
            finally:
                client._loop = None
                client.close()

        client = MCPClient(configs=[])
        client.start = Mock()
        client._loop = Mock()
        client._bindings["mcp__github__read"] = ("github", "read", Mock())
        stop = threading.Event()
        try:
            with patch("src.mcp.client.asyncio.run_coroutine_threadsafe",
                       side_effect=[ready(ConnectionResetError("Reset")) for _ in range(3)]) as schedule, \
                 patch.object(stop, "wait", return_value=False):
                with self.assertRaises(ConnectionResetError):
                    client.call_tool("mcp__github__read", {}, stop)
                self.assertEqual(schedule.call_count, 3)
            with patch("src.mcp.client.asyncio.run_coroutine_threadsafe",
                       return_value=ready(ConnectionResetError("Reset"))) as schedule:
                with self.assertRaises(InterruptedError):
                    client.call_tool("mcp__github__read", {}, stop, lambda *_: stop.set())
                schedule.assert_called_once()
            with patch("src.mcp.client.asyncio.run_coroutine_threadsafe") as schedule:
                with self.assertRaises(InterruptedError):
                    client.call_tool("mcp__github__read", {}, stop)
                schedule.assert_not_called()
        finally:
            client._loop = None
            client.close()

    def test_browser_login_reports_its_url_without_printing_over_the_tui(self):
        import contextlib
        import io
        from src.mcp.oauth import NotionTokenStorage, login_notion

        redirect = None

        def auth_factory(on_redirect, _callback, _storage):
            nonlocal redirect
            redirect = on_redirect
            return SimpleNamespace(context=SimpleNamespace(oauth_metadata=None, protected_resource_metadata=None))

        class FakeContext:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def list_tools(self):
                await redirect("https://example.com/authorize?state=test")
                return SimpleNamespace(tools=[object()])

        with tempfile.TemporaryDirectory() as folder, \
             patch("src.mcp.oauth.HTTPServer"), \
             patch("src.mcp.oauth.NotionTokenStorage", return_value=NotionTokenStorage(Path(folder) / "auth.json")), \
             patch("src.mcp.oauth.notion_auth", side_effect=auth_factory), \
             patch("src.mcp.oauth.webbrowser.open", return_value=True) as browser, \
             patch("mcp.shared._httpx_utils.create_mcp_http_client", FakeContext), \
             patch("mcp.Client", FakeContext):
            urls = []
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                count = asyncio.run(login_notion(on_authorize=urls.append))
            self.assertEqual(count, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(urls, ["https://example.com/authorize?state=test"])
            browser.assert_called_once_with(urls[0])

    def test_mcp_cli_reuses_server_preferences(self):
        import contextlib
        import io
        from src.mcp.oauth import main

        with patch("src.mcp.discovery.load_enabled_servers", return_value={"context7"}):
            with patch("src.mcp.discovery.save_enabled_servers") as save:
                with contextlib.redirect_stdout(io.StringIO()):
                    main(["enable", "github"])
                save.assert_called_once_with({"context7", "github"})

    def test_hosted_presets_preserve_explicit_preferences_and_keep_secrets_out_of_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps({"enabled_servers": ["context7", "playwright"]}))
            enabled = load_enabled_servers(path)
            self.assertNotIn("github", enabled)
            self.assertNotIn("notion", enabled)
            save_enabled_servers({"context7"}, path)
            self.assertEqual(load_enabled_servers(path), {"context7"})
        with patch("src.mcp.discovery.local_secret", return_value="test-secret"):
            configs = server_configs(set(SERVER_NAMES))
        hosted = [config for config in configs if config.url and config.name != "playwright"]
        self.assertEqual(len(hosted), 9)
        self.assertTrue(all(config.url.startswith("https://") and not config.command for config in hosted))
        self.assertTrue(all("test-secret" not in config.url and "test-secret" not in repr(config) for config in hosted))
        self.assertEqual(next(c for c in hosted if c.name == "exa").headers, {"x-api-key": "test-secret"})
        github = next(c for c in hosted if c.name == "github")
        self.assertTrue(github.url.endswith("/readonly"))
        self.assertEqual(github.headers["X-MCP-Toolsets"], "repos,issues,pull_requests")

    def test_http_discovery_tool_call_and_write_approval(self):
        import httpx2
        from mcp import Client

        requests = []

        def respond(request):
            requests.append(request)
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            if request.method != "POST":
                return httpx2.Response(405)
            message = json.loads(request.content)
            if "id" not in message:
                return httpx2.Response(202)
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "http-test", "version": "1"}}
            elif message["method"] == "tools/list":
                result = {"tools": [
                    {"name": "lookup", "inputSchema": {"type": "object"},
                     "annotations": {"readOnlyHint": True}},
                    {"name": "change", "inputSchema": {"type": "object"}},
                ]}
            else:
                self.assertEqual(message["method"], "tools/call")
                result = {"content": [{"type": "text", "text": "Hosted result"}]}
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

        config = MCPServerConfig("linear", url="https://mcp.test/mcp", headers={"Authorization": "Bearer test-secret"})
        client = MCPClient([config])

        async def exercise():
            http = httpx2.AsyncClient(transport=httpx2.MockTransport(respond), headers=config.headers)
            with patch("mcp.shared._httpx_utils.create_mcp_http_client", return_value=http):
                with patch("src.mcp.client.shutil.which") as which:
                    status = await client._connect_config(config, Client, None)
                    which.assert_not_called()
            try:
                self.assertIn("Connected with 2 tool", status)
                self.assertFalse(client.requires_approval("mcp__linear__lookup"))
                self.assertTrue(client.requires_approval("mcp__linear__change"))
                result = await client._clients["linear"].call_tool("lookup", {})
                self.assertEqual(result.content[0].text, "Hosted result")
            finally:
                await client._stacks["linear"].aclose()

        asyncio.run(exercise())
        self.assertTrue(requests)

    def test_remote_failure_redacts_credentials_and_missing_keys_have_instructions(self):
        from unittest.mock import AsyncMock

        config = MCPServerConfig("tavily", url="https://mcp.tavily.com/mcp/", headers={"Authorization": "Bearer test-secret"})
        client = MCPClient([config])
        with patch.object(client, "_connect_one", new=AsyncMock(side_effect=RuntimeError("Bad token test-secret"))):
            status = asyncio.run(client._connect_config(config, None, None))
        self.assertNotIn("test-secret", status)
        self.assertIn("[redacted]", status)
        with patch("src.mcp.discovery.local_secret", return_value=""):
            with patch("src.mcp.oauth.NotionTokenStorage.has_tokens", return_value=False):
                configs = {config.name: config for config in server_configs(set(SERVER_NAMES))}
        self.assertIn("mcp login notion", configs["notion"].disabled_reason)
        self.assertIn("LINEAR_API_KEY", configs["linear"].disabled_reason)
        self.assertFalse(configs["microsoft_learn"].disabled_reason)

    def test_notion_token_storage_is_private_and_preserves_expiry(self):
        from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
        from src.mcp.oauth import NotionTokenStorage

        async def exercise(path):
            storage = NotionTokenStorage(path)
            self.assertFalse(storage.has_tokens())
            info = OAuthClientInformationFull(client_id="test-client", redirect_uris=["http://127.0.0.1:8766/callback"])
            await storage.set_client_info(info)
            with patch("src.mcp.oauth.time.time", return_value=1000):
                await storage.set_tokens(OAuthToken(access_token="test-token", token_type="Bearer", expires_in=60, refresh_token="refresh"))
            self.assertTrue(storage.has_tokens())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch("src.mcp.oauth.time.time", return_value=1200):
                tokens = await storage.get_tokens()
            self.assertLess(tokens.expires_in, 0)
            self.assertEqual(tokens.refresh_token, "refresh")
            self.assertEqual((await storage.get_client_info()).client_id, "test-client")
            path.write_text("bad json")
            self.assertFalse(storage.has_tokens())

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(exercise(Path(folder) / "private" / "tokens.json"))

    def test_notion_oauth_pkce_and_refresh_after_restart(self):
        import base64
        import hashlib
        from urllib.parse import parse_qs, urlsplit
        import httpx2
        from src.mcp.oauth import NOTION_URL, NotionTokenStorage, notion_auth

        authorization = {}
        grants = []

        async def redirect(url):
            authorization.update(parse_qs(urlsplit(url).query))
            self.assertEqual(authorization["code_challenge_method"], ["S256"])

        async def callback():
            from mcp.shared.auth import AuthorizationCodeResult
            return AuthorizationCodeResult(code="test-code", state=authorization["state"][0])

        def respond(request):
            url = str(request.url)
            if url == NOTION_URL:
                if request.headers.get("Authorization") in {"Bearer test-token", "Bearer refreshed-token"}:
                    return httpx2.Response(200, json={"ok": True})
                return httpx2.Response(401, headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.notion.com/.well-known/oauth-protected-resource"',
                })
            if "oauth-protected-resource" in url:
                return httpx2.Response(200, json={"resource": NOTION_URL, "authorization_servers": ["https://auth.notion.test"]})
            if "oauth-authorization-server" in url or "openid-configuration" in url:
                return httpx2.Response(200, json={
                    "issuer": "https://auth.notion.test", "authorization_endpoint": "https://auth.notion.test/authorize",
                    "token_endpoint": "https://auth.notion.test/oauth/token", "registration_endpoint": "https://auth.notion.test/register",
                    "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                })
            if url.endswith("/register"):
                metadata = json.loads(request.content)
                return httpx2.Response(201, json={**metadata, "client_id": "test-client"})
            if url.endswith("/oauth/token"):
                params = parse_qs(request.content.decode())
                grant = params["grant_type"][0]
                grants.append(grant)
                if grant == "authorization_code":
                    digest = hashlib.sha256(params["code_verifier"][0].encode()).digest()
                    self.assertEqual(base64.urlsafe_b64encode(digest).rstrip(b"=").decode(), authorization["code_challenge"][0])
                else:
                    self.assertEqual(params["refresh_token"], ["test-refresh"])
                return httpx2.Response(200, json={
                    "access_token": "test-token" if grant == "authorization_code" else "refreshed-token",
                    "token_type": "Bearer", "expires_in": 60, "refresh_token": "test-refresh",
                })
            self.fail(f"Unexpected OAuth request: {url}")

        async def exercise(path):
            storage = NotionTokenStorage(path)
            auth = notion_auth(redirect, callback, storage)
            with patch("src.mcp.oauth.time.time", return_value=1000):
                async with httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(respond)) as http:
                    self.assertEqual((await http.get(NOTION_URL)).status_code, 200)
            storage._save(oauth_metadata=auth.context.oauth_metadata.model_dump(mode="json"),
                          resource_metadata=auth.context.protected_resource_metadata.model_dump(mode="json"))
            with patch("src.mcp.oauth.time.time", return_value=1200):
                restarted = notion_auth(storage=storage)
                async with httpx2.AsyncClient(auth=restarted, transport=httpx2.MockTransport(respond)) as http:
                    self.assertEqual((await http.get(NOTION_URL)).status_code, 200)
            self.assertEqual(grants, ["authorization_code", "refresh_token"])
            self.assertEqual((await storage.get_tokens()).access_token, "refreshed-token")

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(exercise(Path(folder) / "auth.json"))

    def test_stdio_server_notices_do_not_leak_to_the_terminal(self):
        server = '''
import json, os, sys
os.write(2, b"npm notice PLAYWRIGHT_STARTUP_NOTICE\\n")
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    result = ({"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
               "serverInfo": {"name": "quiet-test", "version": "1"}}
              if request["method"] == "initialize" else {"tools": []})
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''
        script = f'''
import sys
from src.mcp.client import MCPClient
from src.mcp.discovery import MCPServerConfig
client = MCPClient([MCPServerConfig("github", sys.executable, ("-u", "-c", {server!r}), {{}})])
try:
    assert "Connected with 0 tool" in client.start()[0]
finally:
    client.close()
'''
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PLAYWRIGHT_STARTUP_NOTICE", result.stderr)

    def test_local_stdio_read_tool_runs_through_the_existing_agent_loop(self):
        server = '''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "local-read-test", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "read_marker", "description": "Read a fixed marker.",
                  "inputSchema": {"type": "object", "properties": {}, "required": []},
                  "annotations": {"readOnlyHint": True}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "ORYN_STDIO_MARKER"}], "isError": False}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            client = MCPClient([MCPServerConfig(
                "github", sys.executable, ("-u", "-c", server), {},
                access="Local read-only fixture.",
            )])
            try:
                statuses = client.start()
                self.assertTrue(statuses and "Connected" in statuses[0], statuses)
                complete = Mock(side_effect=[
                    ModelResponse(tool_calls=[ToolCall(
                        "load", "load_mcp_tools", {"server": "github"},
                    )]),
                    ModelResponse(tool_calls=[ToolCall(
                        "read", "mcp__github__read_marker", {},
                    )]),
                    ModelResponse("The local MCP returned ORYN_STDIO_MARKER."),
                ])
                history = [{"role": "user", "content": "Read the marker from the local MCP."}]
                answer = run_turn(
                    history, complete, tool_schemas(client.tool_schemas()), root,
                    Mock(), Mock(), mcp_client=client,
                )
                self.assertIn("ORYN_STDIO_MARKER", answer)
                self.assertIn("ORYN_STDIO_MARKER", history[-1]["content"])
            finally:
                client.close()

    def test_server_preferences_require_explicit_opt_in_and_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prefs" / "mcp-settings.json"
            self.assertEqual(load_enabled_servers(path), set())
            self.assertFalse(any(config.enabled for config in server_configs(set())))

            enabled = {"context7", "playwright"}
            save_enabled_servers(enabled, path)
            self.assertEqual(load_enabled_servers(path), enabled)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            configs = server_configs(enabled)
            self.assertEqual({config.name for config in configs if config.enabled}, enabled)

    def test_removed_preset_does_not_discard_other_saved_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mcp-settings.json"
            path.write_text(json.dumps({
                "enabled_servers": ["context7", "retired_preset"],
                "known_servers": ["context7", "retired_preset"],
            }))
            self.assertEqual(load_enabled_servers(path), {"context7"})

    def test_preferences_reject_unknown_server_names(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "Unknown MCP server"):
                save_enabled_servers({"arbitrary-command"}, Path(folder) / "prefs.json")

    def test_invalid_tool_schemas_are_rejected(self):
        malformed = SimpleNamespace(name="bad_schema", input_schema=["not", "an object"])
        with self.assertRaisesRegex(ValueError, "unsupported input schema"):
            provider_tool("fake", malformed)

    def test_playwright_snapshot_cleanup_keeps_a_scoped_hidden_content_escape_hatch(self):
        fence = "```"
        raw = (
            "### Open tabs\n"
            "- 0: Fixture — http://127.0.0.1:8931/\n\n"
            "### Snapshot\n"
            f"{fence}yaml\n"
            "- main [ref=e1]:\n"
            "  - generic [aria-hidden=\"true\"] [ref=e2]:\n"
            "    - button \"Hidden control\" [ref=e3]\n"
            "  - button \"Visible control\" [ref=e4]\n"
            "  - generic [aria-hidden=\"false\"] [ref=e5]\n"
            f"{fence}\n"
            "### URL\n"
            "http://127.0.0.1:8931/\n"
        )
        result = SimpleNamespace(
            content=[SimpleNamespace(text=raw)],
            structured_content=None,
            is_error=False,
        )

        default = result_text(
            result, server_name="playwright", tool_name="browser_snapshot", arguments={}
        )
        self.assertNotIn("### Open tabs", default)
        self.assertNotIn("Hidden control", default)
        self.assertNotIn("[aria-hidden=\"true\"]", default)
        self.assertIn("Visible control", default)
        self.assertIn("[aria-hidden=\"false\"]", default)
        self.assertIn("### URL", default)
        self.assertLess(len(default.encode("utf-8")), len(raw.encode("utf-8")))

        scoped = result_text(
            result,
            server_name="playwright",
            tool_name="browser_snapshot",
            arguments={"target": "main"},
        )
        self.assertNotIn("### Open tabs", scoped)
        self.assertIn("Hidden control", scoped)
        self.assertIn("Visible control", scoped)

    def test_playwright_snapshot_cleanup_keeps_literal_aria_hidden_text(self):
        raw = (
            "### Snapshot\n"
            "```yaml\n"
            "- text: 'The docs mention [aria-hidden=\"true\"] as an example.'\n"
            "- generic \"The literal [aria-hidden=\\\"true\\\"] text\" [ref=e1]\n"
            "- generic [aria-hidden=\"true\"] [ref=e2]:\n"
            "  - button \"Actually hidden\" [ref=e3]\n"
            "- button \"Visible control\" [ref=e4]\n"
            "```\n"
        )
        result = SimpleNamespace(
            content=[SimpleNamespace(text=raw)],
            structured_content=None,
            is_error=False,
        )

        cleaned = result_text(
            result, server_name="playwright", tool_name="browser_snapshot", arguments={}
        )
        self.assertIn('[aria-hidden="true"] as an example.', cleaned)
        self.assertIn('The literal [aria-hidden=\\"true\\"] text', cleaned)
        self.assertNotIn("Actually hidden", cleaned)
        self.assertIn("Visible control", cleaned)

    def test_playwright_find_cleanup_removes_hidden_matches(self):
        raw = (
            'Found 1 match for "Hidden control":\n\n'
            '- main [ref=e1]:\n'
            '  - generic [aria-hidden] [ref=e2]:\n'
            '    - button "Hidden control" [ref=e3]\n'
            '- button "Visible control" [ref=e4]\n'
        )
        result = SimpleNamespace(
            content=[SimpleNamespace(text=raw)],
            structured_content=None,
            is_error=False,
        )

        cleaned = result_text(
            result, server_name="playwright", tool_name="browser_find", arguments={}
        )
        self.assertIn('Found 1 match for "Hidden control":', cleaned)
        self.assertNotIn('button "Hidden control"', cleaned)
        self.assertIn("Visible control", cleaned)

    def test_playwright_cleanup_keeps_nonleading_open_tabs_heading(self):
        raw = (
            "### Result\n"
            "Page content before the literal heading.\n"
            "### Open tabs\n"
            "This heading is part of the page result.\n"
            "### End\n"
            "Remaining result content.\n"
        )
        result = SimpleNamespace(
            content=[SimpleNamespace(text=raw)],
            structured_content=None,
            is_error=False,
        )

        cleaned = result_text(
            result, server_name="playwright", tool_name="browser_evaluate", arguments={}
        )
        self.assertEqual(cleaned, raw)

    def test_playwright_cleanup_leaves_tab_list_other_servers_and_errors_alone(self):
        fence = "```"
        raw = (
            "### Open tabs\n- 0: Fixture\n"
            "### Snapshot\n"
            f"{fence}yaml\n"
            "- generic [aria-hidden]:\n"
            "  - button \"Hidden\"\n"
            f"{fence}\n"
        )
        result = SimpleNamespace(
            content=[SimpleNamespace(text=raw)],
            structured_content=None,
            is_error=False,
        )
        self.assertEqual(
            result_text(result, server_name="playwright", tool_name="browser_tabs"),
            raw,
        )
        self.assertEqual(result_text(result, server_name="other"), raw)

        error = SimpleNamespace(
            content=[SimpleNamespace(text=raw)],
            structured_content=None,
            is_error=True,
        )
        self.assertEqual(
            result_text(error, server_name="playwright", tool_name="browser_snapshot"),
            f"MCP server reported a tool error:\n{raw}",
        )

    def test_playwright_snapshot_tool_description_explains_scoped_inspection(self):
        tool = SimpleNamespace(
            name="browser_snapshot",
            description="Read the current page snapshot.",
            title="",
            input_schema={"type": "object", "properties": {}},
        )
        description = provider_tool("playwright", tool)["description"]
        self.assertIn("aria-hidden", description)
        self.assertIn("target", description)
        self.assertIn("preserve", description)
        self.assertNotIn("aria-hidden", provider_tool("fake", tool)["description"])

    def test_playwright_call_tool_preserves_hidden_content_for_an_explicit_target(self):
        fence = "```"
        raw = (
            "### Open tabs\n- 0: Fixture\n"
            "### Snapshot\n"
            f"{fence}yaml\n"
            "- main [ref=e1]:\n"
            "  - generic [aria-hidden=\"true\"]:\n"
            "    - button \"Hidden control\"\n"
            f"{fence}\n"
        )
        result = SimpleNamespace(
            content=[SimpleNamespace(text=raw)],
            structured_content=None,
            is_error=False,
        )
        name = "mcp__playwright__browser_snapshot"
        client = MCPClient(configs=[])
        client.start = Mock()
        client._loop = Mock()
        client._bindings[name] = ("playwright", "browser_snapshot", Mock())
        future = Mock()
        future.result.return_value = result

        with patch("src.mcp.client.asyncio.run_coroutine_threadsafe", return_value=future):
            output = client.call_tool(name, {"target": "main"})

        self.assertNotIn("### Open tabs", output)
        self.assertIn("Hidden control", output)

    def test_duplicate_tool_names_are_registered_once(self):
        tool = SimpleNamespace(
            name="lookup", description="Lookup data", title="",
            input_schema={"type": "object", "properties": {}},
        )
        bad_tool = SimpleNamespace(name="bad", input_schema={"type": "string"})

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=[tool, tool, bad_tool], next_cursor=None)

        config = MCPServerConfig("fake", "fake", (), {})
        client = MCPClient([config])

        async def connect_and_close():
            count = await client._connect_one(FakeClient, lambda **_kwargs: None, config, "/fake")
            await client._stacks["fake"].aclose()
            return count

        count = asyncio.run(connect_and_close())
        self.assertEqual(count, 1)
        self.assertEqual([schema["name"] for schema in client._schemas], ["mcp__fake__lookup"])
        self.assertEqual(list(client._bindings), ["mcp__fake__lookup"])

    def test_live_session_closes_in_the_task_that_opened_it(self):
        tool = SimpleNamespace(
            name="lookup", description="Lookup data", title="",
            input_schema={"type": "object", "properties": {}},
        )

        class TaskBoundClient:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.owner = None
                self.closed_in_owner = False
                self.instances.append(self)

            async def __aenter__(self):
                self.owner = asyncio.current_task()
                return self

            async def __aexit__(self, *_args):
                self.closed_in_owner = asyncio.current_task() is self.owner

            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=[tool], next_cursor=None)

        config = MCPServerConfig("fake", "fake", (), {})
        client = MCPClient([config])

        async def connect_and_stop():
            with patch("src.mcp.client.shutil.which", return_value="/fake"):
                status = await client._start_config(config, TaskBoundClient, lambda **_kwargs: None)
            self.assertIn("Connected with 1 tool", status)
            client._server_stops["fake"].set()
            await client._server_tasks["fake"]

        asyncio.run(connect_and_stop())
        self.assertTrue(TaskBoundClient.instances[0].closed_in_owner)

    def test_reconnect_reloads_credentials_and_closes_the_previous_owner(self):
        tool = SimpleNamespace(name="lookup", description="Lookup data", title="",
                               input_schema={"type": "object", "properties": {}})

        class TaskBoundClient:
            instances = []

            def __init__(self, *_args, **_kwargs):
                self.closed_in_owner = False
                self.instances.append(self)

            async def __aenter__(self):
                self.owner = asyncio.current_task()
                return self

            async def __aexit__(self, *_args):
                self.closed_in_owner = asyncio.current_task() is self.owner

            async def list_tools(self, cursor=None):
                return SimpleNamespace(tools=[tool], next_cursor=None)

        original = MCPServerConfig("context7", "fake", env={"API_KEY": "old"})
        fresh = MCPServerConfig("context7", "fake", env={"API_KEY": "new"})
        client = MCPClient([original])
        client._uses_presets = True
        with patch("mcp.Client", TaskBoundClient), \
             patch("src.mcp.client.shutil.which", return_value="/fake"), \
             patch("src.mcp.client.server_configs", side_effect=lambda enabled: [
                 MCPServerConfig("context7", "fake", env=fresh.env, enabled="context7" in enabled)
             ]) as configs:
            try:
                client.start()
                client.reconnect("context7")
                self.assertTrue(TaskBoundClient.instances[0].closed_in_owner)
                self.assertEqual(client.configs[0].env, {"API_KEY": "new"})
                self.assertEqual(len(client.tool_schemas()), 1)
                self.assertIs(client._bindings["mcp__context7__lookup"][2], TaskBoundClient.instances[1])
                client.set_enabled(set())
                self.assertEqual(client.tool_schemas(), [])
                with self.assertRaises(ValueError):
                    client.reconnect("context7")
                with self.assertRaises(ValueError):
                    client.reconnect("unknown")
                client.set_enabled({"context7"})
                self.assertEqual(len(client.tool_schemas()), 1)
                self.assertEqual(configs.call_count, 3)
            finally:
                client.close()
        self.assertTrue(all(c.closed_in_owner for c in TaskBoundClient.instances))

    def test_disabling_a_server_removes_its_tools_and_schema(self):
        config = MCPServerConfig("context7", "unused", (), {}, enabled=False)
        client = MCPClient([config])
        client._bindings["mcp__context7__lookup"] = ("context7", "lookup", object())
        client._schemas.append({"type": "function", "name": "mcp__context7__lookup"})
        asyncio.run(client._reconfigure({"context7"}))
        self.assertEqual(client._bindings, {})
        self.assertEqual(client._schemas, [])
        self.assertEqual(client._status_by_name["context7"][0], "disabled")

    def test_missing_server_command_marks_only_that_server_unavailable(self):
        config = MCPServerConfig("context7", "missing-mcp-command", (), {})
        client = MCPClient([config])
        with patch("src.mcp.client.shutil.which", return_value=None):
            status = asyncio.run(client._connect_config(config, None, None))
        self.assertIn("not installed", status)
        self.assertEqual(client._status_by_name["context7"][0], "unavailable")
        client._started = True
        self.assertEqual(client.status_snapshot()[0]["tool_count"], 0)

    def test_slow_server_tool_call_times_out(self):
        class PendingFuture:
            cancelled = False

            def result(self, timeout=None):
                import time
                time.sleep(timeout or 0)
                raise TimeoutError

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        class SlowClient:
            def call_tool(self, _name, _arguments):
                return self

            def __await__(self):
                yield

        client = MCPClient([])
        client._loop = object()
        client._bindings["mcp__context7__slow"] = ("context7", "slow", SlowClient())
        future = PendingFuture()
        with patch("src.mcp.client._TOOL_CALL_TIMEOUT_SECONDS", 0.01):
            with patch("src.mcp.client.asyncio.run_coroutine_threadsafe", return_value=future):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    client.call_tool("mcp__context7__slow", {})
        self.assertTrue(future.cancelled)

    def test_oversized_mcp_result_is_truncated_before_model_history(self):
        complete = Mock(side_effect=[
            ModelResponse(tool_calls=[ToolCall("load-call", "load_mcp_tools", {"server": "context7"})]),
            ModelResponse(tool_calls=[ToolCall("mcp-call", "mcp__context7__lookup", {})]),
            ModelResponse("The result was safely shortened."),
        ])
        mcp = SimpleNamespace(
            tool_directory=Mock(return_value=[{
                "server": "context7", "description": "Library documentation.",
                "state": "connected", "tool_count": 1,
            }]),
            tool_schemas=Mock(return_value=[{
                "type": "function", "name": "mcp__context7__lookup", "description": "Look up docs.",
                "parameters": {"type": "object", "properties": {}},
            }]),
            diagnostic_snapshot=Mock(return_value={}),
            requires_approval=Mock(return_value=False),
            call_tool=Mock(return_value="x" * 20_001),
        )
        history = [{"role": "user", "content": "Look up a large page."}]
        with tempfile.TemporaryDirectory() as folder:
            run_turn(
                history, complete, [], Path(folder), Mock(), Mock(), mcp_client=mcp,
            )
        result = history[-1]["content"]
        self.assertLessEqual(len(result), 20_000)
        self.assertIn("original result was 20001 characters", result)
        mcp.call_tool.assert_called_once()

    def test_on_demand_loading_keeps_requests_small_and_tools_scoped_to_the_turn(self):
        configs = [
            MCPServerConfig("github", access="Repositories, issues and pull requests.",
                            headers={"Authorization": "Bearer never-send-this-secret"}),
            MCPServerConfig("context7", access="Library documentation."),
            MCPServerConfig("linear", access="Issue changes need approval."),
            MCPServerConfig("notion", access="Workspace pages.", enabled=False),
            MCPServerConfig("exa", access="Web search."),
        ]
        client = MCPClient(configs)
        client._started = True  # Seed an already-discovered inventory; no network or processes.
        client._status_by_name = {config.name: ("connected", "Connected.") for config in configs}
        for server in ("github", "context7", "linear", "notion"):
            count = 40 if server == "github" else 1
            for index in range(count):
                tool = SimpleNamespace(name=f"lookup_{index}", description="Detailed usage. " * 40,
                                       input_schema={"type": "object", "properties": {}})
                schema = provider_tool(server, tool)
                client._schemas.append(schema)
                client._bindings[schema["name"]] = (server, tool.name, None)
        native = tool_schemas()
        inventory = [*native, *client.tool_schemas()]
        initial_inventory = list(inventory)
        client.call_tool = Mock(return_value="Repository result")
        history = [{"role": "user", "content": "Find my repositories"}]
        requests = []
        invalid = [{}, {"server": None}, {"server": []}, {"server": ""},
                   {"server": "github", "extra": True}, {"server": "missing"},
                   {"server": "notion"}, {"server": "exa"}]
        responses = iter([
            ModelResponse(tool_calls=[
                *[ToolCall(f"invalid-{index}", "load_mcp_tools", args) for index, args in enumerate(invalid)],
                ToolCall("unloaded", "mcp__github__lookup_0", {}),
                ToolCall("load-1", "load_mcp_tools", {"server": "github"}),
                ToolCall("too-early", "mcp__github__lookup_0", {}),
            ]),
            ModelResponse(tool_calls=[ToolCall("load-again", "load_mcp_tools", {"server": "github"})]),
            ModelResponse(tool_calls=[ToolCall("lookup", "mcp__github__lookup_0", {})]),
            ModelResponse("Found your repositories."),
        ])

        def complete(_messages, tools, **_kwargs):
            requests.append(tools)
            return next(responses)

        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                with patch("src.agent.skills.discover_skills", return_value=({}, [])):
                    answer = run_turn(history, complete, inventory, root, Mock(), Mock(), mcp_client=client)
                self.assertEqual(answer, "Found your repositories.")
                self.assertEqual(inventory, initial_inventory)
                self.assertEqual([tool["name"] for tool in requests[0]],
                                 [tool["name"] for tool in native] + ["load_mcp_tools"])
                self.assertIn("Repositories, issues", requests[0][-1]["description"])
                self.assertNotIn("never-send-this-secret", json.dumps(requests))
                self.assertNotIn("Detailed usage.", json.dumps(requests[0]))
                self.assertLess(len(json.dumps(requests[0])), len(json.dumps(inventory)))
                for tools in requests[1:]:
                    names = [tool["name"] for tool in tools]
                    self.assertEqual(len(names), len(set(names)))
                    self.assertEqual(len([name for name in names if name.startswith("mcp__github__")]), 40)
                    self.assertFalse(any(name.startswith("mcp__context7__") for name in names))
                outputs = {message["tool_call_id"]: message["content"]
                           for message in history if message["role"] == "tool"}
                for call_id in [*(f"invalid-{index}" for index in range(len(invalid))), "unloaded", "too-early"]:
                    self.assertTrue(outputs[call_id].startswith("Tool error:"), outputs[call_id])
                self.assertEqual(outputs["lookup"], "Repository result")
                call_ids = [call["id"] for message in history for call in message.get("tool_calls", [])]
                self.assertEqual(call_ids, list(outputs))
                client.call_tool.assert_called_once()
                self.assertEqual(client.call_tool.call_args.args, ("mcp__github__lookup_0", {}))
                self.assertIsInstance(client.call_tool.call_args.kwargs["cancel_event"], threading.Event)

                # Saved tool names do not automatically load schemas in the next user turn.
                history.append({"role": "user", "content": "Now something else"})
                fresh = Mock(return_value=ModelResponse("Hello"))
                with patch("src.agent.skills.discover_skills", return_value=({}, [])):
                    run_turn(history, fresh, inventory, root, Mock(), Mock(), mcp_client=client)
                self.assertEqual([tool["name"] for tool in fresh.call_args.args[1]],
                                 [tool["name"] for tool in native] + ["load_mcp_tools"])

                # Loading a write-capable server does not approve or execute its actions.
                denied = Mock(return_value=False)
                write = Mock(side_effect=[
                    ModelResponse(tool_calls=[ToolCall("load-linear", "load_mcp_tools", {"server": "linear"})]),
                    ModelResponse(tool_calls=[ToolCall("write", "mcp__linear__lookup_0", {})]),
                    ModelResponse("The action was denied."),
                ])
                denied_history = [{"role": "user", "content": "Change a Linear issue"}]
                run_turn(denied_history, write, inventory, root, Mock(), Mock(),
                         mcp_client=client, confirm_mcp=denied)
                denied.assert_called_once()
                self.assertIn("denied", denied_history[-1]["content"])
                self.assertEqual(client.call_tool.call_count, 1)

                # A connection lost after loading removes its schemas from the next request.
                def disconnect(_messages, tools, **_kwargs):
                    self.assertFalse(any(tool["name"].startswith("mcp__github__") for tool in tools))
                    return ModelResponse("Reconnect GitHub.")

                lost_responses = iter(["load", "disconnect"])
                def lost_complete(messages, tools, **kwargs):
                    if next(lost_responses) == "load":
                        return ModelResponse(tool_calls=[ToolCall("load-lost", "load_mcp_tools", {"server": "github"})])
                    return disconnect(messages, tools, **kwargs)

                def event(phase, call, _result):
                    if phase == "result" and call.name == "load_mcp_tools":
                        client._status_by_name["github"] = ("unavailable", "Disconnected.")

                run_turn([{"role": "user", "content": "Look up GitHub"}], lost_complete,
                         inventory, root, Mock(), Mock(), mcp_client=client, on_tool_event=event)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
