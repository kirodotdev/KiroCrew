---
name: browser-auth
description: Authenticate and browse sites that require a logged-in session using Playwright MCP. Use when [BROWSE] marker is present.
triggers: BROWSE, browse, browser_navigate, browser_snapshot, browser_click
---

# Browser Auth — Authenticated Browsing

You are browsing websites with Playwright MCP. Public pages need no auth — just
navigate. For pages that require a logged-in session (dashboards, internal
tools, anything behind a login wall), authentication is supplied by **injecting
cookies the user exported from their own browser** as Playwright *storage
state*. There is no bundled SSO; the model is simply "reuse the session cookies
the user already has".

## Step 1: Check / Refresh Auth Credentials

```bash
kirocrew browse auth health
```

**If healthy**, refresh storage state so Playwright has the freshest cookies:
```bash
kirocrew browse auth refresh
```

**If unhealthy or no cookies found**, the user needs to export a fresh cookie
jar from the browser where they are already logged in, then re-run the refresh.
See "Exporting a cookie jar" below, then run `kirocrew browse auth refresh`.

## Step 2: Navigate

Use Playwright MCP tools directly — cookies are pre-loaded via storage state
(no manual injection needed once refreshed):

- `browser_navigate` — go to URL (use `waitUntil: "domcontentloaded"` for SPAs)
- `browser_snapshot` — get page structure with interactive elements (fast, no visual wait)
- `browser_click` — click elements
- `browser_fill_form` — fill input fields
- `browser_type` — type text
- `browser_take_screenshot` — capture page for user
- `browser_press_key` — keyboard input
- `browser_wait_for` — wait for a specific selector before interacting
- `browser_evaluate` — run JavaScript (requires user confirmation)

### SPA Screenshot Pattern

Many single-page apps never reach "network idle" because of background
telemetry/polling. Use this pattern:

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

### Login redirect or expired cookies (401 / 403 / redirect to a sign-in page)

Session cookies expire. When a navigation lands on a login page or returns a
401/403, the fix is to re-export the cookie jar and re-load it:

```bash
kirocrew browse auth refresh
```
Then call `browser_set_storage_state` with the storage-state file path:
```
filename: ~/.kiro/crew/playwright-storage-state.json
```
This reloads cookies WITHOUT restarting the MCP server. Then retry navigation.

If the refresh reports no valid cookies, the exported jar is stale — the user
must log in again in their own browser, re-export the cookie jar, and re-run
`kirocrew browse auth refresh`. Tell the user:

> "Your session cookies expired. Please log in again in your browser, export a
> fresh cookie jar, then let me know so I can refresh."

### 403 from a CDN / "bot detected"
Some sites block headless browsers by User-Agent. Spoof a normal User-Agent for that host:
```
browser_route pattern="https://blocked-site.example.com/**" headers=["User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"]
```
Remove when done: `browser_unroute pattern="https://blocked-site.example.com/**"`

## Exporting a cookie jar

Authenticated browsing works by reusing the session cookies from a browser
where the user is already logged in. The user exports those cookies to a
**Netscape/Mozilla cookie jar** (a plain-text `cookies.txt` file) using any
standard browser extension or tool that produces that format. KiroCrew parses
that file (`parse_netscape_cookies`) and converts it to Playwright storage
state during `kirocrew browse auth refresh`.

- Default cookie-jar location is conceptually `~/.kiro/crew/browser-cookies.txt`
  (any Netscape/Mozilla-format file works).
- Only the cookies for the site being browsed are needed; a full-jar export is fine too.
- Cookies expire — when auth fails, the user re-exports a fresh jar and you re-refresh.

## Credential Lifetimes

| Credential | Lifetime | Refresh |
|---|---|---|
| Site session cookie | Varies by site (hours to weeks) | Re-export cookie jar + `kirocrew browse auth refresh` + `browser_set_storage_state` |

Session cookies read from storage state are applied at browser-context
creation. After a `browser_set_storage_state`, no MCP restart is required.

## Debugging Auth Failures

If navigation fails with auth errors, use these tools:
- `browser_network_requests` with `requestHeaders: true` — see what cookies/UA were sent
- `browser_console_messages` with `level: "error"` — catch client-side auth errors
- `browser_snapshot` — check if you're on a login page vs the real content

## Platform Behavior

**Extension mode** (recommended for macOS):
- Playwright attaches to the user's running Chrome browser
- All existing browser sessions work automatically — no cookie export/injection needed
- Uses the real authenticated session already open in the browser
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

**Headless mode** (default on Linux / servers):
- Launches a separate Chromium with cookie injection via storage state
- User sees page content via screenshots only
- Extension mode is also available on Linux with a GUI desktop if Chrome is installed

### How Headless Mode Works

No special setup beyond exporting cookies. The flow:

1. **Auth prerequisite:** user exports a cookie jar from a browser where they're logged in
2. **On first browse:** the gateway ensures Playwright MCP + browsers are installed
3. **Cookie injection:** `kirocrew browse auth refresh` converts the cookie jar to Playwright storage state
4. **Navigate:** cookies are pre-loaded into the browser context via `contextOptions.storageState`

### Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| Playwright install fails on old glibc (aarch64) | glibc too old for bundled Chromium | Use a newer OS image or run browsing on a supported host |
| 401 / redirect to login | Session cookies expired | User re-exports cookie jar; run `kirocrew browse auth refresh` |
| Screenshots are the only output | Headless — no visible browser | Always show screenshots to user |

## Security Notes

- `browser_evaluate` is NOT auto-approved — it can access cookies. Requires user confirmation.
- Do NOT use `browser_evaluate('window.location = ...')` — use `browser_navigate`
- NEVER exfiltrate cookies or auth tokens via evaluate

## Troubleshooting

**Playwright MCP tools not available** (browser_navigate not in tool list):
1. Run `kirocrew browse setup` to install Playwright MCP + browsers
2. If installed but tools not in session, the MCP server needs to be in your agent config. Tell the user:
   > "Playwright MCP is installed but not loaded. Add it to your agent config, then restart the gateway: `kirocrew stop && kirocrew gateway`"

## How It Works (Technical)

The config at `~/.kiro/crew/playwright-config.json` sets:
- `isolated: true` — required for `storageState` to take effect (without it, Playwright uses a persistent profile and ignores our cookies)
- `contextOptions.storageState` — pre-loads exported cookies at context creation
- `capabilities: ["network", "storage"]` — `network` enables `browser_route` for UA spoofing; `storage` enables `browser_set_storage_state` for cookie hot-reload

## Prerequisites

- A Netscape/Mozilla cookie jar exported from a browser where the user is logged in (for sites that require auth)
- Playwright MCP + browsers installed (`kirocrew browse setup`)
- Config auto-generated at `~/.kiro/crew/playwright-config.json`
