"""Manage one Playwright MCP HTTP service that outlives Oryn restarts."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

from src.mcp.playwright_brave import (
    PLAYWRIGHT_BRIDGE_HOST,
    PLAYWRIGHT_BRIDGE_PORT,
    PLAYWRIGHT_BRIDGE_ENDPOINT_FILE,
    brave_cdp_endpoint,
    url_port,
)


BRIDGE_UNIT = "oryn-playwright-bridge.service"
BRIDGE_URL = f"http://{PLAYWRIGHT_BRIDGE_HOST}:{PLAYWRIGHT_BRIDGE_PORT}/mcp"
_STATE_PATH = PLAYWRIGHT_BRIDGE_ENDPOINT_FILE
_LOCK = threading.Lock()


def _run_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise RuntimeError("systemctl is unavailable; Oryn cannot manage the persistent Playwright service.")
    return subprocess.run(
        [systemctl, "--user", *args], capture_output=True, text=True, timeout=8, check=False,
    )


def _active() -> bool:
    result = _run_systemctl("is-active", "--quiet", BRIDGE_UNIT)
    return result.returncode == 0


def _port_accepting(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _stored_endpoint() -> str:
    try:
        return _STATE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_endpoint(endpoint: str) -> None:
    _STATE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = _STATE_PATH.with_suffix(".tmp")
    try:
        temporary.write_text(endpoint + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(_STATE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def _stop_service() -> None:
    if _active():
        result = _run_systemctl("stop", BRIDGE_UNIT)
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown systemd error"
            raise RuntimeError(f"Could not stop the persistent Playwright service: {detail[:200]}")
    _STATE_PATH.unlink(missing_ok=True)


def _start_service(endpoint: str) -> None:
    systemd_run = shutil.which("systemd-run")
    if not systemd_run:
        raise RuntimeError("systemd-run is unavailable; cannot start the persistent Playwright service.")
    script = Path(__file__).with_name("playwright_bridge_service.py")
    env = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", ""),
    }
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    command = [
        systemd_run, "--user", f"--unit={BRIDGE_UNIT}", "--collect",
        "--property=Restart=no",
    ]
    command.extend(f"--setenv={key}={value}" for key, value in env.items())
    command.extend(("--", sys.executable, str(script), "--serve-http"))
    _write_endpoint(endpoint)
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        _STATE_PATH.unlink(missing_ok=True)
        detail = result.stderr.strip() or result.stdout.strip() or "unknown systemd error"
        raise RuntimeError(f"Could not start the persistent Playwright service: {detail[:240]}")


def ensure_playwright_bridge(endpoint: str | None = None) -> str:
    """Start/reuse the one local HTTP service; do not open Brave's WebSocket here."""
    endpoint = endpoint or brave_cdp_endpoint()
    # Validate address and confirm the DevTools TCP listener exists without creating a CDP session.
    port = url_port(endpoint)
    if not _port_accepting("127.0.0.1", port):
        raise RuntimeError("Brave's remote-debugging endpoint is not listening; reopen Brave debugging, then reconnect.")

    with _LOCK:
        active = _active()
        same_session = _stored_endpoint() == endpoint
        if active and same_session and _port_accepting(PLAYWRIGHT_BRIDGE_HOST, PLAYWRIGHT_BRIDGE_PORT):
            return BRIDGE_URL
        if active:
            # This is a single deliberate replacement, e.g. after Brave restarted. No retry loop.
            _stop_service()
        elif _port_accepting(PLAYWRIGHT_BRIDGE_HOST, PLAYWRIGHT_BRIDGE_PORT):
            raise RuntimeError(
                f"Port {PLAYWRIGHT_BRIDGE_PORT} is occupied by an unknown local process; refusing to connect to it."
            )

        _start_service(endpoint)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if _port_accepting(PLAYWRIGHT_BRIDGE_HOST, PLAYWRIGHT_BRIDGE_PORT):
                if _stored_endpoint() == endpoint:
                    return BRIDGE_URL
            if not _active():
                break
            time.sleep(0.1)
        _stop_service()
        raise RuntimeError("Persistent Playwright service did not become ready; see its user journal for details.")


def stop_playwright_bridge() -> None:
    """Stop the persistent service when the user explicitly disables Playwright in Oryn."""
    with _LOCK:
        _stop_service()
