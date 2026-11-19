"""Oryn's small, known-good MCP server presets."""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from src.mcp.playwright_brave import brave_cdp_endpoint
from src.security.secrets import local_secret


SERVER_NAMES = (
    "github", "context7", "microsoft_learn", "huggingface", "tavily",
    "firecrawl", "exa", "linear", "notion", "playwright",
)
PLAYWRIGHT_SERVERS = frozenset({"playwright"})
_DEFAULT_ENABLED: set[str] = set()
_LEGACY_SERVERS = {"context7", "github", "playwright"}


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict, repr=False)
    disabled_reason: str = ""
    enabled: bool = True
    access: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    oauth: bool = False


def mcp_settings_path() -> Path:
    return Path.home() / ".mini-hermes" / "mcp-settings.json"


def load_enabled_servers(path: Path | None = None) -> set[str]:
    """Load explicit user MCP switches; never connect to a server by default."""
    try:
        data = json.loads((path or mcp_settings_path()).read_text(encoding="utf-8"))
        enabled = data["enabled_servers"]
        if not isinstance(enabled, list) or any(not isinstance(name, str) for name in enabled):
            return set(_DEFAULT_ENABLED)
        enabled = set(enabled) & set(SERVER_NAMES)
        known = data.get("known_servers", list(_LEGACY_SERVERS))
        if not isinstance(known, list) or any(not isinstance(name, str) for name in known):
            return set(_DEFAULT_ENABLED)
        known = set(known) & set(SERVER_NAMES)
        if not enabled <= known:
            return set(_DEFAULT_ENABLED)
        return enabled | (_DEFAULT_ENABLED - known)
    except (OSError, ValueError, KeyError, TypeError):
        return set(_DEFAULT_ENABLED)


def save_enabled_servers(enabled: set[str], path: Path | None = None) -> None:
    """Atomically save non-secret server preferences with owner-only file access."""
    if not enabled <= set(SERVER_NAMES):
        raise ValueError("Unknown MCP server.")
    target = path or mcp_settings_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as settings:
            settings.write(json.dumps({
                "enabled_servers": sorted(enabled), "known_servers": list(SERVER_NAMES),
            }) + "\n")
            settings.flush()
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def server_configs(enabled_servers: set[str] | None = None) -> list[MCPServerConfig]:
    """Use official hosted endpoints and Playwright controls for the user's local Brave."""
    enabled = load_enabled_servers() if enabled_servers is None else enabled_servers
    if not enabled <= set(SERVER_NAMES):
        raise ValueError("Unknown MCP server.")
    try:
        brave_cdp_endpoint()
        playwright_disabled_reason = ""
    except RuntimeError as exc:
        playwright_disabled_reason = str(exc)
    # Credentials stay in request headers, never URLs or subprocess arguments.
    presets = (
        ("github", "https://api.githubcopilot.com/mcp/readonly", "GITHUB_PERSONAL_ACCESS_TOKEN",
         "Read-only repository, issue, and pull-request access.", True),
        ("context7", "https://mcp.context7.com/mcp", "CONTEXT7_API_KEY",
         "Current library documentation and code examples.", False),
        ("microsoft_learn", "https://learn.microsoft.com/api/mcp", "",
         "Public Microsoft documentation and code samples.", False),
        ("huggingface", "https://huggingface.co/mcp", "HF_TOKEN",
         "Models, datasets, papers, and configured Hub tools; changes need approval.", False),
        ("tavily", "https://mcp.tavily.com/mcp/", "TAVILY_API_KEY",
         "Web search, extraction, mapping, and crawling.", True),
        ("firecrawl", "https://mcp.firecrawl.dev/v2/mcp", "FIRECRAWL_API_KEY",
         "Web search, scraping, and crawling; limited tools work without a key.", False),
        ("exa", "https://mcp.exa.ai/mcp", "EXA_API_KEY",
         "Web search and page reading; basic tools work without a key.", False),
        ("linear", "https://mcp.linear.app/mcp", "LINEAR_API_KEY",
         "Issues, projects, and comments; changes need approval.", True),
        ("notion", "https://mcp.notion.com/mcp", "NOTION_ACCESS_TOKEN",
         "Workspace pages and databases; changes need approval.", False),
    )
    configs = []
    for name, url, key_name, access, required in presets:
        key = local_secret(key_name, "mcp.env") if key_name else ""
        if not key and name in {"tavily", "firecrawl"}:
            key = local_secret(key_name, f"{name}.env")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if name == "exa" and key:
            headers = {"x-api-key": key}
        if name == "github":
            headers.update({"X-MCP-Toolsets": "repos,issues,pull_requests", "X-MCP-Readonly": "true"})
        reason = f"Add {key_name} to ~/.mini-hermes/mcp.env." if required and not key else ""
        oauth = name == "notion" and not key
        if oauth:
            from src.mcp.oauth import NotionTokenStorage
            if not NotionTokenStorage().has_tokens():
                reason = "Sign in with ./oryn mcp login notion, then restart Oryn."
        configs.append(MCPServerConfig(
            name, url=url, headers=headers, oauth=oauth,
            enabled=name in enabled, disabled_reason=reason, access=access,
        ))
    return [
        *configs,
        MCPServerConfig(
            "playwright", sys.executable,
            (str(Path(__file__).with_name("playwright_brave.py")),),
            {},
            disabled_reason=playwright_disabled_reason,
            enabled="playwright" in enabled,
            access=("Playwright MCP controls for the user's existing Brave session via its approved CDP endpoint. "
                    "Uses accessibility snapshots and fresh element references. If Brave's endpoint is unavailable, "
                    "the local server fails closed instead of starting a separate browser."),
        ),
    ]
