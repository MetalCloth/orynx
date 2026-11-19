# Local Brave browser through Playwright MCP

Oryn's `playwright` MCP now targets the user's existing Brave profile. Oryn's model remains the planner; Playwright MCP provides browser observations and actions. Firecrawl remains a separate remote browser, and `computer_*` remains the path for native desktop controls.

## What Oryn sees

`browser_snapshot` returns a structured accessibility snapshot of the current web page, with roles, accessible names, visible text, and fresh references such as `button "Search" [ref=e8]`. Oryn can pass that reference to a Playwright action. Page-changing actions return updated page state; references are only valid for the snapshot they came from.

This is the browser page's accessibility representation, not Linux AT-SPI. It does not describe Brave's menus or other desktop apps. Playwright can also report CSS-pixel boxes. Screenshot tools remain hidden because Oryn's MCP adapter currently drops image blocks; use the existing desktop screenshot path for visual questions about the visible tab.

Official references: [Playwright snapshots](https://playwright.dev/mcp/snapshots), [interaction tools](https://playwright.dev/mcp/tools/interaction), and [vision mode](https://playwright.dev/mcp/vision-mode).

## How Oryn connects

The local launcher reads Brave's current `DevToolsActivePort` file under `${XDG_CONFIG_HOME:-~/.config}/BraveSoftware/Brave-Browser` and passes the resulting loopback WebSocket endpoint to the pinned `@playwright/mcp@0.0.85` process. The endpoint can change when Brave restarts, so Oryn reads it each time the MCP server starts or reconnects.

If the file is missing or malformed, the launcher exits with a clear error. It does not start a headless, isolated, or temporary browser as a fallback. Reconnect Playwright in Oryn after enabling Brave remote debugging. This design also avoids using `/json/version`, which the user's Brave currently answers with HTTP 404.

Brave's **Allow remote debugging?** dialog grants broad access to the browser session. The approval is per connection, not a permanent “always allow” setting; restarting or reconnecting the MCP can prompt again. Oryn keeps the MCP process alive across tool calls, so ordinary calls should reuse the established connection. Do not create a new MCP process for every action.

## Oryn's tool boundaries

- The `playwright` preset is the local Brave route and is enabled through Oryn's existing MCP setting.
- `browser_snapshot`, `browser_find`, tab reads, and selected diagnostics are read-only; actions such as clicking and typing require Oryn approval.
- Private or unverified navigation targets remain guarded by the existing URL policy.
- Browser Use is not used. The native `browser_*` tools backed by Firecrawl remain separate from this local Brave session.
- Local browser calls bind the turn to the local desktop route. They are not automatically replayed if a result is uncertain.
- Browser text is untrusted page content, not instructions to Oryn.

## Validation status

Unit-level validation covers the launcher reading the current endpoint, rejecting a missing or malformed endpoint, and configuring the local route. A disposable Browser Use test from the previous implementation does not validate this Playwright connection. **The Playwright MCP has not yet been confirmed against the user's live Brave tab.** The next live check should be one read-only `browser_snapshot` on the existing tab after Oryn restarts or reconnects; no navigation or page action is needed to prove attachment.
