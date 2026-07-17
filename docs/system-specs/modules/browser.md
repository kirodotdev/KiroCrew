## Browser Module

Thin auth layer for Amazon internal website browsing via Playwright MCP.

### Architecture

```
User clicks Globe → backend injects [BROWSE] → agent loads browser-auth skill
  → kirocrew browse auth health / refresh
  → Playwright MCP tools: browser_navigate, browser_click, browser_snapshot, etc.

Without Globe → agent uses ReadInternalWebsites (builder-mcp)
```

### Two Modes

| Mode | Platform | How it works |
|------|----------|--------------|
| **Extension** | macOS (recommended) | Playwright attaches to user's running Chrome via extension. All existing auth (Midway, Sentry, MCS, Kerberos) works automatically. |
| **Headless** | Linux Cloud Desktops | Launches separate Chromium with `--auth-server-allowlist` + storage state cookie injection. |

### Key Design Decisions

**Delegate browsing to Playwright MCP** — we don't implement click/fill/navigate/screenshot.
Playwright MCP handles all browser interaction. KiroCrew only handles Amazon-specific auth.

**Two auth strategies:**
- Extension mode: zero auth work — real Chrome session has everything
- Headless mode: storage state (`~/.midway/playwright-storage-state.json`) + Kerberos via `--auth-server-allowlist`

**AIM-managed installation** — `aim mcp install npm:@playwright/mcp`. Auto-installed on gateway startup if missing.

**Globe button triggers browse mode** — Backend injects `[BROWSE]` marker. Without it, agent uses `ReadInternalWebsites` (builder-mcp) instead.

### Auth Flow (Headless Mode)

1. `kirocrew browse auth health` — validates Midway cookie, Kerberos ticket, AEA posture
2. `kirocrew browse auth refresh` — converts `~/.midway/cookie` → `~/.midway/playwright-storage-state.json`
3. Playwright loads cookies from storage state at context creation (no manual injection)
4. `--auth-server-allowlist=*.amazon.com,*.a2z.com,*.aws.dev` handles SPNEGO challenges
5. For federate-gated sites: `kirocrew browse auth federate <url>` completes 4-hop SPNEGO chain via curl

### Auth Flow (Extension Mode)

1. User has Chrome open with existing auth (Midway, Sentry, MCS extension)
2. Playwright MCP connects via extension token (`PLAYWRIGHT_MCP_EXTENSION_TOKEN`)
3. All navigation uses the real authenticated session — no cookie injection needed

### Config Files

| File | Purpose |
|------|---------|
| `~/.kirocrew/playwright-config.json` | Playwright MCP config: `--auth-server-allowlist`, `storageState`, `isolated: true`, capabilities |
| `~/.midway/playwright-storage-state.json` | Playwright storage state (generated from `~/.midway/cookie`) |
| `~/.kirocrew/playwright-extension-mode` | Flag file: extension mode enabled |
| `~/.kirocrew/playwright-extension-token` | Chrome extension connection token (0o600 perms) |
| `~/.kiro/settings/mcp.json` | MCP server config (args: `--extension` or `--config`) |

### Source Files

| File | Purpose |
|------|---------|
| `browser/auth.py` | Midway cookies, federate SSO, KRB5CCNAME, health checks, URL validation |
| `browser/setup.py` | AIM install, config generation, storage state refresh, MCP config patching |
| `browser/cli.py` | `kirocrew browse` CLI: setup, auth health/refresh/inject/federate, extension on/off |
| `mcp_playwright_proxy.py` | Stdio proxy: intercepts Playwright MCP responses, compresses accessibility trees |
| `skills/browser-auth/SKILL.md` | Agent skill for auth + Playwright MCP workflow |
| `scripts/refresh-playwright-cookies.py` | Standalone script: `~/.midway/cookie` → storage state |
| `config/playwright-mcp-config.json.template` | Template for the Playwright config structure |

### Context Window Optimization (Playwright Proxy)

Playwright MCP's `browser_snapshot` returns full accessibility trees (50-100K tokens on heavy pages). The **Playwright proxy** (`kirocrew mcp-playwright-proxy`) intercepts these responses and auto-compresses them before they reach the LLM — the full tree never enters context.

**How it works:**
- kiro-cli → `kirocrew mcp-playwright-proxy` (stdio) → real `npm-playwright-mcp` (subprocess)
- Proxy forwards all messages bidirectionally
- Intercepts responses with accessibility trees (>5K chars with tree-like structure)
- Compresses to compact outline: only interactive elements (links, buttons, inputs, headings, images) with refs
- ~95% token reduction on heavy pages

**Registration:** The proxy is auto-registered in `mcp.json` via:
- `kirocrew setup` — new installs get the proxy from the start
- Gateway startup — `_migrate_playwright_to_proxy()` auto-migrates existing `npm-playwright-mcp` entries
- Settings → Browser save — `patch_mcp_extension()`/`patch_mcp_headless()` always write the proxy command

**Source:** `src/kiro_crew/mcp_playwright_proxy.py`

### Live Browse Mirror

The dashboard mirrors the headless `[BROWSE]` Chromium in near-real-time **without
opening any debug port on the browser**. The headless Chromium runs on the gateway
host; the only window onto it from a laptop is the dashboard (reachable over the
reverse SSH tunnel).

**Relay path.** The Playwright proxy already intercepts every
`browser_take_screenshot` response and re-encodes it to JPEG. It additionally
re-POSTs that already-captured frame to the gateway's loopback
`POST /api/browser/frame`, which rebroadcasts it over the existing WS as a
`browser_frame` event; the `BrowserLiveView` panel renders the latest frame. This
rides Playwright's existing authenticated, pipe-based control channel — it
deliberately does **not** add a `--remote-debugging-port`. An earlier revision
attached to a CDP debug port for smoother frames; that port was an unauthenticated,
full-control endpoint on an authenticated browser session (a net-new
local-process-takeover surface), so it was dropped (Mesh-2068).

**Frame validation (`build_frame_payload`).** A pure helper normalizes the POSTed
body into the `browser_frame` payload so the framing contract is unit-testable:
- `data` must match the base64 charset (`_B64_RE`) — this structurally excludes
  `:` (no `://` URL), whitespace, and `<`/`>` (no HTML/script), which is the right
  boundary control for browser-captured image bytes; no text redaction is applied.
- `format` must be in the `{jpeg, png, webp}` allowlist; `svg` is deliberately
  excluded because an SVG data URI can carry executable script (XSS safety).
- `session_key` is passed through only if it matches a bounded safe charset
  (`_SESSION_KEY_RE`, ≤128 chars) so the WS payload can't carry arbitrary text.

**Active pump.** Frames from agent screenshots alone are sparse, so the proxy runs
a background active pump that injects its own idle-gated `browser_take_screenshot`
into the Playwright subprocess to keep the mirror current between agent shots
(~1-3 fps). It is single-in-flight (with a timeout so a hung browser can't wedge
it), demuxes the proxy-namespaced response id (`__mc_pump_` prefix — never
forwarded to kiro or written to disk), and backs off when there are zero
subscribers (learned from the frame endpoint's response subscriber count). It is
disabled in extension mode (the user already sees their own Chrome) and gated on
recent real browse activity.

**Pump audit.** The proxy is a stdlib-only stdio subprocess and cannot reach
`sel.py`, so each pump injection is reported to loopback
`POST /api/browser/pump-audit` and the gateway emits the SEL
`browser_take_screenshot` tool-invocation audit event on the proxy's behalf,
keeping proxy-internal tool calls auditable.

**Panel.** `BrowserLiveView` is a resizable, persisted panel and is threaded with
the resolved session *title* (the raw `session_key` is only a client-side lookup
key against the dashboard's own slot store).

**Source:** `src/kiro_crew/browser/screencast.py`, `src/kiro_crew/mcp_playwright_proxy.py`

**Fallback tools** in kirocrew-core (for manual use if needed):

| Tool | Purpose |
|------|---------|
| `browse_outline` | Compress snapshot text → compact outline with refs |
| `browse_search` | Regex search snapshot text → matching lines only |

### Dashboard Integration

- **Globe button** in ChatInput toggles browse mode → sends `{ browse: true }` in POST
- **Backend** prepends `[BROWSE]` marker to message when browse mode active
- **Settings → Browser** panel: toggle extension mode, paste token, auto session restart
- **BrowserAuthPrompt** component: notification banner when auth gate detected
- **API endpoints:**
  - `GET /api/browser/config` — read extension mode + token status
  - `PUT /api/browser/config` — save extension mode + token (patches `mcp.json`, restarts sessions)
  - `POST /api/browser-auth-retry` — retry auth (calls `ensure()`)
  - `POST /api/browser-event` — broadcast browser activity events via WebSocket
  - `POST /api/browser/frame` — ingest a browse screenshot, rebroadcast as `browser_frame` WS event, return live subscriber count (loopback-only, in `internal_paths`)
  - `POST /api/browser/pump-audit` — SEL audit for proxy active-pump screenshot injections (loopback-only, in `internal_paths`)

### Security

| Control | Implementation |
|---------|----------------|
| URL validation | `federate_auth()` restricts to `*.amazon.com`, `*.a2z.com`, `*.aws.dev`, `*.amazon.dev` |
| Token storage | Written with `os.open(..., 0o600)` — not world-readable |
| SEL audit | All browser API endpoints emit SEL audit events |
| `browser_evaluate` | NOT auto-approved — requires user confirmation (cookie exfiltration risk) |
| Storage state | Written with `0o600` permissions via `os.open` |

### Platform Matrix

| Platform | Mode | Auth | Browser |
|----------|------|------|---------|
| macOS | Extension (recommended) | Real Chrome session | User's Chrome via extension |
| macOS | Headed (fallback) | Storage state + SPNEGO | Separate Chromium |
| AL2/AL2023 x86_64 | Headless | Storage state + SPNEGO | Playwright Chromium |
| AL2/AL2023 NICE DCV | Extension (opt-in) | Real Chrome session | User's Chrome via extension |
| AL2 aarch64 (glibc 2.26) | Fallback | N/A | `ReadInternalWebsites` only |

### Credential Lifetimes

| Credential | Lifetime | Refresh | MCP Restart? |
|---|---|---|---|
| Midway session cookie | ~20 hours | `mwinit` + `kirocrew browse auth refresh` + `browser_set_storage_state` | No |
| Kerberos TGT | ~6 hours | `kinit -f` | Yes (read at Chromium launch) |
| Extension token | Permanent (until Chrome extension reinstalled) | Re-copy from extension popup | Yes |

### MCS Enforcement (Future)

Cookie replay will stop working when MCS proof-of-possession is enforced.
- **Extension mode:** unaffected — real Chrome has AEA extension
- **Headless mode:** will break — fall back to `ReadInternalWebsites` or switch to extension mode
