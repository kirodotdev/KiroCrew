## Browser Module

The agent browses through one driver, and the person watches it in the chat.
[Browser Harness](https://github.com/browser-use/browser-harness) (MIT) drives a
Chromium-family browser over CDP; the gateway pins it, runs its daemon, holds the
only CDP connection to the browser through a loopback relay, and streams the page
into the transcript as a live card the person can take over with one click. The
`browser` MCP tool keeps its name, its governance check and its URL gate; only
what sits behind it changed.

This document is the spec for the whole stack. The design and its measurements
are in
[rfc-browser-live-harness.md](../../request-for-change/rfc-browser-live-harness.md);
that RFC supersedes
[rfc-playwright-cli-migration.md](../../request-for-change/rfc-playwright-cli-migration.md).
The whole stack landed in one change: the driver, the stream, the tool, the card,
Settings and the removal of what they replaced, so no release carries a half of
this.

### Architecture

```
browser ◀──one CDP websocket──▶ gateway relay ◀── Harness daemon (as if it were the browser)
                                     │
                                     ├── screencast sessions ──▶ browser_frame ──▶ live card
                                     └── input sessions ◀── /api/browser/input ◀── takeover

agent turn ──MCP browser(op, args)──▶ ops ──▶ supervisor.call(<helper>) ──▶ daemon IPC
```

| Part | Where |
|---|---|
| Driver install, environment fence, browser discovery, relay, supervisor, helper worker, status and install routes | the `browser_harness` package: `env.py`, `install.py`, `chromium.py`, `discover.py`, `relay.py`, `supervisor.py`, `helper_worker.py`; `dashboard/handlers/browser_harness_status.py` |
| Screencast, adaptive ladder, takeover input, the live hub, the card's routes and the `browser_live` event | `screencast.py`, `takeover.py`, `live_hub.py` in that package; `dashboard/handlers/browser_live.py` |
| The `browser` MCP tool's ops, the snapshot walker and the op route that also drives the card | `mcp_tools/browser.py`, `ops.py`, `snapshot_walker.js` in that package; `dashboard/handlers/browser_ops.py` |
| The live card in the transcript | `BrowserLiveCard*.tsx` under the chat page, the `useBrowserLive*` hooks, and the binary-frame branch of `useWebSocket.ts` |
| Settings → Browser and the Preview tab | `website/src/pages/settings/Browser*.tsx`, `config/sections_browser.py`, `website/src/components/WebPreviewPanel*.tsx` |
| What goes into Kiro's Chromium before the first op: the edition credential seam | `credentials.py` and `credential_files.py` in that package; the `browser_credentials` platform slot |
| Screenshot directory, retired keys, doctor row, Preview tab's native view | `browser_snapshots.py`, `config/resolution.py`, `doctor_checks/install.py`, `website/electron/browser-view.js` |

One supervisor per gateway owns the daemon and the relay. One relay connection
per browser. One live card per agent turn. The side panel keeps one surface,
the **Preview** tab, for the person's own local dev server; the agent never
drives it.

### The driver

The gateway installs `browser-harness==0.1.13` into its own virtual environment
at `<data-home>/browser-harness/venv` (`python -m venv --clear` on that
directory alone, so the Chromium beside it survives a re-install; then
`pip install --require-hashes -r browser_harness/requirements.lock`: 12 pinned
requirements, 176 sha256 hashes, compiled for Python 3.11+ on every platform).
Kiro's own Chromium (Chrome for Testing `154.0.8037.0`, the build Playwright's
`chromium-1244` ships, fetched from Google's bucket and then Playwright's CDN,
which alone hosts linux-arm64) is downloaded into the same prefix for hosts with
no usable Chrome (`chromium.py`). The prefix is a `READONLY` crew-home leaf, so
a sandboxed process can neither replace the interpreter nor the browser the
gateway executes; the install job (`POST /api/browser/install`, one at a time,
`409` otherwise, `{"components": ["harness"|"chromium"]}` to redo one part) runs
in the gateway, outside the sandbox.

**Startup auto-install.** The owner never has to press Install: at every gateway
start, in the background task that first reclaims a stray daemon
(`server_runtime/browser_lifecycle.py`), `auto_setup_browser_harness`
(`handlers/browser_harness_status.py`) starts the SAME install job when the
Harness is missing or stale (`install.harness_current`:
installed AND the marker's `lock_sha256` equals the shipped lock's digest, so a
re-pin without a version bump reinstalls). It adds Kiro's Chromium only when
discovery would launch it (no usable Chrome/Edge, or `browser.choice` pins
`kiro_chromium`/`headless`) and it is not on disk; a host that drives the user's
Chrome never downloads it unasked (Settings' Install still installs both). After
a successful install it warms the connection the way Test does ONLY for a
headless Kiro Chromium: connecting the user's Chrome or Edge is what raises
Chrome's Allow sheet, and a headed Kiro Chromium would open a window, so both stay
lazy until the first `browser` call or Test. One attempt per gateway process (a
flag on the dashboard state): a failure is not retried; it stays visible as the
failed `install_job` and Install is the retry. While it runs, `GET
/api/browser/status` reports `installing: true` and `install_job.status:
"running"`, and an Install press gets the usual `409`. It never runs in a process
carrying `PYTEST_CURRENT_TEST`, `CI`, `KIROCREW_E2E_EPHEMERAL` or
`KIROCREW_FAKE_ACP_TEST_MODE` (the last marks a fake-agent test rig started outside
pytest and CI, such as the GUI user test's `gateway --test-mode`; any value but
empty/`0`/`false`/`no`/`off`); there is no config key. `KIROCREW_SKIP_MODEL_DOWNLOAD`
does not skip it: a real cloud microVM gateway sets that.

The daemon starts with a scrubbed environment built by one function and pinned
by one test:

| Variable | Value | Why |
|---|---|---|
| `BH_TELEMETRY`, `BROWSER_HARNESS_TELEMETRY`, `ANONYMIZED_TELEMETRY` | `false` | the three switches the package reads |
| `BH_POSTHOG_HOST` | `http://127.0.0.1:9` | a second fence if a switch is missed |
| `BH_TAB_MARKER` | `0` | no badge painted on the page |
| `BH_UPDATE_CHECK` | `0` | the CLI's PyPI probe never runs |
| `BU_CDP_URL` | the relay's URL, `http://127.0.0.1:<port>/<secret>` | connects to the relay, not the browser; the per-start secret is the only credential that opens it |
| `BU_NAME` | the gateway's daemon name | a named daemon creates its OWN tab instead of attaching the first page it finds (the person's) |
| `BH_HOME`, `BH_CONFIG_DIR`, `BH_RUNTIME_DIR`, `BH_TMP_DIR`, `BH_AGENT_WORKSPACE` | under `<data-home>/browser-harness-ipc` | the socket, pid file, log and scratch never leave the hidden leaf |
| `BROWSER_USE_API_KEY`, `BU_BROWSER_ID`, `BU_CDP_WS`, `BU_AUTOSPAWN`, the cloud API/OAuth variables, inherited `BH_*` roots | removed | the only ways in to the cloud, or to a path outside the leaf |

The removed keys stay removed even when the gateway's own environment carries
them. The gateway never runs Harness's CLI, its `run.py`, its MCP server or its
update check, and never calls `browser_js`, `browser_cdp` or `http_get`; it
calls the daemon through Harness's own IPC (a Unix socket on POSIX, loopback TCP
with a token on Windows), whose path or token lives in a `HIDDEN` crew-home
leaf. A data home so deep that the socket path would pass the AF_UNIX limit
(100 bytes) is REFUSED with a `BrowserUnavailable` naming the path: the only
shorter place is the system temp dir, which no sandbox mask covers, and the
daemon resolves symlinks before binding, so an alias cannot help (the guard
measures the RESOLVED home for the same reason). The daemon runs with
`python -I`, like the helper worker, so the gateway's working directory can never
shadow the pinned package; the INSTALLER's two spawns (`python -I -m venv`,
`<venv>/python -I -m pip`) run the same way, with the `PYTHON*` and `PIP_*`
families scrubbed from their environment and the prefix as their working
directory, because a pod gateway's cwd is the agent's workspace and a `pip/`
package written there would otherwise run as the owner the moment they press
Install. `NO_PROXY`/`no_proxy` always carry `127.0.0.1,localhost`: neither
`urllib` nor `websockets` bypasses a proxy for loopback on its own, and a shell
with `HTTPS_PROXY` set would send the relay URL (secret and all) to the proxy
host. A version bump re-reads the package's telemetry and auth modules before it
lands, because 0.1.13's telemetry is on by default and its run event carries the
agent's whole script unscrubbed.

**The helper worker.** The gateway's Python has no `browser_harness` package, so
each helper call runs in a worker the venv's interpreter executes
(`<prefix>/bin/python -I browser_harness/helper_worker.py`, JSON lines on its
pipes, one call at a time behind a lock, `CALL_TIMEOUT_SECS = 90`). The pipe is
read with an 8 MiB line limit (`WORKER_LINE_LIMIT`; asyncio's 64 KiB default is
smaller than an ordinary snapshot) and the worker refuses to write a reply over
4 MiB (`REPLY_CAP_BYTES`, an error reply instead); a line that still overruns is
a fatal protocol failure that discards the worker, since the rest of the line is
still in the pipe. The walker caps what it returns (1,500 nodes, 2,000 characters
per console entry). The worker's allowlist is the whole surface, and the
supervisor checks the same table before it sends (`supervisor.HELPERS`, pinned
equal by a test):

| helper | kwargs | Harness function |
|---|---|---|
| `goto` | `url` | `goto_url` (returns `Page.navigate`'s reply, `errorText` on failure) |
| `click` | `x, y, button="left", clicks=1` | `click_at_xy` |
| `mouse_move` | `x, y` | the worker's own `Input.dispatchMouseEvent` (Harness has no hover) |
| `drag` | `x, y, to_x, to_y` | the worker's own press / stepped moves / release |
| `type_text` | `text` | `type_text` (`Input.insertText`) |
| `press_key` | `key, modifiers=0` (1 Alt, 2 Ctrl, 4 Meta, 8 Shift) | `press_key` |
| `scroll` | `x, y, dy=-300, dx=0` | `scroll` |
| `screenshot` | `path, full=False, max_dim=None` | `capture_screenshot` |
| `page_info` | -- | `page_info` (`{url,title,w,h,sx,sy,pw,ph}` or `{dialog}`) |
| `wait_for_load` | `timeout=15.0` | `wait_for_load` |
| `new_tab`, `switch_tab`, `list_tabs`, `close_tab`, `current_tab` | `url` / `target, activate` / `include_chrome` / `target` / -- | the tab helpers |
| `js` | `expression, target_id=None` | `js`, ONLY for `snapshot_walker.js` |

`run.py`, `browser_js`, `browser_cdp`, `http_get`, `upload_file`, `fill_input` and
the agent-helper loader are not reachable through it. A helper's failure comes back
as `HelperError` with Harness's own one-line message; `HarnessNotInstalled` and
`BrowserUnavailable` come out of `ensure()`.

**The supervisor.** `HarnessSupervisor` (one per gateway, `get_supervisor(home)`)
brings browser, relay, daemon and worker up in `ensure()` (serialised: a second
caller waits for the first), reports `status()` (`installed`, `version`,
`wanted_version`, `browser {kind, headless}`, `connection`
`connected|waiting_allow|disconnected|error`, `detail`, `agent_tabs`, `chromium`),
and stops all of it in `stop()`. `waiting_allow` has no deadline because the
connection is what keeps Chrome's Allow sheet on screen. The gateway stops the
supervisor on shutdown and before an in-place restart's exec
(`server_runtime/browser_lifecycle.py`, registered with `pre_exec_stops`), and
reclaims a stray daemon from a previous life at startup, then runs the startup
auto-install (see the driver section).

### The browser

| Host | Browser | First run |
|---|---|---|
| macOS, Linux desktop, Windows with Chrome | the user's Chrome | enable `chrome://inspect/#remote-debugging` once, then Allow |
| Windows without Chrome | Edge | same |
| Server, no display, remote gateway | Kiro's Chromium, `--headless=new --remote-debugging-pipe --user-data-dir=<data-home>/browser-profile` (`--remote-debugging-port=0` on Windows only) | none |
| Chrome's `RemoteDebuggingAllowed` policy is off | Kiro's Chromium | none |

A snap-packaged Chromium is skipped: snap confinement breaks the debugging port.
On macOS and Linux Kiro's Chromium speaks CDP over `--remote-debugging-pipe`
(fds 3 and 4, moved into place by `/bin/sh` before `exec`), so it opens no port
at all; Python cannot hand a Windows child fds 3 and 4, so there it keeps an
ephemeral port, reachable by any local process (security table).
`--no-sandbox` is added only when the gateway runs as root. Kiro's Chromium is
always launched with `--use-mock-keychain --password-store=basic`: its network
service otherwise fetches the cookie-encryption key from the OS keystore, and on a
fresh Chrome for Testing that is a macOS Keychain (or Linux keyring) prompt a headless
browser can never answer, behind which every `http(s)` navigation -- loopback included
-- hangs forever while `data:` pages still load. The key stays in memory; the profile
it protects is Kiro's own hidden leaf. Kiro's profile is a
`HIDDEN` crew-home leaf: cookies from a takeover login stay there and the agent
cannot read the file. `browser.choice` (`auto|user_chrome|kiro_chromium|headless`)
overrides the order by hand.

Chrome 144+ asks "Allow remote debugging?" once per connection. The supervisor's
`waiting_allow` state never times out that prompt; the live card shows the one
step ("click Allow in Chrome") until it is answered.

### Credentials

Kiro's Chromium starts with an empty profile, so on a headless or remote host a
site behind a sign-in the agent cannot perform (a device-bound login, a hardware
key) is a wall the person has to climb by takeover every time. An edition that
already holds the user's session fills it (`browser_harness/credentials.py`):
`PlatformContext.browser_credentials`, a `BrowserCredentialProvider`
([platform-context](platform-context.md)). The public Default's `name()` is
`None`, so nothing is called and the public browser is unchanged.

**Opt-in.** `browser.credentials.enabled` (default unset) and
`browser.credentials.domains` (default empty) are set from Settings -> Browser
(the sign-in switch and, under More, the sign-in sites; see Settings -> Browser
below) or the config file. An unset `enabled` and an empty `domains` take the
provider's `default_settings()` (`{"enabled": bool, "domains": [...]}`), so an
edition can ship them on; a value the owner set always wins; with neither the
provider is off. While it is off only `name()`, `default_settings()` and
`fingerprint()` are called. A cookie is set only when its domain is in BOTH the
provider's `domains()` and `browser.credentials.domains` (each entry covers itself
and its subdomains; a single label, an IP, a wildcard or a port is not an entry).

**The channel is the gateway's own.** `_start` calls `_apply_credentials` after the
relay is connected and BEFORE the daemon starts: `Storage.setCookies` on the browser
target, sent with `CdpRelay.send` on the relay's own upstream connection (the one
the screencast and takeover use; the daemon's requests are renumbered beside it),
into the default browser context the daemon's tabs live in. It never goes through
the agent's `browser` tool or the helper worker, whose allowlist has no cookie
helper. One refused batch is retried cookie by cookie, so one bad cookie does not
lose the rest. Verified against Chrome for Testing 154: a cookie set at browser
level before any tab exists is read by a tab created after it, as a session cookie.

**When it is applied again.**

| Trigger | What happens |
|---|---|
| Every launch and relaunch | `_start` applies before the daemon starts |
| An agent `navigate` or `tabs` op | the op route (`dashboard/handlers/browser_ops.py`, under the gateway-wide op lock) calls `refresh_credentials_if_source_changed()`: one `fingerprint()` call bounded at 2 s and a config read. When the fingerprint or the resolved `browser.credentials` settings differ from the last apply, the provider is re-pulled before the page loads. A `None` fingerprint means "cannot tell" and is not a change. The re-pull is bounded at 8 s (`NAV_REFRESH_BUDGET_SECS`): past it the page loads with what the browser holds, `reason` reads `refresh_timeout` (while the settings are unchanged; after a settings change the block reads `pending`), and the old key is kept so the next navigation retries. The three bounds sum to `budgets.CREDENTIAL_WORST_S` (15 s), which the gateway's worst case and so the shim's HTTP timeout include. A failure never fails the op |

The navigation trigger is what both a person re-running their sign-in tool (the
tool rewrites its file, the fingerprint moves) and a switch flipped in Settings
rely on: the next page carries the change. While a start holds the supervisor's
lock (the person's Chrome can wait on its Allow sheet without deadline) the check
waits at most 5 s (`CREDENTIAL_LOCK_WAIT_SECS`) and then lets the page load; the
next navigation tries again. **The person's own Chrome or Edge is never filled**: a
start on either records `skipped` / `user_browser` and the provider is not called.

**Session cookies.** Every cookie is set WITHOUT `expires`, so nothing the provider
supplies is written into Kiro's profile on disk and all of it is gone when the
browser closes (verified: CDP reports the cookie `session: true`). Expiry still
counts: a cookie whose `expires` has passed is dropped. A cookie a re-pull omits
stays until the browser closes.

Each cookie is normalised to CDP's `Network.CookieParam` (string `name`, `value`,
an explicit `domain` that is a host name, `path`; boolean `secure`, `httpOnly`; `sameSite`
`Strict|Lax|None`, anything else is Lax, and None without Secure becomes Lax because
Chromium would refuse it). A cookie with a control character (its domain lands in
the SEL row) or a non-boolean flag is dropped. A provider call runs in a worker
thread bounded at 15 s (`PROVIDER_TIMEOUT_SECS`); a raise or a timeout becomes a
fixed code (`provider_failed`, `provider_timeout`, `provider_bad_shape`) and the
browser starts without the cookies.

**A missing or expired source is a state, not an error.** A provider that raises
`credential_files.CredentialSourceError` reports its code (`source_missing`,
`source_symlink`, `source_not_owned`, `source_permissions`, `source_too_large`,
...); `source_missing`, and cookies that were all dropped (`source_expired`), put
the block in `needs_refresh`, as does the provider's own `status()` answering
`needs_refresh: true`. The provider's `status()` is read after each apply and
reduced to `{needs_refresh, message}`; `message` is edition-authored text
(printable, at most 200 characters) Settings shows as-is.

**File hygiene for providers.** `browser_harness/credential_files.py` is the
generic reader an edition's provider uses for its source file:
`read_source_file(path, max_bytes=1 MiB)` opens with `O_NOFOLLOW` and checks the
OPEN descriptor (`fstat`), so a swap between check and read changes nothing: a
regular file, not a symlink, owned by the gateway's uid, no group or other bits,
within the size cap (Windows, with no POSIX owner and mode bits, keeps the
regular-file, not-a-link and size checks). `source_fingerprint(path)` is
`inode:size:mtime_ns` from `lstat`, never the contents, and is what
`fingerprint()` should return.

**Values never leave.** `GET /api/browser/status` carries a `credentials` block:
`provider` (the provider's name, or `null`), `enabled`, `domains`, `state`
(`none|pending|disabled|applied|needs_refresh|skipped|error`), `reason`,
`applied_at`, `applied` (cookies set) and `provider_status`. `provider`, `enabled`
and `domains` are read from the live config on every read, so a flipped switch
reads back flipped at once; the apply's outcome (`state`, `applied`) is kept only
while the browser it filled is still up and the settings are the ones it applied,
and otherwise `state` says what comes next (`pending`, `disabled` or `none`).
Every apply that set a cookie writes ONE SEL row (`browser_harness.credentials_injected`,
caller `gateway`): provider name, reason, cookie count and at most 20 printable
domains. Nothing here returns, logs or audits a value; the value travels only in
the `Storage.setCookies` params the relay writes to the browser. No helper or op
the agent reaches reads cookies, and the snapshot walker never reads
`document.cookie`. There is no dashboard route that writes or reads cookies.

**No sandbox list changes.** The provider runs in the gateway, so it needs no
sandbox access, and an edition keeps its own source files hidden with its own
sandbox policy.

**Accepted residuals.** (i) The agent can browse as the signed-in user on those
sites, and a page's own script reads non-`httpOnly` cookies as it always would;
what is withheld is the value as data. (ii) On Windows Kiro's Chromium keeps its
debugging port (see Security), so a local process that finds it can read the
cookies the same way it could already drive the browser. (iii) One browser serves
every chat on the gateway, so the provider's cookies reach every agent browsing
here, members and restricted sessions included; the opt-in and its domain
allowlist bound that exposure. Turning the switch off stops further use from the
next page load; cookies already set stay until the browser closes, and it does not
undo a page already read.

### The relay

- **One upstream connection, one secret.** The relay listens on `127.0.0.1:0`
  and every path it serves carries a per-start secret (`/<secret>/json/version`,
  `/devtools/browser/<secret>`); the bare Chrome-shaped paths and a wrong secret
  are 404 before anything is forwarded. The sandbox does not fence loopback, so
  a process the agent runs CAN find the port by probing; the secret is what it
  does not have -- it is in the daemon's environment and nowhere else. The
  upstream is a websocket (the user's Chrome; Kiro's Chromium on Windows) or the
  debugging pipe (Kiro's Chromium elsewhere).
- **Disjoint ids.** Every daemon request is renumbered and mapped back on reply,
  so several clients never collide; the relay's own requests use ids from
  1,000,000,000 up.
- **Private sessions.** The relay attaches its own flat sessions for screencast
  and input. Replies and events on those sessions stay in the relay and never
  reach the daemon's shared event buffer.
- **Agent tabs stay live.** Every target the daemon creates gets focus emulation,
  so a tab behind another tab keeps painting and Harness's input calls on it do
  not hang. The user's own tabs are never attached.
- **Tab ledger.** Each target the agent creates is recorded at the create reply,
  whether or not the page then loads; `tabs` lists only those and session end
  closes only those.
- Late `screencastFrameAck` errors after a stop are swallowed; close is clean.

### The stream

`Page.startScreencast` on a relay-owned session: JPEG, quality 60, 1280 wide.
Chrome emits a frame only when the page changes, so a still page costs nothing.
The relay acks a frame to Chrome only after the card has acked that frame's
sequence number (bounded at 2 s, so a card that stopped acking slows the stream
instead of freezing it, and the bounded wait counts as a late frame), on a fixed
time grid (`due = max(last_due + period, now - period)`), so transit time counts
toward the interval. Measured in the RFC: 57.6 fps headed and 57.8 fps headless
at cap 60 on macOS (53.5 under ack-after-sleep; headed Chrome tops out near 95
fps on a 120 Hz display, headless Chromium's compositor is clocked at 60, and
lifting the cap reaches 81-84 fps at about 450% CPU, so the product does not).

Once a second the stream picks a frame rate and a picture, as two axes:

| Axis | Steps | Changing it |
|---|---|---|
| fps | 60, 45, 30, 20, 15, 10, 5 | pacing only, Chrome untouched |
| picture (quality / width) | 60 / 1280, 45 / 960, 40 / 800, 30 / 640 | restarts the screencast on the same session |

A slow link costs frame rate first. The fps drops at once when more than 20% of
frames arrive later than 1.5 periods or the link cannot carry it, as far as the
measured rate needs, and climbs after 2 calm seconds to the best step the link
covers with 30% headroom. The picture drops one step (or as far as the measured
frame time needs) only when, at 5 fps, one frame has taken over 0.5 s to reach
the card for 2 windows in a row; it climbs one step after 5 windows in a row in
which the sharper picture's frame would land in time with 30% to spare. Frames are
acked one at a time, so a high round trip alone only slows the frame rate: on
2 Mbit/s with a 150 ms round trip the stream runs at about 2.5 fps and keeps the
1280-wide picture. A window with no frame (a still page, the agent thinking) is no
evidence and changes nothing. The hub's `rung` is `picture * 7 + fps step`.

**Link memory.** A stream starts where the last stream of the same card on the
same relay stood when it ticked or stopped, within 10 minutes, so a new turn or a
tab switch does not re-learn a slow link from the top. The memory is weakly keyed
on the relay, so a closed browser forgets it.

**Settle frame.** When no frame has come for 400 ms (and none since the last
settle), the screencast restarts once at JPEG quality 85 and 1280 wide; Chrome
answers a `startScreencast` with one frame of the current page, which is masked
and sent like any frame and is not ladder evidence. A frame within 250 ms of that
one still belongs to it; the next one after is a page change and restarts the
cast at the ladder's picture. At most one settle per 2 s (a blinking text cursor
changes the page every half second), none while the person holds the
browser, none with a fixed `medium` or `low` quality. `Page.captureScreenshot`
was measured and rejected for this: on Chromium it makes the running screencast
emit two more frames at the ladder's picture, which would cover the sharp still.

An off-screen or collapsed card stops the screencast (the card reports
visibility), unless the turn replay records: then the cast keeps running for
the recorder only (see Turn replay). Input sent with no frame for 3 s restarts it. While the person holds
the browser the frame rate is capped at 15 fps; the picture stays. `browser.live.*` holds the knobs: `enabled`, `quality`
(`auto|high|medium|low`), `max_fps` (15|30|60), `mask_passwords`; `max_fps` caps the
fps axis and a fixed `quality` caps the picture axis. They are read per stream start from the
live config snapshot, so a Settings change applies to the next turn, never to a
cast under the person's hands. With `mask_passwords` (default on) the page is
asked, at most every 250 ms, for the boxes of visible `input[type=password]`
(a read-only `querySelectorAll` + `getBoundingClientRect` on the relay's own
session, walking open shadow roots to the same depth the snapshot walker does),
scaled from CSS to frame pixels; a failed query keeps the last boxes rather than
unmasking. The boxes are kept in CSS pixels and scaled per frame, so a frame at
another width (a picture change, the settle frame) is masked at its own scale.
Every clock read and sleep is injected, so the ladder and the settle frame are
tested on a fake clock.

**Wire.** Owner websockets only. State changes are the JSON event
`browser_live` (`session_key`, `turn_id`, `state`, `url`, `title`, `browser`,
`headless`, `step`, `steps`, `rung`, `fps`). Frames are BINARY messages: a
4-byte big-endian header length, a UTF-8 JSON header
(`{"type":"browser_frame","session_key","stream","seq","w","h","mask":[[x,y,w,h],...]}`),
then the JPEG bytes. `stream` names the screencast the frame came from and `seq`
restarts at 1 with every stream (a new turn, a tab switch, a gateway restart),
so the card scopes its once-per-frame ack to `(stream, seq)` and a new stream is
never held behind an old stream's high-water mark; the ack body names the stream
too, and the hub ignores an ack for a stream that is no longer the session's. `mask` holds the boxes of
focused or visible password fields, asked of the page before the frame is sent,
clipped to the frame (a full-width field must not produce a box the card's
decoder would refuse) and painted over by the card.
`detail` rides on a `state: error` event and is rendered verbatim.

**The hub and the turn.** `live_hub.LiveHub` holds one `LiveSession` per slot
key: turn id, step strip, state (`waiting_allow | live | in_control | done |
login_needed | error`), input lock and stream. The glue is in the op route
(`dashboard/handlers/browser_ops.py`), the one place that knows both the session
and the browser: the FIRST `browser` op of a turn opens the hub's turn with a
fresh `turn_id` (stable until the chat runner closes it from `_end_turn_tail`,
`chat_turn/queue_cycle.py`, when the agent's turn ends -- except while the person
holds the browser: an agent that cannot wait for the hand-back ends its turn
mid-takeover, and folding the card then would stop the stream under their hands,
so the card stays theirs and the hand-back itself folds it), every op is a step, and
after each op the card follows the daemon's current tab -- one cheap
`current_tab` helper call; a new target (first op, `tabs new`, `tabs switch`)
starts or restarts the screencast on it, the same target only refreshes the
address pill. One daemon serves every chat, so every op runs under one
gateway-wide lock and is preceded by a tab bind: the session keeps the agent tab
it was bound to while the browser still has it (`switch_tab` back to it if the
daemon moved), adopts the daemon's current tab when no other session owns it,
and otherwise opens a tab of its own. Chat B therefore never clicks on chat A's
page, `tabs` lists and switches only the caller's own tabs, and a takeover in
chat A fences A's tab while B keeps browsing. With `browser.live.enabled` off
the hub publishes no event at all, so no card is mounted; the ops still run. While the owner's Chrome shows its Allow sheet the op route tells
the hub `waiting_allow`, bounded at 60 s per op (the Allow sheet itself outlasts
the op). The session key the shim resolves (`dashboard:<slot>` or the bare slot)
and the key the card posts (`activeSlot`) meet through
`state_parts.slot_directory._normalize_slot_key`.

### Takeover

Clicking the frame posts `POST /api/browser/input/claim`. Every live-view and
input route (`/api/browser/live/visibility`, `/api/browser/live/ack`,
`/api/browser/input/claim`, `/api/browser/input/release`,
`/api/browser/input/event`, `/api/browser/live/open-in-browser`,
`/api/browser/live/reopen`, `/api/browser/live/close`) accepts the
dashboard owner's cookie or token and refuses the internal secret with 403, so
an agent cannot claim input for itself, and validates that the caller owns the
session. While claimed, mouse, wheel and key events from the card go to
`Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` on the relay's input
session; keystrokes are never logged and never enter the transcript. The event
body is `{type: mouse|wheel|key, action: down|up|move, x, y, w, button, clicks, dx,
dy, key, code, text, modifiers}` in frame pixels (`action` is what lets the card
drag and hold keys; without it a mouse event with a `button` is a click and one
without is a move; a move carries `clicks: 0` and the held button, so a drag
stays a drag), every field bounded before it is sent (`InputEventError`
`invalid_input_event` otherwise). CDP takes viewport CSS pixels and the JPEG is
narrower than the viewport whenever the picture is below the top one, so
positions and wheel deltas are divided by `frame width / deviceWidth` at the one
place the event becomes CDP. The frame width is the card's: a pointer or wheel
event carries `w`, the width of the frame the card drew and mapped against
(`takeover.frame_scale`). Consecutive frames differ in width (a lower rung, then
the sharp settle still, and frames the card never drew at all), so the stream's
own latest `frame width / deviceWidth` can belong to another frame: over a slow
link that put a click on a 1280-wide frame at twice its place, off the field or
off the page, while every request answered 200. The stream's latest ratio is
only the fallback for an event without `w`. A hand back the gateway
refuses puts the card back in control with the gateway's text in an inline
`ErrorNotice`; the lock is still held, and Hand back is the retry. A take over the
gateway refuses leaves the card live with the same notice, and Take over is the
retry. The claim and the op route share one per-session fence
(`LiveHub.op_fence`): the route holds it from the tab bind to the op's end, the
claim takes the lock first (so no new op starts), cancels the op in flight and is
confirmed only once the route has left the fence, so a takeover never lands
inside an op; `in_control` is published only if the lock is still held after the
claim's own awaits (an Esc in between has already said `live`). A claim on a
finished card (`done`, `error`) is refused (409 `turn_over`). **An agent op is
not lost while the person is in control: it waits for the hand-back and then
runs**, which is how the agent resumes by itself after a sign-in. The wait lives in
the TOOL, not in one HTTP call: the op route answers at once with
`user_in_control` and `hold: true`, and the `browser` tool parks itself as a
`DeferredTool` (the shim runs one worker thread, and the ACP tool-stall watchdog
ends a tool call silent for 600 s) that every `HANDBACK_POLL_S` (5 s) posts
`/api/browser/handback` -- a short long-poll of at most `HANDBACK_WAIT_S` (3 s) on
`LiveHub.wait_for_release`, the awaitable the release sets -- and every 30 s pings
the session keepalive. On `released` the op is posted again and runs. Bounded by
`HOLD_TIMEOUT_S` (10 minutes) in the shim, after which the agent is told
`user_in_control` ("held for 10 minutes; tell them you are waiting and end your
turn"). An op the claim cut short mid-flight answers `interrupted` with `hold`:
the tool waits for the hand-back and then tells the agent to snapshot first; the op
is never re-run blind. A cancelled tool call stops stepping. The turn's thinking
line reads "Waiting for you to hand the browser back". Esc or "Hand back"
releases; the turn's end releases too unless the person holds the browser then
(the card stays theirs and the hand-back folds it). The claim is plain HTTP, so a
card closed mid-takeover sends no release: a claim that outlived the turn it was
taken in (held past the turn's end, or taken on a reopened view) is handed back,
and the card folded, once the card has shown no sign of being open (a claim, an
input event, a frame ack, scrolling into view) for `TAKEOVER_IDLE_S` (5 minutes).
While it holds the browser on screen the card re-posts its visibility every
`TAKEOVER_HEARTBEAT_MS` (60 s, `useBrowserLive.ts`), since a static page sends no
frames to ack.
It is checked wherever the agent side asks who holds the browser
(`in_control`, the hand-back poll, a new turn's first op), so the next op frees
the chat. A claim inside its own running turn is bounded by the hold instead. A
held claim never sits on the gateway-wide op lock, so other chats keep
browsing; one waiting behind another chat's op answers `browser_busy` after
`LOCK_WAIT_S`. Keystrokes typed into the
frame stop at the canvas (`stopPropagation`): the dashboard's window-level
shortcuts (Alt+digit switches session) never fire mid-takeover. A pointer
release outside the drawn frame is clamped to its edge, never dropped, so the
page's button is released where the drag ended. "Open in Chrome"
(`Target.activateTarget`, plus `Page.bringToFront` while a stream session is
attached) focuses the real tab for what the frame cannot carry (a password
manager's autofill popup, a file picker), and still works from the finished
card: the tab outlives the screencast. The eight routes refuse the
internal secret (403 `internal_secret_required`), require the dashboard owner
(403 `owner_only`) and accept only a `session_key` naming a slot this dashboard
owns (404 `unknown_session`).

**The hand-back note.** While the person holds the browser, the card, the side
panel's big view and the phone sheet show a hand-back bar
(`pages/chat/BrowserLiveHandback.tsx`) under the picture: two to four choices
and a one-line note. The choices are the ones the agent's parked op offered
(the tool's optional `handback_options`, at most four strings of at most 40
characters, sent on every `/api/browser/handback` poll and published as the
event's `handback_options`, cleared by the hand-back and a new turn), else the
card's own Done, Didn't work, Wrong site, Stop (`pages.chat.browserLiveHandback.*`).
A choice is a single-choice toggle; Enter in the note (or its send button) hands
back with the choice and the note; Esc in the note, Esc on the picture and the
header's Hand back hand back with nothing said. The bar is outside the canvas,
which forwards only keys that reach its own element, and the note field stops
propagation, so nothing typed there reaches the page. `POST
/api/browser/input/release` takes `{session_key, note?, choice?}`: the note is the
person's own words, at most `HANDBACK_NOTE_MAX` (500) characters, the choice at
most `HANDBACK_CHOICE_MAX` (40); anything longer or not a string is 400
`invalid_note` / `invalid_choice` and nothing is handed back; control characters
are dropped. The answer's `delivered` says where it went:

* `agent`: an agent op of this turn is parked on this takeover: the op route
  answered it `hold` (`LiveHub.park`, `LiveSession.parked`), so the tool polls
  `/api/browser/handback` until the hand-back. The flag is cleared by the
  hand-back, the turn's start and its end. The hub keeps `{choice, note}` on the
  session (`LiveHub.take_handback` takes it once; a second note while one is
  untaken goes to chat); the poll answers `released` with `handed_back`, and the
  tool appends one line to the op's result (or to the `interrupted` text):
  `handed_back: {"choice", "note"}`, marked as the user's own words, not page
  content. Should the poll go away without taking it, the turn's next op answer
  carries it.
* `chat`: no op is parked -- the turn is between ops (it may end without
  another), a view was reopened after its turn, or the turn ended under the
  takeover. The card fires `mc:browser-handback-chat`
  (`BROWSER_HANDBACK_CHAT_EVENT`), and the chat page sends `choice: note` as the
  person's next message to that slot, as if typed in the composer (queued
  behind a running turn). So a note is never held for an op that may not come.

The note never carries page content, is never logged on the input path, and is
recorded on the turn replay as the person's event (`actor: user`, label
`You handed back: <choice> - <note>`), cleaned like every label.

### The live card

One card per turn, attached to the turn's first `browser` call (anchor =
`tool_call_id`, else `ts`), rendered after the turn's last browser row and never
folded. A browser row is recognised by the stamped `tool_name` when there is one
(`browser` or a server-qualified `…/browser`, `…::browser`, `…___browser`) and
otherwise by its pill text, which for the managed-MCP tool reads
`🔧 @kirocrew-core/browser` -- the `@server/tool` form every MCP call arrives in; a new `turn_id` closes the previous record as done. Takeover grows the
same element; it never swaps in another component. Frames are decoded from the
binary message (the TS mirror of `decode_frame`, every field checked), acked ONCE
per `(stream, seq)` even when no live turn shows them (so the relay never
stalls), kept in a ring (1800 frames / 48 MB; only the newest two turns of a
session keep theirs) and painted to a `<canvas>` outside React's render
cycle (`createImageBitmap`, newest frame wins, mask boxes after the image); the
first frame is the one React render, later frames none. Visibility is posted only
on a change of (any card instance showing the CURRENT turn intersecting) AND
(document visible): a finished turn's card still on screen does not keep a later
turn's screencast running while that turn's card is scrolled off. A turn no card
shows (another slot's browsing, a card the virtualised transcript has not
mounted) is reported hidden after 30 of its frames, so its screencast stops
instead of streaming into memory nobody looks at, and resumes when a card mounts. A claim is optimistic and reverts on a
refused POST; so does a hand back (see Takeover); Esc releases and is never
forwarded; nothing on the input path is logged. Replay plays the frames the
stream already sent, kept in memory for the life of the page, original spacing
capped at 500 ms per gap. Strings live at `pages.chat.browserLiveCard.*`.

**The loop line.** While a loop op (`reflex`) runs, the event carries `loop`:
`{moves, rate, steps, max_steps, seconds, max_seconds, stop}` (every key present,
a counter the op did not send is `null`, `stop` is `null` while it runs and then
the loop's own short token: `done`, `until`, `stuck`, `risk`, `budget`,
`user_in_control`, or a rarer one it names), otherwise `loop: null`. The reflex
loop reports once as it starts, after every move and once with its stop
(`OpContext.loop` -> `LiveHub.loop`); the line belongs to the op that ran the
loop, so the next op and a new turn clear it. The card and the big view show
"Fast mode: 37 moves · 6.2/s", a thin bar for the larger of `steps/max_steps`
and `seconds/max_seconds`, and once stopped one short sentence per token
(`pages.chat.browserLiveLoop.stop_*`, `stop_other` for any other). Stopping it
early is Take over (the loop checks for the person at every step). A step
reported while a loop runs carries `loop: true`; a run of them in the step strip
and the big view's step list shows its newest three behind one "N earlier steps
folded" control that opens the run, and a run never folds the step the replay is
on. The card renders these fields only: no loop-specific copy or op name lives in
the card.

**The big view.** Expand (the live card's menu, first item) and the done card's
Replay open `useBrowserLive.stage`: on a desktop chat page the side panel's
Preview tab switches to **Agent browser** (`components/PreviewTabBody.tsx`), the
chat card folds to one line ("Live/Replaying · host · Showing in side panel" and
Bring back to chat), and the stage (`pages/chat/BrowserLiveStage.tsx`) shows the
picture big; on a phone, or a page with no side panel, it is a full-screen sheet
(`BrowserLiveSheet.tsx`). Local preview on the Preview tab's mode row, the Bring
back button on that row, the panel closing or leaving the tab all bring the
picture back to the chat; the local preview stays mounted (hidden) throughout, so
its page and annotation survive. The stage has a Full screen toggle
(Fullscreen API, hidden where it is unsupported), Take over and Hand back as on
the card, and the replay timeline (`BrowserLiveTimeline.tsx`): a scrubber with one
marker per action, play/pause, previous/next step, 1x/2x/4x, Live while the turn
runs, and the step list. Replay from the done card starts at 0:00; clicking a
marker or a step pauses there; scrubbing a running turn leaves Live and Live
returns. Markers that would cover each other (centres under 20 px apart) share one
marker with a count, placed at the group's first action and seeking to exactly
that action. Overlays draw the action at the play head for 1.2 s: a ripple at a
click, the drag path, a scroll arrow, a type/key chip, the URL on a navigation,
in the pixels of the frame that is showing; while an action with a point is drawn,
the first frame recorded at most 1 s after it is shown (its point belongs to the
page after the target was scrolled into view). Recordings come from the routes in
[Turn replay](#turn-replay); with none (an older gateway's 404) the stage plays
the card's in-memory frames and step log.

| State | Shows |
|---|---|
| Waiting for Allow | which browser, the one step, no timeout |
| Live | address, live dot, which browser, Take over, frame, current action, step strip |
| Hover | "Click to take over" |
| In control | grown card, accent border, "You are in control", Open in Chrome, Hand back (Esc) |
| Done | one line: thumbnail, step count, duration, Open live (tab still open) or Open in Chrome, Replay; "tab closed" once it is gone |
| Reopened | the Live and In control faces under the finished turn's id, plus Close live view in the menu |
| Login needed | "Take over to sign in" |
| Error | the thumbnail and `detail` through `ErrorNotice` |

The Allow face's "Use Kiro's own browser instead" opens `/settings/browser`.

**Reopening a finished card.** A turn's end stops the screencast, but the agent's
tab stays open (headless and Kiro's own Chromium as much as the person's Chrome),
so the done line offers **Open live** (the button, or a click on the thumbnail)
while that tab is still there. `POST /api/browser/live/reopen`
`{session_key, turn_id}` calls `LiveHub.reopen`: it restarts the screencast on the
tab the card last showed (`LiveSession.shown_target`), through the running
supervisor's relay (read, never started: a browser that is not running has no such
tab), under the session's op fence, and the SAME card goes `live` again under the
SAME `turn_id`, with the fit-to-view of the live form, Take over and Hand back
exactly as during a turn. It runs no op and moves no daemon tab, so it does not
take the gateway-wide op lock; the url gate is untouched (the person's own input
was never behind it). Refused: a `turn_id` that is not the session's newest card
(409 `stale_turn`; only the newest card shows Open live), a running turn (409
`turn_running`, the agent's ops own the stream), `browser.live.enabled` off (409
`live_off`), and a tab the relay's ledger no longer lists, a relay that hung up, or
a tab another session owns (409 `tab_gone`). The event carries `turn_over` (the
card's turn has ended; with a live state this is a reopened view) and `tab`
(`open`, `closed`, or `""` when the card showed no tab); `tab: closed` puts "tab
closed" on the done line and drops Open live, and Replay stays. A reopened view is
the person's: a later turn that never browses ends without folding it
(`turn_ended` on a session whose turn already ended is a no-op), Hand back keeps it
live, and **Close live view** in its menu (`POST /api/browser/live/close`,
`LiveHub.close_view`) folds it back to the done line. A turn that browses after a
reopen opens a NEW turn (`_open_turn` checks `turn_over`), the running stream
carries over to it, and that turn's end folds it as usual; an agent op while the
person holds the reopened tab is held for the hand-back like any other. The done
line keeps two actions: Open live and Replay when the tab is open (Open in Chrome
moves into the live view's menu), else Replay and Open in Chrome.

### Turn replay

`browser_harness/recording.py` keeps one recording per (session, turn) so the
card can play a finished turn back: what the page looked like, and what the agent
(or the person, during a takeover) clicked, typed, dragged and scrolled, and when.

**Capture.** The hub attaches the gateway's one `LiveRecorder` at startup
(`bind_live_hub`). `turn_started` opens the turn's recording (a reopened view
reopens the same turn), `_fold` ends it. Frames come from the stream through the
sink's recorder seam (`_StreamSink.records` / `wants_frame` / `record`). When the
recorder's 5 fps gate is open (`wants_frame`), the stream runs a box query FOR
THAT FRAME, bypassing the card's 250 ms cache and even with
`browser.live.mask_passwords` off (the card's header then still carries no mask),
and hands the recorder the JPEG with those boxes and `mask_ok`. `mask_ok` is true
only when the query ran for the frame, answered `{boxes, frames, partial}` (or a
bare box list), was not `partial` (nesting past the walker's depth cap), and
reported at most `MASK_BOX_LIMIT` (32) boxes, and, when Chrome stamped the
frame with its scroll offset (`scrollOffsetX/Y`), read the page at that same
offset (`sx`/`sy` in the reply, within `MASK_SCROLL_TOLERANCE_PX`, 1 CSS px): a
wheel scroll between the capture and the query would otherwise put every box
in the wrong place or off the frame; a throw, another shape or a page
exception is unknown. A secret field is `input[type=password]`, an input whose
`autocomplete` names `current-password`, `new-password` or `one-time-code` (a
"show password" toggle usually keeps it), or one styled with
`-webkit-text-security`. The query walks open shadow roots and same-origin iframes
(offset by the iframe's content box: border plus padding); every visible embedded
document it cannot read or place exactly (cross-origin, a scaled or transformed
iframe, any `object`/`embed`/`fencedframe`/`portal`) is reported in `frames`,
which the recorder paints over and the card ignores. A frame that arrives while
the gate is shut is held; the stream's once-a-second tick offers it (`flush_held`)
only when no newer frame replaced it, it is under 1.5 s old, its size matches, and
a fresh query finds exactly the boxes the query before it found, so the password
fields provably stood still across its window; otherwise it is dropped. A held
frame from before the turn began is dropped by the recorder. The stream also keeps casting while the card is hidden, sending nothing to
the card and acking Chrome at most every 200 ms (`HIDDEN_RECORD_PERIOD_S`), so a
turn nobody watched is still recorded. A box query that answers with something
other than the expected shape keeps the last boxes for the card, like a query that
throws.
Agent actions come from the op route generically: `LiveHub.op_mark` before the op,
`LiveHub.record_op` after it, which takes the label of the step the op reported
and the point from the op's own result (`x`/`y`, or `from`/`to` for a drag),
multiplied by the stream's `scale` onto frame pixels. An action a loop op runs
inside itself (each `reflex` move) is its own event, through `OpContext.mark` /
`OpContext.record` (`LiveHub.inner_mark` / `record_op(..., loop=True)`), tagged
`loop: true`; the loop op then records no summary event of its own. The person's
takeover input is recorded from the validated `InputEvent` after it was
dispatched, and a hand-back note as their event.

**Hard rules.** A frame is kept only with `mask_ok`, and its boxes, grown by
`MASK_PAD_PX` (4) on every side for a field that moved between capture and query,
are painted into the JPEG itself (Pillow) before it is written; an unknown mask, a box that
does not fit the frame, a JPEG that does not decode at its header size, or no
Pillow drops the frame, and an unknown mask also drops a frame held for the rate
gate. Frames are kept only when they differ from the last kept one, at most 5 fps
(`MIN_FRAME_INTERVAL_MS`): a burst collapses onto its latest frame, kept at the
next frame past the interval, the next action or the turn's end. Event args pass
an allowlist (refs, coordinates, buttons, keys, bounded strings); every URL, in
args, `url`, `start_url` and inside labels, keeps scheme, host, port and path only
(no userinfo, query, fragment or `;` path parameters, where sign-in codes and
tokens ride), and a scheme other than http(s), ws(s), file, about or chrome keeps
only `scheme:` (`javascript:` and `data:` carry their payload in the URL); a label
is cut from its first URL to that URL cleaned, and at any `?` or `#`; a `type`
whose args say `secret`, or that did not succeed (a password-field refusal, a
stale ref, an interruption), keeps `chars` and `secret: true` only, and a
`press_key` that did not succeed keeps no single-character key; the person's typed characters are only
ever counted (one `type` event of `chars` when the typing stops), named keys are
`key` events, a press-move-release is a `click` (under 4 px) or a `drag` with its
path (at most 64 points). Ops fold onto `click|type|key|drag|scroll|navigate|other`
by a generic name table; an unknown op is `other`.

**Storage.** `<data home>/browser-recordings/<sha256(slot key)[:32]>/<turn_id>/`
with `meta.json` (atomic rewrite), `frames.jsonl`, `events.jsonl` and
`frames/<i>.jpg`; directories 0700, files 0600, every file opened `O_NOFOLLOW`.
`browser.recording.max_session_mb` (200) and `max_total_mb` (1024) cap the bytes;
before each frame the oldest finished turns go first (the session's, then any
session's); a running turn that alone cannot fit stops keeping frames and is
marked `truncated`, its action log continues (at most 5000 events). The index is
read back from the `meta.json` files on first use; a turn that was running when
the gateway went away reads as ended at its last frame or event. Deleting a chat
(`slot_teardown._remove_browser_recordings`) removes the recordings of every
spelling of its slot key that no live slot still holds; the writer queue of that
session is drained first so a late frame cannot recreate it. `browser.recording.enabled`
off records nothing.

**Routes.** Owner-only GETs in `dashboard/handlers/browser_recordings.py`, same
trust class as the card's POSTs: the internal secret is 403
`internal_secret_required`, a non-owner is 403 `owner_only` before the path is
parsed (so a refusal never says whether a session or turn exists), and any miss
is 404 `not_found`. The session must be a slot this dashboard owns; every segment
is matched against a strict pattern (`TURN_ID_RE`, `<digits>.jpg`) and the files
are looked up through the store, never by joining the parameter onto a path.
Every answer is `Cache-Control: no-store`, `nosniff`, `Cross-Origin-Resource-Policy:
same-origin`.

* `GET /api/browser/live/{session}/recordings` ->
  `{"turns":[{turn_id, started_ts, ended_ts|null, live, frame_count, event_count,
  bytes, start_url, truncated}]}`, oldest first.
* `GET /api/browser/live/{session}/recordings/{turn_id}` -> `{turn_id, started_ts,
  ended_ts, live, width, height, start_url, truncated, frames:[{i, ts, w, h}],
  events:[{id, ts, op, label, args, point:{x,y}|null, path:[{x,y}]|null,
  url|null, status: ok|error, actor: agent|user, loop?: true}]}`. Times are epoch ms,
  coordinates frame pixels; `args.name` is the tool's op name.
* `GET /api/browser/live/{session}/recordings/{turn_id}/frames/{i}.jpg` -> the
  masked JPEG.

### The tool

`browser` is one MCP tool in the `kirocrew-core` shim with fifteen ops
(`browser_harness.ops.OPS`), not a tool namespace. The shim runs two gates before
anything leaves the process: `capabilities.browse` governance (a denial refuses
outright; there is no other browser) and the navigate gate (public http(s) only,
plus the exact loopback `host:port` entries of `dashboard.browser_local_origins`,
never the gateway's port; applied to `navigate` and to the URL a new tab opens
with). It then posts `{session_key, op, args}` to `POST /api/browser/op` (strict,
internal-secret only) and renders the gateway's answer. It holds no state.

`browser_harness.ops.run_op(op, args, ctx)` runs in the gateway. It reaches the
browser only through `ctx.helper(name, **kwargs)` -- one allowlisted Harness
helper over the daemon's IPC -- and the `js` helper is only ever given
`snapshot_walker.js` plus one method call. The walker installs `window.__kcWalker`
per document; it mints `eN` refs into `window.__kcRefs` (dropped when the element
leaves the DOM), never reads a password or credential-shaped value, and hooks
`console.*` so `console` returns in-page messages since the last navigate or
snapshot.

| Op | Behaviour |
|---|---|
| `navigate`, `back` | stamp the old document and wait for a new one (or a matching href) with an injected clock |
| `snapshot` | the accessibility walker, run in the page; refs are per snapshot |
| `click` | a ref or `x`,`y`; `clicks: 2` is a double click |
| `drag`, `hover`, `scroll` | ref or coordinates (`mouse_move` / `drag` helpers) |
| `type` | refuses a password field twice: by the ref's type and by whatever holds focus when the text would land |
| `press_key`, `select_option`, `wait_for` (text / textGone / time / load), `console` | as named |
| `screenshot` | writes a PNG under the screenshot directory below and returns its absolute path |
| `tabs` | lists and switches `relay.agent_targets()` only |
| `reflex` | a loop of the ops above over a closed move set, judged by the decision seam, in `step`, `realtime` or `virtual_time` mode; `not_available` when it is off ("The fast loop" below) |

Every op calls `live_hub.step`; `run_op`'s own `in_control` check is the backstop
under the route's fence (the route holds the op until the hand-back, see Takeover).
A refusal is a value (`{ok: false, code}`); a transport
failure is the route's `browser_error`; `timeout` is the route's budget (20 s, 75 s
for the slow ops, each plus the decision hooks' 12 s headroom); `browser_busy` is the bounded wait for another chat's op on the
gateway-wide lock (75 s plus the hooks' 12 s headroom); `not_installed` comes from `browsing_available(home)`.
The budgets live in one module (`browser_harness/budgets.py`) that both the route
and the tool shim import: the shim's HTTP timeout is DERIVED as the route's worst
case (browser start + the 10-minute hold + lock wait + bind + op) plus a margin, so the tool never
reports an op failed while the gateway is still landing it -- a retry of a click
that landed is a double click. The two focus checks (`type` without a ref,
printable `press_key`) and the frame mask resolve the deepest active element
through open shadow roots (`document.activeElement` is the shadow HOST for a
web-component login form). The
`{{BROWSER_BEGIN}}..{{BROWSER_END}}` section of `config/prompt.md` and every
builtin skill that drove a browser (`web-browse`, `web-verify`, `web-preview`,
`browser-auth`, and the rest) speak only of this tool; `browser-auth` is "take
over to sign in".

**What the outline walks.** A `display: contents` wrapper, and a 0 x 0 box that
hides nothing (not `display: none` or `visibility: hidden`, no clipping overflow,
`contain` or clip), are walked through: they emit nothing and each child judges
its own visibility. A password field an ask would refuse has no ref and says why
(`[covered]` or `[not usable yet]`); a text box something lies over keeps its ref
and is marked `[covered]`. A cross-origin iframe is a slot the gateway reads
itself (`browser_harness/frames.py`): an out-of-process one through its child
session, a same-site one Chrome keeps in the tab's process through the tab's
session in a fixed isolated world of that frame, in both cases only directly
under the tab and named `f<tag>e<n>`. The quick ask's hit test at a field in a
frame passes when the browser's topmost node there is the frame's `<iframe>`, or,
for a same-process frame, a node of that same frame.

### Decision-model help (`browser.step`)

An optional add-on: with no decision model, or with any of its gates closed, every
op runs exactly as the table above says. `browser_harness/decide.py`
(`BrowserDecisions`, set on `OpContext.decisions` by the op route with the caller's
own session key) is the only browser module that reaches the decision seam, and it
imports `kiro_crew.decisions` inside functions only and names no provider: every
request goes through `decisions.gate.decide` and the provider registry
(`docs/system-specs/modules/decisions.md`, section 15, owns the point, its scopes
and its gates). Three uses:

| Use | Ops | Request | Used when | Otherwise |
|---|---|---|---|---|
| pick | `click`, `hover`, `type`, `select_option` with `target` (a description) and no `ref`/`x`/`y` | one Choice over the outline's refs, each option `role "name"` plus the nearest heading above it (at most 200, the ones sharing most words with `target`), with Nouls `present`, `error_shown`, `not_found`, `bot_check` | `present > 0.7`, the pick's p >= 0.6, no risk Noul > 0.7: the op runs on that ref and the result carries `picked` | refused `needs_ref` |
| verify | `click`, `type`, `navigate` that succeeded | before/after outlines, or before/after screenshots with the image scope; Nouls `as_expected`, `changed`, `error_shown` | any valid answer: the result carries `verify` `{as_expected, changed, error_shown: yes/no/unsure, by: outline/screenshot}` | the result is today's, byte for byte |
| box | `click`, `hover` whose pick found no control (`present < 0.3`) on a page with a visible `<canvas>` (`canvasRects`) | one screenshot, Noul `present` + Box `where` | `present > 0.7`, confidence >= 0.6 and the box centre inside a canvas: the op runs on that pixel | refused `needs_ref` |

A `target` with no hook to resolve it (`OpContext.decisions` unset, or every gate
closed) is refused `needs_ref` without touching the page, so the agent is told to
pass a `ref` rather than having text typed into whatever holds focus.

Cost when off: no network, no governance probe and no page call. Only the ops above
pay one keystone read off the event loop (`gate.is_enabled`), and none when the
configured provider is `none`. Each request is capped at 500 ms (text) or 800 ms
(image), and by `provider.timeout_ms`; the hook's own page work is bounded too
(everything before the op -- outline, pick, canvas locator, before-frame -- 6 s,
one frame with its two checks 3 s, the whole check after the op 6 s), and past any of them the op's own result is returned as it is. The
route adds their sum (`budgets.DECISION_HEADROOM_S`, 12 s) to every op's budget, and
the lock wait and the shim's derived timeouts include it, so a slow hook never eats
into the op's own time and a finished op is never reported as a timeout. It all
runs under the op lock, so these caps are also the longest it holds the lock.

What leaves: the `target`, the page title and the walker's outline (no field
values of password or secret-named fields, no cookies, no URL), scrubbed by the
gate. A screenshot under the `browser_images` scope -- on by default once consent
stands, off for an explicit no -- and only to a provider that declares images: taken with `max_dim` 1568, read and deleted at once from the
screenshot directory, never sent while the walker's `passwordBoxes` is not 0 --
the live view's mask rule (any `input[type=password]` with a box, open shadow roots
included) plus same-origin frame documents and any filled field the walker treats
as secret -- or while the person holds the browser (`ctx.in_control`). Both are
checked before AND after the capture, and a frame that fails the second check is
discarded. A cross-origin frame cannot be read, as for the live view; a password
input renders as bullets there too. When a frame cannot be taken the step check
falls back to the outlines. The box is scaled by the viewport size `canvasRects`
returns with the canvas boxes, so it lands in the CSS pixels `click` uses. Tests:
`test/test_browser_decisions.py`.

#### The fast loop (`reflex`)

`reflex` is one op that runs many actions with no LLM turn between them
(`browser_harness/reflex.py`, with `reflex_space.py` for the action space and
`reflex_realtime.py` for the timed modes, reached from `ops.py` by a call-time
import). Each decision is ONE request built and sent by `decide.reflex_step`, so
`decide.py` stays the only browser module that reaches the seam, and it speaks
only the seam's own question types. The agent gives a `goal` and a CLOSED move
set: `keys` (named keys or single characters), `drag` (`"eA>eB"` or
`"x,y>x,y"`), `scroll: true`, `type` (`"eN=value"`), and `targets` -- the
operations (`click`, `hover`, `type`, `select`) whose target the model picks from
the page's own element table, limited to `click` refs when those are named and,
for `type`, filled only with `values`.

| Part | What the code does | What the model answers |
|---|---|---|
| Observation | ONE walker call, `observe(opts)`: the indexed element table (visible, enabled, in-viewport controls with role, name, current value, checked/selected/expanded state, select options, each with its ref and a guard -- a hash of the element and its row/form/dialog text), the visible text, declarative `read` probes (`name=css@text\|value\|checked\|box\|count\|attr:NAME`, at most 16, 4 KB, a password or secret-named field reads as null; no script), a mutation counter, an animation-frame counter and a fingerprint, all limited to `region` when given. `grid` (+ `grid_items`) adds the cells read by `cells(selector)`. `see: true` adds a masked frame under `browser_images` with the frame rules above | -- |
| Request | one per decision, `extra.kind = "reflex"`: Choice `operation` over the moves offered (key presses, scrolls, drags, bound typing, WAIT, and one option per targeted operation); per targeted operation a speculative Choice `<op>_target` over the elements that operation fits (a select's options as `eN:i`); Choice `value` over `values` when there are several; Nouls `done`, `stuck`, `off_path`, the agent's `until_noul`, and `risk_i` for at most 8 moves not yet judged | the judgments |
| Use | only the target head matching the picked operation is read; the others are discarded unread, so a target never meets an operation it does not fit | -- |
| Executor | before input the walker's `check(ref, kind, guard)` re-reads the target: the same guard, enabled, visible, inside the viewport, not covered at its centre (`elementFromPoint`), editable and not secret for typing, the option still offered for a select. A failed check drops that answer (`stale`) and nothing is input. Typing replaces the field's text (`selectText`, then the `type` op). The model's answer is never a selector, a coordinate or script | -- |
| Settle | after an action, two animation frames (at most 50 ms), or after typing into a combobox until its suggestions show (at most 200 ms); `settle_ms` overrides | -- |
| Stop | `done`/`stuck`/`off_path`/`until` above 0.7; a move whose risk is at least 0.5 is not offered, and a pick of one stops with `risk` and a `handoff`; a picked move not yet judged is judged by the next request before it may run; `until_text` or `until_url`; `max_steps` (applied actions, default 60, at most 300), `max_seconds` (default 20, at most 45), `max_decisions` (requests, default 600, at most 2000); a takeover (`in_control`, before each read and each action); nothing left to offer (`no_actions_left`) | -- |
| Risk by code | arrows, Tab, Escape, Home, End, the page keys, scrolls, hover and typing (a replaced value) are judged safe by code and never asked about | -- |
| Values | every value to type must appear verbatim in the goal, a fact or the page (text and element names and values), read once before the loop, else `bad_value` before anything runs | never writes text |
| Landing | after every action the href goes through `url_allowed`; a refused one moves the tab to `about:blank` and stops with `redirect_refused` | -- |

Three modes, one op:

| `mode` | Loop | For |
|---|---|---|
| `step` (default) | observe, decide, act, settle; a move whose state did not change the fingerprint is withdrawn until the state changes; WAIT only with `wait: true` (100 ms) | forms, menus, grids |
| `realtime` | a question every `tick_ms` (at least 50) without waiting, at most `max_in_flight` (default 6, never more than the provider declares) in flight. After an applied action every question asked before it is `superseded` (with `supersede: "change"`, also every one asked before the mutation counter moved); a WAIT supersedes nothing. With `lead` the state carries `lead_ms`, the recent p90 round trip, and an answer that lands sooner waits until then; a late one is used when it lands. After an applied action the loop settles (two frames) and asks again at once. Superseded answers are counted, never put on the card | pages that move on their own |
| `virtual_time` | hold the page (`page_clock` `hold`: a break on its next timer or animation-frame callback), read it and decide, let it run (`run`), act, run `step_ms` (default 50); `release` in a `finally`, so a takeover's cancellation lets the page go | a moving page with a slow or one-at-a-time model |

The loop reads the provider's capabilities (`decide.capabilities`, which is
`gate.capabilities`), never its name: a provider that takes one request at a time
runs `realtime` as `virtual_time`, one with no image support runs `see` on state
only, and a slow one gets a per-call ceiling of three times its declared latency
(still clamped by the gate). Each is said in `notes`. The timed modes open the
provider's connection first (`decide.warm`, which is `gate.warm`).

`virtual_time` is not CDP virtual time. Chromium's `Emulation.setVirtualTimePolicy`
cannot be handed back: `advance` without a budget fast-forwards an idle page
(measured: about 9,000 s of page time in 0.5 s), `pause` leaves the tab frozen with
navigation hung, and detaching the session does not undo it; and it does not stop
animation frames. A bare `Debugger.pause` is no better: on an idle page it lands in
the next script, which is the walker's own call, and that call never returns. The
callback breakpoints (`EventBreakpoints.setInstrumentationBreakpoint` for
`requestAnimationFrame.callback`, `setTimeout.callback`, `setInterval.callback`)
stop timers and frames, leave the walker able to read the page, and are undone by
removing them and resuming. What a hold does not stop: CSS animations and
transitions, network callbacks, and the page's wall clock (`performance.now`,
`Date`, frame timestamps), so a page integrating unclamped elapsed time sees each
hold as one long frame; most animation loops clamp their frame step. A held page
drops input, so the page runs before the input and the target check happens after
that. A tab whose browser refuses the hold runs the call as `step` and says so; the
extension-attached user Chrome cannot open a CDP session at all, so
`virtual_time` is measured on Kiro's Chromium only.

The result is a summary: `stop`, `mode`, `steps`, `moves`, `recent` (last 20),
`actions` (count per label), `decisions`, `decision_ms_avg`, `seconds`, `rate`
(moves per second), `see`, `notes`, the final `state` (the grid, the reads, or the
text cut to 1,500 characters) and, in `realtime`, `applied`, `waits`,
`superseded`, `stale`, `unjudged`, `in_flight_max`, `in_flight_cap`,
`rtt_p50_ms`, `lead_ms` and `held_ms_avg`. Each APPLIED action is one live-card
step labelled `Reflex <n>: <label> (<rate>/s)`, and the loop ends with
`Reflex <stop>: <moves> moves, <rate>/s`; no new field is added to the step
payload.

Off is free: with no hooks, a closed gate (the same probe as the hooks above), or a
first decision that fails (in `realtime`, three with none answered), the op answers
`not_available` before it has touched the page and with no provider call when gated
off; a later failure ends the loop with `decision_unavailable` (in `realtime`, ten
in a row) and the summary. `reflex` is in `SLOW_OPS`, so its route budget (75 s
plus the decision headroom) covers the 45 s ceiling and the loop always ends with
its own summary. It holds the gateway op lock for its whole run, like any op.

The element table, the guard, the occlusion re-check, the combobox settle and the
operation-plus-target heads follow browser-use/jev-ultrafast (MIT); the pipelined
clock, supersession and held early answers follow hosseintoussi/jev-flappy-bird
(MIT). Neither is a dependency; see `NOTICE`. Tests:
`test/test_browser_reflex.py`, `test/test_browser_reflex_realtime.py`.

### Capability model

**An installed driver is availability, not approval.** The tool is always
advertised and checks `capabilities.browse` governance on every call; a
governance denial refuses outright and never falls back to another driver,
because a fallback would let browsing continue and defeat the control
(`mcp_core_parts/governance.py`). The browser-not-installed case is a plain
refusal that points at Settings → Browser.

`navigate` auto-drives only public HTTP(S) targets. Literal loopback, private,
link-local, reserved, alternate- or percent-encoded IP or host name, non-ASCII
host, parser-differential and non-HTTP forms are refused; DNS names are not
resolved, so public-name-to-private-address rebinding remains an accepted
residual. The one exception is `dashboard.browser_local_origins`, an
operator-written list of exact loopback origins (`localhost`, `127.0.0.1` or
`[::1]` plus a required port, e.g. `localhost:5173`). The config coercer drops
every other shape; the tool admits a `navigate` whose own `host:port` equals an
entry byte for byte, after the same refusals and with userinfo refused outright;
the gateway's own API port is refused even when listed. The list is read fresh
per call and fails closed to empty. The agent cannot extend it: `config.json` is
read-only inside the sandbox and `PUT /api/dashboard/config` does not accept the
key. **Accepted residual:** once a listed page is open, `click`/`back` follow
whatever that page links to, since only `navigate` is URL-gated.

Passwords never pass the model: `type` refuses a password field, the frame masks
one, and logins happen during takeover.

### Screenshot directory

`browser_snapshots.py` names the one directory the `screenshot` op writes to:
`<data-home>/browser-snapshots` (`snapshot_dir()`), derived from `config_dir()`
so an isolated `KIROCREW_HOME` stays isolated. It is never inside a working
directory by construction, which is why `messaging/outbound_files.py` admits it
as the second approved root for an inline image in a channel reply: a
screenshot the agent just took can be sent, and admitting the directory grants
nothing the `file_send` tool does not already deliver from it. The read is
pinned to the directory's canonical spelling with the leaf kept literal, so a
link planted at that name resolves the file elsewhere and fails the pin.

### Settings → Browser

One card and two controls; everything else is under a collapsed More group.

- **Main card** (`BrowserStatusCard.tsx`): the browser, a status dot and line, and
  ONE button that follows `primaryState` (`browserStatus.ts`): `installing`
  (an install job runs -- the gateway's own at startup or one this page asked
  for; disabled spinner, indeterminate bar, `install_job.stage` as its
  `aria-valuetext`; the line reads "Installing"), `install_failed` (not installed and the last job failed or
  was interrupted; red, `install_job.error_detail` verbatim in an `ErrorNotice`,
  Retry re-posts the install),
  `not_installed` (no job ran; Install), `waiting_allow` (yellow; "Allow in
  Chrome" re-posts the attach, which brings Chrome's prompt back), `error` (red;
  the gateway's `detail`, or the last attach's, verbatim in an `ErrorNotice`;
  Retry re-posts the attach), `disconnected`
  (Connect) and `connected` (Disconnect). A failed job while the engine IS
  installed (a Redownload) does not take over the card; it is an `ErrorNotice`
  on the Engine row. A button whose request is in flight is disabled and
  `aria-busy`.
- **Which browser** (`browser.choice`) offers `auto`, `user_chrome`,
  `kiro_chromium`. **Show the live view in chat** (`browser.live.enabled`).
- **Use \<provider\> sign-in** (`browser.credentials.enabled`), under Show the
  live view in chat and shown only when `status.credentials.provider` is non-null
  (an edition supplies a sign-in provider; the public build never renders it). It
  reads the EFFECTIVE `credentials.enabled` (the edition's default while the key is
  unset); its info tip says the sign-in goes to Kiro's own browser only, never the
  person's Chrome. The status line follows `credentials.state`: `applied` is
  "N cookies applied" (`credentials.applied`), `needs_refresh` is the provider's
  own `provider_status.message` verbatim (a catalog line when it gave none),
  `pending`, `disabled` and `skipped` are catalog lines; `error` is an
  `ErrorNotice` with the reason code. Its label names the provider, so search does
  not index it (recorded in `settingsCoverage.test.ts`); its `configKey` still
  anchors key links.
  A write to any `browser.credentials.*` path re-reads the status: turned on
  or changed it reads `pending` until the next page Kiro's Chromium opens
  applies it, turned off it reads `disabled` (cookies already set stay until
  the browser closes; see Credentials). No cookie value is ever shown.
- **More** (a collapsible `SettingsSection`, which a deep link opens): Quality
  (`browser.live.quality`), Max frame rate (`browser.live.max_fps`), the Preview
  tab's local addresses (`dashboard.browser_local_origins`, editable chips), the
  sign-in sites (`browser.credentials.domains`, editable chips, only with a
  provider; an entry must be two or more lowercase DNS labels with no port,
  wildcard or IP, the PATCH route's `item_pattern`; empty follows the provider's
  own list), Engine
  (Harness and Chromium versions; Redownload posts `{components: ["chromium"],
  refresh: true}` so a Chromium already on disk is REPLACED, the repair for a
  damaged copy; the ordinary install keeps a whole one), and **Run with no
  window**.
- A grey footer line states the privacy fence (no usage data, no cloud browser);
  it is a statement, not a control.

Every explanation is an `InfoTip` beside its label (hover, focus or tap;
`aria-describedby` while open, `title` while closed), passed as the settings
primitives' `hint`, never a description line under a control.

`browser.choice` keeps four values and the CLI may write any of them. The page
splits it: the picker reads `headless` as `kiro_chromium`, and picking
`kiro_chromium` while `headless` writes nothing; the no-window switch writes
`headless` on and `kiro_chromium` off (the same browser, with a window) -- it never
guesses an earlier pick. `browser.live.mask_passwords` has no control (it stays
on) and stays in the schema. There is no first-run step list: the gateway
installs on its own. The pre-release `browser.first_run_dismissed` flag that hid
the old list is retired: a config carrying it loads and drops it on the next
save (`_SECTION_KEYS_DELIBERATELY_DROPPED`).

Every failure is an `ErrorNotice`, with the Ask-agent hand-off except on a rejected
config save (the origins add field may hold a draft): a rejected request,
an attach that answers `ok: false`, and an install job that failed. Data comes from
`GET /api/browser/status` (polled every 2 s while a job runs or Chrome waits for
Allow, else every 15 s; `supervisor.status()` plus `installing`, `install_job
{status: running|succeeded|failed|interrupted, stage, error_detail}`,
`chromium.downloaded` and `platform`) and `POST /api/browser/install|test|disconnect`,
all owner-only and refusing the internal secret; writes go through
`PATCH /api/config/kirocrew` one path at a time. Every control carries its
`configKey` except Run with no window, the second view of `browser.choice`: it
anchors by label, so a `key:browser.choice` link lands on the Which browser
picker only. Every string is in the i18n catalog (`pages.settings.browserPanel.*`).

The config section is `browser`: `choice`, `live.enabled`, `live.quality`,
`live.max_fps`, `live.mask_passwords`.
`dashboard.browser_local_origins` stays where it is.

#### Retired keys

`dashboard.use_builtin_browser` and `dashboard.browser_view_port` configured the
agent browser this stack replaced. They are no longer fields. A `config.json`
that still carries them loads, logs ONE warning naming them
(`config/resolution.py`, `warn_retired_section_keys`), and drops them on the
next save (`_SECTION_KEYS_DELIBERATELY_DROPPED`). `PUT /api/dashboard/config`
no longer accepts `use_builtin_browser`, and the change-card catalog no longer
offers `browser.use-the-built-in-browser`.

### The Preview tab

The side panel's Browser tab is the **Preview** tab: the person's own local dev
server, for front-end work that wants page annotation. It keeps the native
embedded Chromium view (`website/electron/browser-view.js`), the element
annotation overlay (`website/electron/browser-annotate.js`, splicing the shared
page helpers in `website/electron/browser-page-helpers.js`) and
`WebPreviewPanel`. It opens only the origins in
`dashboard.browser_local_origins`; an outside URL answers "Open this in your
Chrome, or ask Kiro". The list is judged on the URL the user chose; the
cookie-isolation host swap (`localhost` framed as `127.0.0.1`, so the dashboard's
host-scoped cookie never reaches the dev server) is applied only to the URL the
frame, the liveness probe and the native view load, never to what is shown,
persisted or checked. The list is LIVE: a change to `dashboard.browser_local_origins`
or any `browser.*` key that reaches the gateway from outside this dashboard (the
CLI, another window, `config.json` in an editor) is noticed by the live config
watcher, whose `browser.settings` applier pushes a `refresh` frame; the frame
invalidates the dashboard's config query, so an open Preview tab and Settings ->
Browser re-read without a reload. The agent does not drive it: the Electron main process
exposes `open`, `navigate`, `setBounds`, `setOverlayActive`, `setInactive`,
`close`, `getState` and `annotate` on the `browserAPI` preload bridge and
nothing else (`website/electron/preload.js`, `website/electron/ipc-registrar.js`,
`website/electron/runtime/window/browser-panels.js`).

The view renders arbitrary untrusted web content, so it is the most locked-down
`WebContentsView` in the app: a persistent partition isolated from the
dashboard's cookie jar, `contextIsolation`, no `nodeIntegration`, `sandbox`,
no `webviewTag`, and the shared right-click menu only.

#### Native view focus

In the desktop app keyboard focus belongs to exactly one child view of the
window, and hiding the focused view does not move it. So when the native view
leaves the screen while it holds focus (an overlay, an inactive tab, a collapsed
panel, close), the manager in `website/electron/browser-view.js` hands focus
back to the dashboard view (`focusHost`), and on window focus `reclaimFocus`
does the same for a hidden view that still holds it. Without this every
dashboard text input would look alive and receive no keystrokes.

### Doctor

`kirocrew doctor` prints one informational row (`_doctor_browser_leftover` in
`doctor_checks/install.py`) when the prefix an earlier version's browser CLI
installer wrote (`_LEFTOVER_BROWSER_CLI_LEAF` under the data home) is still on
disk: it names the directory and says it can be deleted. Doctor never deletes or
modifies it, and the row is silent when the directory is absent.

### Security

| Risk | Control |
|---|---|
| The relay can reach the user's whole Chrome | the agent gets only the listed ops, on tabs it created; the relay never attaches the user's tabs. **Accepted residual:** CDP is browser-wide, so a bug in the tab filter would let the agent act on a tab the user opened; tests pin the filter and the card always shows which tab is live |
| Harness sends data out | five environment fences, a pinned version, a test on the fence; no CLI, MCP server or update check runs |
| Agent claims input | `/api/browser/input/*` and `/api/browser/live/*` refuse the internal secret |
| Agent types a password | `type` and printable `press_key` refuse password fields, resolved through open shadow roots; the frame masks them (boxes clipped to the frame, shadow roots walked) |
| Frames show logged-in pages | owner websockets only; never written to disk except as a requested `screenshot` |
| Another local process uses the relay | loopback, ephemeral port, AND a per-start secret on every path (404 without it); the URL with the secret is given only to the daemon. The sandbox does not fence loopback, so the secret, not the port, is the control |
| Another local process reaches the browser's own debugging endpoint | Kiro's Chromium on macOS/Linux has none: it speaks CDP over `--remote-debugging-pipe`. **Accepted residual, Windows:** Kiro's Chromium keeps `--remote-debugging-port=0` (Python cannot hand a Windows child fds 3 and 4), so any local process can drive it. **Accepted residual, the user's own Chrome/Edge:** its debugging port is the user's, enabled by them; Chrome 144+ raises a fresh "Allow remote debugging?" sheet for every new connection, so a second client needs a second click the person would see |
| The daemon's socket leaves the hidden leaf | a data home too deep for the AF_UNIX path limit is refused, never relocated to system temp |
| Another chat drives the tab a person has taken over | every op binds to the calling session's own tab under one gateway-wide lock; a takeover fences that session's tab, and a tab another session owns is never streamed to this session's card |
| A takeover lands inside an agent op | the claim and the op route share a per-session fence: the claim cancels the op in flight and is confirmed only once the route has left it |
| An agent-written `pip/` or `venv/` package in the gateway's cwd runs on Install or the startup auto-install | both installer spawns run `python -I` with `PYTHON*`/`PIP_*` scrubbed and the prefix as cwd |
| The relay URL (and its secret) goes to a corporate proxy | `NO_PROXY` always carries the loopback names |
| Agent spells a password one key at a time | `press_key` with a printable key runs the same focused-password-field refusal as `type`; named keys and Ctrl/Meta chords stay allowed so the agent can leave the field |
| Agent reads Kiro's profile cookies or the daemon's IPC credential | `HIDDEN` leaves in every sandbox mode |
| Agent reads injected credentials | the gateway applies them over the relay before the daemon starts; no route reads or writes cookies; status, logs and SEL carry counts and codes only; the person's own Chrome is never written to. An edition keeps its own source files hidden with its sandbox policy |
| A provider widens what it may set | off unless `browser.credentials.enabled`; cookies outside BOTH its declared domains and `browser.credentials.domains` are dropped; every cookie is a session cookie; the provider adds no launch switch |
| A provider reads a file the agent planted | `credential_files.read_source_file`: `O_NOFOLLOW`, `fstat` on the open descriptor, owner uid, no group/other bits, size cap |
| Local addresses | `navigate` keeps the public-only gate plus `dashboard.browser_local_origins`, applied TWICE by one shared predicate (`browser_harness/url_gate.py`): in the shim to the URL asked for, and in the gateway to the URL the navigation (or `back`) committed on, so a public page that redirects to loopback, private or link-local is refused (`redirect_refused`) and the tab moved to `about:blank`. **Accepted residual:** DNS rebinding (a public name resolving to a private address) is not seen by either check; names are not resolved |
| Governance | `capabilities.browse` is checked on every call and a denial never falls back |
| Page content | treated as untrusted input; a URL, instruction or form target read off a page never decides the next navigation |
| Preview tab | native view, deny-all partition, no agent channel, local origins only |

### What this stack replaced

The browser CLI shell path (Playwright's CLI, installed per host by `npm`), the
gateway's `show` live-view supervisor and its `/browser-view` same-origin relay,
the Browser panel's address-bar launcher (`POST /api/browser/open`), the
native-panel command bus (`/api/browser/command`, `/api/browser/command-drain`,
`/api/browser/command-result`) and the Electron control plane behind it, the
per-agent browser-session environment and the stranded-daemon orphan class that
reaped it, the `dashboard.use_builtin_browser` and `dashboard.browser_view_port`
keys, the `browser_event` websocket event nothing emitted, and the daily
banner workflow that checked the CLI's install script are gone. No migration and
no flag: one change removed what it replaced. An installed CLI prefix is left on
disk and reported by doctor; its sandbox leaves went with it (nothing the gateway
executes lives there), while the retired CLI's session-material leaves stay
masked because a leftover file still holds the owner's sessions.

### Related

- [web-browse](../../../src/kiro_crew/builtin_skills/web-browse/SKILL.md) for
  opening a page so the user can see it.
- [web-verify](../../../src/kiro_crew/builtin_skills/web-verify/SKILL.md) for
  screenshotting a front-end change as evidence.
- [web-preview](../../../src/kiro_crew/builtin_skills/web-preview/SKILL.md) for
  the Preview tab.
- [computer-use](computer-use.md) for the screencast pattern the live view
  follows (`computer_use_frame` to owner websockets).
- [config](config.md) for how a retired key is dropped on save.
- [mcp](../../architecture/mcp.md) for MCP registration, transport, and trust
  boundaries.
