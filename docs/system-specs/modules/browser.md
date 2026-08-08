## Browser Module

Thin auth layer for enterprise-authenticated website browsing via Playwright MCP.

### Architecture

Browser Mode is a durable capability toggle in **Settings → Browser**. The gate is
**tool availability**, not a per-message marker: enabling it registers the browse
proxy so the `browser_*` MCP tools appear in the agent's tool list; disabling it
deregisters the proxy so the tools disappear. There is no `[BROWSE]` marker and no
per-message browse flag; the agent itself decides, per task, whether to operate a
browser or read with `web_fetch` (the system prompt and the web-browse / web-verify
skills tell it how).

```
Browser Mode ON  → proxy registered → browser_* tools present in the agent's
  tool list → agent operates the browser (browser_navigate, browser_click,
  browser_snapshot, …) when a task needs it, else uses web_fetch

Browser Mode OFF → proxy deregistered → no browser_* tools → agent reads with
  web_fetch / web_search
```

Registration IS the authorization: the tools exist iff Browser Mode is on.
`register_playwright_proxy()` refuses to write the proxy (returns `"mode-disabled"`)
while the flag is off, and the boot-time convergence removes any stale proxy when
the flag is off, so no setup/CLI path can mount the tools without the operator's
Settings-level consent. The durable `browser-mode-enabled` flag is a keystone the
agent cannot write (see [security](security.md)); the dashboard Settings API is its
sole writer.

### Two Modes

Two independent choices in **Settings → Browser** decide how a page opens: the
transport (attach to your own browser vs. let Playwright launch its own) and, for
the launch path, which engine Playwright launches.

| Mode | Platform | How it works |
|------|----------|--------------|
| **Extension (attach)** | macOS (recommended) | Playwright attaches to the user's running **Chromium-family** browser (Chrome, Edge, Brave, Arc, Opera) through Microsoft's "Playwright Extension" (one Chrome Web Store listing covers the family; the connection token is optional and only skips the per-connection approval prompt). All existing auth (enterprise SSO session, Sentry, Kerberos) works automatically. Chromium-family only: Playwright ships no attach extension for Firefox or Safari, so those cannot be attached. |
| **Launch (headless)** | Linux Cloud Desktops, macOS fallback | Playwright launches its **own** browser build. The launch engine is one of `chromium` / `firefox` / `webkit` (default `chromium`), selected in Settings and persisted in the `browser-engine` file. `firefox` and `webkit` are Playwright's own patched builds, not the user's installed Firefox/Safari, so they carry no user logins. Chromium adds `--auth-server-allowlist` + storage-state cookie injection. |

### Key Design Decisions

**Delegate browsing to Playwright MCP** — we don't implement click/fill/navigate/screenshot.
Playwright MCP handles all browser interaction. KiroCrew only handles enterprise-SSO auth.

**Two auth strategies:**
- Extension mode: zero auth work — real Chrome session has everything
- Headless mode: storage state (`~/.kiro/crew/playwright-storage-state.json`) + Kerberos via `--auth-server-allowlist`

**Provision on Browser-Mode enable.** Turning Browser Mode on from Settings runs
`ensure_playwright_installed(engine)` in `browser/setup.py`, which bootstraps Node
when absent (through the bundled `ensure-node.sh`) and then makes `@playwright/mcp`
launchable the SAME way the proxy runs it and the whole MCP ecosystem installs it
— **via `npx`, never `npm install -g`**:

- **Detect first.** If a launcher already resolves (a standalone
  `mcp-server-playwright`/`playwright-mcp` binary, `npx`, or a
  `KIROCREW_PLAYWRIGHT_CMD` override), there is nothing to fetch — a re-enable, or
  a host that already has `npx`, downloads nothing.
- **npx-only host:** prime the `npx` cache with one pinned fetch so the first
  browse is not cold, then `playwright install <engine>` through the bundled
  `playwright-core` (revision-matched with the launcher). If the browser can't be
  provisioned yet it fails **soft** (`step: "browser-deferred"`, `ok: true`):
  `@playwright/mcp` downloads it on first use.
- **npm-free host** (Node without `npm`/`npx`): fails **soft** at `step:
  "package"` with the two npm-free paths — the official Docker image
  (`mcr.microsoft.com/playwright/mcp`) and the `KIROCREW_PLAYWRIGHT_CMD` override.

**Public-registry pin.** Every npm/npx fetch Kiro Crew triggers — the prime, the
browser install, and the proxy's own runtime `npx` launch — pins the public
registry via `npm_config_registry=https://registry.npmjs.org/` in the child env.
`@playwright/mcp` is a public package, but a user's ambient `.npmrc` may point the
default registry at a private mirror (corporate proxy, AWS CodeArtifact) whose
token expires; without the pin a bare fetch 401s. The pin is env-var based (not an
argv flag) so npm and npx honor it identically on macOS, Linux, and Windows. A
network whose only egress is that private mirror can set `KIROCREW_PLAYWRIGHT_CMD`
to bypass the fetch entirely.

**Version pin (npx hosts).** The enable-time prime records the exact
`@playwright/mcp` version it resolved to the `playwright-mcp-version` flag, and the
runtime proxy launches `@playwright/mcp@<that version>` instead of the floating
`@latest`. This makes an offline launch deterministic (resolves from the warm npx
cache, no registry round-trip) and stops `latest` from advancing past the browser
revision provisioned at enable time (the "Executable doesn't exist" drift). The
flag is validated against a strict semver pattern on read, so a tampered value
degrades to `@latest` rather than reaching an npx argv; absent, it also falls back
to `@latest`. It resolves through the shared launch path, not the runtime PATH,
which setup and the proxy now derive identically via `node_augmented_path` so a
marker-bootstrapped Node toolchain the gateway did not inherit is found by both.

**Provisioning is always advisory — enabling never surfaces a raw error.** Turning
Browser Mode on registers the proxy (the capability is on) BEFORE and independent
of any download, and no provisioning outcome is ever shown to the operator as a raw
npm/playwright failure — the raw stderr is logged for debugging, never surfaced.
The outcome is honest about usability without ever being alarming:

- **`ok: true, step: "done"`** — browser provisioned.
- **`ok: true, step: "browser-deferred"`** — the launcher resolves but the browser
  was NOT yet attempted (npx cache still warming); a re-save once warm completes it.
- **`ok: false, step: "browser"`** — the browser download WAS attempted and failed
  (offline, unwritable/full cache). Honestly not-yet-usable (a headless browse needs
  the executable), so NOT reported as usable — but still a calm "toggle off/on to
  retry" note, never a stderr dump.
- **`ok: false, step: "node" | "package"`** — no Node, or no npm/npx launcher; calm
  "Browser Mode is on; to finish setup…" note naming the Docker / `KIROCREW_PLAYWRIGHT_CMD`
  paths.

The dashboard renders every one of these as a MUTED advisory (info icon), never a
red error. `ensure_playwright_installed` is best-effort and never raises; the save
handler also wraps the call so an unexpected exception still returns 200 with a
deferred note rather than 500-ing. It returns a structured `{ok, step, detail, engine}`.

**Browser Mode is a persistent capability toggle.** The durable
`browser-mode-enabled` flag file under the data home is the gate. While it is on,
the browse proxy is registered and the `browser_*` tools are in the agent's tool
list; while it is off they are removed and the agent uses `web_fetch` instead.

### Auth Flow (Headless Mode)

1. `kirocrew browse auth health` — validates SSO cookie, Kerberos ticket, AEA posture
2. `kirocrew browse auth refresh` — converts `~/.kiro/crew/browser-cookies.txt` → `~/.kiro/crew/playwright-storage-state.json`
3. Playwright loads cookies from storage state at context creation (no manual injection)
4. `--auth-server-allowlist=*.example.com` handles SPNEGO challenges (the OSS build ships no enterprise allowlist)
5. For SSO-gated sites: `kirocrew browse auth federate <url>` completes the federated login SPNEGO chain via curl

### Auth Flow (Extension Mode)

1. User has Chrome open with existing auth (enterprise SSO session, Sentry extension)
2. Playwright MCP connects via extension token (`PLAYWRIGHT_MCP_EXTENSION_TOKEN`)
3. All navigation uses the real authenticated session — no cookie injection needed

### Config Files

| File | Purpose |
|------|---------|
| `~/.kiro/crew/playwright-config.json` | Playwright MCP config: `--auth-server-allowlist`, `storageState`, `isolated: true`, capabilities |
| `~/.kiro/crew/playwright-storage-state.json` | Playwright storage state (generated from `~/.kiro/crew/browser-cookies.txt`) |
| `~/.kiro/crew/browser-mode-enabled` | Flag file: Browser Mode enabled (durable capability toggle; presence = on) |
| `~/.kiro/crew/browser-engine` | Selected launch engine (`chromium` / `firefox` / `webkit`); absent or unrecognized reads back as `chromium` |
| `~/.kiro/crew/playwright-extension-mode` | Flag file: extension (attach) mode enabled |
| `~/.kiro/crew/playwright-extension-token` | Chrome extension connection token (0o600 perms) |
| `~/.kiro/settings/mcp.json` | MCP server config (args: `--extension` or `--config`) |

### Source Files

| File | Purpose |
|------|---------|
| `browser/auth.py` | SSO cookies, federated SSO, KRB5CCNAME, health checks, URL validation |
| `browser/setup.py` | Browser Mode + engine flags, `ensure_playwright_installed` (Node bootstrap + `@playwright/mcp` + `playwright install <engine>`), config generation, storage state refresh, MCP config patching |
| `browser/cli.py` | `kirocrew browse` CLI: setup, auth health/refresh/inject/federate, extension on/off |
| `mcp_playwright_proxy.py` | Stdio proxy: intercepts Playwright MCP responses, compresses accessibility trees |
| `skills/browser-auth/SKILL.md` | Agent skill for auth + Playwright MCP workflow |
| `scripts/refresh-playwright-cookies.py` | Standalone script: `~/.kiro/crew/browser-cookies.txt` → storage state |
| `config/playwright-mcp-config.json.template` | Template for the Playwright config structure |

### Context Window Optimization (Playwright Proxy)

Playwright MCP's `browser_snapshot` returns full accessibility trees (50-100K tokens on heavy pages). The **Playwright proxy** (`kirocrew mcp-playwright-proxy`) intercepts these responses and auto-compresses them before they reach the LLM — the full tree never enters context.

**How it works:**
- kiro-cli → `kirocrew mcp-playwright-proxy` (stdio) → real `npm-playwright-mcp` (subprocess)
- Proxy forwards all messages bidirectionally
- Intercepts responses with accessibility trees (>5K chars with tree-like structure)
- Compresses to compact outline: only interactive elements (links, buttons, inputs, headings, images) with refs
- ~95% token reduction on heavy pages

**Registration (one canonical server, no duplicates):** kiro-cli splits an agent
`@server` reference on `/`, so a slash-containing key like `@playwright/mcp` is
mis-parsed as `@server/tool` and exposes none of the server's tools. The proxy is
therefore always registered under the **slash-free canonical alias**
`playwright-mcp` (`mcp_server_alias("@playwright/mcp")`), and every writer drops
the superseded keys it historically used so exactly one Playwright entry survives:

- `kirocrew setup` — new installs get the canonical proxy entry from the start.
- `patch_mcp_extension()` / `patch_mcp_headless()` — write the entry under the
  canonical alias, after `_drop_superseded_playwright()` removes any legacy proxy
  key (`@playwright/mcp`, `playwright-proxy-mcp`) **and** KiroCrew's legacy
  *direct* `npm:@playwright/mcp` entry (that `npm:`-prefixed key is a KiroCrew
  install artifact; a user's own direct server under the bare `@playwright/mcp`
  key is left untouched — authorship is by launch target, not key name).
- Gateway startup — `_migrate_playwright_to_proxy()` delegates to
  `migrate_owned_playwright_registration()`, which converges KiroCrew's own
  browse entry in `~/.kiro/settings/mcp.json` to the canonical key — including
  upgrading a legacy *direct* `npm:@playwright/mcp` entry (from installs predating
  the compression proxy) to the proxy — converges KiroCrew's own
  `~/.kiro/crew/mcp.json` at the SOURCE (so a stale proxy key there is healed once,
  not re-injected into every rebuild), and sweeps the KiroCrew-generated agent
  configs (the exact `kirocrew*` filenames it writes, not a bare `*.json` glob, so
  a user's own agents are never rewritten) so a duplicate proxy entry collapses
  onto the one canonical server. This self-heals an existing machine on a plain
  restart.
- Every agent-config rebuild — `converge_playwright_servers()` runs right after
  key normalization in `build_agent_config` as a BACKSTOP (the owned source files
  are healed at boot above; this catches anything re-merged mid-rebuild),
  collapsing any Playwright-proxy entry by *resolved launch target* (an entry that
  invokes `mcp-playwright-proxy`). This catches a slash-free legacy key (e.g.
  `playwright-proxy-mcp`) that key normalization — which only rewrites
  slash-containing keys — cannot fold. Convergence keeps the canonical key
  (unless a user's own *direct*, non-proxy server already occupies it — then the
  proxy is left under its own non-conflicting key rather than clobbering the user
  entry), rewrites dropped `@refs` in `tools`/`allowedTools`, and never touches a
  user-declared, non-proxy server. Together with discovery read-folding and pool
  launch-target dedupe, all surfaces agree on the single `playwright-mcp` server,
  so no second backend, dashboard row, or probe is derived.

**Ownership signals (authorship of an MCP entry).** `~/.kiro/settings/mcp.json` is
co-owned with kiro-cli, and kiro-cli validates it (and agent specs) with
`deny_unknown_fields`, so an in-spec ownership sentinel is impossible. KiroCrew
therefore records the MCP keys it writes in an out-of-band sidecar manifest
`~/.kiro/crew/owned-mcp-keys.json` (`_record_owned_mcp_key`, mode 0600), written by
`patch_mcp_extension()` / `patch_mcp_headless()`. Drop/converge decisions consult
this manifest **first** (`_load_owned_mcp_keys`); the `mcp-playwright-proxy`
launch-target heuristic (`_spec_is_proxy`) and the `npm:@playwright/mcp` legacy-key
rule remain the fallback for entries written by installs that predate the
manifest. This stops the unmarked-entry population from growing so future
migrations rely less on forensic heuristics on a destructive, every-restart path.
*Follow-up:* extend the manifest to the agent-config writers and to all
drop/converge sites (currently the sidecar sweep still keys on the owned-filename
allowlist + launch target), and consider consolidating the per-surface
convergence passes behind one canonicalizing `mcp_utils` accessor.

**Source:** `src/kiro_crew/mcp_playwright_proxy.py`, `src/kiro_crew/browser/setup.py`

### Live Browse Mirror

The dashboard mirrors the headless browse Chromium in near-real-time **without
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
local-process-takeover surface), so it was dropped.

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

**Panel.** The live mirror renders in the floating `BrowserLiveView` window — a
lifecycle-driven, resizable, persisted overlay that auto-opens in the corner on
the first frame of a browse session (minimize→chip, close→dismiss-this-session).
It consumes the frame stream via `useBrowserFrame` and is threaded with the
resolved session *title* (the raw `session_key` is only a client-side lookup key
against the dashboard's own slot store). It is read-only — no interactive
control channel.

> Note: the chat side panel's **"Web Preview"** tab (`WebPreviewPanel`, opened
> from the + menu) is a SEPARATE feature — a URL-addressable iframe that
> live-previews a local dev server the user is running (per-session URL), NOT
> this agent-browse screenshot mirror. It does not consume the `browser_frame`
> stream. The URL bar has back/forward (a URL-bar history stack — an iframe's
> own cross-origin page history isn't readable), an inline reload, an
> open-in-browser link inside the field, and an **expand** toggle: expand
> broadcasts `PREVIEW_FOCUS_EVENT`, on which App collapses the left nav and
> ChatPage hides the session list + maximizes the side panel (chat shrinks to its
> minimum). A **dimension selector** (Monitor icon = responsive desktop;
> Smartphone icon = a device size — iPhone/Pixel/Galaxy/iPad presets) renders the
> iframe at that device's pixel size. A **crop** button (shown only where the
> snip pipeline works — `isScreenSnipSupported()`, non-mobile) dispatches
> `PREVIEW_SNIP_EVENT`; ChatPage handles it by reusing the shared snip pipeline
> (`captureScreen` via getDisplayMedia — routed through Electron's
> `setDisplayMediaRequestHandler` in the desktop app — → `SnipOverlay` drag-crop →
> `uploadFiles` attaches the PNG to the composer, pinned to the slot that
> initiated the capture so a mid-capture slot switch can't misfile it).
>
> **Liveness.** A cross-origin iframe keeps displaying its last document after
> its server dies, so while a URL is framed the panel polls it (`fetch` no-cors,
> tab-active only); two consecutive connection failures ⇒ the iframe is unmounted
> and a "server not reachable" state shown instead of the stale page, and a later
> successful probe auto-restores it.
>
> **Security.** The iframe is loopback-only (http(s), mixed-content-guarded). Cookies
> are scoped by host but not port, so `isolatePreviewHost` swaps a preview whose
> host equals the dashboard's (both loopback) to the other loopback alias
> (`localhost` ⇄ `127.0.0.1`) — the dashboard's host-scoped auth cookie is never
> sent to the framed dev server. The dashboard CSP admits loopback `frame-src`
> unconditionally (`_LOOPBACK_FRAME_SRC` in `server.py`), so previews render in the
> packaged app, not only in instances mode.
>
> It can be **fed from chat** via `detectPreviewUrl` (assistant messages, newest
> first; within a message a marker beats a bare URL): the agent's hidden
> It can be **fed from chat** via `detectPreviewUrl` (assistant messages, newest
> first; within a message a marker beats a bare URL). **Click-to-load:** neither
> path ever navigates the iframe — both hand the URL to the panel as a **"Load
> preview" card** (`setSessionPreviewPending`), and the GET fires only on the
> user's explicit Load click, so agent output can't drive the scripted iframe to
> an arbitrary host without consent. Chat-fed URLs are additionally
> **loopback-only** (enforced in `setSessionPreviewPending`): agent output —
> which browsed/read content can prompt-inject — can only ever offer a local dev
> server, never an external host; the manual URL bar is not restricted. The
> agent's hidden
> `<!-- kirocrew:preview url="…" -->` marker (`web-preview` skill) is treated as
> explicit intent — ChatPage surfaces the card AND opens the tab, deduped via a
> PERSISTED `mc-webpreview-applied:<slot>` key (+ in-memory ref) so a route
> remount won't reopen a dismissed card. A bare localhost URL in prose offers the
> card WITHOUT opening the tab, and only when no target is set yet.

**Source:** `src/kiro_crew/browser/screencast.py`, `src/kiro_crew/mcp_playwright_proxy.py`, `website/src/hooks/useBrowserFrame.ts`, `website/src/components/BrowserLiveView.tsx`, `website/src/components/WebPreviewPanel.tsx`, `website/src/utils/detectPreviewUrl.ts`, `src/kiro_crew/dashboard/server.py` (`_LOOPBACK_FRAME_SRC`)

**Fallback tools** in kirocrew-core (for manual use if needed):

| Tool | Purpose |
|------|---------|
| `browse_outline` | Compress snapshot text → compact outline with refs |
| `browse_search` | Regex search snapshot text → matching lines only |

### Dashboard Integration

- **Settings → Browser** panel: turn Browser Mode on/off (the durable capability
  toggle), pick the launch engine (`chromium` / `firefox` / `webkit`), toggle
  extension (attach) mode, and paste the extension token. Enabling Browser Mode
  triggers the Playwright install and re-registers the proxy.
- **Backend** gates browsing by tool availability: enabling Browser Mode
  registers the proxy (so the `browser_*` tools appear), disabling deregisters it.
  No `[BROWSE]` marker and no per-message chat toggle; the agent decides when to
  operate a browser vs. read with `web_fetch`.
- **BrowserAuthPrompt** component: notification banner when an auth gate is detected
- **API endpoints:**
  - `GET /api/browser/config`: read `enabled`, `engine`, `engines`, `installed`,
    `extension_mode`, and `token` status
  - `PUT /api/browser/config`: save `enabled`, `engine`, `extension_mode`, and
    `token`. On a fresh enable this downloads `@playwright/mcp` + the engine
    browser off the event loop and re-registers the proxy; a failed install
    reports an actionable `code` in the body rather than 500-ing
  - `POST /api/browser-auth-retry` — retry auth (calls `ensure()`)
  - `POST /api/browser-event` — broadcast browser activity events via WebSocket
  - `POST /api/browser/frame` — ingest a browse screenshot, rebroadcast as `browser_frame` WS event, return live subscriber count (loopback-only, in `internal_paths`)
  - `POST /api/browser/pump-audit` — SEL audit for proxy active-pump screenshot injections (loopback-only, in `internal_paths`)

### Security

| Control | Implementation |
|---------|----------------|
| URL validation | `federated_login()` (stubbed enterprise SSO flow) restricts to `*.example.com` placeholders; the OSS build ships no enterprise allowlist |
| Token storage | Written with `os.open(..., 0o600)` — not world-readable |
| SEL audit | All browser API endpoints emit SEL audit events |
| `browser_evaluate` | NOT auto-approved — requires user confirmation (cookie exfiltration risk) |
| Storage state | Written with `0o600` permissions via `os.open` |

### Platform Matrix

Extension (attach) mode is Chromium-family only. The launch (headless) path lets
the operator pick the engine (`chromium` / `firefox` / `webkit`, default
`chromium`); `firefox` and `webkit` are Playwright's own builds, not the user's
installed Firefox or Safari.

| Platform | Mode | Auth | Browser |
|----------|------|------|---------|
| macOS | Extension (attach, recommended) | Real browser session | User's Chromium-family browser (Chrome/Edge/Brave/Arc/Opera) via extension |
| macOS | Launch (fallback) | Storage state + SPNEGO | Playwright's own `chromium`/`firefox`/`webkit` build |
| AL2/AL2023 x86_64 | Launch (headless) | Storage state + SPNEGO | Playwright's own `chromium`/`firefox`/`webkit` build |
| AL2/AL2023 NICE DCV | Extension (attach, opt-in) | Real browser session | User's Chromium-family browser via extension |
| AL2 aarch64 (glibc 2.26) | Fallback | N/A | read-only `web_fetch` only |

### Credential Lifetimes

| Credential | Lifetime | Refresh | MCP Restart? |
|---|---|---|---|
| SSO session cookie | ~20 hours | your SSO login + `kirocrew browse auth refresh` + `browser_set_storage_state` | No |
| Kerberos TGT | ~6 hours | your Kerberos login | Yes (read at Chromium launch) |
| Extension token | Permanent (until Chrome extension reinstalled) | Re-copy from extension popup | Yes |

### Proof-of-Possession Enforcement (Future)

Cookie replay will stop working when SSO proof-of-possession is enforced.
- **Extension mode:** unaffected — real Chrome has the AEA extension
- **Headless mode:** will break — fall back to `ReadInternalWebsites` or switch to extension mode
