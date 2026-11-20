# Local Brave browser through Playwright MCP

Oryn's `playwright` MCP now targets the user's existing Brave profile. Oryn's model remains the planner; Playwright MCP provides browser observations and actions. Firecrawl remains a separate remote browser, and `computer_*` remains the path for native desktop controls.

## What Oryn sees

`browser_snapshot` returns a structured accessibility snapshot of the current web page, with roles, accessible names, visible text, and fresh references such as `button "Search" [ref=e8]`. Oryn can pass that reference to a Playwright action. Page-changing actions return updated page state; references are only valid for the snapshot they came from.

This is the browser page's accessibility representation, not Linux AT-SPI. It does not describe Brave's menus or other desktop apps. Playwright can also report CSS-pixel boxes. Screenshot tools remain hidden because Oryn's MCP adapter currently drops image blocks; use the existing desktop screenshot path for visual questions about the visible tab.

Official references: [Playwright snapshots](https://playwright.dev/mcp/snapshots), [interaction tools](https://playwright.dev/mcp/tools/interaction), and [vision mode](https://playwright.dev/mcp/vision-mode).

## How Oryn connects

The local launcher reads Brave's current `DevToolsActivePort` file under `${XDG_CONFIG_HOME:-~/.config}/BraveSoftware/Brave-Browser`. When Playwright is enabled, Oryn starts one user-level systemd service, `oryn-playwright-bridge.service`, running the pinned `@playwright/mcp@0.0.85` HTTP server at `http://127.0.0.1:8931/mcp`. A small anchor client stays connected inside the service and keeps its shared CDP browser alive when Oryn exits or restarts. On Oryn's next start, it reuses that service if the Brave endpoint still matches, instead of opening another browser-debugging connection.

If Brave's endpoint is missing or its TCP listener is unavailable, Oryn refuses to start the service. If Brave restarts and the endpoint changes, Oryn stops the old service and starts one replacement; Brave may ask for approval for that new connection. This does not launch a headless, isolated, or temporary browser. The endpoint file is read directly because this Brave currently answers `/json/version` with HTTP 404.

Brave's **Allow remote debugging?** dialog grants broad access to the browser session. It is still per browser-debugging connection; the persistent anchor is intended to keep that connection open across Oryn restarts. Normal Oryn exit does not stop this service. Turning Playwright off in Oryn stops it. The service makes one read-only tab-list request when it starts to establish the shared browser session, then holds that client open; it does not navigate or change a page.

The HTTP MCP endpoint binds only to loopback and requires a random token in the Authorization header. The token is stored in `~/.mini-hermes/playwright-bridge-token` with mode 0600, reused across Oryn restarts, rotated when the service is replaced, and removed when disabled. Missing or wrong tokens receive HTTP 401; browser Origin headers are rejected. Programs running as the same Linux user can read the token, so this is not isolation from your own account. The HTTP `connected` status means Oryn reached this MCP service; the service's first browser operation may still trigger Brave's approval dialog. Inspect or stop the service with:

```bash
systemctl --user status oryn-playwright-bridge.service
journalctl --user -u oryn-playwright-bridge.service
systemctl --user stop oryn-playwright-bridge.service
```

## Oryn's tool boundaries

- The `playwright` preset is the local Brave route and is enabled through Oryn's existing MCP setting.
- `browser_snapshot`, `browser_find`, tab reads, and selected diagnostics are read-only; actions such as clicking and typing require Oryn approval.
- Private or unverified navigation targets remain guarded by the existing URL policy.
- Browser Use is not used. The native `browser_*` tools backed by Firecrawl remain separate from this local Brave session.
- Local browser calls bind the turn to the local desktop route. They are not automatically replayed if a result is uncertain.
- Browser text is untrusted page content, not instructions to Oryn.

## Validation status

Validation covers endpoint parsing, service reuse across Oryn restarts, one-time replacement after Brave restarts, refusing unknown listeners, and the HTTP route. Live checks confirmed that Oryn loaded Playwright tools after restarting, read tab and DOM data from the existing Brave profile, and that the service's anchor kept the Brave CDP socket established while Oryn connected. The browser snapshot exposed the Chrome remote-debugging page's checked setting and loopback listener; the Netflix snapshot was sparse. No page settings were changed.

The first reconnect after installing authentication replaces the older unprotected service once. This can trigger one new Brave consent prompt. Normal Oryn reconnects then reuse the authenticated service.

Authentication validation confirmed HTTP 401 for missing/wrong tokens, HTTP 403 for browser Origin headers, and successful MCP discovery of 22 tools with the token. An earlier replacement connection timed out while opening Brave's debugging WebSocket. The anchor rejects the adapter's `MCP server reported a tool error:` prefix as failure instead of logging a false connection success. It does not retry automatically.

The follow-up live check on 2026-11-20 succeeded through the authenticated bridge: Oryn listed 10 existing Brave tabs and read the current page's accessibility snapshot. After that client exited, a separate Python process connected and repeated both reads successfully. The bridge retained the same service PID and credential after both clients disconnected. The earlier timeout did not reproduce, so its cause remains unconfirmed. These checks did not navigate, switch tabs, click, type, or change page settings; page-changing actions and a Brave restart were not tested in this follow-up.

The user subsequently confirmed that repeated Allow prompts appeared inside Brave. A follow-up checked the actual established debugging socket, rather than only the bridge PID: its local port, Node process, and file descriptor remained unchanged through six tab reads across three MCP clients, including after each client closed. A separate check ran three consecutive conversation-loop turns with scripted model responses and real MCP tab reads; the same socket remained established through all three turns and after the client closed. Socket polling during the first check observed no changes at 100 ms intervals. No browser restart or bridge replacement was performed.

At the user's request, the check was repeated while they watched Brave: three conversation-loop turns with scripted model responses and real tab reads, followed by a tab read from a fresh MCP client, all succeeded. The original debugging socket remained established throughout, with no socket changes observed at 100 ms intervals. The user confirmed that no Allow popup appeared. Reuse across turns and client reconnects is therefore verified for this running Brave session. The cause of the earlier repeated prompts remains unknown; approval behavior after a Brave restart or bridge replacement remains untested in this follow-up.

The full Python suite passed all 277 tests, including the bridge lifecycle and HTTP authentication checks; all six offline evaluations passed. The hosted-preset test now excludes the local Playwright HTTP connection when counting the nine hosted services.
