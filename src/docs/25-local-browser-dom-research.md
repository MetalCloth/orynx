# Local browser DOM access: research and decision

## Current decision

Oryn uses its existing **Playwright MCP** for the user's local Brave session. Oryn's model stays the only task planner. The Browser Use MCP prototype was removed from the active path after the user selected Playwright. The follow-up chapter, [Local Brave browser through Playwright MCP](26-local-brave-dom.md), documents the current configuration.

Playwright MCP's `browser_snapshot` returns a structured accessibility snapshot with roles, names, visible text, and fresh element references. Its interaction tools can act on those references and return updated page state. This is the direct tool surface Oryn needs; it does not require embedding Playwright's test runner or another agent. See the official [snapshot guide](https://playwright.dev/mcp/snapshots) and [interaction guide](https://playwright.dev/mcp/tools/interaction).

The snapshot describes the **web page**, not Linux AT-SPI or Brave's native menus. Native browser chrome, other apps, and visual-only questions continue to use Oryn's existing desktop tools. Playwright's optional vision tools can supplement the tree for canvas or visual ambiguity, but Oryn's MCP adapter currently drops image blocks, so those tools remain hidden.

## Why Playwright MCP rather than Browser Use

Browser Use's reputation is mainly about the full autonomous agent and its cloud browser. Its local CLI MCP is a flexible persistent Python workspace over CDP, while Playwright MCP supplies structured snapshots and browser actions. In Oryn we want Oryn's existing model to plan and prefer a bounded tool interface, so Playwright MCP is a better fit. We are not using Browser Use's own planner or cloud features.

Both tools can connect to a logged-in Chromium browser over CDP. Both need Brave's remote-debugging permission, and neither can make Brave's confirmation dialog permanent. Browser Use's first-party comparison reports no significant winner in its 100-task benchmark; it used one model and a shared cloud browser, so that result does not establish which tool works better with this local Brave profile. See [Browser Harness benchmark and method](https://github.com/browser-use/browser-harness/blob/main/README.md).

## Local connection design

The Playwright MCP server is launched through [the Brave endpoint launcher](../mcp/playwright_brave.py). It reads Brave's current `DevToolsActivePort` and passes its loopback WebSocket endpoint to a pinned `@playwright/mcp` release. Reading the WebSocket path avoids relying on `/json/version`, which this Brave currently answers with HTTP 404.

The launcher fails closed if the endpoint is absent or malformed. It must not start an isolated, headless, or temporary browser. Reconnecting or restarting Oryn creates a new browser-debugging connection and may show Brave's **Allow remote debugging?** dialog again. Within one Oryn process, the MCP server is persistent and should be reused across calls; do not create one server per action.

## Oryn integration boundaries

- The existing `playwright` setting remains the opt-in switch. The isolated Playwright setup is replaced by the local Brave attachment.
- `MCPClient.tool_environment()` marks Playwright as `local_desktop`, so browser actions share the task's local route with CUA and cannot silently substitute Firecrawl's remote browser.
- Browser navigation to private or unverified addresses remains guarded by the existing URL policy.
- Browser interactions are not automatically retried after uncertain results. Read fresh state before deciding whether an action needs repair.
- Browser snapshots are page content and may contain hostile instructions. Treat them as untrusted data.
- Firecrawl remains the remote browser route. CUA and Dotool remain responsible for native menus, browser chrome, and non-browser applications.

## Validation status

Unit tests cover the endpoint-file parser, fail-closed behavior, MCP preset, route classification, and action approvals. No live Playwright connection has been made after this configuration change. The next live check should be performed once through Oryn's long-lived MCP connection and read only the existing tab's snapshot; do not navigate or alter page content merely to prove attachment.

The earlier Browser Use temporary-browser test is not evidence for this Playwright connection. Browser Use diagnostic attempts created short-lived connections and caused repeated Brave approval prompts; do not repeat that pattern. The Playwright server must be kept alive across calls, and approval may be needed again after a deliberate reconnect or Oryn restart.
