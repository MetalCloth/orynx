"""Launch Playwright MCP against the already-running Brave profile only."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import sys


PLAYWRIGHT_MCP_VERSION = "0.0.85"
PLAYWRIGHT_BRIDGE_HOST = "127.0.0.1"
PLAYWRIGHT_BRIDGE_PORT = 8931
PLAYWRIGHT_BRIDGE_ENDPOINT_FILE = Path.home() / ".mini-hermes" / "playwright-bridge-endpoint"


def brave_cdp_endpoint(profile_dir: Path | None = None) -> str:
    """Read Brave's current DevTools WebSocket endpoint without exposing its page data."""
    if profile_dir is None:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        profile_dir = config_home / "BraveSoftware" / "Brave-Browser"
    try:
        lines = (profile_dir / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(
            "Brave remote debugging is not active. Enable it in Brave, then reconnect Playwright in Oryn."
        ) from exc

    if len(lines) < 2:
        raise RuntimeError("Brave's DevToolsActivePort file is incomplete; reconnect Playwright in Oryn.")
    port, ws_path = lines[0].strip(), lines[1].strip()
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise RuntimeError("Brave's DevToolsActivePort file is invalid; reconnect Playwright in Oryn.")
    if not ws_path.startswith("/devtools/browser/"):
        raise RuntimeError("Brave's DevToolsActivePort file is invalid; reconnect Playwright in Oryn.")
    if any(char in ws_path for char in "?#"):
        raise RuntimeError("Brave's DevToolsActivePort file is invalid; reconnect Playwright in Oryn.")
    return f"ws://127.0.0.1:{port}{ws_path}"


def main() -> int:
    serve_http = sys.argv[1:] == ["--serve-http"]
    try:
        endpoint = (
            PLAYWRIGHT_BRIDGE_ENDPOINT_FILE.read_text(encoding="utf-8").strip()
            if serve_http else brave_cdp_endpoint()
        )
        if serve_http and not endpoint:
            raise RuntimeError("Oryn has not saved Brave's current debugging endpoint.")
        if serve_http:
            url_port(endpoint)
    except (OSError, RuntimeError) as exc:
        print(f"Playwright local Brave connection unavailable: {exc}", file=sys.stderr)
        return 2

    npx = shutil.which("npx")
    if npx is None:
        print("Playwright local Brave connection unavailable: npx is not installed.", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["PLAYWRIGHT_MCP_CDP_ENDPOINT"] = endpoint
    command = [npx, "--yes", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}"]
    if serve_http:
        try:
            with socket.create_connection(("127.0.0.1", url_port(endpoint)), timeout=0.5):
                pass
        except (OSError, RuntimeError, ValueError):
            print("Playwright local Brave connection unavailable: Brave's debugging endpoint is not listening.",
                  file=sys.stderr)
            return 2
        command.extend((
            "--host", PLAYWRIGHT_BRIDGE_HOST, "--port", str(PLAYWRIGHT_BRIDGE_PORT),
            "--shared-browser-context",
        ))
        # Preload the gate in the pinned CLI itself, leaving no unprotected backend port.
        gate = Path(__file__).with_name("playwright_http_auth.cjs")
        env["NODE_OPTIONS"] = f'--require="{gate}"'
        env["ORYN_PLAYWRIGHT_TOKEN_FILE"] = str(PLAYWRIGHT_BRIDGE_ENDPOINT_FILE.with_name("playwright-bridge-token"))
    os.execvpe(npx, command, env)
    return 0


def url_port(endpoint: str) -> int:
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("Brave's debugging endpoint must use loopback WebSocket transport.") from exc
    if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or port is None:
        raise RuntimeError("Brave's debugging endpoint must use loopback WebSocket transport.")
    return port


if __name__ == "__main__":
    raise SystemExit(main())
