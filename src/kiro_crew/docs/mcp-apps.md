# MCP Apps

Kiro Crew renders **MCP Apps** natively: when an MCP tool returns a `ui://` resource
alongside its text, the dashboard mounts that resource as a live, interactive
component in the chat instead of showing you a wall of JSON. Ask for a diagram and
the excalidraw server gives you an editable Excalidraw canvas in the conversation;
other servers ship PDF viewers, forms, and dashboards the same way. This is the
[SEP-1865](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)
`ui` extension — Kiro Crew targets the **Stable 2026-01-26** revision — and it works
with any conforming server: nothing is hardcoded per vendor.

If you just want it working: **Developer → Shared MCP gateway → on**, then switch
your server on under **Poolable MCP servers**. The rest of this page explains why
both are needed and what to check when a render does not appear.

## Enabling it

Two gates must both pass. Neither is about the app itself — both are about **MCP
pooling**, because the gateway daemon is what intercepts the tool result and
resolves the `ui://` resource. Both have a UI toggle; you should not need to edit
config by hand.

> **Platform:** the shared gateway needs Unix-domain sockets, so it is supported on
> **macOS and Linux only**. On Windows the toggle is disabled and MCP Apps are
> unavailable.

### From the dashboard

Both live on the **Developer** page (sidebar → **Developer**):

1. **Turn on "Shared MCP gateway."** ⚠️ This restarts all active sessions onto the
   new MCP routing, so in-flight agent work is interrupted — do it between tasks,
   not mid-turn. Your dashboard stays signed in. The toggle asks for confirmation
   and offers a roll-back.
2. **Nothing else is required for apps.** "Share MCP Backends" and the poolable
   allowlist decide whether several sessions reuse one MCP server *process* —
   a resource choice, independent of whether apps render. A server that is not
   shared still renders its apps.

   A row in the allowlist is **read-only** when the server can't be shared: it's
   denylisted, its transport isn't stdio (HTTP servers aren't shared), or it's
   already opted in via its own `poolable: true` and so isn't governed by the
   allowlist.

Optionally, to render apps in the right side panel instead of inline, turn on
**Settings → Chat → Messages → "MCP Apps in Side Panel."** No restart or refresh
needed.

### The same thing in config

For scripted or headless setups:

```json
{
  "mcp_gateway": { "apps_enabled": true },
  "dashboard":   { "mcp_app_panel": true }
}
```

`apps_enabled` defaults to `true`, so an untouched config already renders apps.
Backend sharing is a separate, opt-in decision:

```json
{ "mcp_gateway": { "enabled": true, "poolable_servers": ["excalidraw"] } }
```

A server can also opt itself in from its own MCP entry, which is the escape hatch
for third-party configs you don't want to duplicate into the allowlist:

```json
{ "mcpServers": { "excalidraw": { "command": "...", "poolable": true } } }
```

MCP Apps have their own switch, `mcp_gateway.apps_enabled`, independent of
backend sharing. The full resolution order in `_mcp_apps_enabled()` is:

| Condition | Result |
|---|---|
| `KIROCREW_MCP_APPS` = `0`/`false`/`no`/`off` | disabled (explicit kill-switch, wins over everything) |
| `KIROCREW_MCP_APPS` = `1`/`true`/`yes` | enabled (explicit override — tests, e2e harness) |
| `KIROCREW_MCP_APPS` unset | follows `mcp_gateway.apps_enabled`, read **live** from config |

Read live per call, so toggling the feature takes effect without restarting the
daemon.

### Worked example: excalidraw diagrams

The excalidraw MCP server ships a `ui://` app, so it is the quickest way to see
this working end to end:

1. Add the server to your MCP config as usual and confirm the agent can call it.
2. Ask for a diagram: *"draw me a sequence diagram of the login flow."*

You should get a live, editable Excalidraw canvas in the chat — hand-drawn shapes
that animate in as they stream, which you can then drag around and edit.

**If you get a wall of JSON-ish text instead:** nothing intercepted the result.
Check `KIROCREW_MCP_APPS` is not set to an off value and that
`mcp_gateway.apps_enabled` is on — those are the only two switches that govern
rendering. There is no error message when a result goes un-intercepted, which is
what makes it confusing: the tool still worked, you just got its text.

**Sharing is a separate question.** A shared backend serves several sessions from
one process, so a server that reads per-session credentials or env vars from its
own process environment should stay unshared. That choice does not affect whether
its apps render.

## Where apps render: inline or side panel

| | Inline (default) | Side panel (`mcp_app_panel: true`) |
|---|---|---|
| Where | in the chat bubble, capped to the content column | its own **MCP App** tab in the right panel |
| Width | `--mc-content-width` (follows your Chat content-width setting; 900px fallback) | the panel's own resizable width |
| Survives scrolling | the transcript is virtualized, so the row can unmount | the panel is not virtualized |
| Chat bubble shows | the app | `▸ Opened in the side panel` (click to focus/reopen) |

The flag picks the destination **at render time**, so flipping it does not move
diagrams already in your scrollback — render a new one.

## The `app` panel tab — a docking type with its own rules

Side-panel hosting is not "the app, but in a tab." It is a **new tab kind** whose
lifecycle differs from every other tab in the panel, because of one constraint:

> An MCP App iframe is null-origin and sandboxed with no storage. **Unmounting it
> reloads the app.** The rendered diagram comes back (it is rebuilt from the stored
> payload) but anything the user drew on the canvas does not. Moving the iframe in
> the DOM counts as unmounting — an element cannot be re-parented without
> destroying its document, and a React portal is not an escape hatch, because it
> changes the real DOM parent too.

Every rule below exists to avoid that.

**It is a `TabKind`, deliberately not a `ViewKind`.** The panel's category views
(Files, Changes, Logs…) unmount when you switch away from them — that is fine for a
file list and fatal for a live app. App tabs take the keep-mounted path that
terminal and document tabs use, shown and hidden with `display`.

**It auto-opens, once.** A render opens the panel and focuses a new **MCP App**
tab. The claim is made at most once per (chat, tool call) and is held at module
scope, so returning to the chat does not re-open a tab you closed. If you close it,
the bubble's `▸ Opened in the side panel` control is the way back — it re-creates
and focuses the tab, rebuilding the frame from the stored payload.

**Closing the panel hides it; it does not unmount it.** So does opening the find
pane. A live app tab keeps the panel subtree mounted regardless of those, and only
visibility yields. With no app tab present, both behave exactly as before.

**Frames from other chats stay mounted.** All app frames across all chat slots are
rendered from one list, each keyed by *its own* slot plus tab id, with only the
active chat's active tab visible. Keying this way is what lets a frame survive a
chat switch: its key never changes, so React never remounts it. (Tool-call ids are
unique per session, not globally — hence the slot in the key.)

**App tabs are never persisted.** Unlike file or terminal tabs, they are stripped
from the saved tab strip, because the payload arrives only on a live render event
and is never written to storage — a restored tab could show nothing. A page reload
therefore starts clean, and auto-open re-arms.

**The warm set is bounded.** Each app tab holds a live multi-MB iframe, so a chat
keeps at most three, evicting the least-recently-used one (never the tab you are
looking at). Eviction is recoverable: the payload survives, so the bubble control
rebuilds the frame — only in-canvas edits are lost.

### Known limits

Two paths still lose in-canvas edits, by design rather than oversight:

- **Crossing the mobile breakpoint.** The panel is portaled into the desktop
  activity-bar grid column and rendered inline on mobile — two different React
  trees, so the crossing remounts the frame.
- **Navigating away from Chat and back** (e.g. to Settings). The panel's tree is
  owned by the chat page, so leaving the route unmounts it.

In both cases the diagram returns; the canvas edits do not. Closing that class
properly means hosting app frames above the router, which is not done yet.



## What a server must do to get content rendered

Nothing specific to Kiro Crew. Conform to SEP-1865:

1. **Serve a `ui://` resource** — the app's HTML, returned from `resources/read`.
2. **Associate it with the tool.** Either form works:
   - on the **tool definition**: `_meta.ui.resourceUri` (SEP-1865's primary form —
     preferred, and what Kiro Crew harvests at backend spawn), or
   - on the **tool result**: `result._meta.ui.resourceUri` per call.

   The deprecated flat key `_meta["ui/resourceUri"]` is also read, for
   compatibility. Only a string beginning `ui://` is eligible.

The per-result `_meta` is optional and some servers omit it, which is why
Kiro Crew harvests declared URIs from `tools/list` when the backend starts — a
tool that declared a `ui://` resource renders even when its individual results
carry no `_meta`.

## Deviations from SEP-1865

Kiro Crew targets the Stable 2026-01-26 revision. Two things an app author should
know, because a spec-conforming app may otherwise wait for something that never
arrives.

**No sandbox proxy — the frame is null-origin instead.** The spec requires a web
host to wrap the view in an intermediate *sandbox proxy* at a different origin
(`allow-scripts allow-same-origin` on the outer frame) and to hand the HTML over
via a `ui/notifications/sandbox-proxy-ready` → `ui/notifications/sandbox-resource-ready`
handshake. Kiro Crew does not do this. It renders app HTML in a **single
null-origin iframe** — `sandbox="allow-scripts allow-forms"`, deliberately
without `allow-same-origin` — with the CSP injected as a `<meta>` element ahead
of any server-supplied byte.

This is a deliberate trade, not an oversight: the spec's proxy arrangement gives
the *inner* frame `allow-same-origin` relative to the proxy origin, whereas a
null-origin frame has no origin to share at all. The consequences for an app:

- The two `sandbox-*` notifications are never sent and never answered. Do not
  wait for them; the `ui/initialize` handshake is the only entry point.
- There is no stable per-app origin, so `_meta.ui.domain` has no effect. Anything
  keyed to an origin — OAuth callbacks, CORS allowlists, API-key origin pinning —
  will not work. Cookies and `localStorage` are unavailable for the same reason.
- Because the frame has no storage, unmounting it loses in-canvas state. That is
  why the panel goes to such lengths to keep frames mounted (see above).

**Methods this host does not implement yet.** A conforming app must tolerate
these being absent, per the spec's own graceful-degradation rule:

| Method | Status |
|---|---|
| `ui/message` | not implemented |
| `ui/update-model-context` | answered `-32601` |
| `ui/resource-teardown` | not sent |
| `ui/notifications/tool-cancelled` | not sent |
| app-initiated `resources/read`, `ping` | not answered |
| `pip` display mode | not offered (`availableDisplayModes` is `inline`, `fullscreen`) |

`HostContext` carries `theme`, `displayMode`, `availableDisplayModes` and
`containerDimensions`. The spec's `styles.variables` theming channel is not sent,
so an app should declare its own fallbacks for every CSS variable it consumes and
key off `theme` for light/dark.

Everything in the spec's `draft` revision — app-provided tools,
`sampling/createMessage`, `ui/download-file` — is out of scope until that revision
stabilises.

## How a render actually reaches your screen

Useful when something renders as text and you need to find where the chain broke:

1. The model calls the tool. The gateway sees the `tools/call` result.
2. If the result (or the tool's definition) names a `ui://` resource, the gateway
   issues its own out-of-band `resources/read` to fetch it. **Best-effort with a
   10s deadline** — on timeout the original tool result is delivered unmodified,
   so a slow app degrades to text rather than wedging the turn.
3. The gateway writes the payload to a spool file at
   `$KIROCREW_HOME/mcp-apps/<uuid4hex>.json` and injects an opaque marker
   `[kirocrew-mcp-app:<uuid4hex>]` into the tool result *text*.
4. That text reaches the dashboard backend as a tool result. `mcp_apps_render.py`
   detects the marker, loads the spooled payload, pushes an `mcp_app_render`
   websocket event to the chat slot, and strips the marker from the transcript.
5. The frontend mounts the payload's HTML in a sandboxed iframe and completes the
   `ui/initialize` handshake with the app.

## Security posture

Worth understanding before you point Kiro Crew at an unfamiliar server, because
app HTML is **server-controlled code running in your dashboard**.

- **The iframe is `sandbox="allow-scripts allow-forms"`** — deliberately *without*
  `allow-same-origin`. The app is null-origin: no access to your dashboard's DOM,
  cookies, or storage.
- **The spool id is validated `^[0-9a-f]{32}$`** and the path is built only from
  that validated id, so a malicious id cannot traverse the filesystem. The model
  never reads the payload file — only deterministic code does.
- **Missing, corrupt, or oversized spool files are tolerated**, so a bad payload
  cannot crash a turn.
- **The dashboard CSP allows `https://esm.sh`.** `srcdoc` iframes inherit the
  parent's CSP header, and apps commonly load their module graph from esm.sh via
  importmap — without that allowance the app's scripts never execute and you get a
  blank frame.
- The host declares a limited capability set to the app (`serverTools`,
  `openLinks`). Link opening is gated to `https://` only.

## Troubleshooting

| Symptom | Most likely cause |
|---|---|
| Tool output renders as plain text | `mcp_gateway.apps_enabled` is off, or `KIROCREW_MCP_APPS` is set to an off value and is overriding it |
| Still text with both of those right | the broker did not start — check the gateway log for `mcp-gateway: broker ready`, which names the switch that started it |
| The gateway toggle is disabled / greyed out | you're on Windows — the broker needs Unix-domain sockets (macOS and Linux only) |
| A server's sharing row won't toggle | it's denylisted, or not stdio transport (HTTP servers can't be shared), or already opted in via its own `poolable: true` |
| Frame mounts but the canvas is blank | the app's scripts did not execute — check the browser console for CSP or network errors reaching its CDN |
| Feature toggle missing from Settings | stale frontend bundle — hard-refresh the dashboard |
| A new render appears inline despite `mcp_app_panel: true` | the flag is read at render time; diagrams already in scrollback do not move |
| Panel shows "This app render is no longer available" | the payload was evicted (bounded per slot) — ask the agent to render it again |
| Agent sessions all restarted unexpectedly | expected: flipping the **Shared MCP gateway** toggle re-routes MCP and interrupts in-flight work |

For **which** iframe host a new dashboard feature should use, and why an iframe
can never be moved in the DOM without reloading it, see
[Dashboard iframe hosts](dashboard-iframe-hosts.md).
