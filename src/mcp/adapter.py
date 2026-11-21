"""Convert MCP tool descriptions and results to Oryn's text tool format."""

import json
import re
from typing import Any


_MODEL_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ARIA_HIDDEN_NODE = re.compile(
    r"""\[\s*aria-hidden(?:\s*=\s*(?:"true"|'true'|true))?\s*\]""",
    re.IGNORECASE,
)
_SNAPSHOT_HEADING = re.compile(r"^### Snapshot(?:\s|$)")
_SECTION_HEADING = re.compile(r"^###\s+")
_OPEN_TABS_HEADING = "### Open tabs"
_CODE_FENCE = "```"


def has_linked_snapshot(text: str) -> bool:
    """Recognize the pinned MCP's snapshot artifact section."""
    return re.search(
        r"^### Snapshot\r?\n- \[Snapshot\]\([^\r\n]+\)(?:\r?\n|$)",
        text,
        re.MULTILINE,
    ) is not None


def mcp_loader_tool(directory: list[dict[str, Any]]) -> dict[str, Any]:
    """Advertise a compact service directory instead of every MCP function schema."""
    return {
        "type": "function",
        "name": "load_mcp_tools",
        "description": (
            "Load a connected MCP server's full tool definitions when its capabilities "
            "are needed for the user's task. This only loads definitions; it does not run "
            "an external action. Returns the loaded tool names; their full argument schemas "
            "will be available in the next model request and for the rest of this user turn. "
            "Load before calling mcp__ tools, even if their names appear in chat history. "
            "Use exact names and arguments from the loaded definitions. Disabled or "
            "unavailable servers must first be configured by the user in /mcps. "
            "MCP directory: " + json.dumps(directory, ensure_ascii=False, separators=(",", ":"))
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "server": {"type": "string", "enum": [entry["server"] for entry in directory],
                           "description": "Exact MCP server name from the directory, e.g. github."},
            },
            "required": ["server"],
            "additionalProperties": False,
        },
        "strict": True,
    }


def provider_tool_name(server_name: str, tool_name: str) -> str:
    name = f"mcp__{server_name}__{tool_name}"
    if not _MODEL_TOOL_NAME.fullmatch(name):
        raise ValueError("MCP tool name cannot be represented by the model API.")
    return name


def provider_tool(server_name: str, tool: Any) -> dict[str, Any]:
    schema = tool.input_schema
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(f"MCP tool '{tool.name}' has an unsupported input schema.")
    description = tool.description or tool.title or f"Call {tool.name} on the {server_name} MCP server."
    if server_name == "playwright" and tool.name == "browser_snapshot":
        description += (
            "\n\nOryn omits aria-hidden nodes from an unscoped snapshot. Set target to a "
            "CSS selector or element reference to inspect a specific subtree; targeted "
            "snapshots preserve aria-hidden nodes."
        )
    return {
        "type": "function",
        "name": provider_tool_name(server_name, tool.name),
        "description": description[:4_000],
        "parameters": schema,
        "strict": False,
    }


def _remove_open_tabs_section(text: str) -> str:
    """Drop only Playwright's leading tab preamble, retaining page-provided text."""
    lines = text.splitlines(keepends=True)
    start = next(
        (index for index, line in enumerate(lines) if line.strip()),
        len(lines),
    )
    if start == len(lines) or lines[start].rstrip("\r\n") != _OPEN_TABS_HEADING:
        return text
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if _SECTION_HEADING.match(lines[index].rstrip("\r\n"))
        ),
        len(lines),
    )
    return "".join(lines[:start] + lines[end:])


def _has_aria_hidden_attribute(line: str) -> bool:
    """Match snapshot attributes outside quoted role names and YAML text values."""
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote == '"':
            if char == "\\":
                index += 2
                continue
            if char == '"':
                quote = None
        elif quote == "'":
            if char == "'":
                if index + 1 < len(line) and line[index + 1] == "'":
                    index += 2
                    continue
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == ":":
            # In Playwright snapshots, a colon ends the node's role/attribute header;
            # anything following it is a YAML text value, not node metadata.
            return False
        elif char == "[" and _ARIA_HIDDEN_NODE.match(line, index):
            return True
        index += 1
    return False


def _filter_aria_hidden_yaml(lines: list[str], *, fenced: bool = True) -> list[str]:
    """Remove marked YAML nodes and their indented descendants from a snapshot."""
    output: list[str] = []
    in_yaml = not fenced
    hidden_indent: int | None = None
    yaml_fences = {f"{_CODE_FENCE}yaml", f"{_CODE_FENCE}yml"}
    for line in lines:
        content = line.rstrip("\r\n")
        stripped = content.strip()
        if not in_yaml:
            if stripped in yaml_fences:
                in_yaml = True
            output.append(line)
            continue
        if fenced and stripped == _CODE_FENCE:
            in_yaml = False
            hidden_indent = None
            output.append(line)
            continue
        if hidden_indent is not None:
            if not stripped:
                continue
            indent = len(content) - len(content.lstrip(" "))
            if indent > hidden_indent:
                continue
            hidden_indent = None
        if content.lstrip().startswith("- ") and _has_aria_hidden_attribute(content):
            hidden_indent = len(content) - len(content.lstrip(" "))
            continue
        output.append(line)
    return output


def _filter_find_result(text: str) -> str:
    """Remove hidden nodes from browser_find's unfenced snapshot-tree fragment."""
    lines = text.splitlines(keepends=True)
    tree_start = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("- ")),
        len(lines),
    )
    if tree_start == len(lines):
        return text
    return "".join(
        lines[:tree_start] + _filter_aria_hidden_yaml(lines[tree_start:], fenced=False)
    )


def _filter_snapshot_sections(text: str) -> str:
    """Filter only YAML payloads under Playwright snapshot headings."""
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        content = lines[index].rstrip("\r\n")
        output.append(lines[index])
        index += 1
        if not _SNAPSHOT_HEADING.match(content):
            continue
        section: list[str] = []
        while index < len(lines):
            content = lines[index].rstrip("\r\n")
            if _SECTION_HEADING.match(content):
                break
            section.append(lines[index])
            index += 1
        output.extend(_filter_aria_hidden_yaml(section))
    return "".join(output)


def result_text(
    result: Any,
    *,
    server_name: str | None = None,
    tool_name: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> str:
    parts = [
        block.text for block in (result.content or [])
        if isinstance(getattr(block, "text", None), str)
    ]
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False, default=str))
    if not parts:
        parts.append("MCP tool returned no readable text.")
    text = "\n".join(parts)
    if result.is_error:
        return f"MCP server reported a tool error:\n{text}"
    if server_name == "playwright":
        if tool_name == "browser_tabs":
            return text
        text = _remove_open_tabs_section(text)
        is_scoped_snapshot = (
            tool_name == "browser_snapshot"
            and isinstance(arguments, dict)
            and bool(arguments.get("target"))
        )
        if not is_scoped_snapshot:
            text = _filter_snapshot_sections(text)
            if tool_name == "browser_find":
                text = _filter_find_result(text)
    return text
