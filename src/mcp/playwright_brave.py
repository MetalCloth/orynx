"""Launch Playwright MCP against the already-running Brave profile only."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


PLAYWRIGHT_MCP_VERSION = "0.0.85"


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
    try:
        endpoint = brave_cdp_endpoint()
    except RuntimeError as exc:
        print(f"Playwright local Brave connection unavailable: {exc}", file=sys.stderr)
        return 2

    npx = shutil.which("npx")
    if npx is None:
        print("Playwright local Brave connection unavailable: npx is not installed.", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["PLAYWRIGHT_MCP_CDP_ENDPOINT"] = endpoint
    os.execvpe(npx, [npx, "--yes", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}"], env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
