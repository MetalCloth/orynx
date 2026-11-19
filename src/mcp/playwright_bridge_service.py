"""Run the Playwright HTTP server plus one persistent MCP client that anchors Brave."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.mcp.discovery import MCPServerConfig
from src.mcp.playwright_brave import PLAYWRIGHT_BRIDGE_ENDPOINT_FILE, url_port
from src.mcp.playwright_bridge import BRIDGE_URL


def _wait_for_http_server(process: subprocess.Popen[bytes], timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", 8931), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _connect_anchor(stop: threading.Event):
    from src.mcp.client import MCPClient

    anchor = MCPClient([MCPServerConfig("playwright", url=BRIDGE_URL)])
    try:
        anchor.start()
        if anchor.diagnostic_snapshot()["connected_count"] != 1:
            raise RuntimeError("the HTTP MCP client did not connect")
        anchor.tool_schemas("playwright")
        result = anchor.call_tool(
            "mcp__playwright__browser_tabs", {"action": "list"}, cancel_event=stop,
        )
        if result.lstrip().startswith(("### Error", "Tool error:")):
            raise RuntimeError("the initial read-only browser tab check failed")
        return anchor
    except Exception:
        anchor.close()
        raise


def main() -> int:
    try:
        endpoint = PLAYWRIGHT_BRIDGE_ENDPOINT_FILE.read_text(encoding="utf-8").strip()
        url_port(endpoint)
    except (OSError, RuntimeError) as exc:
        print(f"Playwright bridge cannot read Brave's endpoint ({type(exc).__name__}).", file=sys.stderr)
        return 2

    server_script = Path(__file__).with_name("playwright_brave.py")
    server = subprocess.Popen(
        [sys.executable, str(server_script), "--serve-http"], env=os.environ.copy(),
    )
    stop = threading.Event()
    previous_handlers: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, lambda *_args: stop.set())

    anchor = None
    try:
        if not _wait_for_http_server(server):
            print("Playwright HTTP server did not become ready.", file=sys.stderr)
            return 3

        # Keep one MCP client connected so the server's shared CDP browser is not
        # disposed when Oryn closes and reconnects its own HTTP client.
        anchor = _connect_anchor(stop)
        if stop.is_set():
            return 0
        print("Persistent Playwright anchor connected to Brave.", flush=True)

        while not stop.wait(0.25):
            if server.poll() is not None:
                return server.returncode or 0
        return 0
    except InterruptedError:
        return 0 if stop.is_set() else 5
    except Exception as exc:
        print(f"Persistent Playwright anchor failed ({type(exc).__name__}); not retrying.", file=sys.stderr)
        return 5
    finally:
        if anchor is not None:
            anchor.close()
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=2)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
