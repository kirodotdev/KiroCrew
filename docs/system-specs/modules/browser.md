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
| Driver install, environment fence, browser discovery, relay, supervisor, helper worker, status and install routes | the `browser_harness` package: `env.py`, `install.py`, `build_deps.py`, `chromium.py`, `discover.py`, `relay.py`, `supervisor.py`, `helper_worker.py`; `dashboard/handlers/browser_harness_status.py` |
| Screencast, adaptive ladder, takeover input, the live hub, the card's routes and the `browser_live` event | `screencast.py`, `takeover.py`, `live_hub.py` in that package; `dashboard/handlers/browser_live.py` |
| The `browser` MCP tool's ops, the snapshot walker and the op route that also drives the card | `mcp_tools/browser.py`, `ops.py`, `snapshot_walker.js` in that package; `dashboard/handlers/browser_ops.py` |
| An app page's media buttons on its own agent's tab | `media.py` in that package; `dashboard/handlers/browser_media.py` |
| Quick asks, the Secrets-vault sign-in, turn replay, decision-model help and the fast loop | `quick_ask.py`, `vault_fill.py`, `recording.py`, `decide.py`, `reflex.py`, `reflex_space.py`, `reflex_realtime.py` in that package; `dashboard/handlers/browser_ask.py`, `dashboard/handlers/browser_recordings.py` |
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
The venv links to the base interpreter that built it, which under Toolbox is a
versioned directory Toolbox later prunes. A finished install (marker at this
`HARNESS_VERSION`) whose interpreter link dangles, built by the gateway's own
Python minor version (`pyvenv.cfg` `version`) and holding the pip it needs, is
repaired in place instead of cleared: the dangling `python*` links are unlinked
and `python -m venv --upgrade --without-pip` re-points the venv at the gateway's
interpreter, keeping site-packages, so the pip step finds every pin already
installed and needs no package index. Any other missing interpreter (no marker,
another version, another minor version, a venv filled from a runtime copy, below) is
still a `--clear` rebuild.
On Linux with a glibc older than 2.27 (Amazon Linux 2: 2.26) the locked
`pillow==12.3.0`, which `browser-harness` itself pins, has no wheel and pip has to
build its sdist. `build_deps.py` checks first, from the filesystem alone, for the C
compiler and archiver the build will run (`$CC` and `$AR`, else the interpreter's
own `CC` and `AR`; `cc`/`gcc` on `PATH` only when neither names a compiler, and no
archiver check when neither names one), the gateway
interpreter's `Python.h`, and `zlib.h` and `jpeglib.h` wherever pillow 12.3.0's
`setup.py` looks: the system include directories, `C_INCLUDE_PATH`, `CPATH`,
`INCLUDE`, a glued `-I<dir>` in `CFLAGS`/`CPPFLAGS`/`LDFLAGS`, and
`ZLIB_ROOT`/`JPEG_ROOT`. Pillow also asks pkg-config, which the check does not run,
so with `PKG_CONFIG_PATH` or `PKG_CONFIG_LIBDIR` set the headers are left to pip. A
host missing any of them is refused at `preparing`, before the marker or the venv
is touched, with what is missing and the `yum`/`apt-get` command that installs it.
A venv that already holds the locked pillow and is not being rebuilt is not
checked, since pip has nothing to build.
A runtime that already ships the harness asks no index at all. When the gateway's
own interpreter carries `browser-harness`, `cdp-use` and `fetch-use` at the
versions the lock pins (a distribution that bundles the harness beside the
gateway), the gateway reads the file records for every distribution named by the
lock. The launch directory and the working directory are not searched.
A missing harness-family distribution, a different harness-family version,
an absent RECORD or any recorded `.pth` file selects the hash-pinned pip path.
Other lock distributions absent from the runtime are skipped. The venv is made
`--clear --without-pip` on every runtime install, and the gateway copies the
recorded files into its site-packages with content-only copies, each read from a
descriptor that must still identify the regular file checked, opening each directory
on the way without following links where descriptor-relative opens are supported
(Windows keeps the path-based identity check). Anchored paths,
paths containing `..`, and `__pycache__` entries are skipped. A recorded file that
resolves outside its distribution's directory selects the pip path. No runtime
directory is added to the venv's import path.
`<venv>/python -I -c` must import `browser_harness` and resolve those three
versions before the marker is written with `source: "runtime"`. The marker is
removed before rebuilding, so a failed copy or interpreter check reads as not
installed. A host whose index is unreachable or refuses its credentials can
therefore still install. A failed interpreter check fails the install at
`installing_harness`; it does not fall back to pip.
The copied dependencies use the runtime's releases rather than the lock's, and
nothing hash-checks this copy. Until the gateway takes it, the runtime tree has
the gateway's own exposure: only the `kiro_crew` package directory is sealed
([security](security.md), the packaged install directory). The driver's installed
code is always under the sealed prefix, so later runtime edits or pruning cannot
change it. Nothing is built, so the pillow check does not apply. The pip path
reads the marker's source, independent of the venv's Python layout, and rebuilds
a runtime-filled venv rather than repairing or reusing its packages.
Kiro's own Chromium (Chrome for Testing `154.0.8037.0`, the build Playwright's
`chromium-1244` ships, fetched from Google's bucket and then Playwright's CDN,
which alone hosts linux-arm64) is downloaded into the same prefix for hosts with
no usable Chrome (`chromium.py`). The prefix is a `READONLY` crew-home leaf, so
a sandboxed process can neither replace the interpreter nor the browser the
gateway executes; the install job (`POST /api/browser/install`, one at a time,
`409` otherwise, `{"components": ["harness"|"chromium"]}` to redo one part) runs
in the gateway, outside the sandbox. Each installer subprocess is owned through
`Popen` in its own process group. Cancelling the install waits for its worker to
stop the child tree through `platform_compat.terminate_and_reap_sync` (a short TERM
grace on POSIX, then KILL and reap; tree termination on Windows). The stop flag
is checked before another stage or subprocess starts and before the marker is
written. Chromium downloads check it at progress callbacks; an in-flight network
read still has to return before the worker can settle.

**Startup auto-install.** The owner never has to press Install: at every gateway
start, in the background task that first reclaims a stray daemon
(`server_runtime/browser_lifecycle.py`), `auto_setup_browser_harness`
(`handlers/browser_harness_status.py`) starts the SAME install job when the
Harness is missing or stale (`install.harness_current`:
installed AND the marker's `lock_sha256` equals the shipped lock's digest, so a
re-pin without a version bump reinstalls). A copied venv remains current when
the runtime's package tree moves, is pruned or changes versions. It adds Kiro's
Chromium only when discovery would launch it (no usable Chrome/Edge, or
`browser.choice` pins `kiro_chromium`/`headless`) and it is not on disk; a host
that drives the user's Chrome never downloads it unasked (Settings' Install still
installs both). After
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

**Repair after an update.** The venv links to the interpreter that built it. A
packaged runtime that keeps each release in its own directory can remove that
interpreter while the gateway runs, after the startup check found the install
whole, so the next status read would show Install with no reason. When a status
read (`GET /api/browser/status`) or an agent's `browser` op finds the marker but
no venv Python, `repair_browser_harness` starts the same install job unasked:
once per gateway process, never over a job that is running or did not succeed,
and under the same test/CI skip. It checks those conditions again right before
it starts the job, after its last await, so two callers at once start one job.
The op that started it, and any op while an install job runs (a repair another
caller started, or an Install press), answers `browser_repairing` ("being
installed; try again in a minute"), which the tool passes to the agent as
written.

The daemon starts with a scrubbed environment built by one function and pinned
by one test:

| Variable | Value | Why |
|---|---|---|
| `BH_TELEMETRY`, `BROWSER_HARNESS_TELEMETRY`, `ANONYMIZED_TELEMETRY` | `false` | the three switches the package reads |
| `BH_POSTHOG_HOST` | `http://127.0.0.1:9` | a second fence if a switch is missed |
| `BH_TELEMETRY_TIMEOUT` | `1` | a POST that never happens could not stall a helper either |
| `BH_TAB_MARKER` | `0` | no badge painted on the page |
| `BH_UPDATE_CHECK` | `0` | the CLI's PyPI probe never runs |
| `BU_CDP_URL` | the relay's URL, `http://127.0.0.1:<port>/<secret>` | connects to the relay, not the browser; the per-start secret is the only credential that opens it |
| `BU_NAME` | the gateway's daemon name | a named daemon creates its OWN tab instead of attaching the first page it finds (the person's) |
| `BH_HOME`, `BH_CONFIG_DIR`, `BH_RUNTIME_DIR`, `BH_TMP_DIR`, `BH_AGENT_WORKSPACE` | under `<data-home>/browser-harness-ipc` | the socket, pid file, log and scratch never leave the hidden leaf |
| `BROWSER_USE_API_KEY`, `BU_BROWSER_ID`, `BU_CDP_WS`, `BU_AUTOSPAWN`, the cloud API/OAuth variables, `BH_AUTH_PATH`, inherited `BH_*` and `BROWSER_HARNESS_HOME` roots | removed | the only ways in to the cloud, or to a path outside the leaf |

The removed keys stay removed even when the gateway's own environment carries
them. The gateway never runs Harness's CLI, its `run.py`, its MCP server or its
update check, and never runs that MCP server's `browser_js`, `browser_cdp` or
`http_get` tools (the agent's `js`, `cdp` and `http_get` ops call Harness's
helpers of those names through the helper worker instead, see "The tool"); it
calls the daemon through Harness's own IPC (a Unix socket on POSIX, loopback TCP
with a token on Windows), whose path or token lives in a `HIDDEN` crew-home
leaf. A data home so deep that the socket path would pass the AF_UNIX limit
(100 bytes) is REFUSED with a `BrowserUnavailable` naming the path: the only
shorter place is the system temp dir, which no sandbox mask covers, and the
daemon resolves symlinks before binding, so an alias cannot help (the guard
measures the RESOLVED home for the same reason). The daemon runs with
`python -I`, like the helper worker, so the gateway's working directory can never
shadow the pinned package; the INSTALLER's two spawns (`python -I -m venv`, then
`python -I -m pip --python <venv>/python` when the gateway's interpreter has pip,
else `<venv>/python -I -m pip`, or the copied venv's `-I -c` check) run the same way,
with the `PYTHON*` and `PIP_*` families scrubbed from their environment and the
prefix as their working directory, because a pod gateway's cwd is the agent's
workspace and a `pip/` package written there would otherwise run as the owner the
moment they press Install. Their environment also sets `PIP_CONFIG_FILE` to the
null device, so pip loads no config file (the user's `~/.config/pip/pip.conf` or
`~/.pip/pip.conf`, `/etc/pip.conf`, the venv's own) and installs from its default
index, PyPI: an `index-url` in the user's file, such as a CodeArtifact repository
whose login has expired, would otherwise answer 401 for every locked wheel and
fail the install with pip's raw output. A mirror set in pip's config or in
`PIP_INDEX_URL` is therefore not used; the lock's hashes pin every file either
way. `NO_PROXY`/`no_proxy` always carry `127.0.0.1,localhost`: neither `urllib`
nor `websockets` bypasses a proxy for loopback on its own, and a shell with
`HTTPS_PROXY` set would send the relay URL (secret and all) to the proxy host. A
version bump re-reads the package's telemetry and auth modules before it lands,
because 0.1.13's telemetry is on by default and its run event carries the agent's
whole script unscrubbed.

**The helper worker.** The gateway never imports `browser_harness` (its Python
carries none unless its runtime bundles the harness), so each helper call runs in
a worker the venv's interpreter executes
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
| `fill_input` | `selector, text, clear_first` | `fill_input`: focuses the selector's element, selects all and deletes when `clear_first`, presses one key per character, then fires `input` and `change`. The `fill` op passes only `:focus`, the field it has just focused and judged |
| `dispatch_key` | `selector, key, event` | `dispatch_key`: a script-made DOM `KeyboardEvent` (`isTrusted` false) on the selector's first match, after focusing it |
| `press_key` | `key, modifiers=0` (1 Alt, 2 Ctrl, 4 Meta, 8 Shift) | `press_key` |
| `scroll` | `x, y, dy=-300, dx=0` | `scroll` |
| `screenshot` | `path, full=False, max_dim=None` | `capture_screenshot` |
| `screenshot_clip` | `path, x, y, width, height, max_dim, magnify` | the worker's own `Page.captureScreenshot` of one box (an element, or a region magnified up to `magnify`), fitted to `max_dim` device pixels |
| `page_info` | -- | `page_info` (`{url,title,w,h,sx,sy,pw,ph}` or `{dialog}`) |
| `dialog` | `action=None, text=None` | the worker's own read of the dialog `page_info` reports, and with `action` (`accept` or `dismiss`) a `Page.handleJavaScriptDialog` (`text` goes to a prompt on accept only). The `dialog` op reads it first because an open dialog holds the page: any other helper that needs the page's script times out after Harness's 5 s, and `run_op` then answers `dialog_open` instead of the helper's error |
| `wait_for_load` | `timeout=15.0` | `wait_for_load` |
| `wait_for_element` | `selector, timeout, visible` | `wait_for_element` (polls `querySelector` every 0.3 s; `visible` adds `checkVisibility`) |
| `wait_for_network_idle` | `timeout, idle_ms` | `wait_for_network_idle`: reads the daemon's event buffer (`drain_events`) for the current tab's `Network.*` events until none is in flight for `idle_ms` |
| `new_tab`, `switch_tab`, `list_tabs`, `close_tab`, `current_tab` | `url` / `target, activate` / `include_chrome` / `target` / -- | the tab helpers |
| `js` | `expression, target_id=None` | `js`: `snapshot_walker.js` for every op that reads or writes the page, and the agent's own expression for the `js` op |
| `js_isolated` | `expression` | the worker's own `Runtime.evaluate` in an isolated world of the tab's top frame (`Page.createIsolatedWorld`), ONLY for `snapshot_walker.js` |
| `frame_owner_count`, `closed_secret_count` | -- | the worker's own count over `DOM.getDocument` with `pierce`: every `iframe`/`frame`/`object`/`embed`, and the secret-looking fields inside closed shadow roots |
| `page_clock` | `state` (`hold`, `run`, `release`) | the worker's own event breakpoints on the page's timer and animation-frame callbacks (`reflex` `virtual_time`) |
| `cdp` | `method, params=None` | the worker's own call of Harness `cdp(method, **params)` on the daemon's current tab, for the `cdp` op; a param named `session_id` or `_response_timeout` (the helper's own arguments) is refused |
| `http_get` | `url, headers=None, timeout` | `_local_http_get` (Harness `http_get`'s plain `urllib` GET from the worker, redirects not followed, body read at most 512 KiB, no browser; `BROWSER_USE_API_KEY` is scrubbed, so never Browser Use's fetch proxy), for the `http_get` op |
| `set_files` | `expression, files` | the worker's own `DOM.setFileInputFiles`: `expression` (the walker's `fileInput(ref, true)`) is evaluated in the walker's isolated world and must give a node, and `files` is 1 to 100 paths, the gateway's staged copies (the `upload` op). A folder never goes this way: on some builds a folder set on a `webkitdirectory` field, or dropped on it, answers ok and leaves the field empty with no `change`, so `upload` refuses a folder for a folder field (`folder_field_unsupported`; follow-up #40410) |
| `chooser_files` | `x, y, files, folder=False` | the worker's own `upload via: chooser`: `Page.setInterceptFileChooserDialog` on for this call only, a click at `x, y`, then the first `Page.fileChooserOpened` within 3 s gets `DOM.setFileInputFiles` by its `backendNodeId`; `no_chooser`, `not_multiple` (2+ paths on `selectSingle`) and `not_folder_input` (`folder` on an input without `webkitdirectory`, read by `DOM.describeNode`) set nothing (the `upload` op itself never sends a folder this way: `_fits` refuses it first); a chooser opened in a frame that is not the tab's top frame or of its origin (`Page.getFrameTree`) is `frame_refused`; interception off in `finally` |
| `drop_files` | `x, y, files` | the worker's own `upload via: drop`: no press; `Input.dispatchDragEvent` `dragEnter`, `dragOver`, `drop` at `x, y` carrying the staged paths; the browser's own hit test (`DOM.getNodeForLocation`, through every shadow root, closed ones too, honouring `pointer-events: none` as the drop does) must find a node in the tab's top frame that is not itself a frame element (`IFRAME`, `FRAME`, `OBJECT`, `EMBED`, `PORTAL`, `FENCEDFRAME`), checked again before each of the three events (the page's own drag handlers run in between; a failed check after `dragEnter` sends `dragCancel`), else `frame_refused` |

`run.py`, Harness's MCP server, `upload_file` and the agent-helper loader are not
reachable through it (`fill_input` is, and only the `fill` op calls it, always on
`:focus`); the `upload` op uses the
worker's own `set_files`, `chooser_files` and `drop_files`, never Harness's `upload_file`. A helper's failure comes back
as `HelperError` with Harness's own one-line message; `HarnessNotInstalled` and
`BrowserUnavailable` come out of `ensure()`.

**The supervisor.** `HarnessSupervisor` (one per gateway, `get_supervisor(home)`)
brings browser, relay, daemon and worker up in `ensure()` (serialised: a second
caller waits for the first), reports `status()` (`installed`, `version`,
`wanted_version`, `browser {kind, headless}`, `connection`
`connected|waiting_allow|disconnected|error`, `detail`, `agent_tabs`, `chromium`
`{version, platform, build, present, executable}`),
and stops all of it in `stop()`. `waiting_allow` has no deadline because the
connection is what keeps Chrome's Allow sheet on screen. The gateway stops the
supervisor on shutdown, before an in-place restart's exec, and before the hard
exit of a graceful shutdown that ran out of time (`server_runtime/browser_lifecycle.py`,
registered with `pre_exec_stops`; the cleanup hook unregisters it once its own
stop has returned; a teardown cut before its last child sets that handle aside,
never to be reused, so the next stop reaches the children it never got to
without retrying the tab close; that best-effort close is bounded at 3 s, so a
stalled CDP connection cannot spend the stop's bound before the children stop).
On Windows `terminate_and_reap` has no graceful step, so before the forced stop of
Kiro's Chromium (`_stop_handle`, `_abandon_start`, and the port launcher's
`_stop_unowned`) the supervisor sends CDP `Browser.close` over a connection of its
own (`relay.request_browser_close`, to the URL in `DevToolsActivePort`) and waits up
to `_STOP_GRACE_SECS` for the process to exit, so Chromium writes its profile
(`exit_type` `Normal`) and keeps recent cookies. A browser that ignores the request,
or has no live port, gets the forced tree kill after that wait. macOS and Linux are
unchanged. The gateway also
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

**Old libcups: the headless shell.** The full Chrome for Testing build links
`libcups.so.2` and binds every symbol at load, so on a Linux host whose libcups
lacks `ippValidateAttributes`, `cupsFindDestDefault`, `cupsFindDestSupported` or
`cupsUserAgent` (Amazon Linux 2: cups-libs 1.6.3) it exits 127 before it opens
the debugging pipe. There `chromium.wanted_build` picks the same version's
`chrome-headless-shell` (the `{build}` in both download URLs), which links no
libcups, and keeps it in `chromium/154.0.8037.0-headless-shell/` beside the full
build's directory. Everything that reads or fetches Kiro's Chromium goes through
that one answer: `chromium_state` (so on such a host a full build already on
disk reads as not downloaded, and the startup auto-install fetches the shell),
`download_chromium`, and therefore the executable the supervisor launches, with
the same flags. The shell has no window, so on such a host Kiro's Chromium is
always headless: `supervisor._choose` reports a `kiro_chromium` choice as
headless even when a display and a windowed pin would otherwise give it one, so
status, the live card and the startup warm never offer a window it cannot show. `libcups.py` decides from the filesystem alone: the first
native 64-bit `libcups.so.2` in the linker's system directories (never
`LD_LIBRARY_PATH`), its ELF dynamic symbol table parsed in Python. It launches
nothing, so `install_state` and the status poll still launch nothing, and it
needs no binutils. No libcups found, or none readable, is no evidence and keeps
the full build; macOS and Windows always get the full build. A version bump
re-reads `libcups.REQUIRED_SYMBOLS` from the new build's
`nm -D --undefined-only`.
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
cannot read the file. Tabs do not survive a relaunch of Kiro's Chromium: before
each launch the supervisor removes the profile's session files
(`Default/Sessions/` and the older `Current`/`Last` `Session`/`Tabs` files), so
pages from closed chats are not reopened; cookies and logins are kept. The
user's own Chrome or Edge is never touched. `browser.choice` (`auto|user_chrome|kiro_chromium|headless`)
overrides the order by hand.

Under `auto`, each start first checks that the person's Chrome or Edge accepts
debugging connections (a live `DevToolsActivePort`). When it does not (remote
debugging is off), the start uses Kiro's Chromium, with a window, instead of
failing, and status reports Kiro's Chromium until a later start finds the
person's browser usable. If Kiro's Chromium is not downloaded yet, the start
fails saying so, and Settings shows Install on the main card. A hand pick of
`user_chrome` never falls back: it fails with the remote-debugging step.

A Kiro's Chromium that exits before the daemon answers fails the start at once
with `Kiro's Chromium exited with code <n>`. On macOS and Linux its stderr goes
to a pipe of the gateway's own, drained for the browser's whole life (only the
newest 4 KiB kept, in memory), and its last stderr lines follow, redacted, so a
missing shared library is named by the loader's own report rather than by a
60 s `the Harness daemon did not start in time`. On Windows stderr is discarded.

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
| An agent `navigate` or `tabs` op | the op route (`dashboard/handlers/browser_ops.py`, under the gateway-wide op lock) calls `refresh_credentials_if_source_changed()`: one `fingerprint()` call bounded at 2 s and a config read. When the fingerprint or the resolved `browser.credentials` settings differ from the last apply, or the earliest `expires` of a cookie the gateway set has passed (`PlacedCookies.next_expiry()`, a memory read), the provider is re-pulled before the page loads. A `None` fingerprint means "cannot tell" and is not a change. The re-pull is bounded at 8 s (`NAV_REFRESH_BUDGET_SECS`): past it the page loads with what the browser holds, `reason` reads `refresh_timeout` (while the settings are unchanged; after a settings change the block reads `pending`), and the old key is kept so the next navigation retries. The three bounds sum to `budgets.CREDENTIAL_WORST_S` (15 s), which the gateway's worst case and so the shim's HTTP timeout include. A failure never fails the op |

The navigation trigger is what both a person re-running their sign-in tool (the
tool rewrites its file, the fingerprint moves) and a switch flipped in Settings
rely on: the next page carries the change. While a start holds the supervisor's
lock (the person's Chrome can wait on its Allow sheet without deadline) the check
waits at most 5 s (`CREDENTIAL_LOCK_WAIT_SECS`) and then lets the page load; the
next navigation tries again. **The person's own Chrome or Edge is never filled**: a
start on either records `skipped` / `user_browser` and the provider is not called.

**Per-chat contexts.** Under `browser.isolation: per_session` (The relay, below)
the default context's cookies do not reach a chat's context, so each new context is
filled on its own: `seed_context` applies the same provider and settings with
`Storage.setCookies` carrying the context's `browserContextId` (target reason
`context`, one SEL row as for any apply). Every re-apply above pulls from the
provider ONCE and writes the same cookies into the default context and each open
context (`credentials.apply(contexts=...)`), so the whole refresh stays inside
the one navigation budget however many chats are open; the status's
`applied_per_context` and the SEL row's `contexts=` carry each context's landed
count, so a context the cookies missed stays visible. On the person's own
Chrome or Edge nothing is filled: the chat signs in through a takeover.

**Session cookies.** Every cookie is set WITHOUT `expires` (CDP reports it
`session: true`). Chromium can still keep a session cookie in the profile's
`Cookies` file (seen on a real machine: the row with `is_persistent=0` after
Chromium stopped), and a delete it has not yet committed is lost at its exit. So
`_stop_handle` deletes every cookie the gateway set, in every context
(`PlacedCookies.delete_all`, bounded like the tab close), and, once Kiro's
Chromium has exited, removes those rows from the profile's cookie files itself
(`credentials.purge_from_profile`: `Default/Cookies` and `Default/Network/Cookies`,
matched on name, path and host). Every stop goes through it: the gateway's exit,
Settings' Disconnect and a relaunch. A Chromium that crashes while the gateway is
down can leave them until the next start's apply overwrites or drops them. Expiry still counts: a cookie whose `expires` has
passed is dropped.

**Stale cookies are deleted.** `credentials.PlacedCookies` (one per browser, in
the supervisor's memory, reset when a new browser starts) remembers per context
each cookie the gateway set: its CDP params without the value, and the `expires`
the provider gave. Every apply passes it in. Per context it first deletes each
remembered cookie whose `expires` has passed, from memory alone, before the
provider is asked: a provider slower than the 8 s re-pull budget, or the switch
turned off, never keeps an expired cookie in the browser. After a pull that
succeeded (no error code, or `source_expired`) it deletes each remembered cookie
the pull leaves out, including one whose domain the owner removed from
`browser.credentials.domains`. A failed pull (`provider_failed`,
`provider_timeout`, a `CredentialSourceError` such as `source_missing`) deletes
nothing more, so a hiccup signs no one out. An expired cookie the pull returns
renewed is set again after its delete. A cookie is forgotten only once its delete
landed; one Chromium refused is tried again at the next apply. When an expired cookie
went and the pull is cut short by the budget or fails, a renewal is owed
(`next_expiry()` answers `0.0`) and the next navigation pulls again; only a pull
that works across the whole browser pays it, never one chat's context seed. The delete is
`Storage.setCookies` with the same name, domain, path and flags, an empty value and
`expires: 1`: Chromium drops the cookie and writes nothing to the profile (verified
against Chrome for Testing 154, in the default context and in a per-chat one, with
the profile's `Cookies` database empty after close). Only cookies the gateway set
are touched; a cookie a site set under its own name stays. A site that re-issued
the same name, domain and path itself loses that copy too, which is what the
provider's domain declaration accepts. Expiry is checked before a navigation, not
while a page sits open. The status's `removed` counts the deletes. Turning the
switch off removes only the cookies already past their `expires` (see Accepted
residuals).

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

**Finishing a sign-in.** A provider may also have the OPTIONAL method
`complete_sign_in(target, url) -> str | None` (not a Protocol member; read with
`getattr`; the public Default has none, so nothing below runs there;
[platform-context](platform-context.md)). `browser_harness/sign_in.py` wraps the
op's own task (`run_op`): after a `navigate` that committed, it asks the provider
only when the page is `https`, its host is in BOTH `domains()` and
`browser.credentials.domains`, the browser is Kiro's own Chromium with
`browser.credentials.enabled` on (read through the supervisor's own credential
settings, so the apply and this question agree), and the
person does not hold the tab. `target.reason` is `sign_in`; `domains()` and the
method run in one worker thread bounded at 15 s (`budgets.SIGN_IN_PROVIDER_S`).
Once per op: a completion that lands on a sign-in page again is not asked again;
`tabs new`, `back` and a click that reaches a sign-in page are not covered. A
returned URL is opened in the same tab only when it is `https`, has no userinfo,
no control character, backslash or space, is at most the navigate cap, has a host
in both domain lists, and passes the route's committed-URL gate
(`OpContext.url_allowed`); the opened page's landing is judged by that gate again
(a refused one is blanked, as for any navigate). The opening is bounded at 20 s
(`budgets.SIGN_IN_NAV_S`); the route adds both bounds to every `navigate`'s budget
(`budgets.SIGN_IN_WORST_S`), and so to the lock wait and the shim's timeout. It
runs inside the op's task, so a takeover cancels it like any op. The navigate
never fails because of it: the result gains `sign_in: {outcome, provider}`, where
`outcome` is `not_offered`, `provider_timeout`, `provider_failed`,
`provider_bad_shape`, `url_refused`, `held`, `nav_failed`, `redirect_refused` or
`completed`. **The URL never leaves the gateway as data:** after a completion the
agent is told the final page as origin and path only (query and fragment dropped,
then the exfiltration and credential redactors), the live card's step reads
"Finish sign-in on <host>", a refusal names the host only, and each asked
completion writes ONE SEL row (`browser_harness.sign_in`, caller `gateway`):
provider name, both hosts and the outcome. Nothing is set or kept: the site sets
its own cookies, which the stale-cookie deletes above never touch.

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
`applied_at`, `applied` (cookies set), `removed` (stale cookies deleted) and
`provider_status`. `provider`, `enabled`
and `domains` are read from the live config on every read, so a flipped switch
reads back flipped at once; the apply's outcome (`state`, `applied`) is kept only
while the browser it filled is still up and the settings are the ones it applied,
and otherwise `state` says what comes next (`pending`, `disabled` or `none`).
Every apply that set or deleted a cookie writes ONE SEL row (`browser_harness.credentials_injected`,
caller `gateway`): provider name, reason, cookie count, at most 20 printable
domains and, when it deleted any, `removed=<n>`. Nothing here returns, logs or audits a value; the value travels only in
the `Storage.setCookies` params the relay writes to the browser. No op returns a
cookie as such, and the snapshot walker never reads `document.cookie`; the agent's
own `js` and `cdp` can read cookies the way any script or DevTools client can (see
Security, "Agent runs its own script or CDP command"). There is no dashboard route that writes or reads cookies.

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
next page load; cookies already set stay until the browser closes or their
`expires` passes, and it does not
undo a page already read. (iv) A sign-in completion URL is opened in the tab, so
the tab's address, history, the live view and a later `tabs list` may show it
while the site keeps it; this is safe only because the provider contract requires
it to be single-use.

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
  closes only those. A chat whose session ends -- its tab closed, archived as
  idle, or its open conversation deleted -- closes the ledgered tabs it owned, in
  the background and after any op still in flight (`browser_ops.close_chat_tabs`);
  every other chat keeps its own. The same close drops the chat's hub session
  (`LiveHub.forget`), so no tab is sent its card again. A chat reopened on the
  same key before that close runs keeps the tabs and the session, and one reopened
  while it runs keeps its new session. Closing the
  tab of a chat linked to a channel session (`slack:<ts>`) closes none: that
  session keeps running and shares the tab's hub key.
- **Per-chat browser contexts** (`browser.isolation: per_session`, default
  `shared`; `browser_harness/session_contexts.py`). A chat's first op creates a
  browser context for its hub key with `Target.createBrowserContext` on the
  relay's own connection, seeds it (`HarnessSupervisor.seed_context`, Credentials
  above; bounded by `NAV_REFRESH_BUDGET_SECS` like a re-pull, past which the
  context is kept unseeded rather than fail every first op on a hung provider) and
  sets `CdpRelay.create_context`; the bind calls `ensure()` BEFORE reading the
  relay, since a helper call restarts a dead daemon onto a fresh relay that has no
  context and no `create_context` (the tab would open in the default context with
  isolation on), and records that relay on the session (`LiveHub.bind_context`,
  `bound_relay`) as the PIN every helper of the op passes to
  `HarnessSupervisor.call(relay=)` (`caller_checked_call`): a restart between any
  two steps, the bind's own included, refuses the op (`BrowserRestarted`, route
  code `browser_restarted`) rather than drive the new browser's default context.
  The post-op follow (`follow_current_tab`) is pinned too and checks the pin
  before its `current_tab`, so an op whose own steps read the restart as a failed
  step still answers `browser_restarted` instead of streaming the new browser's
  default-context tab on the chat's card; the chat's next op makes a new context;
  the relay adds that `browserContextId` to every
  daemon `Target.createTarget` naming none, so the chat's first tab and each
  `tabs new` open in its context, and the ledger records each tab with the context
  it opened in (`CdpRelay.context_of`). The op route clears `create_context` when
  the op ends, so a tab the daemon opens on its own lands in the default context.
  A chat's tabs are the ledgered ones it owns IN THE CONTEXT ITS BIND PUT IT IN
  (`LiveHub.context_of`, recorded by `_bind_target`: its own under `per_session`,
  the default under `shared`; the setting is read once per op, at the bind, so a
  flip mid-op cannot show `tabs list` another mode): `tabs list` shows only those, `tabs switch` accepts only those, and
  `_bind_target` keeps only such a tab, so a flip of the setting either way opens
  a new tab rather than carry one over, and a chat that chose isolation never
  slides back into the shared cookie jar through a tab it held before (the other
  side's tabs stay owned and are closed at session end). Under isolation
  `_bind_target` never adopts the daemon's current tab. `KiroCrewConfig.load`
  answers a present but unparseable file with defaults (a missing file is a
  clean default, `shared`), so `per_session()` reads the load's
  `_base_unreadable` / `_overlay_unreadable` marks and answers such a read with
  the LAST VALUE READ CLEANLY (`session_contexts._last_clean`): `shared`, or
  nothing ever read, keeps browsing in the shared browser as before (the default
  path is not changed by a broken file); `per_session` refuses the op
  (`IsolationUnknown`, one warning, route code `isolation_unknown`) rather than
  slide the chat into the shared jar. The map lives on the relay (`ensure_session_context`,
  `dispose_session_context`, created with `disposeOnDetach`): Chrome disposes a
  context when the connection that made it closes, so a reconnected relay starts
  with none and each chat gets a fresh one. Session end closes the chat's
  ledgered tabs, then `Target.disposeBrowserContext`. A context is in memory
  only: its sign-ins end with the chat. Headful Chrome shows a context's tabs in
  a window of their own.
  The setting is read at each op; turning it off moves a chat to a tab in the
  default context on its next op, and its context tabs close when the chat ends.
- **Popups.** A page an agent tab opens itself (`window.open`, a `target=_blank`
  link, a "Sign in with ..." window) is created by the page, so no create reply
  names it. At start the relay sends its own `Target.setDiscoverTargets`, and any
  `page` target whose `openerId` is already ledgered joins the ledger with focus
  emulation and its opener's browser context (so under `per_session` a
  popup stays in the chat's context), the relay keeping the popup-to-opener link (`CdpRelay.opener_of`,
  bounded at 1,024 links, oldest dropped). A page the person's own tab opens has
  no ledgered opener and stays out. The discovery events
  (`targetCreated` / `targetInfoChanged` / `targetDestroyed`) reach a daemon
  client only after that client sends `Target.setDiscoverTargets` itself, so the
  daemon's event buffer is unchanged. The op route binds first (which records the
  context this op browses in) and then follows a new popup only if it sits in that
  context: after a flip of `browser.isolation`, a popup opened by the chat's tab
  from the other side is still claimed (session end closes it) but never switched
  to, streamed or described.
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
1280-wide picture. The link is read from the faster frames of a window (its upper
quarter of per-frame rates): a busy card delays some acks, a slow link delays all,
and while that link carries 30 fps late acks never take the rate below 30 fps. A
window with no frame (a still page, the agent thinking) counts as calm against the
link last measured, so a rate stepped down climbs back while nothing moves; the
first frame after 0.3 s of stillness, and any input, puts the stream on its fastest
step at once. The hub's `rung` is `picture * 7 + fps step`.

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
the browser the frame rate may reach 60 fps (a slow link still steps it down); the picture stays. `browser.live.*` holds the knobs: `enabled`, `quality`
(`auto|high|medium|low`), `max_fps` (15|30|60), `mask_passwords`; `max_fps` caps the
fps axis and a fixed `quality` caps the picture axis. They are read per stream start from the
live config snapshot, so a Settings change applies to the next turn, never to a
cast under the person's hands. With `mask_passwords` (default on) the page is
asked, at most every 250 ms, for the boxes of visible `input[type=password]`
(a read-only `querySelectorAll` + `getBoundingClientRect` on the relay's own
session, walking open shadow roots to the same depth the snapshot walker does),
scaled from CSS to frame pixels; a failed query keeps the last boxes rather than
unmasking. While the person's input is under 1 s old the card's frame goes with the
boxes the last query read and the next query runs beside it, so a frame never waits
for the page; a secret field that appears mid-gesture can show unmasked in the card
until that query answers (the recorder never takes such a frame, see Turn replay). A
query run that way voids the recorder's proof that the fields stood still, so the
first held frame after a drive needs two fresh matching queries. The query runs in an isolated world made on the frame it reads
(`screencast.MASK_WORLD`), so a page script cannot replace those functions to
report no secret field; no world means a failed query. The world is made when
the stream starts; a query that has to make it again (a navigation destroyed it,
as a submitted one-time code does between the capture and the query) read a
document the frame may not show, so the recorder never keeps that frame. A
recorded frame also needs its query and the one before it to find the same
boxes (`_mask_prev`; a recording stream runs one query when it starts): a page
that drops a filled one-time-code field in the same document, after the capture
and before the query, shows the field in neither, so that frame is not kept. The opaque
boxes of cross-origin frames are part of what must agree, so a frame that held a code
form and was removed in that window is not kept either. The boxes are kept in CSS pixels and scaled per frame, so a frame at
another width (a picture change, the settle frame) is masked at its own scale.
Every clock read and sleep is injected, so the ladder and the settle frame are
tested on a fake clock.

**Wire.** Owner websockets only. State changes are the JSON event
`browser_live` (`session_key`, `turn_id`, `state`, `url`, `title`, `browser`,
`headless`, `step`, `steps`, `rung`, `fps`, `turn_over`, `tab`, `loop`,
`handback_options`, `ask`, `aim` once something aimed, and
`started_at_ms`/`ended_at_ms`, the turn's wall-clock start and end in epoch ms, 0
while not yet). A newly
connected owner socket is sent each open chat's last published event
(`LiveHub.snapshot_events`, from `dashboard/ws.py`), so a reloaded tab shows the
card, a waiting quick ask included, without waiting for a change. The card takes
its turn times from those two fields on first sight, so a restored card still
binds to its own turn's tool rows (the bind window compares the row's timestamp
with them), however long ago the turn ran. A running card whose record already finished takes it only when its anchor row sits inside that window; otherwise
it stays unbound until its own turn's record arrives. With the gateway's start the window
opens only 10 s before it (`BIND_LEAD_MS`): the row is the call that opened the
turn, so an earlier turn's row, mounted first after a reload, cannot claim it. Frames are BINARY messages: a
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
The gateway's event carries no `detail` (`live_hub.live_event` never sets it); the card renders one verbatim when an event has it. At the hub's
JSON publish boundary every textual value, including nested quick-ask labels,
summary and reason, passes the platform-aware credential and exfiltration-URL
redactor. A redactor failure drops the event rather than sending raw text. This
changes neither browser state nor pixel frames.

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
and otherwise opens a tab of its own; a daemon whose current tab was closed (an
ended chat's, or one the person closed) counts as having none. Chat B therefore
never clicks on chat A's page, `tabs` lists and switches only the caller's own
tabs, and a takeover in chat A fences A's tab while B keeps browsing. A popup
belongs to the chat that owns its nearest owned ancestor by opener links, which
outlive a closed popup, and the walk stops at another chat's tab
(`browser_ops.claim_popups`): before the
bind and again after the op, a popup that chat has not claimed yet becomes its
current tab (`follow_popup`, `switch_tab` with `activate=False`), and the op's
answer carries `popup: {target_id, url, title, hint}`, with `url` and `title`
left out when the navigate gate refuses the address, and capped at 2048 and 300
characters (the live hub's page caps) when it admits it. After a `click` or
`press_key` (`POPUP_LATE_OPS`), a popup the first look after the op missed (a
`window.open` after a fetch) gets `POPUP_LATE_READS` (3) more looks,
`POPUP_LATE_POLL_S` (0.15 s) apart, stopping when one is found or the person
holds the tab (asked again after each wait, and before the card follows the tab); a click that opens nothing therefore answers up to about 0.45 s
later, still under the op's locks. A tab whose url is still `""` (a popup before its first document
commits) is left to load: neither the op's own gate (`_refuse_current_url`) nor
the card's follow (`follow_current_tab`) sends it to `about:blank`. The card does
not stream it either: the follow stops the session's stream, so no frame of the
page it lands on is published or recorded before a later follow judges that
committed URL. Only the exact string passes: an answer with no url, or a url that is not a string, is
still judged and refused. When the bound tab is gone
and it was a popup, the bind walks the opener links back to the nearest tab the
chat still has instead of opening a blank one, so a sign-in window that closes
itself returns the agent to the page that opened it. When the op's own action
closes the popup (a "Log in" button that calls `window.close()`), the daemon loses
its session mid-op; if the bound tab was a popup and leaves the ledger within
`POPUP_CLOSE_GRACE_S` (1 s), the route returns the session to the opener and the
op answers `ok` with `popup_closed: {target_id, returned_to, hint}` instead of
`browser_error`. A bind whose bound popup closed under it binds again onto the
opener. Every popup move of the chat's tab (following a popup, or going back to
a closed popup's opener) is kept on the live session until an answer reports it
(`LiveHub.note_tab_moved`); the route adds it to the next answer it returns, as
`popup` or `popup_closed` with a hint, whether that answer succeeded, refused or
failed (the browser tool prints it after the error text too), and a HOLD, an
interrupted op, a quick ask's answer or a governance deny leaves it pending,
because their text cannot carry it. While it is pending, an
op that acts on an element or point of the page (`click`, `drag`, `scroll`,
`type`, `paste`, `press_key`, `hover`, `select_option`, `dialog`, `reflex`) is refused
with `popup_opened` or `popup_closed` and `ok: false`, because a ref or coordinate
belongs to the page it came from; every other op still runs on the new tab. A failure caused by the op's own action
closing its popup is answered `ok` with `popup_closed`. If following the opener then hits the navigate gate (the popup sent the opener to a refused address as it closed), the answer is that refusal, audited `denied`. Any other failure stays an error. `tabs list` claims first,
and a chat's end claims before it closes its tabs, so its popups close with it.
With `browser.live.enabled` off
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
claim's own awaits (an Esc in between has already said `live`). Only a lock
release leaves `in_control`: a supervisor state reported while the person holds
the browser (`waiting_allow`, `error`), and a non-`live` state the claim took over,
is queued on the session and published by the hand-back instead of `live`; the
turn's fold, a new turn and a started stream drop it. A claim on a
finished card (`done`, `error`) is refused (409 `turn_over`). **An agent op is
not lost while the person is in control: it waits for the hand-back and then
runs**, which is how the agent resumes by itself after a sign-in. The wait lives in
the TOOL, not in one HTTP call: the op route answers at once with
`user_in_control` and `hold: true`, and the `browser` tool parks itself as a
`DeferredTool` (the shim runs one worker thread, and the call has its own silence
timer on the ACP side, `acp-client.md` "Per-call silence timer") that every `HANDBACK_POLL_S` (5 s) posts
`/api/browser/handback` -- a short long-poll of at most `HANDBACK_WAIT_S` (3 s) on
`LiveHub.wait_for_release`, the awaitable the release sets -- and every 30 s pings
the session keepalive with the call's heartbeat (`tool_call_id`, phase
`waiting_for_handback`), beside the MCP progress note the stdio loop sends. On `released` the op is posted again and runs. Bounded by
`HOLD_TIMEOUT_S` (10 minutes) from each park, owned by the gateway's hub
(`LiveSession.parked_until`, one per session, so every call parked there hears
the end; a re-posted op held again starts a new hold on both sides): a poll after it
answers `hold_over: true`, and the shim's own deadline is `HOLD_GRACE_S` later,
only for a gateway that stopped answering. Either way the agent is told
`user_in_control` ("held for 10 minutes; tell them you are waiting and end your
turn"). A hand-back that finds an op parked (`LiveSession.parked`) inside the hold,
sending no words to chat, resumes every call parked there, one polling after the
end too (`LiveSession.hold_handed_back`, kept until the next claim or turn, or a
hand-back whose words go to chat). An op the claim cut short mid-flight
answers `interrupted` with `hold`:
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
owns (404 `unknown_session`). For a slot an app owns while that app is turned off,
every route that starts or keeps a view (visibility, ack, take over, input, reopen,
open in browser) answers 403 `app_disabled` and first ends what still runs: the
takeover is handed back and the stream is told no one watches. Hand back and close
always work, and a person's own chat has no app. Turning an app off from the
gateway ends its slots' views at once and broadcasts `app_state`
(`{"app", "enabled": false, "seq", "boot"}`; turning it on sends `true`), both while
it still holds the app's lifecycle lock, so one app's frames go out in the order the
changes committed and an enable from another tab cannot be overtaken by a late "off".
`seq` grows strictly within one process (the monotonic clock, bumped by one when two
frames read the same tick) and `boot` a random id drawn once per gateway
process (a pid can repeat after a restart, and the clock can restart lower): a tab
keeps the newest frame per app, drops an older `seq` from the same `boot`, and takes
any `seq` from a new `boot`. Every dashboard re-reads
the app list on it, and `useWebSocket` relays it as `mc:app-state`, which a mounted
`BrowserView` follows directly (`app-sdk/appState.ts`), because an app page reads its
app once.

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
  hand-back, the turn's start and its end, and it lapses when the hub's hold
  ends: a hand-back after that goes to chat, never to the turn's next op, and
  the op's poll answers `hold_over` so the op is not run behind words the agent
  never sees (`LiveHub.hold_over`). A claim before the poll saw a hand-back parks
  the op again on the same end, so that earlier hand-back resumes nothing; a note
  it carried rides on the `hold_over` answer instead. The hub keeps `{choice, note}` on the
  session (`LiveHub.take_handback` takes it once; a second note while one is
  untaken goes to chat); the poll answers `released` with `handed_back`, and the
  tool appends one line to the op's result (or to the `interrupted` text):
  `handed_back: {"choice", "note"}`, marked as the user's own words, not page
  content. Should a poll that was still inside its hold go away without taking
  it, the turn's next op answer carries it.
* `chat`: no op is parked -- the turn is between ops (it may end without
  another), a view was reopened after its turn, or the turn ended under the
  takeover. The live-view store (`browserLiveStore.release`) sends `choice: note`
  as the person's next message to that slot, text alone, on every surface that
  shows the view (the chat card, a crewmate pane, an app's `BrowserView`), so it
  goes out once (queued behind a running turn). Unless the gateway confirms the
  send, the note is kept in the slot's composer draft and the view says so. So a note is never held for an op that may not come.

The note never carries page content, is never logged on the input path, and is
recorded on the turn replay as the person's event (`actor: user`, label
`You handed back: <choice> - <note>`), cleaned like every label.

### The live card

One card per turn, attached to the turn's first `browser` call (anchor =
`tool_call_id`, else `ts`), rendered after the turn's last browser row and never
folded. A browser row is recognised by the stamped `tool_name` when there is one
(`browser` or a server-qualified `…/browser`, `…::browser`, `…___browser`) and
otherwise by its pill text, which for the managed-MCP tool reads
`Tool: @kirocrew-core/browser` (or the older wrench lead) -- the `@server/tool` form every MCP call arrives in; a new `turn_id` closes the previous record as done. Takeover grows the
same element; it never swaps in another component. Frames are decoded from the
binary message (the TS mirror of `decode_frame`, every field checked), acked ONCE
per `(stream, seq)` even when no live turn shows them (so the relay never
stalls), kept in a ring (1800 frames / 48 MB; only the newest two turns of a
session keep theirs) and painted to a `<canvas>` outside React's render
cycle (`createImageBitmap`, newest frame wins, mask boxes after the image); the
first frame is the one React render, later frames none. Visibility is posted only
on a change of (any card instance showing the CURRENT turn intersecting) AND
(document visible): a finished turn's card still on screen does not keep a later
turn's screencast running while that turn's card is scrolled off. A new turn
reports its visibility again, since the gateway may hold a fresh session for it
(a chat reopened on its key). A visibility
write the gateway refused is sent again on the next change, except a 404
`unknown_session`: that chat is closed, and resending it would fail on every tab
switch. A turn no card
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
the card's in-memory frames and step log. Recording-index and recording-list
request failures appear through `ErrorNotice` without disabling that fallback;
the intentional 404 (no recording) stays quiet.

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
carries over to it, and that turn's end folds it as usual. The card opens that
turn with no picture and Chrome sends no frame of a still page, so `turn_started`
publishes the new turn and then restarts the cast (`LiveStream.restart`), which
Chrome answers with the current picture at once; a view no card sees is left
stopped. Chrome draws no frame at all of a tab it hides (a background tab, a
covered or minimised window) and says so with `Page.screencastVisibilityChanged`,
which the stream logs at info; an agent op while the
person holds the reopened tab is held for the hand-back like any other. The done
line keeps two actions: Open live and Replay when the tab is open (Open in Chrome
moves into the live view's menu), else Replay and Open in Chrome.
An app's `BrowserView` with `stayLive` sends this same reopen by itself when its
slot's current turn folds with `tab: open` and the view is on screen in a visible
dashboard document, once per `turn_id` while it stays mounted. A hidden document
defers that attempt without consuming it until visible, if the current turn is
still folded with its tab open: a refusal shows as the button's refusal does and
is not retried, Close live view on the reopened turn leaves it folded, and the next
turn that folds gets its own reopen. While that reopen is due or unanswered the
view keeps its live face on the last frame, with no Take over, instead of the
done face; a refusal or a closed tab ends the hold. The `turn_id` is the one the
view already holds for its Show the live page button; the route and its checks
are unchanged. A view reports on screen for each new turn in one write, never
off and on again: the gateway may take two such writes in either order, and an
off taken last stops the cast under a view that is on screen. Below the picture
the view lists the turn's `steps` (the chat card's step strip), the latest five,
newest last, behind a toggle that counts them all; nothing is listed while the
person is in control.

**Sign-in from the Secrets vault** (`browser_harness/vault_fill.py`). The agent's
side does not change:
the same `ask` op, the same outcome words, and `type` into a password field stays
refused. For a `credentials` ask (targeted or the sign-in walk), `run_ask` reads
the vault in the gateway and puts each matching login item's entry name and
username on the card (`ask.vault = {items: [{name, username}], more?}`, never a
password; a name and a username at most 120 plain characters, else not offered;
at most `MAX_OFFERED` = 50 items, and `more` counts any past that, which the card
says once all are shown); `vault` is absent when the vault cannot be
read. A login matches by base domain, as 1Password's and Bitwarden's defaults do:
its stored URL's host and the ask's host have the same registrable domain (eTLD+1)
by the Public Suffix List with its private section (the `publicsuffixlist` wheel's
bundled copy, pinned exactly, never fetched), port ignored. A host with no
registrable domain (an IP, `localhost`, one label, a public suffix itself) matches
only the same host and port. An http page is offered only logins stored for http;
an http login on an https page is offered. Hosts compare in punycode; a URL that
does not parse, or a host that will not encode, matches nothing. Per-URL match
modes and linked sites (`apple.com` with `icloud.com`) need a vault schema change
and are not offered.

The card shows one button per match, labelled by the login's username (the
person is already on the site, so the entry name adds nothing); a login with no
username shows its entry name, and two offered logins sharing a username also show
each entry name, muted. The first 3 show; a **More** row shows the rest in place.
With a match, the typed fields sit behind an **Other account** row. A click on a
login posts `{action: "fill", vault_item: <name>}` and nothing else (no `values`, no
`save`); the gateway refuses a name the card was not offered (400 `bad_action`),
re-reads the item and re-matches it against the bound origin (an item edited,
moved or deleted since the card was drawn is 409 `vault_unusable`, the ask stays
open), keeps the password and, where the ask has a place for one, the username,
checks them with `check_values` like typed values, and fills through the same
bound, re-verified `fill` / `sign_in`. No fill happens without that click.
Typing (no match, or **Other account** opened beside one) offers **Save to Secrets store**,
off by default (`save: true` on the typed answer), so a second account can be saved
from the card. Only after a fill that landed does the gateway store a NEW login item named after
the host (`github.com`, then `github.com (2)`... on a clash: an existing item is
never overwritten, and a name the Secrets write route would refuse this caller is
skipped too: a Connections client secret, the WakaTime OAuth tokens and, for a
remote caller, a channel credential; `secrets.reserved_name_check`), with the typed username and password and
`urls = [the bound origin]`. The answer carries `saved` back to the card; a save
that failed is said in an `ErrorNotice` after the sheet is gone, by the session's
current card in every view (live or finished) and by the big view in live and
replay alike, until the person dismisses it; the turn ending does not hide it. One
SEL row per vault fill and per save names the item and the origin. No password
reaches anything but the page. The offered usernames reach only the owner's live
card (the owner-only live event); no username or password reaches a log, the SEL,
the replay, the poll or the tool result, so the agent sees neither.

### Turn replay

`browser_harness/recording.py` keeps one recording per (session, turn) so the
card can play a finished turn back: what the page looked like, and what the agent
(or the person, during a takeover) clicked, typed, dragged and scrolled, and when.

**Capture.** The hub attaches the gateway's one `LiveRecorder` at startup
(`bind_live_hub`). `turn_started` opens the turn's recording (a reopened view
reopens the same turn), `_fold` ends it. Frames come from the stream through the
sink's recorder seam (`_StreamSink.records` / `wants_frame` / `record`). When the
recorder's 5 fps gate is open (`wants_frame`) and the person's input is over 1 s
old (`RECORD_INPUT_QUIET_S`), the stream runs a box query FOR THAT FRAME, bypassing the card's 250 ms cache and even with
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
the person's input is under 1 s old is never recorded: no query brackets it, so
it drops any held frame and the proof that the fields stood still, and recording
resumes once two fresh queries agree. A frame that arrives while the gate is shut
is held; the stream's once-a-second tick offers it (`flush_held`) once input is
quiet, only when no newer frame replaced it, it is under 1.5 s old, its size matches, and
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
or `paste` (a `type` event, its `html` never kept) whose args say `secret`, or
that did not succeed (a password-field refusal, a stale ref, an interruption),
keeps `chars` and `secret: true` only, and a
`press_key` that did not succeed keeps no single-character key; the person's typed characters are only
ever counted (one `type` event of `chars` when the typing stops), named keys are
`key` events, a press-move-release is a `click` (under 4 px) or a `drag` with its
path (at most 64 points). Ops fold onto `click|type|key|drag|scroll|navigate|other`
by a generic name table; an unknown op is `other`.

**Storage.** `<data home>/browser-recordings/<sha256(slot key)[:32]>/<turn_id>/`
with `meta.json` (atomic rewrite), `frames.jsonl`, `events.jsonl` and
`frames/<i>.jpg`; directories 0700, files 0600, every file opened `O_NOFOLLOW`.
The root is a `HIDDEN` crew-home leaf (sandbox-masked, precreated, and on the
file-tool sensitive list), so another chat's agent cannot read or forge it
outside the owner-only routes.
`browser.recording.max_session_mb` (200) and `max_total_mb` (1024) cap the bytes;
before each frame and each action the oldest finished turns go first (the
session's, then any session's), never the turn that just ended; a running turn
that alone cannot fit stops keeping frames and is marked `truncated`, and its
action log continues while it fits (at most 5000 events). A write that fails
(disk full) removes its JPEG and cuts the index back, so the next frame, which
reuses the number, is kept once the disk recovers. The index is
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

`browser` is one MCP tool in the `kirocrew-core` shim whose ops are
`browser_harness.ops.OPS`, not a tool namespace. The shim runs two gates before
anything leaves the process: `capabilities.browse` governance (a denial refuses
outright; there is no other browser) and the navigate gate (public http(s) only,
plus the exact loopback `host:port` entries of `dashboard.browser_local_origins`,
never the gateway's port; applied to `navigate`, to `http_get`'s URL and to the
URL a new tab opens with). It then posts `{session_key, op, args}` to `POST /api/browser/op` (strict,
internal-secret only) and renders the gateway's answer. It holds no state.

#### One door: `run_browser_op`

The route's body is `dashboard/handlers/browser_ops.run_browser_op(session_key, op,
args, *, state, caller)`, which answers `(body, http_status)`. The route only
authenticates and validates the body, then calls it; an app's `PagePort`
(`apps/browser_page_port.py`, handed out by `ctx.browser.page(slot)`) calls the same function with the slot's own
runtime key (`dashboard:<slot>`), so the Bot and an app share every gate from one
copy, in this order: the `capabilities.browse` check for that key; the navigate gate
on the address the op asks to open (`navigate`, and `tabs new` with a URL), which
answers `url_blocked` before the browser starts, so a refused address is never
requested even by a caller inside the gateway that skips the shim; an app slot's
approved `browserSites`; the takeover hold; the gateway-wide lock (an app's op queues
behind the Bot's); the second browse check; the per-session
fence; the tab binding; the chat's `browser.run` step limits (`run_budget.check`, refusal code `run_budget`); and the per-caller checks of every helper and frame send.

An in-gateway caller is audited as a `GatewayCaller` (`app:<name>`, path
`app.browser`). When the person holds the page it is answered `user_in_control`
without parking the slot: it has no hand-back poll to wait on. It never takes the
note the person left for the Bot at a hand-back, so that note reaches the Bot's next
op, and it opens no card turn (it joins one the Bot has open), since only the Bot's
turn end folds a card.

`LiveHub.aim(key, x, y, label)` sets the cursor an app draws over the live view: the
`aim` field of the `browser_live` event (`{x, y, label, seq}`, viewport CSS px,
label at most `MAX_AIM_LABEL` characters), sent only once something aimed and only
while a stream shows the session. It is for the live view to draw over the frame (the
component that draws it is the RFC's step 1c), and it never enters the page.

`browser_harness.ops.run_op(op, args, ctx)` runs in the gateway. It reaches the
browser only through `ctx.helper(name, **kwargs)` -- one allowlisted Harness
helper over the daemon's IPC -- and the `js` helper is given `snapshot_walker.js`
plus one method call, except for the `js` op, which passes the agent's own
expression. The walker is sent without its comment lines and indents
(`ops.WALKER_SOURCE`): Harness writes each call as one line to its daemon, which
reads at most 64 KiB a line, and `test_browser_walker_ipc_line.py` keeps the call
8 KiB under that. Harness's `fill_input`, `dispatch_key` and
`wait_for_element` evaluate their own fixed expressions in the page's world, with the
selector passed as JSON data; the walker's `selectorBlocked` (its secret-probe guard,
`probeSelectorBlocked`) is asked first, so a selector testing a value attribute or
using an escape while a filled secret is on the page is `bad_selector`. Every method but three runs through
`js_isolated`: the same walker call, evaluated in an isolated world of the tab's
top frame (`helper_worker._local_js_isolated`), so a page script that replaces
`document.querySelectorAll`, `getAttribute` or a property getter in its own world
changes neither what the walker reads nor which fields it calls secret, and
cannot force the screenshot guards' counts (`askedValues`, `passwordBoxes`) to 0.
Only `stamp`, `state` and `consoleMessages` stay in the page's own world
(`ops.PAGE_WORLD_METHODS`): they need its `window` and `console`. The refs live
in the isolated world, which has one fixed name (`frames.TOP_WALKER_WORLD`, equal
to the helper worker's); Chrome hands every session the same world for the same
name, so the stream's session reads the refs the helper made (the quick ask's
targets, the frame slots). A count
that cannot be taken refuses: `screenshot` answers `secret_check_failed`, and a
decision-hook capture is skipped. Any frame, `object` or `embed` the top
document cannot read (`unreadableFrames`), hidden or not, refuses too, with or
without the live view: `screenshot` answers `unreadable_frame_on_page`. The live
stream's frame inspector (`FrameOps.count`) still adds its child-frame counts,
but it does not yet prove it checked every frame, so it is not trusted to lift
the refusal. A hidden frame counts because a page can show it for the moment the
image is taken. An `object` or `embed` always counts: the field counts walk only
into iframe and frame documents, so a PDF opened in the tab refuses too. A frame
in a closed shadow root is out of the walk's reach, so the browser's own count
of `iframe`/`frame`/`object`/`embed` elements (`frame_owner_count`:
`DOM.getDocument` with `pierce`, read by the helper worker, which also sees
out-of-process frames) is compared with the ones the walk reached
(`framesSeen`); any it missed count, and more seen than the browser holds means
the page changed between the reads and refuses as well. The cost: a page that
embeds a frame from another site (a video, a map, an ad, a sign-in widget)
refuses screenshots for now, live view or not; snapshot still works. A secret-looking field (a password, one-time-code or quick-ask field, or one
the walker's `secretish` names) inside a closed shadow root is out of every
page-side walk, so the browser's own DOM is asked (`closed_secret_count`, and
the live stream's replay mask asks the same through its session): any such
field refuses the screenshot (`secret_field_on_page`) and the decision capture,
and keeps that replay frame from being recorded as masked. A frame owner in a
closed root whose document the browser did not hand back inline runs out of
process and nothing looked inside it, so it counts as one such field. `press_key` refuses
select, copy and cut chords on a focused, filled field `secretish` names, as it
already does on a quick-ask field. While the census finds any secret field in a closed root, or cannot
be taken, every modified key is refused, since focus inside a closed root reads
as its host. The replay also paints over filled fields the walker's secret rule
names (`named` in the mask query), which the owner's live card leaves visible. A
follow-up is to restore live-view screenshots behind a check that every frame
was inspected. A filled field the walker's secret rule names (`secretValues`: an
OTP, card code or token typed into a text field during a takeover, which carries
no quick-ask marker; a password input and `type=hidden` are left out) refuses the
screenshot too (`secret_value_on_page`). The secret rule (`secretish`, the closed-root
census and the replay mask share it) names a field by its type, a quick-ask mark,
an `autocomplete` token (`current-password`, `new-password`, `one-time-code`,
`cc-csc`), or a secret word in its name, id or autocomplete, with camelCase split
into words first (`otpCode` reads as `otp Code`). `screenshot` writes the image into the hidden IPC leaf first, judges the
page again after the capture (URL gate, frames, filled fields), and only then
moves the image into the screenshot directory; a rejected image is deleted, and
a delete that fails is named in the refusal. The decision hooks' capture is taken
into the same hidden leaf, read there and deleted, so it never sits in the
screenshot directory. A page can still edit its own DOM (remove
`data-kc-asked`, change an input's type), which no isolated world prevents.
The walker installs `window.__kcWalker`
per document; it mints `eN` refs into `window.__kcRefs` of its isolated world
(dropped when the element leaves the DOM) and never reads a password or
credential-shaped value. The `console` hook is installed in the page's own world,
by `navigate` and by each `snapshot`, so `console` returns in-page messages since
the last navigate or snapshot.

| Op | Behaviour |
|---|---|
| `navigate`, `back` | stamp the old document and wait for a new one (or a matching href) with an injected clock |
| `snapshot` | the accessibility walker, run in the page; refs are per snapshot |
| `read` | the page's text in the walker's isolated world: the root's `innerText`, the whole page and not only the viewport, followed by the text of each open shadow root and same-origin frame `innerText` skips (Chromium's `innerText` does not enter a shadow root); an element with a box gives its `innerText`; any other element, and a shadow root, is read child by child, and a text node counts only when its range draws a box, so a `display:none` ref, a hidden frame's body and an unassigned slottable give no text while a `display: contents` element's children still do (an element with no box would fall back to `textContent`, script bodies included). So an unselected tab panel (`hidden` or `display:none`) and the body of a closed `details` are left out of the text (checked in headless Chromium); click the tab or open the section first. A frame this world cannot read (cross-origin, or an `object` / `embed`) is counted as `scanFrames` counts it, in a `note`, never read. The gateway redacts credentials and exfiltration URLs and neutralizes fence look-alikes in the whole content before it pages it, so a token cannot straddle two pages and the shim never grows a page past its cap; the shim puts the page, and the page title, between the untrusted-page fences (`context_assembly/markers.py`) with every fence look-alike neutralized, so a forged note or hand-back line stays inside; the URL in the header is neutralized too. `ref` scopes it to one element (`ref_stale` when gone; a cross-origin frame ref is `bad_ref`); `offset` pages it, `READ_CONTENT_CAP` characters a result so the shim's header still fits its 20,000 cap, with `total`, `next_offset` while more remains, and `digest`, a hash of the whole redacted content that changes when the page changed between calls. The page side stops at 2,000,000 characters, or sooner once the JSON-escaped content (6 bytes per non-ASCII unit) would pass 3.5 MiB under the worker's 4 MiB reply cap, and says `truncated`. A `format` other than absent or `text` is refused with `bad_format`; a negative, non-integer or past-the-end `offset` is `bad_offset` |
| `click` | a ref or `x`,`y`; `clicks: 2` is a double click and `clicks: 3` a triple click (Harness `click_at_xy` sends one press and release with that `clickCount`; on real Chromium it selects the paragraph). A ref is scrolled into view once, then re-measured without scrolling every 0.1 s until two reads in a row agree within 1 px (at most 6 re-reads), so a page that moves its layout on scroll is clicked where the target settled; a ref that measures empty to the end is refused `ref_stale` |
| `drag`, `hover`, `scroll` | ref or coordinates (`mouse_move` / `drag` helpers); a ref settles as for `click` |
| `type` | refuses a password field twice: by the ref's type and by whatever holds focus when the text would land. `type {ref?, steps: [{text} | {key, modifiers?}]}` (`browser_harness/type_steps.py`, reached from `ops.py` by a call-time import) runs its steps in order in one op: `ref` is focused once, each text step runs the focused-field checks above and each key step `press_key`'s guards, and the first refusal, a `dialog_open` included, ends it with the step's 0-based index as `step` and at the front of its `error`. A malformed list (not 1 to `STEPS_CAP` (50) steps, a step that is neither shape, a key step whose key `press_key` would refuse as unknown, `text` or a truthy `submit` beside `steps`) is refused `bad_steps` before anything runs; `TEXT_CAP` bounds the text of all steps together. A `\t` in `text` inserts a tab character, not the Tab key |
| `paste` | `text` plus optional `html`, behind the same focus refusals as `type`. The walker dispatches one `paste` event on the field carrying both as `text/html` and `text/plain`, so the page's own editor builds the table or headings as it would from a clipboard paste; the clipboard itself is never written, and nothing on Kiro Crew's side parses or inserts the HTML. A script's paste event inserts nothing by itself, so when the page does not cancel it the focus refusals run again (on `ref` focused again, or on whatever then holds focus when no `ref` was given), the plain `text` is typed (`type_text`) and the result says `handled: false`. Focus inside a cross-origin frame is reached only through the field's frame ref (`f<n>e<n>`), else `focus_in_frame` |
| `fill` | `{ref, text, clear?}` (`browser_harness/helper_ops.py`): `ref` is focused and judged with `type`'s refusals (`password_field`, `asked_field`, `focus_in_frame`; a cross-origin frame ref is `bad_ref`), then the `fill_input` helper runs on `:focus`, so it reaches exactly that field. `clear` (default true) empties it first. Real key events, then `input` and `change`, so a framework-managed field (React, Vue) sees the value `type` may not deliver. `:focus` needs the tab to hold focus, which the relay's focus emulation gives every agent tab. At most `FILL_CAP` (1,000) characters, one key press each (about 6 ms a key on a plain page in headless Chromium); a slow op. In a same-origin frame or a shadow root the `input` and `change` events go to the frame or host element |
| `press_key`, `select_option`, `wait_for` (text / textGone / time / load), `console` | as named. `press_key`'s named keys (`browser_harness/keys.py`) include F1-F12, Insert and ContextMenu; Harness has no key code for them, so the page sees `key` and `code` with `keyCode` 0, and ContextMenu opens no menu (a right click does) |
| `dispatch_key` | `{selector, key, event?}` (`helper_ops.py`): a script-made `keydown`, `keypress` (default) or `keyup` on the first element a CSS selector of at most 500 characters matches. It types no text. `not_found` when nothing matches within 0.5 s (`wait_for_element` first) |
| `wait_for_element`, `wait_for_network_idle` | `{selector, visible?, timeout?}` and `{timeout?, idle_ms?}` (`helper_ops.py`): Harness's waits of the same names, `timeout` in seconds as `wait_for` reads it (default 5, at most 30), `idle_ms` 50 to 10,000 (default 500); `wait_timeout` when the time runs out. Both are read-only |
| `upload` | `{ref, path, via?}` (`browser_harness/upload.py`, reached from `ops.py` by a call-time import) gives a page local files. `via` is `input` (the default, described here), `chooser` or `drop`, below. The walker's `fileInput` first checks that `ref` is a visible, enabled `<input type=file>` (`not_file_input`, `disabled`, `not_visible`, `ref_stale`; a cross-origin frame ref is `bad_ref`); the snapshot marks such an input `[file]`. Then the gateway checks `path`: absolute (`bad_path`), and `lstat` must find one regular file, not a link (`path_symlink`), a folder or a device (`path_not_file`), that exists (`path_missing`), is readable (`path_unreadable`) and is at most `UPLOAD_MAX_BYTES` (25 MiB, `file_too_large`). It copies the file with `hooks.safe_copy_file_nolink`, which opens it once without following a final link, refuses it unless the descriptor holds a regular file with one link whose OPENED real path is outside every sensitive location (`path_refused`, so a folder swapped for a link between the check and the open is caught), and streams that descriptor into a fresh owner-only folder under the hidden IPC leaf (`OpContext.upload_dir`, `<ipc>/uploads/<random>/<name>`), so the page sees the original file name and the browser reads only the bytes that were checked; the newest `UPLOAD_KEEP` (20) copies are kept for a form that submits later; each upload first prunes copies past 24 hours, so copies wait for the next upload to be pruned. The `set_files` helper hands the copy to `DOM.setFileInputFiles`, which fires the page's own `input` and `change` events. One SEL `api_access` row per attempt that reaches the path check (`operation=browser.upload`, the path, the size, an outcome word), never the content. `path` may also be a list of 1 to `UPLOAD_MAX_FILES` (10) paths for an input the walker reports `multiple` (else `not_multiple`, before any file is read); it stays under `path` so the tool gate's path keystone judges every one. Each passes the same checks, all are copied into ONE staging folder, each in a numbered sub-folder of its own so two names the disk treats as one (case, Unicode normalization) never replace each other (`stage`; nothing is staged when any is refused), two with one name (compared without case) are `duplicate_name`, the shared path screen (`hooks.validate_file_path`) runs before any disk probe, so a network path is never touched, and together they are at most 25 MiB. One SEL row per file. A `path` entry may also be a folder or a glob (owner's decision `docs/decisions/2026-11-20-browser-upload-takes-folders-globs-pickers-and-drops.md`). A leading `~` is the user's home; the entry must then be absolute. Folders and globs share one expander (`upload._Walk`): it lists one folder at a time with `os.scandir`, screens each folder before listing it (so a refusal names that folder, never a child of it), and skips and counts in the answer's `skipped` every dot entry, link, Windows name-surrogate reparse point (`platform_compat.lstat_is_name_surrogate`) and anything not a regular file or folder, never following one; the entry budget is charged as each folder is read, so a huge folder stops at the cap. A folder expands to every regular file under it, keeps its layout under its own name (`<stage>/<n>/<folder>/<rel>`), and is handed to the browser as that one staged folder; it goes only onto a drop zone (`via: drop`). A folder for a field the walker reports `directory` (`webkitdirectory`) is `folder_field_unsupported`, since neither a set nor a drop fills one on every build and both answered ok while the field stayed empty (deferred to #40410); for a plain field it is `not_folder_input`; a chooser refuses a folder too. Each names `via: drop` or a glob such as `<folder>/*`. An entry that exists is always itself, so `photo [1].png` is that file; only an entry that does not exist is read as a glob (`*`, `?`, `[` in any part after a fixed folder, on Windows the path `normpath`ed first so forward slashes split, as Windows itself folds `..`; on POSIX the fixed folder is kept as written, so `link/..` is the link target's parent, as the kernel opens it; `**`, a `..` after a pattern part that does not exist as written (on Windows, any) and a pattern that folds to no part are `bad_path`; a `..` after literal parts only is a real folder) matches each part with `fnmatch` and gives the matching regular files, sorted; a matched folder in the last part is skipped and counted; none is `no_match`. Every file found is first asked of the governance ceiling's `filesystem.read` for the caller, and so is every folder before it is listed, so no later refusal names a file, folder or size under a denied path; it then passes the same `_check_path` and copy as a listed one. That check is `OpContext.read_allowed` (fail-closed, naming the caller as the tool gate does: session, the slot's agent and app; the gate judged only the entries as written), and one protected or denied file or folder refuses the whole call (`path_refused`, naming no file under a denied path). One call is at most `UPLOAD_MAX_TREE_FILES` (100) files (`too_many_files`) and 25 MiB together, summed by `lstat` before any copy (`file_too_large`), reads at most 10,000 entries across all its folders and globs and walks at most 64 folders deep (`too_deep`; the walk keeps its own stack), and delivered names, and every folder inside a folder, are unique without case or Unicode form (`duplicate_name`). An upload with no card (below) answers `needs_card` for any path in the strict sandbox's hidden folders (the platform seam's `strict_dirs()`: `~/.config/gh`, all of `~/.docker` and `~/.kube`, and the rest, plus the kernel's `/proc`, `/sys` and `/dev`, where `/proc/self/environ` reads as a regular file holding the gateway's environment; each root and each path taken both as spelled and resolved, so a home that is a link is covered), compared without case, and asks `hooks.safe_copy_file_nolink` to refuse them again on the real path of the descriptor it opened (`refuse=`), so a parent folder swapped for a link after the walk cannot carry the copy there. It also re-reads the tab's host after staging and right before the files go (`trust_scope_changed`), since a chooser or a drop acts on whatever page is then there. `via: chooser`: the ref is resolved like a click target and the `chooser_files` helper clicks it with file-chooser interception on, then answers the chooser it opens (a hidden input, or one the page made and never attached), so no native dialog shows; `no_chooser`, `not_multiple` and `not_folder_input` come from what the chooser reports, and a chooser that opens in a frame of another origin is `frame_refused` (a ref wrapping a cross-origin iframe would otherwise hand a third party the files). `via: chooser` and `via: drop` also take `x`, `y` (the pixels `click` takes, `ops._point`) instead of a ref, because a drop zone is often a plain element with listeners that the snapshot gives no ref; the answer then carries `at` instead of `ref`, and the card names the point ("the point (x, y) on the page"). `via: drop`: the `drop_files` helper drops the staged paths on the ref's centre (a staged folder arrives as a folder entry), and a point over a frame is `frame_refused`. The card names the `via` and marks an entry that is a folder ("folder: every file inside except hidden files and links") or a pattern ("pattern: every matching file"). Before it asks, the card expands the entries with the same walker and governance read check the op uses (`upload.card_preview`; a refusal there is answered at once, with no card) and ends with one line naming what goes: "Sends N files, X KB: " and the first `upload.CARD_NAMES` (5) names, the rest counted as "and M more" (`upload.card_summary`). The ask record keeps that list, and the Allow hands it down as `OpContext.upload_groups` (`browser_card_ask.approved_upload`, read before the answer is spent), so the op sends exactly the files the person saw and never expands the entries again: a file added to a folder after the card is not sent. Before staging, each listed file is asked of `read_allowed` again (`upload._still_allowed`), since the policy may change while the card waits; staging still checks each copy on its opened real path and the 25 MiB total. The 500-character cap is on the call as written; the summary line, bounded by its 5 names of at most 60 characters, is added after it. An approved upload whose card kept no list is `approval_mismatch`. `upload` is an acting op (`op_classes._ACT_OPS`): it is never in the shim's read-only set, and the `app-browser-sites` grant never approves it (`browser_sites.gate_grants` names it). It waits on the op route's own Allow / Deny card, which lists the field and every entry as given (`browser_card_ask`), so it asks on the default spec too, except where the caller's slot (never an app's slot) holds a live `act` site grant for the host the chat is on (`browser_card_ask.upload_grant_host`): that grant covers it, the route hands that host down to the op, which re-reads the bound tab's host before the files go (`trust_scope_changed`), and the op answers `needs_card` for a strict-tier hidden path (`OpContext.upload_grant_host`), which the route turns into the person's card, so the grant never covers those folders and never strands the call either. The route decides from the caller's session key and slot, not `browser_host_trust._eligible`: a subagent or a native child posting under the chat's own key acts on the chat's own tab, the page the grant names, and the host re-check holds it there |
| `screenshot` | writes a PNG under the screenshot directory below and returns its absolute path |
| `tabs` | lists, switches and closes `relay.agent_targets()` only, the popups the caller's tabs opened included (`browser_harness/tabs_op.py`). `close` runs Harness `close_tab`; when it closed the tab the daemon was on, the daemon moves to the newest other agent tab, or to a new blank one when none is left, and the answer names it as `current` |
| `dialog` | reports the page's open JavaScript dialog (kind, text, default answer) and, with `action` `accept` or `dismiss`, answers it (`text` goes to a prompt on accept only) |
| `ask` | a quick ask the person answers on the live card; served by the route (`dashboard/handlers/browser_ask.run_ask`), and `run_op` refuses it |
| `reflex` | a loop of the ops above over a closed move set, judged by the decision seam, in `step`, `realtime` or `virtual_time` mode; `not_available` when it is off ("The fast loop" below) |
| `js`, `cdp`, `http_get` | Harness's own helpers, called as they are (`browser_harness/raw_ops.py`, reached from `ops.py` by a call-time import; the owner's decision `docs/decisions/2026-11-19-the-browser-tool-runs-harness-js-cdp-and-http-get.md`). `js {expression, target_id?}` evaluates the expression in the bound tab, or in `target_id`, which must be one of the caller's tabs (`agent_targets`) or an out-of-process iframe whose `Target.getTargets` `parentId` chain reaches one of them (`raw_ops._frames_of`) and that is still in the browser, else `not_agent_tab`; that target's own URL must pass the navigate gate (empty or `about:blank` passes), before the script and again after it (a value read from a target that moved is withheld), else `url_blocked`, since the committed-URL checks judge only the bound tab and a background tab or popup can have moved; `bad_expression` past `TEXT_CAP`. `cdp {method, params?}` sends one `Domain.method` (`bad_method`) on the bound tab's session: it takes no session id (`session_id` or `params.sessionId` is `not_agent_tab`, since no ledger says whose tab a session is on), a `params.targetId` must be one of the caller's tabs (`not_agent_tab`), and `params` may not carry the helper's own argument names (`bad_params`); a method that hands the browser a path on this computer (`DOM.setFileInputFiles`, `Input.dispatchDragEvent`, `Network.loadNetworkResource`, `Page.setDownloadBehavior`, `Browser.setDownloadBehavior`, and the tunnels `Target.sendMessageToTarget` and `Target.exposeDevToolsProtocol`, whose inner commands this list never sees) is `local_path`, since the browser runs outside the agent's sandbox and `upload` is the checked way to give a page a file. `http_get {url, headers?, timeout?}` fetches a public http(s) URL with no browser and answers a redirect as `{status, location}` without following it: the navigate gate reads its `url` in the shim and the route (`browser_sites.browser_call_url`, so an app slot's sites hold it too) and `ctx.url_allowed` once more (`url_blocked`); at most 32 headers with no line break (`bad_headers`); `timeout` is capped at 15 s, under the route's 20 s budget. All three are acting ops (`op_classes._ACT_OPS`), so the shim's hand-back wait holds them and a site's `read` grant never covers them. Each asks a person on its own card every call, whatever the agent's spec auto-approves or trust the chat holds: the op route opens an Allow / Deny agent ask on the caller's dashboard slot and answers `needs_approval` with its `ask_id`, the tool parks on that ask (`mcp_tools/browser._CardWait`) and posts the call again with `approval`, and the route runs it only on an Allow given for that same session and call digest, spending the answer (`dashboard/handlers/browser_card_ask.py`; `approval_mismatch`, `not_allowed`; `needs_person` with no dashboard slot or window, or for a Crew Mode worker). The card shows the whole call (the script, the method and its params, or the address with its query and headers), refused past 500 characters (`too_long_for_card`), and the Allow choice says what the step can do (Security, "Agent runs its own script or CDP command"), and no site grant names them (`op_classes.card_only`). Each writes one SEL `api_access` row (`operation=browser.<op>`, outcome `invoked`; `method=` for `cdp`, `url=` cut to scheme, host and path for `http_get`), never the expression, params, headers or result. The result is `{result}`: JSON text (a string as it is) redacted for credentials and exfiltration URLs and fence-neutralized whole, then cut at `READ_CONTENT_CAP`, with `truncated` and `total` past it; the shim puts it between the untrusted-page fences. A `js` or `cdp` reply past Harness's own 64 KiB IPC line fails as Harness's error. The pre- and post-op committed-URL checks below run as for any op, so a `cdp` `Page.navigate` that lands off the gate is blanked |
| `iframe_target` | `{url_substr}` (`browser_harness/raw_ops.py`): Harness's `iframe_target`, held to the caller. It lists the browser's targets with `Target.getTargets` (through the worker's `cdp` helper) and answers the first `iframe` target whose URL contains `url_substr` and whose `parentId` chain, through other iframes, reaches one of the caller's tabs (`agent_targets`): `{target_id, url}` (the URL cut to scheme, host and path), or `target_id: null`. A frame of the person's own tabs is never answered, nor a frame whose URL the navigate gate refuses. A same-site frame has no target of its own (the snapshot's frame refs reach it). A read op (`op_classes.READ_ONLY_OPS`); `bad_url_substr` past 2,048 characters |

Every op calls `live_hub.step`; `run_op`'s own `in_control` check is the backstop
under the route's fence (the route holds the op until the hand-back, see Takeover).
Before each page read or input and before the card follows a tab, the current URL
passes the same committed-URL gate; a refused link landing is blanked and answers
`redirect_refused`, while an empty `about:blank` recovery tab remains usable.
A refusal is a value (`{ok: false, code}`); a transport
failure is the route's `browser_error`; `timeout` is the route's budget (20 s, 75 s
for the slow ops, each plus the decision hooks' 12 s headroom, and a `navigate`
also plus the 35 s a sign-in completion may take, `budgets.SIGN_IN_WORST_S`; see
Credentials); `browser_busy` is the bounded wait for another chat's op on the
gateway-wide lock (75 s plus the sign-in's 35 s plus the hooks' 12 s headroom); `not_installed` comes from `browsing_available(home)`.
The budgets live in one module (`browser_harness/budgets.py`) that both the route
and the tool shim import: the shim's HTTP timeout is DERIVED as the route's worst
case (browser start + lock wait + credential re-pull + bind + op + the hooks' headroom) plus a margin, so the tool never
reports an op failed while the gateway is still landing it -- a retry of a click
that landed is a double click. The focus checks (`type` and `paste` without a ref,
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
session, in both cases in a fixed isolated world of that frame, and only directly
under the tab and named `f<tag>e<n>`. Before reading a child's outline or acting
on one of its refs, the op applies the same `OpContext.url_allowed` predicate to
the child's current URL. Refused children stay marked unread; a refused frame-ref
action answers `redirect_refused` through the existing blank-tab refusal path.
The quick ask's hit test at a field in a
frame passes when the browser's topmost node there is the frame's `<iframe>`, or,
for a same-process frame, a node of that same frame.

**What a step changed.** With `browser.report_changes` on (off by default), a
`click`, `type` or `select_option` result also carries `changes`
(`browser_harness/change_report.py`, reached from `ops.py`): the walker's `glance`
reads the page's own document before and after the step, and `diff` names a new address or
title, dialogs that opened or closed, new alert or status text, fields that failed
validation, and controls that appeared or went away; a JavaScript dialog the step
opened reads `js_dialog`, and `{"no_change_seen": true}` means none moved. Frames are
not compared. The reads around the step are bounded by `budgets.CHANGE_BEFORE_S`
(4 s) and `CHANGE_AFTER_S` (8 s), and while the setting is on the route adds their sum
(`CHANGE_REPORT_S`) to those ops' budget, so a slow read costs the report and never the
step's own answer. The user-facing rules (redaction, length caps, what counts as a
field) are in `src/kiro_crew/docs/browser-control.md`.

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
| pick | `click`, `hover`, `type`, `select_option` with `target` (a description) and no `ref`/`x`/`y` | one Choice over the outline's refs, each option's words (`role "name"` plus the nearest heading above it) in the state as `pick_options`, never in the question (at most 100, the ones sharing most words with `target`), with Nouls `present`, `error_shown`, `not_found`, `bot_check` | `present > 0.7`, the pick's p >= 0.6, no risk Noul > 0.7: the op runs on that ref and the result carries `picked` | refused `needs_ref` |
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
stands, off for an explicit no, and off until the owner says yes when the consent
is bound to a `sagemaker://` endpoint -- and only to a provider that declares images: taken with `max_dim` 1568 into the hidden IPC leaf (the screenshot directory when
no leaf is configured), read and deleted at once, never sent while the walker's `passwordBoxes` is not 0 --
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
| Request | one per decision, `extra.kind = "reflex"`: Choice `operation` over the moves offered (key presses, scrolls, drags, bound typing, WAIT, and one option per targeted operation); per targeted operation a speculative Choice `<op>_target` over the elements that operation fits (a select's options as `eN:i`), at most 100 options in page order, counting only the options offered (a risky or withdrawn one takes no room), a dropdown that still does not fit skipped whole; Choice `value` over `values` (at most 20) when there are several, so with at most 40 moves a request carries at most 465 options, within Laya's 512; Nouls `done`, `stuck`, `off_path`, the agent's `until_noul`, and `risk_i` for at most 8 moves not yet judged. Page words (target option words, risk move labels) ride the fenced state as `target_options` and `risk_moves`; the questions name them by key only | the judgments |
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

**One deny turns the whole browser off.** The shim's check is not the only one:
the gateway asks the same row, fail-closed, wherever it drives, shows or
installs the browser (`browser_harness/governance.py`, refusals shaped by
`dashboard/handlers/browser_policy.py`):

| Entry point | Where it is checked |
|---|---|
| `POST /api/browser/op` and every op, reflex loops included | the route, then `HarnessSupervisor.call` before each helper |
| the engine starting (browser, relay, daemon, credential apply) | `HarnessSupervisor._start` |
| hand-back and quick-ask polls of a parked tool call | the two poll routes; the tool ends its wait on `policy_denied` |
| takeover claim and input, visibility, frame acks, "Open in browser", "Open live" | `handlers/browser_live._run` |
| quick-ask answer (fill, fill from the vault, take over, done) | `handlers/browser_ask.api_browser_ask_answer` |
| turn replay (list, detail, frames) | `handlers/browser_recordings._gate` |
| Settings → Browser status, Install, Connect | `handlers/browser_harness_status` |
| startup auto-install and warm connect | `auto_setup_browser_harness` (`policy_denied` outcome) |

Hand back, Close and Disconnect stay open, because each only ends something. A
refusal is 403 `policy_denied`, and the first one also stops a browser that is
still running. The engine itself reads the ceiling with no surface (the policy);
a surface-bound profile is enforced where the caller is known (the shim, the op
route with the shim's session key, and the owner routes on `dashboard:ui`). The op
route asks for that session key again once it holds the browser lock (before the
credential refresh), before every helper the op runs (`browser_ops.caller_checked_call`),
before every CDP send into a cross-origin frame (`browser_ops._GatedRelay`, which asks
the engine's key as well) and before it answers, so a deny in the caller's own profile
that lands while the op is queued or running ends it with `policy_denied` and stops the
browser, as the route's first refusal does. The
dashboard reads `browse_enabled` from `GET /api/dashboard/config`: Settings →
Browser and the live card then show "Browsing is turned off by your
organization's policy" with no install or takeover control. Computer use is not
governed by this row.

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
key. Link and history landings are checked again before a page read, input or
live-card follow with the same gate, so an unlisted private destination is blanked
and refused rather than being operated through a link.

Passwords never pass the model: `type` and `paste` refuse a password field, the
frame masks one, and logins happen during takeover.

**An app's own sites.** A session an app owns whose app holds owner-approved
`permissions.browserSites` ([app-kit-platform §45](app-kit-platform.md)) gets its
`browser` calls approved by the gate's `app-browser-sites` row instead of a card, and
the op route narrows its page gate to those pages (`browser_ops.session_sites`, read
fresh per op): a `navigate` or `tabs new` off them answers `site_not_allowed` before the
browser is touched, and a page that lands off them is moved to `about:blank` by the same
landing checks above. The owner's media buttons on that app's page reach the tab through
`POST /api/browser/media` (`handlers/browser_media.py`; the state read is a `GET`), which runs the walker's `media`
method (play, pause, seek, volume, state of the main `<video>`/`<audio>`) in the
isolated world after the same page check (`browser_harness/media.py`). When the app
declares `backend.hooks.on_browser_media_ended`, `handlers/browser_media_watch.py` makes
that same `state` read every 3 s on the slot's tab after the agent's first successful op,
never queueing for the op lock (a busy tick is skipped), and hands each ending to the
owning app's backend ([app-kit-platform §45](app-kit-platform.md)).

**A person's site grant.** A chat's own `browser` card offers, inside its Trust
menu, a grant on the site the call acts on: "Trust reading pages on `<host>`" on a
reading op, "Trust reading, clicking, typing and uploading on `<host>`" on an acting op, each
for this session only: a site grant has no lifetime choice, and the approve route
refuses one with `ttl_unsupported`. Which ops are which is
`browser_harness/op_classes.host_trust_class` (`read`: the read-only ops,
`navigate`, `tabs new`, `scroll`; `act`: `click`, `type`, `paste`,
`press_key`, `fill`, `dispatch_key`, `hover`, `select_option`, `drag`, `dialog`,
`back`, `tabs switch`, `upload`; `tabs list` and `iframe_target`, which answer from every tab the agent owns, `ask`, `reflex`
and every call of `op_classes.card_only`: `js`, `cdp`, `http_get` and `tabs close` never). An `act` grant also lifts `upload`'s own in-band card on that host (`browser_card_ask.upload_grant_host`); "Trust all tools" does not. The grant rides the command trust tier, and its
rules, the host source and the route's `trust_scope_changed` re-check are in
[security](security.md) ("A browser site grant"). "Trust all tools" stays offered.

### Screenshot directory

`browser_snapshots.py` names the one directory the `screenshot` op writes to:
`<data-home>/browser-snapshots` (`snapshot_dir()`), derived from `config_dir()`
so an isolated `KIROCREW_HOME` stays isolated. Each `screenshot` first prunes the
directory's `shot-*.png` past a day or past the newest 200, sparing any younger
than five minutes (`prune()`); the gateway is its only writer. It is never inside a working
directory by construction, which is why `messaging/outbound_files.py` admits it
as the second approved root for an inline image in a channel reply: a
screenshot the agent just took can be sent, and admitting the directory grants
nothing the `file_send` tool does not already deliver from it. The read is
pinned to the directory's canonical spelling with the leaf kept literal, so a
link planted at that name resolves the file elsewhere and fails the pin.

### Settings → Browser

One card and three controls; everything else is under a collapsed More group.

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
  (Connect) and `connected` (Disconnect). "Not installed" is `needsInstall`:
  the engine is missing, or `browser.kind` is `kiro_chromium` and
  `chromium.downloaded` is false. A failed job or install request while no
  install is needed (a Redownload) is an `ErrorNotice` on the Engine row, not
  the card. A button whose request is in flight is disabled and `aria-busy`.
- **Which browser** (`browser.choice`) offers `auto`, `user_chrome`,
  `kiro_chromium`. **Browser isolation** (`browser.isolation`) offers Shared
  browser and Isolated browser per chat, the gateway's `shared` and `per_session`; the
  picker's value list is `ISOLATIONS` in `browserStatus.ts`, in the gateway's
  order, and a stored value outside it reads as `shared`, the gateway's own
  resolution. A new mode is one entry there plus its label key; the `Record` of
  labels refuses to compile without it. **Show the live view in chat**
  (`browser.live.enabled`).
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
  the browser closes or their `expires` passes; see Credentials). No cookie value is ever shown.
- **More** (a collapsible `SettingsSection`, which a deep link opens): Quality
  (`browser.live.quality`), Max frame rate (`browser.live.max_fps`), the Preview
  tab's local addresses (`dashboard.browser_local_origins`, editable chips), the
  sign-in sites (`browser.credentials.domains`, editable chips, only with a
  provider; an entry must be two or more lowercase DNS labels with no port,
  wildcard or IP, the PATCH route's `item_pattern`; empty follows the provider's
  own list), Engine
  (Harness and Chromium versions; Redownload posts `{components: ["chromium"],
  refresh: true}` so a Chromium already on disk is REPLACED, the repair for a
  damaged copy; the ordinary install keeps a whole one. The route reserves the
  job and closes the start gate at once; the job's first step,
  `HarnessSupervisor.stop_for_chromium_replace`, stops Kiro's own Chromium if it
  runs (never the person's Chrome) before anything is installed, and every start
  that would launch it is refused until the job ends, with the detail "Kiro's
  Chromium is being redownloaded".
  `chromium._swap_in` moves the installed copy aside (a `.previous-*` directory
  beside the version directories) before moving the new one in and puts it back
  if that fails, so a copy is never half-deleted, and a copy a failed restore left
  there is put back by the next install first; on Windows a copy still in use
  refuses the move and the redownload fails with nothing changed), and **Run with
  no window**.
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

The config section is `browser`: `choice`, `isolation`, `report_changes`,
`live.enabled`, `live.quality`, `live.max_fps`, `live.mask_passwords`,
`credentials.enabled`, `credentials.domains`, `recording.enabled`,
`recording.max_session_mb`, `recording.max_total_mb`, and `run.max_ops` /
`run.max_repeats` (0, the default, sets no limit; past a limit the chat's next op
is refused `run_budget`, and a takeover or a new chat counts again from zero).
`dashboard.browser_local_origins` stays where it is.

#### Retired keys

`dashboard.use_builtin_browser` and `dashboard.browser_view_port` configured the
agent browser this stack replaced. They are no longer fields. A `config.json`
that still carries them loads, logs ONE warning naming each one whose value
differs from the default older builds wrote into every config they saved
(`use_builtin_browser: true`, `browser_view_port: 0`; `config/resolution.py`,
`warn_retired_section_keys`), and drops them on the next save or locked write
to `config.json` (`_SECTION_KEYS_DELIBERATELY_DROPPED`, `update_config_locked`).
`PUT /api/dashboard/config`
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
`close`, `getState` and `annotate` on the `browserAPI` preload bridge, plus the
`onDidNavigate` and `onTitleUpdated` listeners, and nothing else (`website/electron/preload.js`, `website/electron/ipc-registrar.js`,
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
| Agent runs its own script or CDP command | `js`, `cdp` and `http_get` (the owner's decision, 2026-11-19: "改成这三个每次都弹卡") ask a person on a card every call, and the card does not come from the host: the default spec auto-approves the whole `@kirocrew-core` server (`allowedTools`), so no permission request is raised for a `browser` call. Every call of the three reaches `run_browser_op`, whatever surface or approval let the tool call through, and the route opens its own Allow / Deny card there and runs the call only on an Allow for that session and call digest, once (`browser_card_ask`): no site grant, "Trust all tools", trust-reads, YOLO, trusted pattern, hook auto-approve or app `browserSites` grant answers it, and a session with no dashboard slot or window to answer (the heartbeat, a channel, an unattended task, a Crew Mode worker) is refused. The card shows the whole call, redacted as every question card is, and refuses one too long to show (`too_long_for_card`), and says what the op can do (`cdp`: every site's cookies, every tab); `capabilities.browse` holds them at the route and every helper; a named tab must be the caller's (`js` `target_id`, `cdp` `params.targetId`), a `js` target's own URL passes the navigate gate, and `cdp` takes no session id, so neither attaches the user's tabs by name; the committed-URL check runs before and after; SEL records the op and the `cdp` method, never the script, params or result; the result is redacted and capped. **Accepted residual:** CDP is browser-wide. A `cdp` command that names no tab still reaches the whole browser: `Storage.getCookies` reads every site's cookies in the user's own Chrome, `Target.getTargets` lists the user's tabs, `Browser.close` closes it, `Target.setAutoAttach` can pause new tabs; a `js` expression reads what its page holds, a filled password field included. Redaction masks credential-shaped text, not every secret |
| Agent fetches a URL outside the browser | `http_get` is plain HTTP from the helper worker with none of the browser's cookies; the navigate gate reads its URL in the shim, at the route and in the op, so loopback, private, link-local and non-http(s) URLs are refused, and an app slot's sites hold it; SEL records the URL without its query, never headers or body. A redirect is not followed (`helper_worker._local_http_get`): the answer is its `status` and `location`, so the next hop is another `http_get`, gated and asked again. At most 512 KiB of the body is read. **Accepted residual:** the gate judges a host name without resolving it, so a public name that resolves to a private address is fetched, as for `navigate` (`url_gate.py`); no browser login rides along |
| Harness sends data out | five environment fences, a pinned version, a test on the fence; no CLI, MCP server or update check runs |
| Agent claims input | `/api/browser/input/*` and `/api/browser/live/*` refuse the internal secret |
| Agent types a password | `type`, `paste` and printable `press_key` refuse password fields, resolved through open shadow roots; the frame masks them (boxes clipped to the frame, shadow roots walked) |
| Agent sends a local file to a page | `upload` takes 1 to 10 entries (files, folders or globs; at most 100 files once expanded) into a visible `<input type=file>`, the chooser a click on the ref opens, or a drop on the ref, each entry judged by the path keystone and every file checked as one file is; inside a folder or a glob links are skipped, never followed, and one protected file refuses the call. The gate's path keystone (`param-paths`) reads `args.path` before the card; the gateway checks it again (absolute, a regular file with one link not reached through a final link, 25 MiB cap, and `hooks.safe_copy_file_nolink`'s sensitive-path check on the real path of the descriptor it opened, so a swapped folder link is caught) and gives the browser a copy read through that descriptor into the hidden IPC leaf, so a link or file swapped in after the check never reaches the page. It is an acting op, and an app's `browserSites` grant never covers it: the person approves it on the op route's own card, which lists every entry (`browser_card_ask`), so the default spec's `@kirocrew-core` grant does not skip it, unless the person gave this session an `act` site grant on the host the chat is on (never on an app's slot); the route re-reads the bound tab's host before the files go. Folders and globs skip dot entries and links, and a path in the strict sandbox's hidden folders (`~/.config/gh`, `~/.docker`, `~/.kube`, cloud credentials) never goes without a card: on a grant it is put on the card (`needs_card`), and the floor's sensitive paths stay refused. `via: chooser` and `via: drop` never deliver into a frame of another origin (`frame_refused`). SEL records the path and size, never the content. **Accepted residual:** an approved upload can send any ordinary file the gateway's user can read to the page that is open, as a person choosing that file would. **Accepted residual (owner's decision 2026-11-20):** after an `act` grant on a site, a page on that site that injects an instruction can make the agent upload, with no card, any ordinary file outside those folders that the gateway's user can read, up to 100 files and 25 MiB a call; the session-only grant, the tab re-check, the caps and the SEL row per file bound it |
| Agent-written HTML reaches a page | `paste` hands it to the page's own paste handler as clipboard data; Kiro Crew never parses or inserts it. **Accepted residual:** a page whose paste handler puts pasted markup into its DOM unsanitised runs it, exactly as it would for markup a person pastes |
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
| A provider sends the tab somewhere the agent did not ask for | only after an agent `navigate` landed on a page of the provider's own (both domain lists, `https`, Kiro's Chromium, the opt-in on); the URL it returns must be `https`, in both domain lists and pass the committed-URL gate, and its landing is gated again; one try per op; the agent, the live card and the SEL see hosts and outcome codes, never the URL's path or query. **Accepted residual:** the single-use URL is visible in the tab's own address and history (Credentials, residual iv) |
| A provider reads a file the agent planted | `credential_files.read_source_file`: `O_NOFOLLOW`, `fstat` on the open descriptor, owner uid, no group/other bits, size cap |
| Local addresses | `navigate` keeps the public-only gate plus `dashboard.browser_local_origins`, applied TWICE by one shared predicate (`browser_harness/url_gate.py`): in the shim to the URL asked for, and in the gateway to the URL the navigation (or `back`) committed on, so a public page that redirects to loopback, private or link-local is refused (`redirect_refused`) and the tab moved to `about:blank`. **Accepted residual:** DNS rebinding (a public name resolving to a private address) is not seen by either check; names are not resolved |
| Governance | `capabilities.browse` is checked on every call and a denial never falls back; the gateway asks it again, fail-closed, at every browser route, the engine start and every helper, so a direct POST or a policy pushed mid-session cannot keep a browser running |
| Page content | treated as untrusted input; a URL, instruction or form target read off a page never decides the next navigation |
| Agent obtains a vault login through a quick ask | the agent's `ask` and its outcome words are unchanged; the owner's card shows each login's entry name and username, never a password; a vault fill needs the person's click on the owner-only answer route (which refuses the internal secret) and goes through the same bound, re-verified fill; the item is re-read and re-matched against the bound origin at answer time; values never reach a log, the SEL, the replay, the live event, the poll or the tool result |
| A typed password is kept | only when the person ticked "Save to Secrets store" (off by default) while typing, and only after the fill landed; the save is a new item (never an overwrite) for the bound origin |
| A saved login is offered on the wrong site | base-domain match by the bundled Public Suffix List (private section included); exact host and port where there is no registrable domain; never an https login on an http page. **Accepted residual:** every host under one registrable domain is treated as one site, as in 1Password's and Bitwarden's defaults |
| Preview tab | native view, deny-all partition, no agent channel, local origins only |
| An app's own agent browses without a card per step | only for a session an app owns, while the app is enabled, trusted and holds owner-approved `permissions.browserSites` (the tool gate's `app-browser-sites` row), except `upload` and a call of `op_classes.card_only`: `js`, `cdp`, `http_get` and `tabs close`, which always ask; the op route holds that session to those pages: a request off them is `site_not_allowed`, a landing off them is blanked (`redirect_refused`). **Accepted residual:** the agent types and clicks unasked in a browser holding the owner's logins on those pages; a stronger sandbox is a follow-up |
| A person's site grant covers more than the site | the key is the host of the page the op acts on (a `navigate`'s own URL, else the chat's current page); a `read` grant never covers an acting op; `ask`, `reflex` and every call of `op_classes.card_only`: `js`, `cdp`, `http_get` and `tabs close` always ask, and an `act` grant also covers `upload` on that host (decided at the op route from the chat's key, so a child posting under that key acts only on the chat's own tab, re-checked against the granted host); a subagent's call or a harness whose subagent calls are not told apart gets no key; the op route re-reads the tab's address before the op and refuses `trust_scope_changed` off the granted host; a Unicode host is keyed in punycode, as Chromium reports it, so the card cannot show a look-alike; host keys are a namespace of their own, so neither `*` nor a command grant approves a browser call, and a shell command naming `browser-trust:v1:` gets no key; only a call approved by a site grant (the trusted-pattern rung, or the card click that made it) is re-checked, a plain "Allow once" runs as before. A note is never dropped to make room: when a session already holds 16 pending notes (or the store 4096 sessions) the grant does not auto-approve the call, which goes to its card. **Accepted residual:** an `act` grant lets the agent click and type unasked on that site while the browser holds the owner's logins there, as an app's sites do; a `read` grant lets it open any URL on that host, including a GET link that changes something; the host is re-checked once, before the op, so a page that redirects to another public host while the op is starting can take that one step there, as a click approved on its card can today |
| An app page's media buttons | `POST /api/browser/media` refuses the internal secret, non-owners and app tokens, asks `capabilities.browse`, acts only on the tab of a session an app with approved sites owns, never starts the browser, leaves a taken-over tab alone (`user_in_control`, under the takeover fence), and reads only the media element's playback state |

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
