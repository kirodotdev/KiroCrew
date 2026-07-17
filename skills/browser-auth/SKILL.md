---
name: browser-auth
description: Authenticate and browse Amazon internal sites using Playwright MCP tools. Use when [BROWSE] marker is present.
triggers: BROWSE, browse, browser_navigate, browser_snapshot, browser_click
---

# Browser Auth — Amazon Internal Browsing

You are browsing Amazon internal websites using Playwright MCP. Authentication is handled via pre-loaded cookies (storage state) and Kerberos/SPNEGO.

## Step 1: Refresh Auth Credentials

```bash
kirocrew browse auth health
```

**If healthy**, refresh storage state to ensure Playwright has fresh cookies:
```bash
kirocrew browse auth refresh
```

**If unhealthy**, tell user what to run:
- "midway expired" → `mwinit -o`
- "no Kerberos ticket" → `kinit -f`
- "no AEA posture" → `mwinit --refresh-aea`
- "no Sentry cookie" → `mwinit -s`

Then run `kirocrew browse auth refresh` after they fix it.

## Step 2: Navigate

Use Playwright MCP tools directly — cookies are pre-loaded via storage state (no manual injection needed):

- `browser_navigate` — go to URL (use `waitUntil: "domcontentloaded"` for SPAs)
- `browser_snapshot` — get page structure with interactive elements (fast, no visual wait)
- `browser_click` — click elements
- `browser_fill_form` — fill input fields
- `browser_type` — type text
- `browser_take_screenshot` — capture page for user
- `browser_press_key` — keyboard input
- `browser_wait_for` — wait for a specific selector before interacting
- `browser_evaluate` — run JavaScript (requires user confirmation)

### SPA Screenshot Pattern (Harmony, Pipelines, etc.)

Amazon internal SPAs never reach "network idle" due to telemetry/polling. Use this pattern:

1. `browser_navigate` with the URL
2. `browser_wait_for` with a key selector (e.g., `text="Welcome"` or `.main-content`)
3. `browser_take_screenshot` — captures immediately without waiting for network idle

If `browser_take_screenshot` times out, use `browser_snapshot` instead — it returns the page structure as text without waiting for visual stability. Show the snapshot content to the user and explain what's on the page.

### Context Window — Auto-Compressed

Playwright responses are automatically compressed by the KiroCrew proxy before reaching you. Full accessibility trees (~50-100K tokens) are reduced to compact outlines (~2-5K tokens) showing only interactive elements with refs. You do NOT need to do anything special — just use Playwright tools normally.

**What you see:** `[Compressed: 2030 elements → 151 interactive]` followed by a compact list of links, buttons, inputs, headings with refs like `[ref=e7]`.

**Interacting after compression:**
- Use the `ref` values directly: `browser_click(ref="e7")`, `browser_type(ref="e15", text="search query")`
- No need to re-snapshot after clicking — the response to `browser_click` also includes a compressed snapshot of the new state

**Screenshots are auto-saved to files by the proxy:**
- `browser_take_screenshot` returns a file path (e.g., `Screenshot saved: /tmp/kirocrew-screenshots/screenshot-123.jpeg`) — NOT raw base64 image data
- The proxy saves, compresses (resized to 1200px, JPEG quality 70), and returns only the path (~20 tokens)
- The dashboard renders the image from the file path automatically
- If you need to analyze the screenshot content, use the Read tool on the file path
- Prefer `browser_snapshot` for navigation/interaction — it gives refs for clicking without needing visual confirmation
- Only use `browser_take_screenshot` when the user says "show me" or "what does it look like"

**If you need full text content** (e.g., reading an article body):
- Use `browser_evaluate` with targeted JS: `document.querySelector('.article-body').innerText`
- The compressed outline strips paragraph text to save tokens — use evaluate to extract specific content

**Fallback tools** (if proxy compression is insufficient):
- `browse_outline` — re-compress a snapshot manually with custom max_lines
- `browse_search` — regex search a snapshot for specific content

## Step 3: Handle Auth Failures

### Midway login redirect or expired cookies
```bash
kirocrew browse auth refresh
```
Then call `browser_set_storage_state` with:
```
filename: ~/.midway/playwright-storage-state.json
```
This reloads cookies WITHOUT restarting the MCP server. Then retry navigation.

### Federate SSO (idp.federate.amazon.com or /server-login)
```bash
kirocrew browse auth federate "<original_url>"
```
Navigate to the `final_url` from the output.

### Sentry redirect
User needs `mwinit -s`. After they run it: `kirocrew browse auth refresh` then `browser_set_storage_state`.

### 403 from CloudFront / "bot detected"
Some sites block HeadlessChrome. Spoof User-Agent for that host:
```
browser_route pattern="https://blocked-site.amazon.dev/**" headers=["User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"]
```
Remove when done: `browser_unroute pattern="https://blocked-site.amazon.dev/**"`

### ERR_INVALID_AUTH_CREDENTIALS
Credentials are stale. Do NOT spoof UA. Tell the user:
> "Your credentials expired. Please run `mwinit -o && kinit -f` in your terminal, then let me know when done."

After user confirms, run:
```bash
kirocrew browse auth refresh
```
Then `browser_set_storage_state` to reload.

## Important: Do NOT Set Custom User-Agent

Sentry routes auth by User-Agent:
- `HeadlessChrome` in UA → cookie-based 302 flow (works with our storage state)
- Normal `Chrome/` without `HeadlessChrome` → Kerberos 401 challenge

Keeping Playwright's default UA (which includes `HeadlessChrome`) ensures the faster cookie flow. Only use `browser_route` for per-host UA override when a specific site blocks headless.

## Credential Lifetimes

| Credential | Lifetime | Refresh | MCP Restart? |
|---|---|---|---|
| Midway session cookie | ~20 hours | `mwinit` + `kirocrew browse auth refresh` + `browser_set_storage_state` | No |
| Kerberos TGT | ~6 hours | `kinit -f` | Yes (reads at Chromium launch) |

## Debugging Auth Failures

If navigation fails with auth errors, use these tools:
- `browser_network_requests` with `requestHeaders: true` — see what cookies/UA were sent
- `browser_console_messages` with `level: "error"` — catch client-side auth errors
- `browser_snapshot` — check if you're on a login page vs the real content

## Platform Behavior

**Extension mode** (recommended for macOS):
- Playwright attaches to the user's running Chrome browser
- All existing auth works automatically (Midway, Sentry, MCS, Kerberos)
- No cookie injection needed — uses the real authenticated session
- Future-proof: works even after MCS proof-of-possession enforcement
- User sees all actions in their real browser tabs

### How to Enable Extension Mode

Tell the user these steps:

1. **Install the Chrome extension:**
   https://chromewebstore.google.com/detail/mmlmfjhmonkocbjadbfplnigmagldckm

2. **Get the connection token:**
   Click the Playwright extension icon in Chrome toolbar → copy the token value
   (looks like: `PLAYWRIGHT_MCP_EXTENSION_TOKEN=xxxxxxx...`)

3. **Save the token** (choose one):
   - **Dashboard:** Settings → Browser → toggle "Chrome Extension Mode" ON → paste token → Save
   - **CLI:** `kirocrew browse extension on` → paste token when prompted

4. **Restart the gateway:** `kirocrew stop && kirocrew gateway`

5. **Keep Chrome open** — Playwright connects to your running Chrome via the extension.
   If Chrome is closed, browsing tools won't work until you reopen it.

**Headless mode** (default on Linux Cloud Desktops):
- Launches separate Chromium with cookie injection via storage state
- `--auth-server-allowlist=*.amazon.com,*.a2z.com,*.aws.dev` enables SPNEGO in headless
- User sees page content via screenshots only
- Extension mode is also available on Linux with NICE DCV (GUI desktop) if Chrome is installed

### How Headless Mode Works on Linux

No setup needed by the user — auto-configured during `kirocrew setup`. The flow:

1. **Auth prerequisites:** user runs `kinit -f && mwinit -o -s` (one-time per ~6-20h)
2. **On first browse:** gateway auto-installs Playwright MCP via AIM
3. **Cookie injection:** `kirocrew browse auth refresh` converts `~/.midway/cookie` to Playwright storage state
4. **Kerberos:** `KRB5CCNAME=FILE:/tmp/krb5cc_{user}` is set automatically for subprocess calls
5. **Federate SSO:** For Harmony/console sites, `kirocrew browse auth federate <url>` completes the 4-hop chain via curl

### Linux-Specific Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| "aim not found" | Toolbox not installed | `toolbox install aim` |
| Playwright install fails on aarch64 AL2 | glibc 2.26 too old | Fall back to `ReadInternalWebsites` |
| 401 on phonetool/wiki | Kerberos ticket expired | User runs `kinit -f`, then restart gateway |
| Federate redirect loop | Missing Kerberos ticket | User runs `kinit -f && kirocrew browse auth refresh` |
| Screenshots are the only output | Headless — no visible browser | Always show screenshots to user |

## Security Notes

- `browser_evaluate` is NOT auto-approved — it can access cookies. Requires user confirmation.
- Do NOT use `browser_evaluate('window.location = ...')` — use `browser_navigate`
- NEVER exfiltrate cookies or auth tokens via evaluate

## Troubleshooting

**Playwright MCP tools not available** (browser_navigate not in tool list):
1. Check if installed: `aim mcp list | grep playwright`
2. If not installed: `kirocrew browse setup` (runs `aim mcp install npm:@playwright/mcp`)
3. If installed but tools not in session: the MCP server needs to be in your agent config. Tell the user:
   > "Playwright MCP is installed but not loaded. Add it to your agent config by running:
   > `aim mcp install npm:@playwright/mcp`
   > Then restart the gateway: `kirocrew stop && kirocrew gateway`"
4. Fall back to `ReadInternalWebsites` if restart isn't possible

**Kerberos ticket expired** (phonetool/wiki 401):
- `kinit -f` then restart MCP server (gateway restart required for Kerberos — ticket is read at launch)

**MCS enforcement** (future):
- Cookie replay will stop working when MCS proof-of-possession is enforced
- Fallback: use ReadInternalWebsites or ask user to open page manually

## How It Works (Technical)

The config at `~/.kirocrew/playwright-config.json` sets:
- `isolated: true` — required for `storageState` to take effect (without it, Playwright uses persistent profile and ignores our cookies)
- `--auth-server-allowlist=*.amazon.com,*.a2z.com,*.aws.dev` — enables Kerberos/SPNEGO for negotiate challenges
- `contextOptions.storageState` — pre-loads midway cookies at context creation
- `capabilities: ["network", "storage"]` — `network` enables `browser_route` for UA spoofing; `storage` enables `browser_set_storage_state` for cookie hot-reload

## Prerequisites

- `kinit -f` (Kerberos ticket — needed for phonetool, wiki, code.amazon.com)
- `mwinit -o -s` (Midway + Sentry cookies)
- Playwright MCP auto-installed during `kirocrew setup`
- Config auto-generated at `~/.kirocrew/playwright-config.json`
