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

A Kiro's Chromium that exits before its debugging port opens, or before the
daemon answers, fails the start at once
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
copy (all but the gateway's own origins, which only the route's request carries),
in this order: the `capabilities.browse` check for that key; the navigate gate
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

The dashboard is refused under every name the gateway trusts as itself, not only
its loopback port: a public URL whose origin (scheme, host, port, compared after
the gate's own host folding) is in the gateway's live `allowed_origins` is
refused. That set holds whichever of these the gateway trusts at the time:
`dashboard.url` (and its host on the served port), a connected tunnel, the tailnet
name, `KIROCREW_CORS_ORIGINS` entries, and the machine host name in a build that
is not local-only; governance may drop any of them (an LTS build keeps only the
ones a policy names). That set is what the CSRF and WebSocket checks accept as the
owner's dashboard, and the agent's browser may be the person's own signed-in
Chrome, so such a tab would act as the owner. Only the gateway knows that set (a
tunnel or tailnet name joins at runtime), so the gateway judges the URL
`navigate`, `tabs new` or `http_get` asks for (`url_blocked`) before it is
requested, as well as every landing. An operator-listed loopback origin keeps its
own rule above. The set comes from the route's request, so it is the Bot's: an
app's own `PagePort` ops (one door, above) do not carry it, since an app's
in-gateway code runs trusted and this gate is no boundary against it. **Accepted
residual:** an address that reaches the gateway but is not one of those origins
(another scheme or port on a trusted host, say) passes the gate, on the request
and on every landing.

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
never queueing for the op lock (a busy tick is skipped), and hands each ending the
walker counted (a new `endId`; the walker's `mediaEnds` also counts a source taken away
at the end) to the owning app's backend ([app-kit-platform §45](app-kit-platform.md)).

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

### Panel tabs (live pages in XR)

The XR page's workspace of pages (`/xr`, which opens it by default, or
`/xr?view=pages`; the land opens only with `?view=land`, `pages/xr/xrView.ts`) can show this dashboard's
own pages live on panels. A page inside an immersive session is not painted, so
the page is drawn somewhere else: in a tab of a browser on the gateway's host,
whose pictures the panel shows and whose input is the hand's. Off by default
(`browser.xr_pages`).

```
XR page ──xr_panel (open, ack, size, detail, visible)──▶ /api/ws ──▶ PanelHub ──▶ panel browser (pipe)
XR page ◀──xr_panel_frame (binary, this socket only)──── /api/ws ◀── PanelStream ◀── screencast
XR page ──POST /api/browser/panel/input, /ask──────────▶ owner gate ──▶ PanelHub ──▶ Input.dispatch*
XR page ──xr_panel (hello, input: a wheel, a move)─────▶ /api/ws ──▶ the same hub door ──▶ Input.dispatch*
```

| Part | Where |
|---|---|
| The second browser: pipe launch, no listener, one incognito context per owner socket | `browser_harness/panel_browser.py` |
| Open, close, scope, the address rule at run time, the sign-in, input, the three questions | `browser_harness/panel_hub.py` |
| The picture: `LiveStream` with a panel's size, two pictures in flight, no repeats, one lossless still | `browser_harness/panel_stream.py`, its pace and flight book `browser_harness/panel_pace.py` |
| The address list and the in-page guard and readers | `browser_harness/panel_page.py` |
| The socket leg and the two routes | `dashboard/handlers/browser_panels.py`, `dashboard/ws.py`, `dashboard/routes/browser_live.py` |
| The panel's side: the source, the socket seam, the marks on grant controls | `website/src/pages/xr/workspace/streamSource.ts`, `website/src/hooks/xrPanelLink.ts`, `website/src/guide/trustRoot.ts` |

**A browser the agent has no way into.** A panel tab is signed in as the owner,
so an agent that could steer it could approve its own tool calls. The panel
browser is therefore not the supervisor's: it is a second Chromium, started by
the same launcher (`supervisor.launch_chromium_pipe`), whose relay is
started with `serve=False`. That relay binds no socket and has no URL, so the
Harness daemon, the helper worker and every op of the `browser` tool have no
connection to it; only `panel_hub.py` holds it. No MCP tool, no CLI command and
no internal-secret route names a panel (`test_browser_panel_routes.py` scans the
package for importers of the panel modules and pins the list). The agent's own
navigate gate is unchanged: it still refuses the gateway's own port. Where the
pipe cannot be handed to a child (Windows) the panel browser refuses to start
and the panel says `failed`: it never falls back to a debugging port.

**A transport nobody can take hold of.** The panel browser's CDP rides two
socket pairs, not two pipes (`launch_chromium_pipe(..., pair=...)`; the
agent's own browser keeps its pipes). A pipe of a process can be opened a second
time through `/proc/<pid>/fd/N` by any process of the same user, an agent's
shell command among them, and a socket cannot: the open fails, for the
browser's ends and for the gateway's. A process that may attach to another one
of the same user can still copy a descriptor out of it (`pidfd_getfd`), so the
host must say that it does not allow that: the browser does not start when
`kernel.yama.ptrace_scope` reads 0, when that file cannot be read, or on a host
that is not Linux (`panel_browser.host_attach_refusal`), and the panel says
which in one line that names the host's setting (`host_attach_open`,
`host_attach_unknown`). One setting, `browser.xr_pages_accept_same_user_attach`,
off by default, is the owner's own word that the browser may start there all
the same; nothing ambient turns it on. What is left where it is on: a command
an agent runs can take hold of a browser that is signed in as the owner.

The panel browser and every child of it run with a core limit of zero, soft
and hard, whatever the gateway's own limit is (`supervisor._PY_REMAP` sets it
before it becomes the browser; a child that cannot set it does not become
one). A browser that dies of a signal would otherwise leave its memory, with
the owner's sign-in in it, in a core file wherever the host's `core_pattern`
points, for any process of the same user to read. So "other processes are
kept out" rests on three things: the socket pairs, the host's word that one
process may not attach to another (or the owner's setting in its place), and
no core file. It does not cover the agent's own browser or the gateway
itself: their memory can hold the same sign-in, and their core limit is the
operator's.

**It talks to this gateway only.** The browser is fenced on its command line
(`panel_fence.py`), so the fence holds for every context, tab, frame, worker and
window, and for an address handed to the browser from outside: every request
goes to a proxy that cannot be reached, except one to the gateway's own host and
port (the implicit rule that sends loopback direct is taken away, so another
port of the loopback is fenced too); no host resolves but the gateway's own, so
the proxy's name is never looked up and a fenced request fails inside the
browser with no packet sent; WebRTC sends no datagram. A request to the
gateway's own origin goes direct and costs nothing more. A picture, a frame or
a link to another site in a Bot's reply therefore does not load on a panel.

**It never waits on a question.** A browser dialog of a tab (`alert`, `confirm`,
`prompt`, the question before leaving) is answered with a no at once, and the
panel says so in one line (`refused`, `why: dialog`). A no is the closed
answer: a `confirm` in front of a delete then deletes nothing.

**Secret fields are covered at the source.** Every document of a tab gets a
style sheet, in the panel's own world, that draws password fields,
one-time-code fields, the product's asked fields and fields with a hidden-text
style or a secret name with no text at all and a hatch over them
(`panel_cover.py`). So no picture that leaves the browser holds one; the boxes
in a picture's header are no longer what the mask rests on. The sheet is one
that the document adopts, and a page's script can take it out of
`document.adoptedStyleSheets`. It is adopted again when it is found missing:
at each sweep of the cover's script, and in front of each question that the
hub asks the page (`panel_page.KEEP_SHEET_JS`). A secret that the
dashboard shows as plain text outside a field stays as the dashboard draws it.

**Who may open and drive a panel.** A panel is opened by an `xr_panel` frame on
`/api/ws`, and only from a socket whose upgrade request the sign-in gate
verified as the owner's own cookie session: not the internal secret, not an app
token, not a non-owner session (`browser_panels.panel_socket`). Its pictures are
sent on that socket and no other. Input and the three questions ride
`POST /api/browser/panel/input` and `/ask`: the dashboard's CSRF origin check,
the sign-in gate, then the same owner gate as the live card's routes (403
`internal_secret_required`, 403 `owner_only`), then `capabilities.browse`, then
the hub, which takes the call only when the caller presents the same access
token the opening socket was verified with, or that sign-in renewed (403
`not_your_panel` for any other; the handle a panel is named by is an address,
never a credential). Nothing new is minted.

A wheel and a pointer move may also come on the owner's socket, so that a scroll
does not wait behind a request of its own: `xr_panel` `{op: "input", handle, n,
event}` (`browser_panels._input_frame`). The door is the same one: the socket
is the owner's own sign-in, `capabilities.browse` is asked for each event, and
the hub takes it with the access token that socket was opened with, so only a
panel that sign-in opened moves. The answer comes back on the socket, named by
`n`. A press, a key and a text are refused there (`not_on_the_socket`) and keep
the route. A frame larger than the route's 16 kB body is answered `too_large`
and reaches no page. Two bounds, each with its own job: the socket's own 4 MiB
bounds a frame, which is parsed before its size is read here; the 16 kB bounds
what reaches the hub. At most four events of a socket wait for the page: wheels
for one panel add up, but only to the hub's bound for one wheel (16,384); what
is over waits as the next wheel. A newer move takes an older one's place, and
with the line full an event is answered `dropped`. The owner is read once a
socket, when it stands, as for `open`. The XR page uses the socket only after the
gateway answered its `hello` with `input: true`; a gateway that does not know
the word drops it, and the page keeps the route.

The XR page marks a press at once with a ring where it was sent
(`StreamSource.pressMark`, drawn by `Panel.tsx`). A press that the page does
not send (no picture yet, a drag, a control its aim already showed as guarded)
shows no ring. A press that only the gateway refuses shows it until the
gateway's answer is back, one round, since the page cannot know sooner. Else it
goes with the first picture that lands a round or more after the press, and
after 600 ms whatever came.

**How many.** One owner socket holds 12 panels (`MAX_PANELS_PER_SOCKET`), and
the gateway shows 12 in all (`MAX_PANELS`): two rings of six pages, the second
over the first, stand round a head with none crossing another. The count is taken again under the hub's lock,
so opens that come in one burst cannot pass it. The next open is told
`failed`, `too_many`, no tab is made for it, and its panel says in one line that
no more than twelve live pages can be open at once
(`website/src/pages/xr/workspace/source.ts`, `MOST_LIVE_PAGES`; a test holds
the two numbers together).

**The sign-in.** The hub sets the owner's verified access token as a session
cookie (`Storage.setCookies`, no expiry) in the socket's own incognito context,
over the debugging pipe. It is never part of an address, a log line, a SEL row
or a file; the browser's profile is a temporary directory removed at stop. The
context is disposed when the socket closes or its last panel closes, and the
browser stops with the last context. The sign-in gate pins a session to the
address it was opened from, so the tab must be that same caller: it reaches the
gateway on the loopback address the owner's own socket was accepted on
(`panel_page.loopback_origin`: `127.0.0.1` or `[::1]`, never a name), and a
socket accepted on any other address, or on a unix socket, holds no panel. The
page browser is fenced to one origin, the one of the socket it started for. A
socket that comes in on the gateway's other loopback address (`[::1]` when the
browser runs for `127.0.0.1`, or the reverse) holds no panel while that
browser runs, and its panel says a line of its own (`other_address`,
`xr.page_elsewhere`): its pages cannot be shown at the browser's origin
either, since the gate pins that socket's session to the address it came from
(`ip:<address>`, compared as text), and the fence is not widened to a second
address for it. An
owner who reaches the gateway through a tunnel or a proxy that ends on loopback
is such a caller; one whose session is pinned to a tailnet identity is not, and
the gate itself refuses the tab. A response carrying the gate's own refusal
(`X-Auth-Required`) ends the panel as `signed-out`. The tab also asks for the
dark colour scheme and for reduced motion; the owner's own stored theme, handed
over from the XR page's storage (bounded, `panel_page.clean_prefs`), still wins.
The XR page hands over what its origin has stored but for its own keys, those
that begin `kirocrew:xr-` (the kept room): a panel's tab gets none of them.
**Renewal.** The dashboard page renews its access cookie while it is open (an
hour before the session's end, or when a request is refused), and the renewed
token is another string. The hub takes a request's verified token for the
opener's own when its claims are the opener's but for their times and it is not
older (`panel_hub.renews`): the same person, the same kind, the same bounds.
It then keeps that token for the socket and sets the cookie again in the
socket's context, in memory, so the tabs do not run out with the old one. A
sign-in with any other claim is refused as before. An access token does not
name the chain it was renewed in, so another sign-in of the same owner with the
very same bounds reads as a renewal too; it can already do on the dashboard
everything the tab can, and the pictures still go to the opener's socket only.
**Accepted limits:** the tab holds no refresh cookie, and it is signed in again
only when the XR page sends input with a renewed token. A panel that nobody
touches for the last hour of a session ends as `signed-out`, with its line;
and the tab is a second dashboard client of the owner, so what it shows counts
as read.

**The guard is one setting, and it starts off** (`browser.xr_pages_guarded`,
default `false`). Four of the rules below take something from what the
dashboard's own page gives: the list of pages, the two pages for looking at
only, the grant controls, and the refusal of `autoSend` and `autoRun`. They
hold only while the setting is `true`, and each paragraph below describes that
way. While it is `false` a panel does what the dashboard's page does:

| With the guard off | Where |
|---|---|
| Every control takes a press, keys and text. The grant mark and the trust root are not looked at: the in-page guard's walk answers no for every element | `panel_page.guard_script(guarded=False)` puts one word in front of `GUARD_JS`, in the isolated world |
| No page is for looking at only: the Task Runner and Schedule take a press, keys and text | `panel_page.look_only(path, guarded=False)` |
| `autoSend` and `autoRun` in an address pass, for a load and for a move, so the dashboard's own sends work on a panel. A note's button whose address an agent chose can then send staged words or start a planned run at one tap, as on the dashboard | `panel_page.UNGUARDED_LAUNCH_PARAMS` is `token` alone |
| Any path of the gateway's own origin opens and is moved to, with any query, but the paths of `panel_page.SHUT_SHAPES` | `panel_page.allowed_path(path, guarded=False)`, `allowed_open`, `move_guard_script(guarded=False)` |

What stays out with the guard off is what reaches other people or other
machines, and the room itself: `/xr` (a room inside a room), the pages that
publish (`/deploy`, `/artifacts/deploy`, `/artifacts/remote/...`), and the
pages that admit people or link other machines (`/settings/people`,
`/settings/instances`, `/instances`), with `tab=people` and `tab=instances` on
the bare Settings path. The dashboard's router takes no notice of case, of a
slash at the end, of two slashes in a row or of an encoded letter, so a path
is compared in the one spelling the router reads (`panel_page._as_routed`,
and the same steps in `MOVE_JS`): `/XR/` and `/%78r` are the XR page. One
rule about which page comes up, and not about what a press may do, holds in
both ways: a Bot that the Bots page is asked for by `member=` is one of the
owner's.

What holds in both ways, because it takes nothing from the owner's use: the
socket's sign-in and the same-user rule, the local-only binds, the fence
round the browser, the cover over secret fields, `token` in an address, a
window that a page opens, a browser dialog, and the rule that a press, a key
or text is not sent when the page cannot be asked or the hub does not know
where the tab stands.

A panel keeps the way it was opened in: its in-page scripts were built for
it, and the hub's own checks read `Panel.guarded`, not the setting. When the
setting changes, the hub closes every panel at its next look (`_tick`), as it
does when `browser.xr_pages` goes off. Only the setting's own `false` takes
the guard off: a config that cannot be read leaves it on
(`panel_hub._default_guarded`). A panel that the hub closed for the change is
told so (`status` `closed`, `why` `way`), and its page opens it again in the
new way (`StreamSource.told`); the socket keeps its sign-in.

The XR page opens a page by one rule in both ways (`streamPath.ts`,
`livePath`): the wider one, so it asks for any page but the shut ones, and a
panel whose open the gateway refuses for its address is dropped
(`StreamSource.refusedOpen`). The palm's tiles and the plus's short list are
all on the guarded list (`listedPath`), so they work in both ways
(`pageMenu.tsx`, `PAGE_MENU`). While the panels are open, the plus lists the
dashboard's own navigation instead (`pagesToOpen`): every page that the
navigation advertises now, by its own name, icon and order, less a page whose
setting has it switched off (as the rail leaves it out), then the short list's
rows that the navigation does not name. A row is listed only when its path
and the path that the dashboard redirects it to (`REDIRECTS`, kept equal to
`App.tsx`'s by a test) both pass `livePath`, so `/instances` is not listed; no
row with a query, and none that leaves the origin (`livePath` refuses a path
that starts with `//` or `/\`). The list is as tall as the window has room
for and scrolls. A press that travelled more than 8 px over it opens no row,
and a row of a list whose rows changed in the last second takes no press
(`OpenPage.tsx`). A panel keeps two addresses (`Workspace.openListed`): the
one a row opened it with (`openedWith`), and the page's real one now
(`pagePath`, path and query; a path that the hub tells with no query keeps the
query that the panel holds for the same path, `StreamSource`). A row turns
the picture to a panel that it opened itself, wherever its page went by
itself since (the Bots page moves on to `?member=` of the Bot it showed
last); else to a panel whose real address now is the row's; else it opens. A
second press on it while the picture still looks where that row's turn left
it opens a copy; another address (another session's chat, another Bot's
page) opens beside. In a session a row opens as before.
For one thing the XR page asks
which way holds: a page comes up by itself only while the panels are open
(below). The frame `{"op": "way"}` on the panel socket is answered with
`{"op": "way", "guarded": true|false}` (`PanelHub.way`), and a socket that
asked is told again when the setting changes (`_way_changed`). The hub runs no
loop for it: the page asks when its socket opens and before a page comes up
(`workspace/panelWay.ts`), and until it has heard "open" nothing comes up. The
default is decided anew before a release.

**Addresses.** A panel opens a path of one fixed shape on the gateway's own
loopback origin (`panel_page.PATH_SHAPES`). The list is of the shapes that are
let through, so a route that is added later is refused until its shape is put
there:

| A panel may hold | Path |
|---|---|
| A session's chat | `/chat`, `/chat/<session>` |
| Bots | `/members`; `/members?member=<name>`: the page of one of the owner's Bots |
| Notifications | `/notifications` |
| All sessions, session dashboards | `/sessions`, `/session-dashboards` |
| The crew board | `/crew-board` |
| Schedule | `/schedule`: for looking at only |
| Task Runner | `/projects` and what is under it; `/tasks` (it leads there): for looking at only |
| Artifacts | `/artifacts`, `/artifacts/<name>` |
| Logs | `/logs` |
| Settings | `/settings` and what is under it, but for the tabs `people`, `connections` and `instances`; `/overview` (it leads to `/settings/overview`) |

**The one query a panel may be opened with** (`panel_page.allowed_open`). An
open names a path of the list and nothing after it, with one exception: the
Bots page with exactly one pair, `member=<name>`. The Bots page with no name
does not show a list: it opens the Bot that browser showed last, with its
message box, so a panel that is opened for a Bot must name it (a tap on a Bot
in the room of pages does). The name must be one of the owner's Bots as the
gateway holds them (`agents` of the live config); when those cannot be read
no name passes, since the Bots page opens the Bot it showed last for a name
that is nobody's. Any other key, a second pair, a fragment, a
name that cannot be decoded, and a query on any other path are refused before
a browser starts: the Bots page acts on other keys of its query as it opens
(`create=1` with `name` and `goal`; `dashboard=preview`). The address the tab
is sent to is built by the gateway from the decoded name, with every reserved
character encoded, and never copied from the caller. The panel is told the
path its tab shows and, on the Bots page, the one Bot its query names
(`panel_page.address_of`); nothing else of a query is told. So the XR page
knows which Bot a panel shows after the person picked another one inside it,
and the kept room brings the same Bot back. `streamPath.ts` spells the address
the same way, so both sides compare one string.

Not on it, on purpose: `/xr` (no room inside the room); the pages that publish
(`/deploy`, `/artifacts/deploy`, `/artifacts/remote/...`); `/apps` and what is
under it, and a built-in app's own route; the pages that set up who or what
reaches the gateway or the agent (`/channels`, `/connections`, `/instances`,
`/mcp`, `/hooks`, `/webhooks`, `/agents`, `/mc-agents`, `/capabilities`,
`/developer`); the pop-out and embed routes; `/knowledge`, which only leads on
to `/capabilities`; and three Settings tabs. `people` and `connections` act as
they open: the one sends a read that admits the people who accepted an invite
(`GET /api/shared-crew/tailscale/invites`), the other starts the enabled MCP
servers to list their tools (`GET /api/mcp`) and a helper that prepares each
provider's sign-in address (`POST /api/connections/premint`). A page whose
opening gives an agent more, or widens who may reach the gateway, is not a
panel's to open. `instances` and `connections` are also where the routes
`/instances` and `/connections` lead. What the pages on the list still do as
they open, with no press: the chat page makes a session when there is none and
reads the agent catalog (`GET /api/agents/catalog`, which puts a default agent
back when its template is gone), as Schedule and Task Runner do; `/members`
opens a Bot's thread and, once ever for a thread that is owed one, asks for its
greeting. A rule on the gateway's side for what a panel's tab may write is not
built.
`website/src/pages/xr/workspace/streamPath.ts` holds the same patterns for the
XR page, and a test of the page reads them from `panel_page.py`.

Two of the pages are for looking at only: the Task Runner and Schedule
(`panel_page.LOOK_ONLY_SHAPES`: `/projects`, `/tasks` and `/schedule`, each
with what is under it). Their own buttons approve a held step, execute a
planned run, tick "auto-approve tool calls" and run a job now, each at one
plain press, and none of them carries a grant mark: a mark is read by the
dashboard's own guide too, and would close those pages to it on the plain
dashboard. So the rule is the gateway's, by path. While a panel's tab stands
at such a path (`panel_page.look_only`, asked in `PanelHub.input` of the path
the hub tells the panel), no press, no key and no text is sent, and the answer
is the one for a guarded control, so the panel says that line; the wheel and
a pointer's move pass, so the person reads and scrolls. A release passes only
for a button that a press left down before the tab moved there. A tab whose
path the hub does not know, or that is not on the list, takes no press either.
What it costs: from an XR page a person cannot open a run or a job to read
it, and cannot stop one. Three controls elsewhere carry the grant mark for
the same reason: a Bot's two switches ("Send replies in one click", "Dynamic
capabilities", `CrewProfileSettings.tsx`) and the top bar's "Request a
feature", which sends a fixed message as the person with steps for the agent
that the person does not see (`FeedbackPill.tsx`).

The tab's document requests are paused (`Fetch`): the tab's own
document must be one of those paths, a redirect included, and a frame's
document must be the gateway's own or of the page's own making (`about:`,
`blob:`, `data:`), as the second net behind the fence. Neither the tab nor a
frame loads an address of the gateway that carries a `token`, an `autoSend`
or an `autoRun` parameter (`panel_page.carries_launch`): with `token` the
dashboard opens a session and sends the words that the token carries, with
`autoSend` it sends what waits in the message box, with `autoRun` and
`applied` the Task Runner executes a planned run, and nobody pressed anything
for any of them. An agent can raise a note with a button whose address it
chooses, so one tap on any words would otherwise be that act. `applied` alone
only selects a run, and passes, as any other query does. The names are read
as the page reads them (`URLSearchParams`): after the browser's decoding, so
`auto%52un` is the name; in lower case, which is stricter than the page; and
wherever the name stands, however often. A name after a `;` is not one for the
page, which splits its query at `&` alone, and the pages read none of these
from the part after `#`, so neither is refused. Nor is the bare path
`/settings` loaded with a `tab` in its query that the list leaves out
(`panel_page.names_unlisted_tab`). Settings takes its tab from the query when
its path names none and draws that pane at once, before it rewrites its
address, so `/settings?tab=connections` would draw the pane, and send what it
sends as it opens, that `/settings/connections` is refused for. A `tab` there
passes only when `/settings/<its value>` is on the list; given twice, every
value must pass; an empty one does not. Under `/settings/<tab>` the path's
tab wins and the query's is not read, and a tab that Settings does not have
draws the first pane. Both rules are one question (`panel_page.query_refused`),
asked of a load in the tab, a load in a frame and a move inside the page. The
request is failed
and the panel says its line. A link inside a page to a page on the list is an
in-page move, and the panel stays on it. An in-page move to an unlisted path
is stopped before it happens: the dashboard is one document, a press on a link
or a tab moves it with no load, and a page that has been drawn has already
done what it does as it opens. The browser tells every such move to
`navigation` first, and the tab's move guard (`panel_page.MOVE_JS`, in the
isolated world, with the gateway's list in front of it) cancels one that adds
or replaces an entry for a path off the list, or for a listed path whose query
is refused as above; the dashboard's router reads the
address, finds it unchanged and stays, and the panel says its line. What that
costs: a control of the dashboard that moves to such an address after a press
does nothing in a panel but that line. Today that is the Task Runner's
"optimize this plan", which moves to `/chat?autoSend=1&newSession=1`. A load is
the pause's to decide, and a step in the history leads to a page that was let
through before. `website/src/test/panelMoveGuard.test.ts` runs the script from
this module's source. The second net: an in-page move to an unlisted path,
or to an address whose query is refused,
that is seen all the same (`Page.navigatedWithinDocument`) is undone with a
step back, and the panel says its line; when the move replaced its entry, that
step lands on the page before, or on the empty page, which ends the panel. A
main document that still lands elsewhere, or on an address whose query is
refused, ends the
panel; downloads are denied. The hub tells the panel the path that its tab
shows now, each time it changes (`op: path`; the path alone, never a query or
a fragment), so that the XR page can keep it and ask for that page again when
its socket comes back. The empty page the tab was made with stays behind
its first page in the tab's history, so a step back past the first page lands
elsewhere and ends the panel. A wheel turned sideways past a page's edge is
such a step in a browser, and a finger dragged sideways on a panel is that
wheel: the panel browser runs with that step off
(`panel_browser.NO_SWIPE_BACK_ARG`). A window that a page opens never loads anything:
the browser attaches every new page and holds it at its start
(`Target.setAutoAttach`, `waitForDebuggerOnStart`), the only pages that are let
go on are the ones the gateway makes (`PanelBrowser.new_page`), and any other is
told to its panel in one line (`refused`, `why: window`), has every request it
could make set to wait, and is closed. The once-a-second look for windows of a
socket's context stays as the second net. In front of both, the tab asks for no
window at all (`panel_window.py`): the page's own `window.open` (a main-world
script, `OPEN_JS`) and a click on a link that names another window (the panel's
world, `ASK_JS`) become one word to the hub through the tab's binding,
`window:<the absolute address>`. An address that the panel may show
(`_may_show`) is told to the XR page (`{"op": "window", "id", "path"}`), which
opens it on a panel of its own by the same rule as an open (`PageSource.windows`);
any other is `refused`, `why: window`. A page drawn by the tab does the same in
the XR page (`DomSource.window`: a link that names another window is a window;
one of this origin goes to a panel; another origin or a path that stays out
gives the page's line, `refused`, for 5 s and opens nothing, but for a window of
another origin that he pressed for with no session running, which the browser
opens as its own tab as the dashboard does: a trusted click on the link, or the
page's `window.open` with the frame's transient activation within 1 s of his own
press, `outsideMay`, `OUTSIDE_PRESS_MS`). In a session a page drawn by the tab
opens no browser window at all. On the XR page (`Workspace.tsx`, `windows`): a window that comes within
1 s of a press of his (a page, the empty room, a pinch; `WINDOW_PRESS_MS`) opens
as the plus opens a page, and the picture turns to it; one that comes by itself
stands by the page that opened it (the nearest free place to that page), the
picture does not turn, and it takes no press in its first second
(`WINDOW_CALM_MS`: `mayPress` for a press that the panel passes on,
`DomSource.holdFor` for the browser's own). Opening a link on the person's own
device is not built.

**Grant controls fail closed.** The dashboard marks a control that grants an
agent permission with `data-agent-grant` (`no` on a control inside a trust
region that grants nothing), and a whole region of the agent's own ceiling with
`data-guide-trust-root`. `panel_page.GUARD_JS` runs in an isolated world before
any page script and stops a trusted press or key on such a control at the event
itself; the hub also asks what is under the point and does not send the press.
The panel is told `guarded` and says so in one line. Reject, Stop and Send pass.
The mark is one by one, so a control that nobody marked passes: a worker's
Approve and Trust (`CrewApprovalCards.tsx`) and the command center's Approve
(`AttentionCard.tsx`) carry it; a rule on the gateway's side that does not rest
on marks is not built.

Text and keys go to whatever holds the page's focus, and text is inserted with
no key event, so the page's guard never sees it. The hub therefore asks the
page's `focus` reader, which answers `guarded` for the focused element by the
guard's own walk, and sends no text and no key while the focus is inside a
guarded surface, on a field as much as on a button (`_focus_holds_back`). A
quick ask takes the focus by itself when it opens, so what a person is typing
into the message box stops there instead of landing in its sign-in field. The
nearest mark still wins: with the focus on a control marked `no` (Cancel in a
change card) a key is passed, and the message box is in no guarded region. A
long text goes to the page in pieces, and the hub asks before every piece: a
surface that opens between two pieces and takes the focus gets none of the rest.
Text is stopped at the event as well, as a press and a key are: the page's
guard cancels a trusted `beforeinput`, `paste` and `drop` on a guarded target,
so text that reached the browser by any way leaves a guarded field as it was.
The check is the hub's own and fails closed: when the page cannot be asked (no
isolated world yet, the reader is not in the page, the call fails), a press, a
key and text are not sent, and the answer says `unread`. A pointer move and a
scroll press nothing and still go. A send that the tab's connection cuts (it
closed, answered with an error, or did not answer in time) is answered 200 with
`lost`, and `sent` counts the commands the page acknowledged before the cut
(`panel_hub.input`); the panel's held buttons and keys follow those alone. A
cut `CdpError` is logged at warning with its method and message, so a bad
command of ours never turns quietly into `lost`. The XR page keeps the aim of
the last answer after a lost move, so the next press is not refused as
`stale`, and a lost text piece drops what waits behind it, as `guarded` does
(`StreamSource.move`, `type`).
**Moves.** The XR page sends one move at a time, the newest place behind it,
and none to the place it sent last (in the page's CSS pixels, rounded as it is
sent; `StreamSource.moveAt`): the framework makes a move at every frame for a
pointer that rests, and each was a request, a question to the page and a mouse
event in its tab. Once a wheel turn's answer is in, and after each new picture
of the page, one move to the resting place asks what is under it now, and the
page's own hover follows a page that changed under the pointer: at most one
move a picture, and none for a page with no new picture. A leave forgets the
place.
**The readout.** With `fps=1` in the XR page's address (read where `probe` is
read; no button, setting or route) a block of lines stands over the canvas's
lower left corner, outside the canvas and outside the box that counts fingers
(`workspace/Readout.tsx`, the rules in `workspace/readoutLines.ts`; the two
names differ by more than the case of a letter, which a disk that ignores case
would not tell apart). It is at most 340 px wide and never wider than the
picture less 16 px, and a line breaks at its ` · ` marks: each page's pictures a second over the last 2 s (the
page that is not held back first, then the ones in view), its picture's size,
kilobytes a picture and its way (`stream`, or `dom` for a page drawn by the XR
tab itself), in view n of m; for the first page the picture's way (socket to
texture, decode, the wait for a frame, upload; press to picture); the gateway's
numbers; the room's frames a second and its longest frame; the renderer. It
reads the sources (`PageSource.readout`) and a ring of the room's last frame
times twice a second; the ring is kept only with the word. A click copies the
lines as text. The gateway's three numbers ride in every picture's header
(`PanelStream._frame_header`): `round_ms`, the picture before's time from its
send to the panel's `decoded` ack; `pace`, the pictures a second the stream stands at;
`held`, whether the page is held back for no pointer on it. In a session the
lines stand on a plate (below, "Pinches in a session").
**The device's lines.** With `fps=1` a session adds lines before the pages'
own (`workspace/deviceLines.ts`, pure rules and one book; `workspace/DeviceRead.tsx`
reads the session each frame and writes only that book): `device` (the
session's kind, its blend mode, the rate it says, one eye's view in degrees
from its projection matrix, the picture's size and what it is the size of: the
session's whole base layer or its first projection layer's texture, and for how
many eyes); `granted` (the session's
features); `layers n of m` (pages on a layer of how many may be, and `not
granted` or `off by layers=0`); `pinch` (the last pinch: its hand, `select` when
the device sent one for that hand in this session and `tips` when the finger
tips are read, then `held`, `click`, `long`, `drag` or `cancel`, its
milliseconds, what took its down); `still` (the pinching hand's largest travel
in any 500 ms of the last held pinch and from its start, without the time
after the fingers opened in which the gate still holds a device's pinch);
`hands` (each hand seen or lost; how far under the eyes its ray begins, or
began when it was lost, which is the point that the raised-hand rule compares
(`pinchRoute.at`); the share of the last 2 s it was tracked; and its wrist's
height beside that, where the device gives joints); and with `desk=1` `tip`
(each index tip's height over the desk in millimetres, skin to desk). None of
them changes what a hand or the head does. After a session the page's block
keeps the last session's lines under a heading, so one click copies them.
`layers=0` in the address, with `fps=1` only, keeps every page on its mesh in a
session that grants layers (`Layers.tsx`, `off`); the `layers` line says so. **The speech probe.** With `fps=1&speech=1`
(`workspace/speechProbe.ts`, `workspace/SpeechTiles.tsx`) two tiles stand 34
degrees left and 24 down of where the person looked when the room opened or the
session began, 0.9 m away, one for each language (`en-US`, `zh-CN`). A press
starts the browser's own speech recognition (the Web Speech API, which can send
the sound to the browser maker's servers); a press while it runs stops it, or
gives up one that the browser never said it began (`no-answer`) or that was
told to stop and never ended (`no-end`); a press that travelled starts
nothing, in a session and in the flat preview. The `speech`
line says `MICROPHONE ON` while it listens, then what was heard, the
milliseconds from the end of the speech to the words, the browser's own error
word, and how the session fared meanwhile: `stayed`, `blurred` (WebXR's
`visible-blurred`: something of the system stood over it, as a permission
prompt does), `went dark` or `ended`, the worst of them. A session that ends
stops the recognition. The probe
acts on nothing it hears, sends nothing itself and never uses the gateway's
speech route.
**A page drawn by the XR tab itself.** With `source=dom` in the XR page's
address (in a session too) and a browser that has HTML-in-Canvas
(`'requestPaint' in canvas` and `texElementSubImage2D` on WebGL2: Chrome 157 and
newer), a live page is a `DomSource` (`workspace/domSource.ts`) and not a
stream: a same-origin `<iframe>` of the page is a child of the room's canvas
(`content="drawable"`), and each `paint` of it is copied into the panel's
texture with `gl.texElementSubImage2D` (an `ExternalTexture` over a WebGL
texture of the source's own, allocated anew when the picture's size changes).
Only a page in view is copied: a copy is the whole page at the canvas's scale,
the room's dearest work. A page out of view keeps the mark of a paint it missed,
and the panel tells it that it is in view again before the frame's copy
(`Panel.tsx`), so the first frame that shows it again shows it as it is.
No page browser, hub or socket takes part: the headers allow same-origin
framing already. Each frame the panel tells the browser where the page is drawn
(`PageSource.place`, `canvas.updateElementGeometry` with the full 4 x 4), so the
browser's own hit test sends the pointer, the wheel and the keys into the page;
the panel's press path sends nothing for it (its `pointer` and `scroll` do
nothing, it has no `key`, `text` or `answerPlaces`), and the gates of a stream
page's press do not apply: the picture is the page. The XR tab still hears that
input: the source listens in every document the page loads, in the capture
phase and passively (`pointerdown`, `pointerup`, `pointercancel`, `wheel`,
`keydown`, trusted ones only), and tells the host (`PageSource.listen`), which
counts the person busy as for a stream page (a pointer held, with the same 20 s
cap; the wheel's and the keys' quiet times) and makes an ask's panel his page
for a key or a notch (`keepsPanel`). Who takes the pointer is the room's own ray's
choice, not the browser's: the browser's hit test knows only the iframes (in
the order they were made, not by z-index, tried), and nothing of what the room
draws in front of them. So an iframe takes the pointer (its `pointer-events`
are on) only after a MOUSE's move, and only while the room's ray finds that
page's surface first under the pointer (`PageSource.hover`, from the panel's
page mesh); the room decides again at every frame from the pointer's last
place, and the page tells the room each mouse move in it (a `pointermove` at
the same point of the canvas), so a Bot, a handle, the close mark or a nearer
panel that comes under a resting mouse takes it back. Who takes it is decided
at the down and kept until the up or the cancel. The page's own press ends
wherever it is let go: its own `pointerup` or `pointercancel`, or one anywhere
in the top window, the window's blur, or the pointer leaving the window. When
the release comes outside the page, the page hears the end of its press by
script at the last place it heard (a `pointerup` and a `mouseup`, no click),
and the host counts the press over; when the browser itself sends the page a
release outside its picture, the click that follows is stopped before the
page's handlers. While one page holds its own press, no other page drawn by the
tab takes the pointer, so a release over another page is heard by none. A press that the room takes on
such a page (a finger, a session's ray, a thing that was in front a moment
ago) is handed in by script at the same point (`workspace/domAct.ts`): a real
mouse's order, `pointerdown` (`pointerType: 'mouse'`, `button: 0`), `mousedown`,
the focus of the focusable ancestor, then at the release `pointerup`,
`mouseup` and `click`, sent to the element that took the down wherever the
release is; a scroll is `scrollBy` on the nearest box that can still scroll
that way (a box at its end hands it on outward).
Those events are not trusted, so the host is told of them too. No text is
selected, no context menu or native list opens that way. The panel's path follows the
iframe's address (looked at four times a second); a link to a page that stays
out of XR (`streamPath.staysOut`: `SHUT_SHAPES`, the XR page among them) is not
followed, and a move to one is sent back to the last address. Sized by hand, the
iframe takes the page's new size; closed, it is removed. A Bot's ask comes up on
such a page too, and its tap is the page's own click. With `source=stream`, no
word, or an older browser: the stream, as before. No password cover, reduced
motion or dark colours are set for it (they are the hub's).

**Pinches in a session.** The dot at the picture's centre is a pointer of the
framework, like a hand's ray and the mouse (`workspace/HeadAim.tsx`): on a
streamed page it feeds the panel's own press path (the hub and its gates), on a
page drawn by the tab the hand-in by script, on the room's own things their
handlers. The dot is first (`workspace/pinchRoute.ts`): a hand points only while
it is raised (its ray begins less than `PINCH.raisedM`, 0.35 m, below the
eyes); a hand at rest has no ray and lights nothing. One thing is lit at a time
(a raised hand's ray that lies on a thing of the room, else the dot; a page
draws its own ring of a pointer's place for a hand's ray or the mouse, never
for the dot, which is its own mark), and a
pinch of either hand presses exactly that. A pinch is the device's `select` for
that hand once the device has sent one in the session, else the finger tips'
distance (closed under 1.5 cm, open past 3 cm), never both. The first down owns
the button; the other hand's pinch does nothing meanwhile. Who owns a press is
kept from its down to its up or cancel: while a pinch is held, the ray that it
presses stays on and every other ray stays off, however the hands cross the
0.35 m line (a raised hand that drops keeps its press; a resting hand that rises
stays the dot's and lights nothing). Held, the dot keeps
the ray of the down, turned only by the hand's travel (0.5 rad a metre past
1.5 cm), so a held pinch moved drags a page (a scroll). A pinch is a click when
the fingers open within 500 ms with the hand moved less than 1.5 cm; held
longer it is a long press: the page hears its down (its own long-press timer
runs), and at the release a page drawn by the tab hears a `pointerup` and a
`mouseup` with no click, as a mouse ends a held button; moved more it is a
drag, which ends the same way. A streamed page, to which the stream sends a
press as one click at its release, gets no click for either (shortcut: its own
long press does not run there). A pinch counts to where and when its fingers
opened, not to the gate's release 70 ms later (`pinchRoute.opened`, told at
`selectend`, or at the tips' opening): from then its place (`pinchRoute.place()`),
its travel (`pinchRoute.travel()`) and its speed over the 150 ms before
(`pinchRoute.velocity()`) stand still, so a brisk click whose hand is already
on its way stays a click, and the held dot's turn (`HeadAim.tsx`) ends there
too: a scroll ends where the fingers opened. A raised hand's ray is held the
same way (`hands.tsx`: the ray's pointer has a group of its own, which follows
the hand's space but for that time), so a page or a board that the ray moves
lands where the fingers opened; a pinch that closes again within those 70 ms goes on
(`pinchRoute.reclosed`). At the release the owner is gone: what acts there
reads `pinchRoute.released` (the hand, its place, travel and speed at the
opening), kept until the next pinch's down. The pages read what a pinch was while its
release is handed on (`pinchRoute.ending`). A `selectend` with no `select`
before it, and a hand that goes, end the press as a cancel. On a page drawn by
the tab (`workspace/domAim.ts`) the lit control is the one under the pointer's
own place; only when it lies on none does it snap, to the nearest control that
the page shows whole and uncovered, and that the room shows uncovered too (the
ray from the eye to its middle and to its four corners finds this page's
surface before a Bot, a handle or a nearer panel: `workspace/roomSees.ts`),
within 28 CSS px when the second nearest is
at least twice as far; else nothing is lit and a pinch presses nothing. A frame
inside the page is never lit. At the down the page is asked again: the pinch
presses only the lit control, at the same place within 4 px, lit for 300 ms,
that did not come or move in the last second; a snapped one is pressed at its
middle. A page drawn by the tab never opens the browser's own dialog while a session
runs (nobody sees it in the glasses, and it stops the XR tab until it is put
away): at each load its window gets its own `confirm` (false), `prompt` (null)
and `alert` (returns at once), and the panel's line says the dialog's own text
for five seconds; its `beforeunload` is not heard, so it leaves with no
"leave this page?". The frames inside the page are covered the same way: each
frame that the tab can reach gets the page's dialogs at once and at each of its
loads, at every depth, and frames that come later are watched for; a frame that
it cannot reach and whose sandbox allows dialogs has `allow-modals` taken out
for the session and is loaded once again (put back at the session's end). A
page that holds a frame which the tab can neither reach nor shut (of another
site, with no sandbox) is handed to the stream for as long as the session runs
(`PageSource.streamWanted` says `frame`; the hub puts a streamed page's dialogs
away), and the readout says why; when the session ends it is drawn by the tab
again. On a desk the browser's own dialogs stay. In a session the canvas is asked for a paint when it sent none for a
second, and when none comes 2.5 s after that, the page is handed to the stream
by itself (an ask's panel keeps its answers' places); until the stream shows it
the page takes no pinch, and the readout says so. With `fps=1` the readout
stands in the session on a plate (`ReadoutPlate.tsx`) that is always shown
and always whole in the eye's picture, and through which no press reaches a
thing that it hides. It takes a place where nothing that can be pressed is
behind it as the eye sees it (no page, and no thing of the room that carries
the mark `userData.xrPress`, `PRESS_MARK`; set on a mesh, or on a group for
every mesh under it, and counted while it is shown: the Bots' bodies and
their signs, the desk's tiles and rows, the palm palette, the speech tiles, a
page's close mark, an ask's sign; a new pressable thing sets it) and where it
lies whole in what both eyes show: over the page that the dot lies on (or lay
on last), turned as that page is; under it; beside it, right then left; a
corner of the view: lower left, lower right, upper left, upper right. There
it takes no pointer. Where no such place is, it stands in the first of those
corners with no marked thing behind it (a page may be), so that what the left
palm holds up, low on the left, can still be seen and pressed; with no such
corner, in the one with the fewest marked things behind it, the earlier of two
alike. Things are counted, not meshes: the outermost group or mesh that
carries the mark is one thing (one Bot, one card, the palette). A marked thing
nearer to the eye than the plate (1 m) is never covered while a corner
without one is there, since the plate, drawn over everything, would hide what
stands within reach; with a near thing in every corner it takes the one with
the fewest farther things (`coverOf`, `coversLess`). A page that was brought
nearer than the plate is a near thing too, though it carries no mark: a corner
may stand over a far page, never over that one while another corner is free
of near things (`behindOf`). Near is measured from the eye to the thing's
nearest point, not along the line of sight (a page's on its face, a marked
thing's on its box: `nearestFlatM`, `nearestBoxM`), so a page at 1.75 m to the side
of the view stays a far page. In all of these it takes
the presses that fall on it before anything else does (they do nothing), and
says so on its first line. That holds for every pointer: the dot's ray starts
at the eye and meets the plate, but a hand's ray starts at the hand and
reaches what the plate hides without crossing it. So while the plate takes
presses it tells the route which points it hides from the eye
(`pinchRoute.veil`: the point in the eye's picture against the plate's shape,
as both stand at the press), each hand's book keeps the point where its ray
lies, and a pinch whose lit ray lies on a hidden point presses nothing and
owns nothing (the probe's last pinch says `hidden`). The test is made at the
down; a pinch that began beside the plate keeps its target. A choice begins twice a
second and tries one place a frame, the place it has first; each try is a few
sums on shapes in the eye's flat picture (`plateFit.ts`: a page as its four
corners, cut at the eye's plane; a marked mesh as the upright rectangle around
its box), with no ray cast. The choice has give (`GIVE`), also where the plate
takes its presses: a place it does not stand at must pass with one degree to
spare, and it leaves where it stands only when two choices running say so;
through the first of the two it takes the presses that fall on it, and is
drawn at full opacity as where it takes them.
The plate is as high as its lines (9 to 20) and as
wide as its text needs from where it stands: text of 14 dp, the smallest a
page may show at that distance (`readoutLines.ts`, `plateWidthM`: about 1 m
wide under a page at 1.75 m). With `probe=1` the test hook reads the dot's ray, what it lies on, the
one light and whose it is, and the last pinch (whose, whose pointer, what took
its down and its up, and what it was). Known gaps of a page drawn by the tab: no
drag selects text in it, also with the browser's own drag (why is not known); a
press handed in by script puts the caret where the script's focus puts it, not
where he pressed, and opens no native list; a chat widget's buttons (a frame
inside the page) take no pinch.
**Typing.** The XR page sends what is typed one piece at a time and in order
(`streamSource.ts`): the next piece goes when the hub has answered the one
before, letters that wait are joined, and what waits behind a piece the hub
held back for a guarded focus is dropped. The answer to a press says where
the page's focus is now (`focus`: `editable`, `guarded`, `kind`, `rect`;
nothing of what the field holds), and after the last piece the XR page asks
the `focus` question once. The flat preview goes by that answer
alone: its own text field for a phone shows only while the focus
is `editable` and not `guarded`, and the arrow keys of a real keyboard go to
the page only while it is `editable` (`typing.ts`). That is a comfort of the
preview and no rule: the rule is the hub's, above.

`website/src/test/panelGuard.ts` runs the reader from this module's source on
the real surfaces, so the marks and the reader cannot drift apart unseen.

The rule the marks follow: a plain press is never passed to a control whose
press gives an agent more than it had (a permission, a tool, a server, a secret,
a trusted app or command, a wider approval mode, a go-ahead for a step it asked
leave for), or that widens who may reach the agent or the gateway. A surface
that an agent raised is marked `yes` as a whole at its root, and only the
controls in it that grant nothing say `no`, so a control that is added later is
guarded until someone marks it: a change card (`cards/ChangeCard.tsx`; Cancel,
Dismiss and the open and close of details pass) and the browser card's quick ask
(`pages/chat/quickAsk/QuickAskSheet.tsx`; Not now, Decline and "do it myself"
pass). A Settings tab's content pane is marked as a whole unless the tab is in
`pages/settings/grantTabs.ts`, the list of tabs that grant nothing; the list of
tabs itself is never marked. A surface carries its mark onto what it opens
through a portal (`AgentGrantRegion`, `AgentGrantProvider` and
`useGuideTrustRootAttrs` in `guide/trustRoot.ts`), and a mark that the caller
puts on that content wins over the surface's. The guard itself is unchanged: it
reads the same two attributes, and the nearest mark wins.
`website/src/test/agentGrantSurfaces.test.tsx` walks every kind of change card,
every kind of quick ask and every Settings tab with the guard's own rule.

**Pictures and the pixel budget.** A panel asks for `detail` picture pixels per
CSS pixel (about what it covers on the display; steps 0.5 to 2) and the tab is
laid out at device scale 2, so a picture never has more pixels than the eye can
show. `Page.startScreencast` runs with `maxFramesInFlight: 1` and
`sendLastFrame: true`, at JPEG quality 80 while a pointer is on the page and 60
while none is (`MOVING_QUALITY`, `HELD_QUALITY`, `PanelStream.quality`): about
half the bytes for a page that is watched, not worked on. When the pointer comes
or goes, the cast starts again at the new quality instead of the next ack, and
the start's picture is the next one. The lossless still of a page at rest is the
same either way. The panel acks a picture twice (`xr_panel` `ack`
with `kind`): `decoded` once it is decoded, which paces the next picture (and
`round_ms`), and `landed` once it is in its texture, which every rule that
waits for the person's sight reads: on the hub the wait before a held page's
cast is stopped (`PanelStream._wait_landed`, bounded by 0.25 s); on the XR
page `pictureSince`, the first picture after a page came back into view, and
the times of the readout. A page's first picture is set on the panel's material in
the frame it comes (`Panel.tsx`), so that frame shows it, not the room's dark. A
picture of a new size waits for the panel's next render, which brings its texture
and the page's new size together: the frames before it keep the old picture at the
old size, and none shows the new picture on the old rectangle. An ack with no `kind` (an older XR page) is both. A
newer decoded picture that replaces one before its upload leaves that one's
number never landed. A panel's picture counts as late only when its delivery
goes past its floor by 100 ms or 1.5 periods, whichever is longer
(`panel_pace.PanelController`, `PANEL_LATE_S`). The floor is the lowest of the
last 30 deliveries of pictures at least half its size, which is the round trip
with such a picture on it, counted as at most 0.25 s (`PANEL_FLOOR_CAP_S`): a
small picture (a nearly blank page while it loads) is no floor for the bigger
ones after it, and with no recent picture of half its size the floor is the cap.
A long round trip alone is therefore never late, while a queue that grows and a
decode that is slow are. The link's rate and the mean delivery read each
picture's busy time: from the later of its send and the decoded ack before it,
to its own ack (`Flight.acked`). With two in flight a whole delivery also holds
the wait behind the picture ahead, so a link that carries 20 pictures a second
read by whole deliveries looks like half that and holds the pace at 5; by busy
time it paces at 15. A link that is full from the first picture (a queue that
stands) still steps the pace down by its rate.
Two pictures of a panel are on their way at once (`panel_pace.Flight`,
`PanelStream._wait_card_ack`). The browser still makes the next frame only after
its ack, but the ack no longer waits for the panel's `decoded` of the picture just
sent: it waits for the one before it, so the next picture is made while this one
crosses the link and is decoded. A `decoded` ack of number N releases every
picture up to N, and only N's own delivery is evidence for the pace; a picture
released by a newer one's ack was replaced before its upload. The ack to the
browser still waits for a held page's clock (below). A lossless still waits for
its own ack. The pictures share the owner's `/api/ws` socket with the room's own
messages (the slots, the asks), which wait behind them, so a second picture goes
only while every panel's pictures on that socket, with this one, stay within
`PANEL_FLIGHT_BYTES` (384 kB, about what three panels with one picture each had
on their way); otherwise it waits for its own ack (`PanelHub._socket_flight`).
A picture no ack came for in `CARD_ACK_TIMEOUT_S` is given up as a late
delivery: by the next picture's wait, which goes on at the older one's own
2 s and not at its own, by the socket's count
(`PanelStream.flight_bytes`), and by a held page's check. A stopped cast clears
the book. A held page's cast is stopped for its wait only when no picture is on
its way, after the wait for the last one to land; one whose ack was lost is given
up at 2 s, and the page then waits a whole period with its cast stopped.
A frame with the bytes of the last picture sent is acked and dropped. "Sent"
means that the picture went out on the socket: a frame that was dropped on the
way (the panel left the view while the page was asked for its secret fields,
or the socket did not take it) is not the last picture sent, so the same bytes
go out the next time the browser makes them. The browser makes its next frame
only when the last one was acked (`maxFramesInFlight: 1`), so a frame
that fails on its way is acked too (`LiveStream._on_event`, for a chat's card
as well): without that, one picture that the socket did not take within its
ten seconds was the last one the page sent until a press and the watchdog's
three seconds. A failed frame is acked no sooner than half a second after the
ack before it (`FAILED_FRAME_PERIOD_S`): the ladder is told of no frame that
failed, so nothing else would slow the browser for a socket that takes no
picture until its heartbeat ends it. A page no
pointer is on holds its caret still and is paced at ten pictures a second
(`IDLE_PERIOD_S`, 0.1 s), and all the pages held this way share 30 a second:
with more than three of them that sent a picture in the last second, each
one's wait is their number over 30 (`PanelStream.idle_period`, counted by the
hub over every socket: `PanelHub._held_lately`). The period is a clock from the
stream's last ack to the browser (or the cast's last start): the browser's
capture, the send and the panel's decode and landing run inside it, so a slow
link or capture adds nothing to it. After a quiet spell (the browser sent no
frame for longer than a period after that ack), the picture's period starts
when it has landed instead, so the page does not send two pictures back to back
(`PanelStream._paced_ack`). The page he looks at is not held, though no
pointer is on it: the XR page names it with the socket word `watch`
(`PanelHub.set_watched`; the XR page's pick is `streamWatch.ts`: the page in
view nearest the middle of his look, after his look has rested on it 300 ms,
kept until another has been nearer for 300 ms or no page has been within 5
degrees of his look for 1.5 s (his look meets a page or misses its edge by a
hair), and none while a board or a speak sheet is in
his hands or the pages are put away; a page is measured by its point nearest
his look (`offLook`, told by `Panel`), so a wide page counts where he reads
it, and a page his look meets measures exactly 0; of two pages his look meets at once the watched one stays, and with
neither watched the one in front is taken), and its pictures go at the moving pace at the held
quality (60). One page a socket is watched: watching one stops the watch on
the socket's other pages; a page out of view, closed, or of a socket that ended
is not watched; the word acts only on a panel of the socket that sent it, and
an older gateway drops it (an op it does not know). A hand or an input ends every
wait of a held page, also the wait for its last picture to land: the flag is
cleared once, at the ack, and never by a wait. When the wait is 0.25 s or
longer (`HOLD_WITHOUT_CAST_MIN_S`), the page's cast is stopped for it after
each picture and started again when it is over, or at once when a hand or an
input comes (`PanelStream._hold_without_cast`); a shorter wait keeps the cast
on, since a start's first picture comes 70 to 130 ms after it: a cast that runs makes the page browser's
one compositing thread work for every frame the page draws, also while no
picture is asked for, and every other page drew less for it. A start gives a
picture of the page as it is then, so a change shows within the wait and the
start's 70 to 130 ms. While it is stopped the page counts as shown
(`PanelStream.holding`), so the hub's look starts no cast; a start that fails
leaves it neither holding nor casting, and that look starts it. A repeat of the
last picture (a page at rest) is acked after the wait with the cast left on. A
page that stops gets one lossless picture at the same size, a still. A still
is the first frame of a PNG cast of its own, on a second debugging session of
the tab (`PanelStream._take_still`: `Page.startScreencast` with `format: png`,
the picture's size and `everyNthFrame: 1`; the first frame; then
`Page.stopScreencast`; the wait for the frame is bounded by `STILL_WAIT_S`).
It is not a capture (`Page.captureScreenshot`): a capture shows what the
browser draws over the page by itself, in the page's own document and in a
frame, and no cast does. So every picture, moving or still, shows the page and
nothing else. At 2 picture pixels for a CSS pixel the still is the capture's
picture, pixel for pixel; below that it is the cast's smaller picture, a
little softer. The stream's own cast is not restarted for it, so a still never
sets off another picture. A still waits for
the panel's ack as a moving picture does, and it is not sent when the panel
left the view while the browser made it. The second session is let go when the
stream stops. Password,
one-time-code and other secret fields are painted over on every picture: the
stream's mask, which no setting turns off for a panel.

**A panel out of view.** Its screencast stops (`visible`), and its page goes
behind a curtain. A headless window cannot be hidden: minimized or without
focus, its page is still drawn sixty times a second. So each panel's window
holds a second, empty tab, made right after the panel's own under the hub's
lock (`_hang_curtain`; it is `about:blank`, never attached to, never navigated,
and counted as the hub's own by the sweep that closes a window a page opens).
Out of view, the page is told it has no focus and the curtain is brought in
front (`_draw_curtain`): the page is then a background tab, drawn about once a
second, and it keeps its socket, its timers and its script, so it is up to date
when it comes back. In view again, the page is brought in front, gets its focus
back, and its screencast starts with a fresh picture. That picture is sent
whatever it shows: `PanelStream.set_visible` forgets the last picture sent
before the cast starts, because a page at rest makes its first frame with the
bytes of the last one, and the XR page takes no press on a panel that came back
until a picture lands (`asks.ts`, `pressFate`). A panel is shown and
hidden one step at a time (`Panel.view`): a page event shows it from its own
task while the socket may hide it, and crossed, the two left a curtain drawn
back for a page out of view. The stream changes its cast one step at a time
too (`PanelStream._cast_lock`, for `set_visible` and `restart`): `casting` is
true only once the browser answered, so a hide that came while a cast was
being started found none to stop, and the cast that then ran unseen kept the
panel from ever getting a picture.

**A picture is made from a view of the page's size.** Every picture the hub
sends for a panel, moving or still, was made from a view of exactly the page's
CSS size. Its shape (`w / h = css_w / css_h` to within a pixel,
`panel_stream.has_shape`) is a mark of that and not the thing itself. A tab that
comes in front takes its window's own view (1280 by 657 in a window of
`--window-size=1280,800`), while its page stays laid out at the panel's size:
the cast's frames are then the window's view, with the page in a part of them,
and a press lands beside what is seen. Only the moving picture is wrong, since
a still is taken at the page's own size, so the person sees the page jump
between two shapes. A tab comes in front two ways: a panel comes back into view
(the curtain is drawn back), and a window that the page opened is closed (a
link with `target="_blank"`, `window.open`: the new window is a tab of the
page's own window, and the hub closes it, `_stray`, `_close_windows_of_its_own`).
Three things take the cause away, a fourth holds the rule for what is left,
and a fifth covers the one frame the fourth cannot judge:

- *The tab is laid out again after a show.* `_draw_curtain` calls
  `PanelStream.apply_layout` right after `Target.activateTarget` brings the
  page in front, before a picture is asked for. A new size, a still and a
  reload lay the tab out by themselves.
- *And after a window that the page opened is closed.* The panel browser tells
  the hub once the window is gone (`PanelBrowser.on_stray_closed`, after
  `on_stray`, which is told before the close), and the hub fits the opener's
  window and lays its tab out again (`_stray_closed`, `_lay_out_again`); when
  the browser did not name the opener, every page of that socket that is in
  view. The hub's own look once a second does the same after it closes one.
- *The window has the page's size.* `PanelStream.fit_window` gives the window
  the page's CSS size plus the window's own frame (`Browser.setWindowBounds`),
  at the open (`_prepare`, before the first layout) and at each new size
  (`set_size`). The frame is what the window measures more than the view in
  it, read once from a tab that is not laid out yet (`Browser.getWindowForTarget`
  against `Page.getLayoutMetrics`); a tab that is laid out answers with the
  page's size. A browser that does not say, says a frame over
  `MAX_WINDOW_FRAME`, or refuses the size leaves the window as it is. With the
  window fitted, a tab that comes in front has the page's view whichever way it
  came.
- *A net.* A frame is judged twice and passes both. The view that the cast
  itself names (`metadata.deviceWidth` and `deviceHeight`, `from_view`) is the
  page's CSS size to within a pixel: a frame from another view is dropped
  whatever its shape, because a view of another size can have the page's shape
  (a page of 1403 by 720 and the window's view of 1280 by 657; the frame is
  then the page cut at the right and at the bottom). And the picture has the
  page's shape: the first frame after a show is at times made on a surface 56
  CSS pixels taller than a view that it names rightly (1330 by 1008 for a page
  of 1024 by 720 at detail 1.4). A still is a cast's frame too and passes the
  same two tests. A frame that names no view is judged by shape alone. A dropped frame is never sent: the stream counts it
  (`PanelStream.misshapen`), lays the page out again and acks it, so the next
  one comes and is judged the same way. The ack is on the stream's grid with a
  floor of `MISSHAPEN_ACK_S` and not behind the idle period, so a single
  dropped frame costs no wait and a tab that keeps a wrong size is asked four
  times a second. The count rides in every picture's header (`misshapen`) and
  is in the XR page's probe (`seen.misshapen`). The page counts for itself the
  pictures that arrive with another shape (`seen.offShape`): the gateway sends
  none, so it stays 0, and the real-stack checks read it over every picture
  that arrived, not only the last.
- *A new size waits for the page's own layout.* The net cannot catch a frame
  that the browser makes at the new view before the page has laid itself out:
  it names the right view and has the right shape, and shows the old layout.
  So `PanelStream.set_size`, between `apply_layout` and `restart`, asks the
  page for two animation frames of its own (`_let_page_lay_out`,
  `TWO_FRAMES_JS`): the gateway's fixed text, evaluated in the hub's own world
  (`world_of`, which the hub points at `Panel.world`), never a word from the
  tab or a link. Frames that arrive meanwhile are acked and not sent, as a
  still's are. Only a stream in view that casts asks; at `LAYOUT_WAIT_S`
  (250 ms) or any error of the browser the size goes on exactly as before.
  While it waits, the socket's later ops wait behind it (one op at a time);
  acks do not.
  The hold (`_settling`) starts before the browser takes the new size: it
  covers `fit_window` and `apply_layout` too, and ends after the wait,
  whatever happens in between, and 1 s after it began at the latest
  (`SIZE_HOLD_MOST_S`), so a browser slow to take a size never stops the
  pictures for longer. No still is taken while it holds (`settle`): a
  still's own cast would catch the page before its layout; the next look
  after it takes the still. A still whose cast began before a new size came
  (`_size_gen`) is dropped and not sent. Nothing cancels a size op today (the
  socket's ops run one after another and none is cancelled), so the same size
  sent again after a cancel is not restarted.

**A press goes to what the picture shows under the finger.** The hub's press
goes to the page: it asks the page what is under the point, then sends the
press there. So a thing that is in a picture and not in the page breaks the
rule, and the browser draws some things by itself, in a window of its own: the
list of a `select`, a colour picker, a date or time picker. No picture shows
such a picker: a cast leaves it out, and a still is a cast's frame
(`PanelStream._take_still`). While one is open, a press where the browser
drew it would still be a click on the page behind something that the person
does not see. On a panel the dashboard's own `SimpleSelect` is such a
`select`: the page browser has no pointer that hovers (`(hover: none)`), so the
dashboard takes it for a touch device and renders its native control. Two
things hold the rule:

- *A select's list is drawn in the page.* The cover's sheet
  (`panel_cover.LIST_CSS`, a part of `COVER_CSS`, adopted by every document of
  the tab) gives a select and its picker `appearance: base-select`. The list is
  then an element of the page: it is in every picture, under the control or
  above it when there is no room under it, and a press on a row is a click on
  that `option`. The style also changes the closed control, so the cover's
  script marks each select once with what the page gave it (`data-kc-list`:
  `own` for a control the page drew itself with `appearance: none`, `auto` for
  one the browser drew), and the sheet puts back what it took: no second arrow
  beside the page's own (`::picker-icon`), and the browser's ground and border
  for an `auto` one, at no weight (`:where`), so the page's own style wins.
  Only the appearance and the second arrow are forced. A select with `multiple`
  or a `size` over one has no list to open: it is not marked and stays as it
  is.
- *A net, for every picker that the browser still draws by itself.* The page's
  answer to the press's own question (`panel_page.GUARD_JS`, `target`) says
  when one is open: an `input:open`, or a `select:open` whose list is not drawn
  in the page (the control and its `::picker(select)` are both read: a page
  that takes the sheet's `appearance` from either gets the browser's own list
  back), asked of the element that has the focus and of the document.
  The hub then does not send a press down or a click (`PanelHub.input`): it
  closes the picker with one key down of Escape and no key up (the picker takes
  the key down, so the page hears neither a key nor a click, and a dialog that
  the control stands in stays open) and answers
  `{"sent": 0, "picker": true}`; the panel says a line (`xr.page_picker`). A
  move is sent on and names no target. A release, a wheel and the keys go on as
  before. A browser that does not know `:open` throws at the selector: the
  answer is then "none is open" and the net is off there, with presses read as
  on any other page.

Neither reaches a frame that the dashboard makes for an agent's own HTML
(`sandbox="allow-scripts"`: a widget in a chat, an artifact). A select or a
colour field in it opens the browser's own list or picker, which is in no
picture: it does not open by a tap as far as the person sees, and it stays
open, unseen, until a press inside the frame.

`casting` is the stream's own book of what the browser does, and it follows
the browser's answers: set when a start is answered, cleared when a stop is
(`PanelStream._start_cast`, `_stop_cast`). One answer that does not arrive
makes the book wrong, so nothing rests on it alone. A panel that comes into
view asks for its cast whatever the book says; the browser refuses a start
while a cast of its own runs (`Screencast is already active`), and a refused
start is asked once more behind a stop. A hide draws the curtain also when
the stop was not answered. A show starts no cast behind a curtain that the
browser did not draw back, since a page there gets no frame. And a show that
ended with no cast, for either reason, is asked again by the hub's own look
once a second (`_tick`), not only at the page's or the socket's next word.
A restart (a resize, a change of detail, the watchdog) is a stop and then a
start; one that does not end leaves the book at "no cast"
(`PanelStream.restart`), so a stop that the browser did and did not answer is
put right at the next look, and not only by the watchdog after a press.
Not closed: a browser that is stopped and does not read its pipe holds a
command's write with no time limit (`relay.py` bounds the answer, not the
write), and a show, a hide or a restart that waits on it holds its lock for
as long. The relay also serves the agent's own browser and is left as it is.

When the browser gives no
second tab, or puts it in another window, the panel goes without and only its
pictures stop. `--process-per-site` was tried for the panel browser and is not
used: it saves memory, and with it a hang or a crash of one dashboard page
takes every other one with it.

**A Bot's ask comes up by itself, on a panel of its own** (`workspace/asks.ts`
holds the rules as pure functions, `workspace/useAskPanels.ts` the clock and
the panels, `workspace/AskSign.tsx` what is drawn over a panel). With the
panels open, a new ask of a Bot that has a card (an approval on a session's
row, a question card) brings up that Bot's own page (`/members?member=`) as a
narrow page, 390 CSS px wide, straight ahead of the eye. No picture turns, and
no page of the room moves. The rules, each with a unit row in `asks.test.ts`:

| Rule | Where |
|---|---|
| Only a new ask, and only from a whole read (roster, approvals, questions, the live connection, the whole list of sessions). The asks of the first whole read count as seen. An ask that is gone in a whole read is forgotten, so an ask of the same name later is new | `readAsks`, `model.wholeRead` |
| Not while the person is busy: a pointer is down, two fingers move, the quiet time runs, the picture turns, keys went to a page in the last 5 s, the wheel turned on a page in the last 2 s. It comes up after 2 s of calm | `isBusy`, `nextStep` |
| One at a time, the ask that was seen first. The next waits while an ask's panel stands in view, also while it says that nothing is asked any more | `nextStep` |
| One ask's panel for a Bot. While a part of it is in the picture nothing moves; out of the picture it comes up again | `nextStep` (`again`) |
| It stands 5 cm nearer than every page its whole face would cover and 1 cm nearer than a Bot's front. In the flat preview the bar is a button: a panel that comes nearer grows about its middle as far as the picture allows (its upper edge under the close mark's room, its lower end over the picture's lower end, 6 px of air each); only then is its lower end held, and the mark is what leaves the picture. Each side may leave the picture by under 30 page px. Nearer than that, or nearer than a hand can push a page, it does not come up: the Bot keeps its sign, and a line says once that a page stands in the way. With the drawn room on it is raised clear of the desk | `askPlace`, `askLeastFlat`, `askPanelFlat` |
| No panel takes a press before its first picture, nor before the first since it came back into view. A new live page takes no press, wheel, key or text until a picture of it that is not one flat colour landed (its middle half judged; a still counts only when it is not flat, D55 3), and 3.5 s after live at most once a picture of this open has landed (a page that stays flat opens by that clock alone); until then it is drawn at 60 % and nothing lights on it, the same sign as a warm page taken back (D50, F1; `StreamSource.awaitingContent`, `notYet`, `flatness`: the picture drawn into a 48×30 canvas, its 1,440 samples read back, only for the pictures before the first with content). A press is judged at its down: a down that went nowhere leaves no press, so its release sends nothing. This does not change when a room counts as ready (D45: `pictureAt`). An ask's panel takes no press and no notch of the wheel until 1 s after it. On a Bot's page a press is held back for 0.5 s after a press while that Bot asks and for 1 s after its asks changed, and on every panel for 0.5 s after an ask's panel closed by itself | `pressFate`, `wheelFate`, `StreamSource.pictureAt` |
| An ask's panel takes no press in the second after the controls that answer an ask changed in number or in place on its page, and none until the page has said where they are. Those controls are the ones that carry the dashboard's grant mark (an approval's buttons: Allow once and Trust say yes, Reject says no) and a question card's Dismiss and Submit, which carry the XR page's own mark `data-xr-answer` (`QuestionCard.tsx`; nothing else reads it, the guard does not); a question card's options do not. The page script's `state` answer lists the places of the drawn ones (`grants`, at most 40, with the guard on and off). The XR page asks `state` about three times a second while an ask's panel stands. The page's whole text is not read, so a card is answered while a Bot writes | `panel_page.GUARD_JS` (`grants`), `panel_page.ANSWER_ATTR`, `StreamSource.answerPlaces`, `takeAnswers`, `pressFate` (`answersAt`) |
| A press that the gate lets through on an ask's panel goes at once, and carries the places that the XR page last heard (the event's `grants`, a list of at most 40 places of four numbers, else `invalid_input_event`). For a `down` or a `click`, never an `up`, the hub asks the page's `state` right before it sends: when the places differ it sends nothing and answers `{sent: 0, moved: true, grants}`, and the XR page takes the new places, so the second starts there; a page that cannot say sends nothing either (`unread`). A press with no such word is sent as before. Left open: the few ms between the hub's two calls to the page | `PanelHub.input` (`_places`, `_answers_moved`), `pressPlaces`, `StreamSource.answersBy` |
| On every panel a click carries what he aimed at (`aimed`): the box of the control that the page said stands at the click's place, then its label when it has one (cut to 80 characters, `AIMED_LABEL_MAX`), as the move after the picture on the texture asked it (`StreamSource.aim`, `aimPlace`; the move after each new picture under a resting pointer), or `null` for none there. A press that comes while the move after a new picture is on its way to the same place carries the last answer's box for that place: that move only makes it newer. It is left out when the XR page has no answer for the click's place (a finger with no move before it, or a pointer that went to another place whose answer has not come). Only a `down` or a `click` is judged, never an `up`, a cancel or a move. The hub, which asks the page what stands at the point before every press anyway (`target`), sends nothing when another control stands there now, or the same one more than 4 px elsewhere (`STALE_PX`), or one came where he saw none, or it went, or (both having a label) one of another label stands in the same box: two buttons of one size that swapped places; a control with no label is judged by its box. It answers `{sent: 0, stale: true, target}`; the panel says so once in its line, and the next press is not held. A change anywhere else on the page (a caret, an answer that streams in) never voids a press. Its cost: no extra work a picture and no extra question at the press; about 25 bytes a click, and the label. Left open: a finger's tap with no move before it; the page changing between the picture on the texture and the move's answer (one round trip) | `PanelHub._stale`, `StreamSource.pointer` (`aimed`) |
| A tap on a Bot whose ask's panel cannot come up (a page in the way, or a full room) does what a tap on a Bot that asks nothing does: the Bot's own page opens, and its card is on it | `tapAnswer`, `useAskPanels` (`tapBot`) |
| When an ask set the flat preview's lens back to the start's, the lens goes back to where it was once the last ask's panel is gone and none is about to come up, unless the person changed the lens meanwhile (two fingers, the wheel beside a page, the home button) or kept the panel. No panel takes a press for 0.5 s after it went back, and none, nor a page or a Bot, for 0.5 s after an ask's coming up set it back: the pages are drawn at another size at once, and a press on its way would land beside its aim | `lensSetBack`, `lensGoesBack`, `lensMoved`, `useAskPanels` (`lensChanged`, `comeUp`) |
| When every card of that Bot is gone the panel says so for 3 s and closes, with no turn. A press, a key or the wheel on it in those seconds, or a key typed into it at any time, makes it the person's page: it stays and is kept with the room. A press in those seconds only keeps it and is not sent | `keepsPanel`, `pressFate` (`keep`) |
| Closed by its mark while the ask waits: every waiting ask of that Bot is put off. A later ask is new | `settle` |
| The keys stay with the page that has them when the panel comes up, and go to it only after a press on it | `typing.keysAfter` |
| An open that is refused, or a page that fails: the panel goes after its line, and nothing more comes up until the person opens a page | `useAskPanels` (`stopped`) |

**A Bot whose own thread was cut is told by its failed sheet, in every view** (D35, P3
slice 2): its cut's step (`workspace/fails.ts`, taken in `useAskPanels.ts`) brings
NO page by itself, with places on or with `places=0`; the Bot holds its failed sheet
up instead (THE FAILED SHEET, below), and its page opens when he asks for it. The
rules below decide when that step is taken; where they say a page comes up, the
failed sheet comes in its place. The rules:

| Rule | Where |
|---|---|
| Only the Bot's own thread, and only a cut that a whole read sees begin: the thread was not cut in the read before. A thread is cut when the slot says `interrupted` (no turn in flight and the last turn has no reply), also while a sub-agent of it still runs; each cut turn is told from the next by the slot's `turn_gen` and `turn` (`XrBot.cut`). The Bot's folded `failed` state brings nothing: it turns to failed also when a worker finishes over an old cut, or when he answers its ask. A worker's cut brings no page: its Resume is in the worker's own chat, which a panel cannot open. What a cut's rim shows, red or the dim mark, is the next table's | `readFails`, `model.buildXrCrew` (`cut`) |
| What is cut at the page's first whole read, or at the first whole read after the live connection came back (a gateway that restarted), keeps its rim only. A drop of the live connection closes no failure's page | `readFails` (`started`) |
| Many cuts at once bring one page at most. Cuts of two Bots or more in one read keep their rims only. A cut read alone brings its page at once. Another Bot's cut read while one cut's page waits, or within 5 s after the cut whose page came up, keeps its rim only; that first page stays | `readFails`, `failUp`, `FAILS.massMs` |
| His own Stop is no cut (`stop_event`) and brings nothing | `transcript_rows.is_turn_interrupted` |
| One page per Bot: while it asks, its cut brings none (the ask's panel is that page); when its page stands (his, or an ask's), that page's frame lights once. Asks come first; a failure's page waits while an ask waits or an ask's or failure's panel stands in view, and while he is busy, as an ask does. A full room brings none | `nextFail`, `useAskPanels` (`flash`) |
| At most one page for each cut turn. After he kept a failure's page or sent it away by its mark, its Bot brings none for 10 minutes | `failDone`, `failQuiet`, `FAILS.quietMs` |
| It is no ask's panel for the press gate: it keeps the hold before its first picture, the first second's hold and the hold after a close, then a press goes to the page (it has no answers' places), and any press on it makes it his page. It never closes as done; it closes by itself only when a whole read finds its thread no longer at that cut with no press of his, after the same 3 s. The same cut's key, after a short drop, keeps it. A cut of another gateway process (another `turn_gen`: the gateway restarted) closes it with "The gateway restarted. This page closes. A press keeps it."; else its turn ran again, and it closes with its own line. When its Bot asks on it, it is that Bot's ask's panel from then on | `pressFate` (`done: false`), `failGone`, `useAskPanels` (`mayPress`, `fail`, `gone`) |


**What a Bot's rim says of its cut sessions** (`workspace/rims.ts` holds the
rules, `Workspace.tsx` feeds them, `Crew.tsx` draws them; each rule has a unit
row in `rims.test.ts`). Each word of the rule is a fact that the XR page reads:

| Word | The fact | Where |
|---|---|---|
| A cut | A session of the Bot, its own thread or a worker of it (`mate_root`), says `interrupted`. The rim keys it by the session and its `last_turn_ts`, which a restart of the gateway keeps and a new cut of the session changes (`turn_gen` and `turn` change at a restart; round 14's cut page keeps its key with them) | `model.buildXrCrew` (`cuts`, `rimCutOf`) |
| Unread | The gateway's read state has the session's key in `dashboard.unreadSlots`. A Bot's own thread turns unread only when the Bot spoke (`turnCompletion.ts`), so its error or a restart leaves it read; a worker's error row makes it unread | `XrCut.unread` |
| It began while he is in | The session was in the last whole read, not cut or cut with another key, and is cut in this one, after the page's opening or its reconnect (the same fact as `readFails`, for the workers too). A session that enters the list already cut (a worker's chat reopened on his Mac, a Bot whose roster row comes late) did not begin | `readRims` (`started`, `sessions`, `begun`) |
| He pressed that Bot | A press in XR on its seat, its place in the row, or a panel that shows that Bot's page (`crewRow.isBotsPage`), Resume on round 14's page too. A panel press counts only when the press gate sends it to the page or it keeps the panel (`pressFate` is `send` or `keep`); a page that the tab draws counts its own press at once. It makes seen every cut of the Bot that was read then | `pressCounts`, `pressedCuts`, `Workspace.tsx` (`pressRim`, `rimPanelPress`, `mayPress`) |

| Rule | Where |
|---|---|
| RED (the broken ring in the row, the whole rim at a seat): a session is cut, the gateway does not call the cut old (`interrupted_stale`: its newest row is older than a week, `INTERRUPTED_BULK_RESUME_MAX_AGE_SECS`), he has not pressed that Bot since that cut, and the gateway calls the session unread or the page saw the cut begin. An old cut is only ever the dim mark | `rimOf`, `XrCut.stale` |
| DIM: any other cut shows a still arc, a sixth of the ring, at the side that faces the person, in the cut's red at 40 percent; the same in the row and at a seat. The body rests | `rimOf` (`dim`), `Crew.Ring` (`DIM_MARK`) |
| The order stays `needs`, `working`, red, idle; the dim mark shows beside any of them but red. A red cut under `needs` or `working` shows nothing until they end; `needs` stands over all, a red cut, a seen or old one and a worker that runs (`rims.test.ts`) | `rimOf` |
| The book and the presses live as long as the page: after a reload or a restart every own-thread cut is dim, and a worker's cut is red while the read state calls it unread. A new cut after a press is red again when it begins while he is in | `newRimBook` |

**The board: a Bot holds up what it asks for** (`workspace/board.ts` holds the rules, `useBoards.ts` holds the boards and
sends their answers, `Board.tsx` draws them; each rule has a unit row in
`board.test.ts`, the hook's book in `useBoards.test.ts`). In the room of pages, with and without `desk=1`:

| Rule | Where |
|---|---|
| Approvals only (a request with an id), one board for each ask (`need.id`), one board at a time for each Bot: its oldest approval by time, wherever its thread stands in the list. A held board of another ask of that Bot stands in for the next. An ask that a board answered, or is answering, gets no second board while it is still listed (a worker's approvals list is read again only every 30 s); once it has left the list it is forgotten, so the same id later is a new ask. The hook's `answered()` reads that book (Home's line waits for no board on these asks) | `boardAskOf`, `boardsNow`, `useBoards` (`answered`) |
| One board at a time for each Bot (D24): while a Bot's board stands (at the Bot, in his hand or on the floor), every other ask of that Bot and of its workers waits; no panel comes up for it. A small card tag hung on the board's metal clip says how many wait ("+1"; none when nothing waits). The tag means "this Bot has N more asks behind this board": its other asks and its words that wait there; an ask that came up on its own past a board he handed back is not behind it and is not counted. At the Bot the tag is drawn twice its size in his hand, its "+1" taller than the board's sentence, and stands out past the board's top edge so it covers no words. The Bot carries no sign beside its board. When the board ends (stamped and thrown, answered elsewhere) the next ask comes up: as the Bot's next board when it is an approval, on its panel as before when it is a question. Home counts every ask. A Home line of an approval that waits behind its Bot's board at the Bot grabs that ask's own board: the board at the Bot steps back (its ask waits again) and this one comes into his hand; behind a board in his hand, on the floor or stamped, the line does nothing and opens no page. A board that he handed back himself (D27: his "not now" for that one ask) stops holding its Bot's questions while it stands at the Bot: a question, or an ask flagged as waiting with no request yet (`attention`), comes up on its panel as before, and the tag no longer counts it; its approvals still wait behind it. A board that goes back by itself (a minute out of his view, or its ask changed on the floor) is not marked so. A board in his hand that is out of his view holds no sign: the Bot's sign shows | `asks.freeAsks`, `asks.heldBack`, `useAskPanels` (`boardBots`, `handedBack`), `board.ts` (`grabAsk`, `waitingBehind`, `handBack`, `handedBack`), `home.behindTakenBoard`, `boardPicture.drawTag`, `useTagPicture`, `useHomeLines` |
| In the row the Bot holds up a placard on a stick: 32 cm wide, its lower edge 3 cm over the head (the eyes stay clear) and 2 cm nearer to him than the body's middle, behind the body's front. The room's pages stand 30 cm behind the row, so none hides it; a page brought up for a Bot stands 1 cm before a Bot's front, so it hides the board by depth and takes the press there. While a nearer page hides a board (looked at four times a second along the eye's line to its middle), its Bot carries its sign again, so he still sees that it waits. Over the heads it covers no Bot's body. It is a signature clipboard (Ray's D18): a dark blue board with a lighter rim and some thickness, a black metal clip at its top middle, and opaque white paper under the clip, with the sentence large enough to read from the start view and the command small under it. At a seat it is 13 cm wide with the Bot's name only, large, its lower edge over the seat's press box. A finger's touch of the desk does not reach it there (a touch is a tip that comes down to the desk's top), so a touch on the pad stays the seat's and a pinch takes the board. The Bot carries no sign and the pad no blank board while its board is up. A Bot that is carried takes its board along (`Crew`'s `bodyAt` reads the carry) | `Board.tsx` (`AT_BOT`, `AT_SEAT`, `useBoardPicture`), `Crew` (`boarded`, `bodyAt`), `Workspace.tsx` (`platesShown`) |
| Only a Bot whose body is drawn holds up a board: at the desk a Bot past the last seat holds none, and its ask comes up on its ask's panel as before. A board at a Bot that stops being drawn (it lost its seat) ends with no line; a board in his hand or on the floor keeps its ask until he lets go, whether or not its Bot is drawn. While an ask is on a board, or a board is about to be held up for it (a drawn Bot's oldest approval is listed), that ask's panel does not come up by itself; another ask of the same Bot (a question) comes up as before and settles only itself (its Bot's asks on a board still wait), and its sign counts only the asks that no board holds in view. A tap on the Bot's body opens its page as before; a 500 ms press on the body lifts it, and the board stays at its Bot | `Workspace.tsx` (`boardAskers`, `platesShown`), `useAskPanels` (the step's view of `waiting`), `useBoards` (`asks`, `held`) |
| Two neighbours' boards never cover each other's words. Along the row from left to right, two boards whose Bots stand nearer than a board is wide (32 cm) are a pair: in the row both are drawn 20 cm wide, so none moves up over a page; at the desk the second stands 5.5 cm higher. A pair holds until its Bots stand 4 cm (2 cm at a seat) farther apart than that, so a stroll does not make a board jump | `Board.tsx` (`boardPairs`) |
| Take: a short press on the board at the Bot. It stands 75 cm in front of where he looked, 12 cm lower, facing him, fixed in the room, 50 cm wide, opaque and light as at the Bot. It shows "<who>: <action>" and under it the exact command (or the tool's name), all of it reachable, so every press of the stamp on the paper is the yes. The paper grows with the command until the picture is 0.9 as tall as it is wide (0.45 m); past that the command's type gets smaller, down to 20 px of the 1024 px picture, and past that the command is a window on the paper: a drag on its words scrolls it (the mouse in the preview; with the head's dot, the resting hand's travel up the board, 5 times), and so does the wheel, 3 rows a notch. The top line stays in view, a thin bar at the window's right shows where it stands, and while there is more below a fade and "More below: drag the words up" say so. A scroll sends nothing and is never a throw; one line under the window says to grab the board by its edge to throw it (`xr.board_throw_edge`). A scroll paints the picture at most once a frame, with the row it has then (`useBoardPicture`). The picture keeps that one size however long the command is: only the rows in the window are painted, and the command is wrapped once. The board held up at the Bot shows the command's first line only. No line on an ask's paper opens the Bot's page: that page is reached from the last sheet of a long answer (below) and from the palette | `takeBoard`, `boardText`, `boardPicture.ts` (`layBoard`, `useBoardPicture`, `HELD_CAP`, `CLIPBOARD`), `Board.tsx` (`HELD`, `SCROLL`, `PageLine`) |
| A Bot's words are sheets on its clipboard (TALK slice 1, D19, D26): a Bot holds up a board of its words by itself when a session of it at rest (its own thread, or a worker it handed work to; P3) is unread, the newest message there is the Bot's (never his own), and the Bot is not working: one session at a time, the one that ended first; a tap on a Bot that holds no board holds up its last words, and opens no panel and no page. At the Bot the board shows the first sentence of its newest answer. In his hand: that sentence large, then the words after it, 8 lines a sheet, 5 sheets an answer at most, and a heading never stands as a sheet's last line (it goes on to the next sheet with its words); a sentence that goes on to the next sheet ends this one's last line with an ellipsis; code blocks and tables are taken out and counted. The choices a Bot offers him (`[OPTIONS: …]`) stand as boxes on the sheet where the answer ends (its last sheet of words or its count sheet when they fit, else a sheet of their own); the stamp's print on one sends that choice to the Bot's own thread, with his note, through the card's send, and when the cap cuts an answer its last paragraph stands on the count sheet, above the counts. An answer with more, or with code or a table, ends on a sheet that counts what is left out ("1,700 more words, 1 table, 2 code blocks") and has the one mark that opens the Bot's real page. The foot says which sheet of how many; the paper's turned corner at the lower right shows the next sheet, after the newest answer the turn before, then the first again. No stamp appears on a words board: a throw sends nothing, leaves as particles and files it: the session is marked read up to the shown answer's own message, here only when nothing newer came since, and in the dashboard's other windows. A hand-back sends nothing and marks nothing read (D35): the Bot lowers its clipboard, holds that session up by itself again only once it changed, and a tap or Home's line raises it again. In his hand a words board keeps the sheets it was taken with; a newer answer comes as the next board, after he lets this one go. An approval of the Bot comes first: a words board at the Bot steps back for it, and the approval's tag counts the words that wait; a words board in his hand stays. A board he handed back stands aside for the Bot's words as a question comes past it (D27). A thread that could not be read shows a sheet that says so, never an empty one | `says.ts` (`answersOf`, `proseOf`, `sheetsOf`, `botSpokeLast`, `sessionToRaise`), `useSays.ts`, `board.ts` (`wordsBoard`, `takeWords`, `boardsNow`), `boardPicture.ts` (`laySheet`, `sheetWrap`), `useBoards.ts` (`corner`, `page`), `room.startRoom` |
| A question is a sheet with boxes; a reply is a note (TALK slice 2, D4, D17, D18, D14): a Bot's question that the gateway waits on (`ask_id`) is a board, after the Bot's approvals and before its words; no ask's panel comes for it, and Home's question line takes its board. In his hand: the question's header and words, then each choice a box drawn on the paper, 4 a sheet (more choices go on to more sheets, never smaller boxes; the corner turns them). He presses the stamp on a box: the stamp's print (the green "APPROVED" of D17, D44 `printWord`) stays where it was pressed and that box is his choice; the answer goes once every question has its one choice, by `api.answerQuestion`, keyed by each question's words, once. A press on no box sends nothing and says to press on a box. A question that takes several choices sends them, joined with ", ", when he lets the board go. Thrown with no print and no note it lies on the floor, and when its time ends it is dismissed: only that throw is his no. A board with a print is never dismissed (D4a): let go short of a whole answer, thrown or handed back, it goes back to its Bot with its prints and his note, and one line names the question that still needs a box (`questionLetGo`, `xr.question_open`); one on the floor goes back when its time ends. A pinch on the paper's foot of any board in his hand opens the line for his note: the card's speech listens, in the card's language, and the card's keyboard stands under the board (Enter closes the line); his words show in a pen's blue, a hand-written look, in the margin under the Bot's words: letters slanted and each a little off the line, and a word of a script whose letters join (Hindi, Bengali, Arabic) drawn whole, as one piece, and an emoji with its skin tone or a letter with a separate accent mark is one letter (`penPieces`). His note answers one question only: the one on the sheet he looks at when no print answers it, else the only question left that none answers when he has had its sheet in his hand; it never fills a question on a sheet he has not turned to (`noteFor`, `Board.looked`). When prints answer every question, the note goes with the answer of the question on his sheet, after its choice, and its line closes at that print. A send that failed keeps what he made: the board is at its Bot again with its prints and his note. Each answer carries the board's own `response_id`: the gateway replays his retry, and an answer or a dismissal that someone else gave first (409 `already_resolved`) says it was answered elsewhere and keeps his note on the paper. An answer to a wait that had already ended with no decision to name is no failure (`writes.endedAsk`): answered counts as answered, dismissed as the no, and any other end says it was answered elsewhere. A board in his hand whose ask has left the list keeps his note while he holds it; let go, nothing of it is sent, and the line says it was answered elsewhere. After a printed choice the line says what it sent ("Sent to Juno: Ship the notes", `xr.choice_sent`). A choice printed on a Bot's words takes no second print when those words are raised again, after a reload too: the print is kept in the browser by that answer's own key (`chosen.ts`); on any other board it goes to the Bot's own thread at the hand-back or the throw, through the card's send (`cardSend`); a hand-back with no note sends nothing. Every message sent from XR (the card's and a note) carries one added line that asks for a first answer in one short sentence (`writes.xrMessage`, `xr.first_short`). Past a board he handed back a question comes as its sheet (D27) | `question.ts`, `board.ts` (`boardAskOf`, `stampQuestion`), `boardPicture.ts` (`layQuestion`), `notePicture.ts`, `useComment.ts`, `useBoards.ts` (`stampBox`, `foot`, `note`), `writes.ts` (`boardAnswer`, `xrMessage`) |
| Speak to a Bot (P2, D33, D14, D18): a press on a Bot held still for 500 ms (`HOLD`: the click's own time; in the preview within 8 px of its down, in a session within the pinch's 1.5 cm) is a hold. The Bot leans in with its eyes on him (its `listen` pose; with reduced motion it only faces him) and holds up a blank sheet, "To <name>", sized for its distance so that it reads as a sheet in his hand does; his words are written on it in the pen's hand at a sheet's body size as he speaks (the note's own line, `useComment`), wrapping down the page, the sheet growing with them. The recognizer starts only 300 ms after the sheet, while the hold is still on (`HOLD.listenAfterMs`), so a drag never opens the microphone or, in a session, its permission prompt. One listener at a time: while a hold's sheet listens, a note line open on a board in his hand does not, nor does the new chat card's speech (`useChatCard.micAway`), and the words go to the hold; when the hold ends the card listens again. The card is told in the same step as the hold's line opens or goes (`useBoards`, `BoardHost.micAway`), so its recognizer stops before the hold's starts and starts again only after the hold's has stopped. Until his first word a faint line at its foot says to speak and to let go to send. His release sends the words that the sheet showed (what is still being heard too) to that Bot's own thread through the card's send (`cardSend`, with `xr.first_short`), once, and the Bot nods (only then: a hold that sent nothing gets no nod); its answer comes back as sheets on its clipboard. A shorter press is a tap and raises its words as before. A hold that moves past the same measure is today's drag (D10): the sheet goes down, nothing is sent, and the Bot goes with the hand from that move. A hold let go in silence sends nothing and says nothing. A hold is cut off when another press begins (a second hand, or a new hold, which ends the one still open), and at the window's own release when the Bot never saw it (let go off the canvas); a hold keeps its pointer in both modes, and a hold's sheet that no hold holds goes, so it never stands in the way of its Bot's next ask. A hold cut off (tracking lost), a drag with words, or a send that failed keeps the words, and the next hold on that Bot starts with them. A Bot that holds an ask's board up has it in the way (D24): no sheet comes, one line says to answer it first, and nothing is sent. A words board at the Bot stands aside while he speaks. No chat page and no chat card opens. With no speech recognizer the card's keyboard stands under the sheet; with one, no keyboard shows | `speak.ts` (`HOLD`, `speakEnd`), `Crew.tsx` (`onHold`), `board.ts` (`speakBoard`), `useBoards.ts` (`holdStart`, `holdEnd`), `boardPicture.ts` (`laySpeak`), `Board.tsx` (`SPEAK_AT_BOT`) |
| The sheet he speaks on is the nearest thing in his look: it is drawn after everything else with no depth test, so Home's strip, another Bot's board, a page or a nearer body never lies over it while he holds; the others stay where they are. It belongs to its Bot: it is placed once, when the hold begins, whole inside his view then (made smaller under the view's top, down to its least width; shifted in at a side), its lower edge where the Bot holds it up (the face under it free), facing him from the Bot's place as its clipboard does. It stays there for the hold: when he turns his head it stays with its Bot and may leave the view with it, and as his words make it taller it grows upward, never down over the Bot's face, so the newest lines are the ones nearest its Bot. While he holds a Bot the middle of his look is quiet from when the sheet comes up, as with a board in his hand | `speakFit`, `Board.tsx` `SPEAK_ORDER`, `PlacesRoom.boardHeld` |
| THE DONE SHEET (P3, D35): the words board of a session at rest is a done sheet; only a Bot's own thread raises one by itself and counts as "to read" (D39: a worker's result is what its Bot says of it, and a worker's failure still raises its failed sheet); every line from a field the gateway already sends (`XrRest`: `source_links`, `todo`, the answer's own time). At its Bot: a head ("Juno: done · 10:42") over the answer's first sentence, and a line for each change or issue it links (at most 2) in that one's state's colour. A words board at a Bot 1.8 m from him or farther (a desk of the room of places) holds ONE large line and nothing else, its Bot's name first (D42: "Mira · done · 8:07 PM", `deskWords`; the name on a line of its own over the rest when one line would make the words smaller): a done sheet's "done · 10:42" in the done colour (`--ok`), drawn whole (it shrinks until it fits, the size stepped down past the width ratio's), any other its first line, cut with an ellipsis below the least size, its board wider by its distance, but never wider to his eye than a near desk's line (D39), so the line reads at the letter size of the board of a Bot that came forward; it stands right over its Bot's head, is drawn in front of the other Bots (with no depth test, unless a nearer page hides it), and grows smaller toward its lower edge until it stays out of Home's band, the strip and the since line, but never below a floor where its words are as high to his eye as the strip's small words (`STRIP_LABEL_M`); when it cannot stand over the head at that size it stands beside its Bot at head height, on the side away from Home's band, on no other desk line and off the band, its near edge within 0.4 m of its Bot (a line carries no name); past that reach it stands under its Bot at its desk's height, still in front and, by the same clear step, on no other desk line (`deskFit.ts` `deskPlace`, `besideClear`, `besideOrUnder`, `underAt`) (`Board.tsx` `AT_DESK`, `boardPicture.layDesk`); the whole sheet is what he takes. In his hand, on the newest answer's first sheet: the head, the answer, the links ("PR #7 · open · CI passed"; a line that opens nothing) and at most two todo tasks left open ("Left: write the tests"); the rest of the answer goes on to the next sheets. A turn that only says "ok" (one short sentence, nothing linked or left open) is a plain words board. A done Bot does not come forward: it raises its sheet at its own desk; only an ask brings a Bot to him. A worker's sheet is at the Bot that handed it out. Home counts every unread session at rest ("2 to read", in the done colour, only while there is one) on its strip beside "done today", whole (the four numbers stand closer then, and a longer language's words take a smaller size), and over its large form's "Filed today": the five newest sessions at rest that ended today and that he has read (thrown here, or read on the dashboard), newest first, one line each (who, the answer's first line across the whole width between the name and the time, cut with an ellipsis only there, and the time); the done number stays the whole count. A press on one raises that sheet at its Bot again. No artifact chip: no field names an artifact a turn made (`slot.artifact` is the companion binding) | `model.ts` (`XrRest`, `restOf`), `says.ts` (`DoneFoot`, `onlyAck`, `sessionToRaise`, `toRead`), `useSays.ts` (`doneFoot`, `madeLine`, `keep`), `home.ts` (`doneLines`, `homeLayout`), `Home.tsx`, `boardPicture.ts` (`layDoneAtBot`, `laySheet`) |
| THE FAILED SHEET (P3 slice 2, D35): a Bot whose own thread or a worker of it was cut, and whose rim is red for that cut (`rims.rimOf` marks each cut `red`), holds up a failed sheet before its words (`says.cutToRaise`, the oldest red cut that he has not filed or handed back here, by the cut's own id); a Bot that works holds none. The paper has a red edge; its head says "Juno: stopped · 10:42"; its words are the error row's own (`says.errorWords`): when its text holds a `message`, that message large and its kind and code small under it ("Prompt error · -32603"), else the text as it is, at most three lines with an ellipsis ("The turn stopped before it ended." when the turn left none); no word is rewritten, then "Was: <its first open todo task>", and ONE box, "Try again"; the stamp's print on it reads "TRY AGAIN" (D42: the print's word is what was stamped; an approval's reads "APPROVED"). At a desk far off it holds one large line, "stopped · 10:42", in red. The stamp's print on the box picks that cut turn up again, once (`writes.cutRetry` → `api.continueSlot`, a named write of `useBoards.ts` in the fence); the print is kept by the cut's id, so a second print takes nothing; a reply that does not come sends nothing more and one line says to look at the Bot's page first. Thrown it is filed like a done sheet (read, in pieces), and the line of what changed since he left counts that failure no more (`useSays`'s `filedFailed`, `useSince.tapped`); handed back it is put aside; either way it comes again only for a new cut, and a tap on the Bot raises it. Home's "failed" count follows the rim, as before | `model.ts` (`XrCut.doing`, `red`), `rims.ts` (`rimOf`), `says.ts` (`cutToRaise`, `errorWords`, `capLines`, `DoneFoot.failed`), `useSays.ts` (`failedSheet`, `errorOf`, `putCut`), `useBoards.ts` (`stampBox`), `writes.ts` (`cutRetry`), `boardPicture.ts` (`drawFailedEdge`, `layDesk`) |
| A board that leaves without a throw is lowered: it goes down below where it stood and fades (`BoardLower`, 420 ms; with reduced motion it is gone at once). Particles are for a throw only (D4a). A words board he hands back goes onto its Bot's clipboard first and is lowered there 400 ms later, still unread | `BoardLower.tsx`, `Board.tsx` (`boardLeaves`), `useBoards.release`, `BOARD.lowerAfterMs` |
| Bound to the ask's `need.id` and `version` when taken. A stamp on a board whose ask changed is refused: the board shows the new text and takes no stamp for 1000 ms. An ask answered elsewhere, or withdrawn, ends a held board with one line, and nothing is sent. A press of the stamp in the first 1000 ms in his hand sends nothing: the stamp goes back, and one short line says "Read it first, then stamp." | `stampBoard` (`changed`, `gone`, `wait`), `BOARD.calmMs`, `useBoards` (`xr.board_wait`) |
| Stamp (Ray's D17): a real stamp, about 8 cm tall, a honey wood handle and red knob on a steel block, with no word, and a green rubber face as large as the print it leaves (a wide APPROVED stamp, 13.7 by 4.7 cm on the held board), stands at the right of a held board's middle, off the paper, however tall the board is. A pinch or a mouse press grabs it (only its own down: a drag or a held ray that began elsewhere and ends over the paper presses nothing); carried, it follows the press's ray on the board's plane, 3 cm off the paper, leaning so its handle and side show. A hand that rests (the head's dot holds the press) moves it 5 times its own travel along the board. Let go with its face touching the paper, it presses there, moved in just enough that the whole print lies on the paper at its most turn of 0.22 rad: the answer is sent then, once, and a green "APPROVED" print stays where it was pressed, turned 0.08 to 0.22 rad and uneven. Let go with its face off the paper, nothing is sent and the stamp goes back to its place. A press that ends as a cancel presses only when it is his own release of a long pinch (`pinchRoute.ending === 'long'`, as an ask's card reads it); a hand that tracking lost, or any other cancel, puts the stamp back and sends nothing. A stamped board takes no second press. The press sends `decideApproval` approve once for `targetOf(need)` with one `responseId`, which a retry of the stamp after a failed send reuses, so a retry is never a second answer; a send that failed takes its print away, and a print goes with its board. On in the default build: it is the same single decision as the page's Allow once; every other write of the XR page still waits on `VITE_XR_WRITES`. After the stamp the board stays in his hand. The stamp's lit programs are built while the boards stand, and again when a session starts, so the first take does not stall | `BoardStamp.tsx` (`STAMP`, `pressRay`, `useStampWarm`), `boardPicture.ts` (`PRINT_FACE`, `printPlace`, `printTilt`), `useBoards` (`stamp`, `send`, `prints`), `writes.boardDecide` |
| What was decided is what the gateway says: `boardDecide` returns the verdict that a 200's `decision` (a replay) or a 409 `already_resolved`'s names, else the answer it sent. A stamp that the gateway names a no ends the board; a no that it names a yes says so ("<Bot>'s ask was already allowed"), never "Not allowed". A held board whose ask leaves the list after a stamp whose reply was lost says only that the ask was answered. A stamp whose reply did not come says "The answer may not have gone. Stamp again."; a no that did not go says so, and its Bot holds the board up again | `writes.boardDecide`, `useBoards` (`stamp`, the floor's timer, `xr.board_answered`) |
| Throw: a press on the board's body moved far and still fast at the release. In the preview: 140 px or more, 0.6 px a ms over the last 70 ms of its movement, and its last move at most 60 ms before the release (events' own times), so a drag that slows or stops to place the board is no throw. In a session: the hand moved 9 cm and 0.8 m/s over its last 100 ms. A release whose ray passes through its own Bot's body (within half its width of the line from its feet to its top) is a hand-back whatever the distance; any other move short of a throw is a hand-back too: the board goes back to its Bot and nothing is sent. A release with no move (under 6 px, or 1.5 cm of the hand) does nothing: the board stays in his hand. Thrown after its stamp it comes apart into pieces (the next row). Thrown with no stamp it lies on the floor 45 cm under where it stood for 4 s, and a press picks it up again with nothing sent; when the time ends the no goes once (reject once) with an id of its own, never the stamp's: the gateway replays a response by its id alone. An ask that changed while its board lay there gets no no: the board goes back to its Bot | `releaseKind`, `previewMoved`, `handSpeed`, `throwBoard`, `pickUp`, `floorEnds` (`noId`) |
| A board that leaves comes apart into pieces in its own colours (Ray's D4a, D21). Thrown after its stamp: 3200 soft round pieces, each a core of its colour in a faint glow, mostly fine and a few larger. The board's colours carry the cloud: a place on the bare paper is mostly passed over for another, so the blue backing, the clip, the words and the print give most of the pieces; the words' and the clip's near-black ink is drawn pale, so the words read as light. For the first moment the board is still seen whole; from the edge that leads the throw (up to 0.3 s later at the far edge) the pieces keep the throw's way, slow as in air, spread, and each fades on its own clock in 0.9 to 1.3 s, the paper's pieces first, so the cloud thins out. A board that ends by itself (answered elsewhere, the no from the floor) or that he hands back is lowered instead, never in pieces (`BoardLower`, below). One points draw call, moved on the GPU; its shader is built while the boards stand and again when a session starts, so a leaving does not stall. Each piece also grows 55 % smaller toward its end. With reduced motion the pieces neither move nor shrink: they only fade in place, in 0.6 s. The probe's `__xrPages.dustAt(s)` draws every leaving at a set time of its own | `BoardDust.tsx` (`DUST`, `useDustWarm`, `dustClock`), `boardDustShader.ts`, `Board.tsx` (`Boards`, `leaving`) |
| A held board out of his view for a minute goes back to its Bot (a hand-back) | `awayTick`, `BOARD.awayMs` |

With `?probe=1` the probe reads `boards` (each board and every decision it sent,
with each release and what it was read as) and `boardMarks` (where each board,
its stamp, its paper and its page line stand, in the room and on the screen), and
`__xrPages.boardStale(id)` binds a held board to an older version of its ask.

**The 3D dashboard: a small solid thing of a Bot's own whose numbers move**
(`workspace/dash3d.ts` holds the layout's rules and the object's sizes,
`useDash3d.ts` reads it, `dashKit.tsx` draws the parts, `Dash3d.tsx` stands them;
each rule has a unit row in `dash3d.test.ts` or `useDash3d.test.ts`). The gateway
has no route, tool or field for it: a Bot writes a small document, the layout,
into a field of its own Dashboard named `xr_layout` (an `object` field it declares
itself), with `dashboard_write`, and the room reads it with the Dashboard tab's
own reads. What a Bot is told of it is section 8 of the `dashboard` skill, which
`dashBrief.ts` makes from these rules (the field, the kinds, each limit and unit,
the lamp states, the why lines); `dashBrief.test.ts` fails when the skill's text
and `dashBrief()` differ, and holds each limit the text names against
`readLayout`.

| Rule | Where |
|---|---|
| Who has one: a Bot whose APPLIED Dashboard page of its own declares `xr_layout`, seen by its owner. The Bot's steps are `dashboard_save` (the model names the field), `dashboard_apply`, then `dashboard_write`. A Bot on a shipped page has none; a clear (an empty record) takes it away | `layoutOf`, `useDash3d` |
| What the Bot is told: the write's reply to a `dashboard_write` of `xr_layout` carries `xr_room`, one line in fixed words of what the room makes of the STORED (scrubbed) value: each part by its place and its kind, or the room's own line for what is wrong, "name not checked" for a name that holds a sign the gateway's Unicode table does not know, the grey tray's reason, or "no 3D dashboard" for a clear. Each good part also says what it shows now, from the page's own values with this write's stored value laid over them: the number it reads ("ring 12/20", "bar 12.5 $ of 40, over its limit", "lamps 4 (1 idle, 2 done, 1 grey)"), or a dash and why ("a dash: its field holds no value", "its path finds nothing", "not a list", "not a number", "its limit is no number above 0"). A number is written plainly (whole, or two places, a half away from 0; one of more than 12 digits is only named), not short as the room writes it. A write of any OTHER field carries the same words for the parts that read that field, as their own or as their limit's ("In the XR room: part 1: ring 13/20."), and nothing when no part reads it or the page holds no layout. The values are read only for a page that declares `xr_layout`; when they cannot be read the layout's line names each part's kind only and a number's write says nothing. The whole line is made in one worker hop (`_xr_line`): the read of the values and the reading of the layout and of each value, none of it on the gateway's event loop. It repeats none of the Bot's text: not a name, not a field's name, not a text a field holds. A wrong layout and a wrong value are stored all the same (D99). A raise in it costs the line, never the stored write. The Bot's `dashboard_write` tool adds the line to its sentence | `dashboard_xr_layout.verdict`, `field_verdict`, `part_says`; `partSays` in `dash3d.ts`; `agent_panel_dashboard.api_dashboard_write`, `mcp_panel` |
| A layout is data. `{ "v": 1, "parts": [...] }`, at most 6 parts, each `{ kind, name, field, path?, max?, key?, unit? }`. `kind` is one of `number`, `bar`, `ring`, `lamps`. Nothing in it is run, fetched or drawn as markup; every size and colour is the page's; a key the reader does not know is not read | `readLayout`, `DASH`, `DASH_KINDS` |
| A field, a path key, a lamp row's `key` and a `max` that names a field are looked up as the value's own keys only (`Object.hasOwn`). A path's key and a lamp's key have at most 64 characters; a `key` is read for lamps only and breaks no other kind. A value that is missing, or that its kind does not take, shows a dash and no line in the room. The cases both readers are held to are `test/fixtures/xr_layout_cases.json`: `dash3d.test.ts` reads its `cases` against `readLayout` and `dashSays.test.ts` its `value_cases` against `partSays`; `test/test_dashboard_xr_layout.py` reads both against `dashboard_xr_layout.read_layout` and `part_says`, the pure Python reader that the write's reply is made with. A text is a plain number by ONE rule in both readers (`plainNumber` in `glance.ts`, `_plain_number` in `dashboard_xr_layout.py`): spaces before and after, one percent sign after it with spaces round it, then digits with groups of a point or a comma and digits. The spaces are one written-out list of 25 signs (`PLAIN_SPACES` in both, held to `plain_number_spaces` of the cases file), never `\s`, on which the two languages differ; they are cut off from each end once, so a read takes the text's length in time whatever it holds. `plain_number_cases` of the cases file holds both readers to the same answers. A `name` has at most 16 letters, no control or format character, at most two marks on a letter, and is drawn with `fillText` and its `maxWidth`. A `unit` is one of `%`, `$`, `ms`, `/s`, or it is dropped. A number is written compact, in at most 6 characters | `readPart`, `clean`, `DASH_UNITS`, `shortNumber`, `DashLabel` |
| It is an object, not a screen: solid parts with light on them, in a row on one tray, with no backing panel, so it shows its depth when the head moves. A ring on a post, open in the middle, that fills; a glass column with a level; bulbs on two rails; a block | `dashKit.tsx`, `FORM` |
| No part is drawn in a colour that the room uses for a Bot's state (its ring, its rim): the parts take three hues of their own, each at least 35 degrees of hue from every state colour of the theme. `needs` is not a lamp's state: it is grey, like any word outside `idle`, `working`, `done`, `failed`. That a Bot needs the person, or failed, is the room's to say at the Bot's feet | `dataHues`, `LAMP_STATES`, `lampLook` |
| At rest its Bot holds it: the tray made small, with no lip and no words, the parts standing close (each as wide as its kind), in front of the Bot's own body and 4.5 cm over its feet. It is as wide as the Bot's body at most (21 cm), so it is in front of no neighbour; it ends under the Bot's eyes; and from the room's middle the line from the eye to the near edge of the ring at the Bot's feet passes under the tray, so the ring, which says the Bot's state, is seen. It goes where the Bot's feet go. A press on the Bot's lower body, where the tray is, takes the tray; a press on its face opens its page. A tray that a nearer Bot's body hides at his eye (the line from the eye to it passes through that body) stands over its own Bot's head instead, where it is seen and pressed (`hiddenBy`, D99 2). A tray with no part on it takes a press no higher than its own sockets: a press in the air over it goes to the Bot's body behind it, which is what shows there. A Bot at a seat of the desk has none drawn yet | `restRow`, `restBox`, `OneDash` |
| Taken (a press on it): it stands 0.8 m ahead of where he looked, fixed in the room, placed for his head as it is at the take (up or down too: a tray at rest is taken looking 17 to 21 degrees down), whole in the lens then and while the head turns 9 degrees up or down from there: its lip's lower edge 2 degrees over the foot of a lens 9 degrees shorter at each end (the headset's own lens in a session, the page's camera in the flat preview), as low as that lets it, a thing set on a table (`takenTrayTopY`, D99 2, D103 2). Then `fitTaken` reads the eight corners of its own drawn box, turned as it stands (M71), and for one to four looks stands it farther when it is taller than that lens, or wider than the lens's width less 2 degrees at each side (a tray of six parts), else moves it up or down; so it stands at the same distance whichever way he faces. When his head later rests elsewhere and a drawn piece of it is out of the lens, it is placed again for his head there and glides there, by the held board's rule and numbers (`Board.tsx` `HELD_COME`: his look turned less than 2 degrees for 500 ms after the take, a glide of 300 ms eased at both ends). It never moves while it is whole, while a pinch is down, while a pointer lies on it or while his head moves; a glide waits then too (`takenComes`, `glideGoes`). It stands in front of the Bots behind it: seen from the room's middle its parts cover their eyes and its tray their feet, between the parts they are seen. The parts slide a pitch apart and a sloped lip folds out at the tray's front. Every word is on the lip, none in the air: under each part its number and under that its name, one rule for every kind, and at the lip's lower edge whose it is (`xr.dash3d_header`). A press sends it back. One is taken at a time. The whole thing is one press; no part is a control | `TAKEN`, `TAKEN_DISTANCE_M`, `TAKEN_LOOK_DEG`, `takenTrayTopY`, `WORDS`, `Dash3d.press` |
| A press that moves is no press. A pinch that was held long or that moved (`pinchRoute.ending` is `long` or `drag`) takes nothing and sends nothing back. In the flat preview a press that moves more than `TURN_DRAG_PX` becomes the drag that turns the picture, from where it came down, as on a Bot | `OneDash` (`drag`), `EmptyHand` |
| What never moves of it (the tray, the feet, posts and rails of its parts, the block) is ONE merged shape while it rests, coloured by its points, made again only when a piece changes; the rim that a pointer lights is drawn alone, so a pointer that comes and goes makes no shape again; the bulbs of one part are one instanced draw. So a resting dashboard of a ring, a bar, lamps and a block is 5 to 6 draws. Taken, or on its way, its pieces are drawn one by one, since they slide | `partPieces`, `trayPieces`, `mergePieces`, `LampsMoving` |
| A document that is no layout is an empty grey tray; taken, it says why in one short line (`xr.dash3d_why_*`). One wrong part is an empty socket with its line on the lip, and the others stand | `DashWhy`, `Dash3d.tsx` `WHY` |
| Only what stands where the taken form goes refuses a take: a board in his hand, or the new chat card's keyboard (0.6 degrees from the taken form). Either sends a taken dashboard back; while one is there no tray lights, and a press on one says "Answer the board first." or "Put the keyboard away first." A board that a Bot holds up does not refuse a take; when one comes up anew and its drawn picture overlaps the taken dashboard's inside the view, as the eye sees them in its first 1.5 s at its Bot, it sends the taken dashboard back, since the form would stand in front of that placard; a board at a Bot far to the side leaves it. A board that is out of view in those 1.5 s, or settles later, never sends it back, even when he then turns and the tray stands in front of it; the picture stays true, the tray is in front. A board that lies on the floor does not count. Its press is what is drawn of it and nothing round it (D55 5): a pointer on a drawn piece takes it, and nothing behind that piece gets a press or a move; where the Bot behind it shows, the press is the Bot's. At rest a gap between two of its parts narrower than half a degree at his eye is its own (D56 3: he reads the row of parts as one thing); wider, it is the Bot's. A dashboard that is not shown takes no press. Its group carries `userData.xrPress` | `Dash3d` (`block`, `boardsUp`), `dash3d.ts` (`trayBlock`, `trayPress`, `boardsCameUp`, `picturesOverlap`, `drawnUnder`, `restGaps`, `gapTakes`) |
| Read 1, which page is in force: the Bot's Dashboard read (`GET /api/members/{slug}/dashboard`), kept for its package and its fields' names only, under the room's own key (`dashPageKey`), which the dashboard's frames of a Bot's values and work do not refresh. It arms the Bot's live page and makes the gateway put the Bot's whole page together, so it is made once for a Bot, and again only on: the frame that says the page changed (`dashboard_instance_changed`, handed on by `subscribeDashboardPage`), a 409 of read 2, a clock, the socket coming back. The frame can be lost (the socket was down when the Bot applied its first page), so nothing rests on it alone: when the socket comes back (its first connect after the page loads is no return) every Bot's page is read again, and such rounds start at least 30 s apart; a Bot with a package is read again every 60 s; a Bot with none every 10 minutes, so a first page is seen by then at the latest. The land keeps its own read (`useXrData.ts`) | `dashPageQuery`, `pagesToReadAgain`, `socketReturned`, `useDash3d` |
| Read 2, the values, the layout among them: `POST /api/members/{slug}/dashboard/refetch`, only for a Bot whose page declares the field, only through `writes.ts` `dashRead` (a POST that changes nothing; `safety.test.ts` names it as the one call of `useDash3d.ts`). A field frame of the Bot (`dashboard_fields`) only asks for it again. It is paced for each Bot: one out at a time, a start at most every 2 s, one more after a burst ends. A 409 (`no_live_page`) makes read 1 again, once; values that could not be read again are drawn grey, never kept as if new | `Paced`, `readDash`, `DASH_READ` |

With `?probe=1` the probe reads `dash3d` (each dashboard: at rest or taken, why
it is not drawn, where it stands in the room and in the picture, how large it is,
what each part shows and the words written on it), and `window.__xrDash3d` reads
how each Bot's values were read and hands a frame to the page's own frame
handlers. With `?fixture=1` three sample dashboards stand (one whole, one with a
wrong part, one that is no layout) and nothing of a Bot is read.

`/xr?view=dash&slug=<slug>&camera=<name>` (`DashView.tsx`, `dashView.ts`) stands
one Bot's dashboard alone in an empty room: no crew, Home, page or board, the
Bot's body not drawn, the Aura's 40.7 degree lens. `camera` is one of a fixed
list (`rest`, `taken`, `taken-aside`: at rest from his eye; taken, straight;
taken, 30 degrees aside at the same distance); any other word is the first.
`slug` must be a Bot slug (`[a-z0-9][a-z0-9-]{0,63}`), else nothing is shown and
nothing is asked. The values come by the room's own two reads (`useDash3d`), so a
made-up slug finds no page and shows an empty room; with `fixture=1`, the
samples. With `?probe=1`, `window.__xrDashView` reads the camera, the slug and the
same marks as the room's probe.

What rides with a Bot in the workspace (its ring, its pad, its sign, its press
box, and its place in what the probe reports) stands where its body is, at each
frame (`Crew.tsx`, `Member.parts`). A Bot that a pointer lights, or that a press
is on, stands still at once with no glide (`BotDrive.halt`), and walks back to
its place once nothing holds it.

A press held still on a Bot for 500 ms (`PINCH.clickMs`) lifts it, 3 cm, and
the press carries it until it is let go: in the row along its arc by the ray's
yaw, at the row's height, inside the band of a crew of twelve (`CREW_BAND`);
at the desk over the desk's top. A short press is a tap as before, and in the
flat preview a quick move on a Bot still turns the picture. Let go in the row,
it gets that place, or the free place nearest it when that is nearer than one
step to another Bot (`crewRow.freeYaw`); nobody else moves: the record then
keeps the place that every Bot of that row has, so none of them steps into the
gap that it left. With no free place
in the band it goes back to its own, and nothing is kept. In a session a press
is still only while its hand moved less than `PINCH.stillM` (`pinchRoute.travel`):
a hand that moved before 500 ms lifts nothing, and its pinch stays a tap. The places are kept
in the room's record as an optional `crew` field, slug to yaw, which a reader
of version 1 that does not know it ignores (`room.readCrew`); a new Bot takes
the first free place of the row (`crewHomes`), and when the kept places leave
none free for every Bot, the row stands in its own order. At the desk it sits on the
nearest seat within 1.5 pads and the Bot there takes its seat
(`desk.swapSeat`, kept in `seats`); let go farther, it goes back to its own. A
Bot of the second row is not lifted. In a session the dot's pinch carries it:
a still pinch ends as a cancel there (`hands.tsx`), which puts it down.

**The room with no row (`places.ts`, `placeLife.ts`).** With no desk, the room
opens with no row: each Bot has a place of its own in the room, in depth and at
its own height, with a small desk beside it (`placesWanted`). `places=0` in the
address gives the row above back, unchanged; a desk (`desk=1`) seats the crew
as before.

| Rule | Where |
|---|---|
| The first seven Bots take places made by hand (`SPOTS`), each whole in a level view with its desk and its name sign and 2 degrees of air, the nearest left and right of the middle; a larger crew stands in a ring to the sides, whole outside that view. A place that a drag gave a Bot is kept as a point (`places` in the room's record, beside `crew`); a yaw that the row kept becomes the free place nearest it, and a Bot whose own place such a yaw took has the first free place, so a room that only the row kept opens with every one of seven Bots inside the view | `spotOf`, `placeHomes`, `room.readPlaces` |
| Where a Bot is says what it does: one that works is at its desk, turned to it, the desk's top lit in its colour, leaning in a beat of its own; an idle one stands beside its dark desk, turned a little away; a failed one is slumped at its desk; the one that needs him comes to the low middle of where he looked when it began to ask, with its board | `stage` |
| In the room of places no Bot's chat page comes up by itself (D19), for an ask or for a cut thread. An ask is on its Bot's board. A Bot that hands work out stays at its desk, lit as working, and Home counts it. A Bot whose thread was cut slumps at its desk, Home counts it under "failed", and a tap on it raises its last words on its clipboard; its page opens only when he asks for it (the palette, Home's line, a board's corner). With `places=0` a cut Bot's page and an ask's panel come up as before | `useAskPanels` `AskHost.noPages` |
| Only one Bot stands in front: it stays until its own ask ends, then the Bot whose oldest ask is oldest comes. The others that ask stay at their desks. While the states cannot be known nothing is chosen anew and the one in front keeps its spot. The look it came to is kept in the room's record with the ask (`front`) | `frontOf`, `frontNow`, `room.readFront` |
| A Bot whose board he handed back ("not now", D27) goes home to its desk with its board up, and the next Bot that asks comes forward. Only that ask holds it there, until the ask ends, also when he takes its board again: with a question besides it (a question comes past a handed-back board) it comes forward as any asker, and its other approvals wait behind the board. The ask is kept in the room's record (`home`), so over a reload the Bot stays home and its board comes up handed back | `staysHome`, `heldHome`, `room.readHome`, `board.markBack` |
| No ask's page comes up by itself in this room: the Bot comes with its board (an approval's board, a question's sheet), and its page opens when he asks for it. A page he has open is never covered: a Bot that it would cover stands beside it, whole in view or whole outside it, and its desk is not drawn; where the one in front finds no spot clear of the page, it stays at its desk and its ask's sign, with the count of all its asks, stands at the page's edge | `boardAskers` in `Workspace.tsx`, `besideCovers`, `pageSignCount` |
| Home stands as its strip, so the Bots have the middle; a press on the strip opens its large form and a press on that puts it back. It opens only while its pages are put away and nobody came forward: only then a small chevron stands at its left end and the strip lights under a pointer; otherwise a press on the strip does nothing (D50). With a page open the strip stands above the page | `homeShown`, `homeOpens`, `homeLayout` (`above`), `Home.tsx` (`opens`) |
| A Bot walks between spots, a second for a metre, eased, around other bodies and never across a page he has open; where no way is free it changes spot by shrinking away and growing there. A page that opens or is resized during a walk is checked for again. The Bots that give way to the one in front set out before it. Never more than two walk at a time, except the one in front and those that give way | `walkWay`, `crossesPage`, `LIFE` |
| An idle Bot's life has beats of its own: a look around, a stretch, a nap, now and then a few steps to a neighbour and back. A visit ends at once when its spot comes under the one in front, its board, a page or Home grown. While the middle of his look is on a page, or he holds a board, nothing new moves within 10 degrees of that middle, except the walk of the one in front | `idleAct`, `visitSpot`, `visitHolds`, `readsPage`, `quietFor` |
| A Bot that he put down stands at the point where he let go, its desk beside it, until its state changes; the mark is kept in the room's record (`put`). A drag keeps the Bot's distance from him, between 0.9 and 3.2 m | `keptPoint`, `room.readPut` |
| A desk is drawn only once its Bot's body is on screen and stands at it. All desks are one draw | `PlaceDesks`, `BotDrive.drawn` |
| With reduced motion nothing walks and no idle act runs: a Bot changes spot at once | `still` in `Crew.tsx` |
| The flat preview's start picture is level round the eye line in this room, not shifted down to a row | `startShift` in `Workspace.tsx` |

A Bot in the row moves a little of its own (`crewRow.strollGoal`, worked out
again only when the pick, the state, its place or a stroll's end changes). At
rest the land's `idleLife` picks every 9 to 15 s: a look around is a stroll at
5 cm a second up to 9 cm along the row and 3 cm nearer or farther, inside the
row's band. Along the row it takes at most half the air between its body and
the body of its neighbour on that side, where that one stands or walks to, less
4 mm, so two bodies and their press boxes never meet. It goes to the side it
wanted, or to the other one when only that one is free, and at least 3 cm, so
it is seen. With no side that free (a full row) the look around shows in
place: its body turns about 40 degrees to one side and back over 3.6 s, and now
and then it leans (the `think` pose) or hops once on the way. It walks back at
the next pick. A nap is a nap at its place. While it asks it takes one step of 10 cm
toward him and goes back when the ask is over; a failed one slumps, as before.
A pointer that lights it, a press, a lift, reduced motion, the quiet time
(`hushed`) and a state that cannot be known end a stroll; a stroll that a
pointer ended does not start again until the next pick. At the desk a Bot does
none of this: its pad says the state. With `?probe=1`, `__xrPages.stroll(true)`
makes every Bot at rest stroll a new way every 3 s, and `stroll(false)` none.

A Bot that asks carries a sign at its head's upper side, its name and how many
asks wait, with no pointer on it (`Crew.tsx`, `Sign`), except while it holds up a
board (above), which says what the sign says; a press on the sign is a
tap on the Bot, which brings its ask's panel up. Until the panel's first
picture it shows the Bot's face, name and count. The least width of every page
is 320 CSS px (`layout.ts`, `minCssWidth`; the hub's own least is 320 by 240),
so a kept ask's panel is read back at its width. A press on a panel's edge
that does not move places nothing (`Panel.tsx`, `EDGE_DRAG_M`). A corner's handle takes no part
of the page: it is an L round the corner, as big as the square that it was
outside the page (15/16 of the corner's square), so a press 4 px inside a
corner reaches the page on both paths. The close
mark of the page in front shows at rest, dim, in the rim's faint colour
(`Panel.tsx`, `markAtRest`): in the flat preview the page whose middle is in
the picture and nearest the picture's middle (`previewLook.frontAim`, looked
for every 200 ms), in a session the page that the one light lies on, its
frame and mark included (`pinchRoute.lit`, the panel group's `xrPanel`), and
none while the light is on nothing. A mark shown at rest closes on the first
tap, but one that came by itself (the picture turned, another page became the
one in front) takes no tap in its first second, counted from the later of its
coming and the pages standing still again (`closeMark.closesAtRest`). The lit
mark is white only while a press on it closes (`closeMark.pressCloses`); in
that first second it is drawn faint and not lit (D50, F2). The colour is set
in the panel's frame from the pointer on the mark itself, and a press closes
only a mark that was drawn white in the frame before it. At rest with no
pointer on it the mark is faint too, and a touch tap closes it after its
second. In a
session the quiet time after a close, and after a page that the gateway refused
goes by itself, is the pinches' own (`pinchRoute.hush`, `TAP_AGAIN_MS`): a pinch
in it presses nothing, and after a close each such pinch starts it anew, as a
touch does in the flat preview, so a pinch right after a close never reaches
the page that stood behind the mark. While it runs nothing is lit, by the dot or by a
raised hand's ray (`pinchRoute.light`), and the light comes back by itself
when it ends. In that second no tap counts however the mark is lit and whatever the
pointer is: a mouse that rests where the mark comes lights it as it comes,
and its click closes nothing (`calmAfterComing`; a mark lit more than 400 ms
before its page became the one in front did not come by itself). A mark
lit by the pointer keeps its own rule (150 ms; `closes`). A turn of the picture to a page sets the frame's shift so that the page's upper edge and its close mark are in the picture with the lens as it is, and at the start's lens too, where the whole shift is in force again once the eye steps back (`shiftAfterTurn`). The mouse wheel off
the pages brings the eye nearer, or back, about the picture's middle: the head
does not turn to the pointer (`EmptySpace.tsx`, `EmptySpace`'s `zoom`); the
new lens is held with the frame's shift that the new nearness puts in force,
so a notch in and the same notch back leave the picture where it stood. Such a
notch latches the wheel: the next notch within 8 CSS px of its place and
within 1.5 s goes to the nearness too, also when the notch before brought a
page under the pointer, and a page drawn by the tab does not take the mouse
meanwhile, so the browser hands that notch to the room (`previewLook.ts`,
`wheelLatched`; `Panel.tsx`, `wheelLatched`). Two fingers on a phone are as
before. Accepted: the
Bot's thread is marked read when its panel comes up, as when a person opens
that Bot's page.

**The Home surface** (`workspace/home.ts` the rules, `useHome.ts` the state,
`Home.tsx` the plate). The room's main panel: four numbers, large, and no long
text. They are counted from the crew that the room has already (`model.ts`), and
change as it does: the Bots whose state is working; every ask that waits for the
person; the Bots whose state is failed as the rims draw it (the Bots whose rim
is red, so the number agrees with the row); and what was done today, which
is each thing of a Bot that is at rest and was last active on the local day,
and a Bot's own thread when it is at rest and was the last of that Bot to be
active today (`homeStats`). A number that is nought is dim, and while the
states cannot be known every number is a dash. Under the numbers stands one line
for each of the oldest three things that wait, who asks and three words of the
ask, and one line that counts the rest (`homeLines`). It stands straight ahead,
1.75 m away, upright also with a desk. While no page shows it is large and
level with the eyes (1.24 m wide, a digit 5 degrees tall); while a page shows,
that page has the place straight ahead and the surface is one strip in front of
the uppermost rows of the first page's place: its upper edge on the line of
sight to the page's upper edge, 5 cm nearer than the page, from 14.6 to 17.4
degrees up, inside what a pair of glasses shows of a level look and inside the
flat preview's start picture. Those rows are the dashboard's own top bar; the
band at the page's lower edge is where a Bot holds its board up and where a chat
page has its message box (`homeForm`, `homeLayout`). It travels from one form to the
other in 320 ms, at once with reduced motion. Its switch puts the real pages
away and brings them back (also a button of the preview's bar). A page that is
put away stays open: it keeps its tab at the gateway, its place and its size,
and the record keeps it; its panel is not drawn and its source is told that
nobody sees it. The switch puts away the pages that stand at that moment; a page
that comes later shows (one that he opens, a Bot's page that comes up for an
ask), and a row or a Bot that names a page that is put away takes it out again.
The choice is an optional field of the room's record at the same version
(`room.ts`, `pagesHidden`, left out while the pages show; a reader that does
not know it reads the pages as before). The workspace then opens with its kept
pages put away, except the page that the address asks for. A room with no
record kept opens with no page at all (`startRoom`, TALK slice 1): Home is the
main panel, large and straight ahead, with the Bots and the palette, and the
crew's chat page comes only when he opens it; a record of an older
build, which has no such field, opens with its pages showing. The address word
`pages=show` is read only where no record is kept: such a room then opens with
its pages showing, for a check that reads a page; a kept room opens as it was
kept. The plate takes its own presses in both forms and does nothing with them,
so a press on the strip's numbers reaches no button of the page's top bar behind
it. A page's close mark keeps its whole press area (D54): where the strip would
reach into one (a phone, whose 44 px area is wide), its right end moves left
until it stops 0.3 degrees short of it, the switch staying at its right end;
when the four counts and their words no longer fit before the switch, the counts
drop their words: a count of 0 is not drawn then, and what is left keeps its
colour and its word's first letter; with nothing left only the switch stands
(`homePlace.stripCuts`, each panel's area in `homePlace.closeMarks`). A mark
behind him (its yaw wraps across 180 degrees) cuts nothing. When the room left
between marks is narrower than the switch, the strip is the switch alone, moved
just past the marks to the side nearest straight ahead (`homePlace.stripFit`),
so it never lies on a mark. A page's area here is its close mark's with its top right corner's handle, and a mark keeps the side it cuts until its middle is a degree past the strip's middle (D56 4, `markSides`), so a page held near the middle does not flip the strip from end to end. The switch and each
line take a pointer as a tile of the desk does (the dot and a pinch of a hand
that rests, a raised hand's ray, the mouse). A press on the line of an approval
whose board its Bot holds up takes that board by the board's own hook
(`useBoards`, `take`), so the same board comes out of Home into his hand, with
its one stamp, throw and hand-back; handed back it is at its Bot again, and its
line is still on Home. While the board is in his hand or on the floor its line
has no target (it lights for no pointer and takes no press): one ask is never on
a board at its Bot and in his hand at once, and no page opens beside its board
(`lineDoes`). A press on the line of an ask that its Bot will hold up, before
that board is made, waits for the board (up to 2 s, `LINE_WAIT_MS`) and takes
it as it comes; a wait that ends with no board does what the line opens, and
any other press ends the wait: a pointer, a pinch, or a finger's down on the
desk (`DeskTouch`, `onDown`). A session that starts or ends ends it too. Which ask a board comes for is the boards' own
rule (`boardComes`, as `boardsNow`: the Bot's body is drawn, it holds no board,
and the ask is its oldest approval that no board answered). An ask that a
board answered and that the list still names (the hook's own book,
`useBoards`, `answered()`) gets no second board: its line stays, since "need
you" still counts it, and has no target. An id that left the list and is asked
again is a new ask: its line waits or takes. In the flat preview a drag that begins on the
plate turns the room and a wheel on it brings the eye nearer, as on empty
space; neither reaches a page behind. A press on any other line opens the ask
that the line names (`lineOpens`; `Home`, `onTake`): the Bot's own page, as a
tap on that Bot does, when that page draws the ask's card or the Bot is only
flagged as waiting; the dashboard's page of every session's asks
(`/session-dashboards`) for an approval that only the approvals list carries
and for an ask of work that no Bot owns. A line whose ask can be opened nowhere
(work that no Bot owns, flagged as waiting, with no request read) is drawn and
takes no press. A piece of work that was counted as done today stays counted
until the day turns (`keepDone`, kept by the hook as long as the page lives), so
the number does not go down when a Bot that had finished starts again; "last
active today" is not "finished", so a session that was only opened today counts. A list outside the canvas says the same
numbers and lines to a screen reader (`HomeMirror`).

**Since you left** (D36; `workspace/since.ts` the rules, `useSince.ts` the
state, `HomeSince.tsx` the line, `SinceLight.tsx` the light). When he enters,
one line above Home's strip says what changed since his last visit in this
browser ("Since 14:05", then a number and a label for each group, as the strip
writes its own), and each Bot that changed is lit: a warm pool of light under
where it stands, and nothing else (one sign that reads). The Bot that stands in front has no
pool: the pools say "look over here", and he looks at that one already. The desk's own light keeps meaning that
its Bot works. The last visit is kept in the room's record (`seen`: when it
was, and each Bot's asks, cuts and last activity then; it follows the crew
while he is in the room, so what a Bot did while he was there is not "since"); a browser with no record shows no
line. A Bot is in one group only, the most pressing: it needs him (an ask it
did not have then), it failed (a cut it did not have then, not old), or it
finished (at rest, active after what the record holds of it, and its own
thread has words he has not read). A cut needs no unread words: a thread turns unread only when its Bot
spoke. A cut that is new since the visit has a red rim until he deals with it
(`sinceCuts` feeds the rims' book), so Home's "failed" counts it. It clears as
he deals with each one: the ask is answered or the words are read, on any
device (`stillSince`); the cut runs again, turns old, or the rim's book calls it
seen (a press on the Bot or on a panel of its page: one place decides, `stands`);
he takes that Bot's sheet in his hand (D39: he has looked; "to read" still counts
it until he files it). A take clears only what he took (`lookedAt`): a done sheet
that the Bot finished, a failed sheet its failure; a Bot that still asks stays
"need you", lit. A Bot that needs him keeps its new cuts and whether it was
active: when the ask ends it falls to "failed", then to "finished" (`regroup`).
A press on the line clears all. The line waits for a whole read (the list of
sessions too, `model.wholeRead`), and the record keeps the visit as it stood at
the last whole read. A visit ends when the page is hidden, or the XR session has
ended, for more than five minutes (`VISIT_GAP_MS`): the record keeps the visit
as he left it, and when he is back the line is worked out again. What is left is kept in the record (`since`), so a reload
does not wipe it; what changes while he is in the room is not "since". The
Bots that are lit are exactly the Bots that the line counts (`sinceLit`,
`sinceCounts`). The line stands above Home, and under it when above would be
past what the glasses show of a level look (`sinceLinePitch`). The rules read
only what the model already carries (`XrBot.needs`, `cuts`, `state`, `lastTs`,
`ownUnread`). Words are unread for a browser only while no window of the
person shows that thread: a window that shows it reads what comes in.

**Named rooms** (D37; `workspace/rooms.ts` the rules, `useRooms.ts` the state
and the switch, `RoomCards.tsx` the cards). He keeps up to five rooms, each as
he left it, one press apart. A room keeps itself as he changes it: there is no
save. The room that stands is the room's record as before (`kirocrew:xr-room`);
the list is a second key (`kirocrew:xr-rooms`: each room's name, whether he
named it, its layout, and which one stands). A switch writes the room he
leaves into the list first and only then makes the other room's layout the
standing record (`switchRoom`), so a switch never loses a room, and a build
that reads only the first key opens the room that stood last. A room holds
his own doing: its pages (path, place, size, whether a hand placed each),
whether they are put away, and where he put Bots (`layoutOf`). It never holds
the crew's state: who stands in front, the asks he handed back, his last
visit and what changed since, the marks that end with a Bot's state; nor the
drawn room and the desk, which are this device's setup. Those stay in the
standing record across a switch (`standWith`). At a switch a page of the same
path in both rooms stays live and moves to its place in the new room; the
other open pages go and the other kept pages come (`switchPlan`). A name is
proposed from the room's largest page (`proposeName`), with a number when
another card has it (`uniqueName`), and follows the room until he names it
himself. Rooms are per browser.

**Warm rooms** (D46; `workspace/warmRooms.ts` the rules, `useWarmRooms.ts` the
state). The pages of the rooms he left stay open at the gateway, out of view,
so a switch back shows whole pages at once. A warm page has no panel: its
source is told `visible: false`, so the gateway makes no picture for it (and
never watches it), and the tab keeps its last picture. When he comes back the
page stands with that picture at its first frame, is told `visible: true`,
and the gateway's fresh one follows; the hold of the switch waits only for
the pages that are asked for (`roomReady`). Until that fresh picture lands
(the first picture that arrived after `takenBack()`, 3 s at most,
`FRESH_WAIT_MS`), the page takes no press, wheel or key (they are dropped) and
is drawn at 60 % light (`Panel.tsx`, `FRESH_DIM`); its edge, corner and close
mark work at once (D49). When his dot or pointer has rested 150 ms on a warm
room's card (`RoomCards.tsx`, `CARD_REST_MS`), that room's pages are told
`visible: true` before the press (`useWarmRooms.rest`, `StreamSource.peek`),
and hidden again 2 s after he leaves the card (`PEEK_AFTER_MS`), or 10 s
after the rest began however long it lasts (`PEEK_MOST_MS`, D53 4); a picture
that arrived after that early word counts as fresh. A page told so by a rest is
never the watched page (`watchPage`, D53 5). The gateway forgets the watch at every `visible: false`, so the tab says `watch` again after each `visible: true` for the page he watches (REVIEW-19 S1). Not for the room he stands
in, nor while the tab is asleep or hidden (D52). The dim and the press gate
are one rule (`notYet`): a page is dimmed exactly while a press on it is held
for an old picture. Its three waits each end by themselves (D53 1): a new
page's content (its first picture with content, or 3.5 s after live); a warm
page's fresh picture (3 s after the take); and a live page with no picture
since it came into view (the gateway's next picture, which `visible: true`
always brings, or 3 s after it came into view). No clock ends a wait before a
picture of THIS open has landed (D55 1): with none the page holds, its line
says it waits ("waiting", from 3.5 s in view). Its time in view is added up,
and out of view does not count (D56 1). After 15 s in view it asks the gateway
in words the gateway answers: `visible: false` then `visible: true`, which
starts the cast anew and brings a first picture; the page stays live and keeps
its place among the twelve (an `open` would be dropped for a panel the gateway
holds). After 30 s in view it is failed (`no_picture`), says so, and sends
`close`, so it holds none of the twelve; a press on its glass asks for it anew.
`open` gives up any picture of the open before that waits to be decoded or to
land. The content clock counts from the later of live and the first picture of
this open that landed (D56 2). A picture decoded again for a layer (`again`) is the old
one and ends no wait (D55 2). Over a held hide the gateway
never stopped sending, so the picture is not old and the page does not wait
(M7). Every other hold of a press (an ask's first second and its other
seconds, `asks.ts`; the quiet time after a close) leaves the page at full light
and turns the dot's light off (`Workspace.pressHeld`, D53 2). A release whose
own down was dropped sends no click, and the keys follow only a press that
sends one (D53 6). A held hide past its late time with no picture landed is
sent then, with no timer left. The phone's type field (`PhoneField.tsx`) keeps
what he typed, an Enter too, and sends nothing by itself; his next sign or
Enter sends it all. **Known limit:** the tab tells a fresh picture only by
its arrival time (the header carries no time it was made), so a picture the
gateway sent at once on `visible: true` from an older capture would count as
fresh; that is the best the tab can know, as the gateway sends its first
frame on `visible: true` without waiting for the page to paint. A page on a
composition layer is not dimmed (the dim is the mesh's colour), though its
input is held. The 60 % is of linear light: white text drops from 255 to
about 203. Keys typed into a dimmed page are lost, and the dim is the only
sign of it. A page put away from the start gets one picture before its hide
takes. On a layer the dot's faint light is the only sign that a page takes
nothing yet. The flat test runs on a decoded picture only while a panel draws
the page (else as it lands), and keeps its cost in `seen.flatMs`. A page is kept only when it was
whole: it showed its picture for 4 s without a break at some time since it
opened (`PageSource.wasWhole`, `WHOLE_MS`), or it filled out of his view by
warm ahead's rule (`filledOutOfView`, below): a page that he never had whole
in his view (one at the room's side, out of the lens) is told `visible: false`
by its panel, and the gateway hears it only once the page has filled, so it
goes on getting pictures until then (D48 A). A page with no panel (put away,
or opening put away) is told at once, since no picture of it lands; so is a
page asked for again while out of view (`open()`), and a held hide ends with
that word, so the next `visible: true` is sent (REVIEW-17 M3, M4). A page
whose pictures were cut so before it filled (`hideCut`) never holds its hide
again: a glance at it and away hides it at once (M5). A hide held while the page
loads starts its timer when the page goes live. The hide is said again on
`opened` (harmless: a socket's words run in order; the one picture a put-away
page gets likely comes from `Page.frameNavigated` showing a panel that is
`shown` by default, `panel_hub.py`). A page put away and shown again ("Show
pages", `useHome`) is taken back as a warm page is (`takenBack`): dimmed and
taking no press until its fresh picture (M6). A link
that moves the page to another address starts its filling anew. A page whose `visible: false`
reached the gateway before it filled does not fill out of view, would come
back as its shell, and is never kept. A page that a link inside it moved to
another address counts its time anew.
The rooms hold ten of the gateway's twelve pages at most, the standing room's
and the warm ones' together (`ROOM_PAGES_MOST`); the room he stands in comes
first, then the rooms he left, the most recent first, each whole or not at
all (`fitWarm`). A warm page always gives way: before a page he opens or a
Bot's page is asked for, a warm room that holds a page of that path closes
(no page is open twice), and when the twelve are full the room left longest
ago closes (`giveWay`; the pages taken from the set at a switch count). The
twelve are the gateway's for every socket (his Mac and the glasses), which a
tab cannot hear: a page that the gateway refuses as one too many closes the
room being warmed ahead, else the warm room left longest ago, and is asked for
again at once, until it is given or no warm room is left (`fullHook.ts`,
D48 M1). Outside a session, a tab that is hidden (`document.visibilityState`) keeps no warm page
and warms none, and so does a tab in which he has done nothing (no pointer,
key, wheel or touch) for five minutes outside a session (`IDLE_MS`); a session
is never idle. Shown again, or at his first input, warm ahead starts over as
after a load. The record decides what comes back: a warm page that
the room's record no longer keeps closes (`takeWarm`). A warm room closes
when one of its pages is no longer live (the gateway closed it, or the
socket went), when it is removed, when a switch to it could no longer be
served whole by the warm pages (a page it needs stood elsewhere and closed;
`prune`), and while another tab of this browser shows the room (a
`BroadcastChannel`: the twelve are for both tabs; a tab that says nothing for
150 s is gone, `TAB_GONE_MS`, since a tab hidden for over five minutes beats
once a minute, and a tab that closes says "bye"). A page
that the tab draws itself (`source=dom`) is never warm.

**Warm ahead.** Once the room he stands in is at rest (nothing of it changed
for 4 s and every page is live), the rooms that are cold are opened out of
his sight, one at a time, the ones he left most recently first, then as the
list has them; a room is taken only when its pages that would have to come
fit inside the ten (`nextAhead`), and none whose pages include a path that an
ask's panel shows now. A room warmed ahead counts as left before every room
he really left. Its pages are asked for with no panel and
stay visible to the gateway until each has filled, then are told
`visible: false` and are warm. Nothing in the tab says that a page is whole
but the gateway's lossless still, which it sends once a page has not changed
for 0.4 s: a page counts as filled when a still came 6 s or more after it
was live (the shell's still comes early; a whole page may go on moving a
little, so the still need not be its newest picture), else 15 s after it was
live (`filled`, `AHEAD`; `PageSource.restFacts`). A room that has not filled
after 30 s, or of which a page was refused or closed, is closed again and
not asked for again for five minutes. None is warmed while a switch is under
way, a hand is down, a key or the wheel went to a page in the last 4 s, a
board or a speak sheet is in his hands, or a card of his is held, typed on
or on its way; a room that is being warmed gives way
first when a page of his needs a place, and is closed, not handed over, when
he switches to it before it has filled. `?ahead=0` switches warm ahead off.
A warm room's card has one small lit dot (`RoomCards.tsx`, `WARM_DOT_DP`;
the preview's bar the same), so he knows which press is the quick one.

The standing record says which room it is (`room`, `tagRoom`): a tab that
dies between the two writes of a switch opens the room that its record
names. When the workspace opens, a record that cannot be read, or that names
a room which the list no longer holds (another tab removed it), is made again
from the list's layout of the room that stands, and a record with no tag is
tagged (`standingFor`), so no room is emptied or written over by a bad record. A kept page
that names a session, a Bot or a tab is never stood in for by another page
of its kind: only a bare page (`/chat`) takes an open page that moved on
within it. The room he leaves stands until the new room's pages show their
first picture (`roomReady`, 2.5 s at most; a new streamed page is not in the
room while the old one stands, so nothing draws it and it counts 1.2 s after
the gateway says it is live: its first pictures show an empty body, then grey
boxes, and its words come last, so neither "a picture has reached the tab"
nor "no new picture for a while" says that the page shows anything; a page
slower than that stands for a moment with its grey boxes) and its Bots stand whole at their
places (`botsStand`: the crew is told of the new room's pages and places at
the press, so the Bots change spots while the old room stands); a board at a
Bot has its Bot's share of size, so none shows before its Bot. No record is
written meanwhile,
"+" adds no room, and a switch whose list could not be written does not go.
A new page that the gateway had no room for while the old ones stood is asked
for again when they are dropped.
A page that stays glides to its new place (`Panel`, `glideMs`) and passes
behind the page nearest the middle of his look (`glideBehind`). Two tabs may
stand in two rooms: each writes its own room into the list, shows what the
other names, adds or removes (the `storage` event), and writes no record for
a room that another tab removed.

The cards (`RoomCards.tsx`) are one row over the hand palette's tiles, each
with the room's plan (`planOf`) and its name; the room he stands in is lit.
A pinch goes to a room; "+" makes the room as it stands a new one. A card
held for 450 ms is in his hand: the card's speech listens and what he says
is its name when he lets go (the new chat card's speech stands aside
meanwhile). Thrown away, the room is removed and one line offers it back for
6 s, with no "+" beside it so that its place is kept. A throw is told as a
board's is (`cardThrown`, `board.ts` `releaseKind`): far and still fast at
the release; a slow carry, a drifting hand or a press that ends as a cancel
is no throw. The card of the room he stands in goes back to its place, and
one line says "You are in this room". The first time there is more than one room one line
says "Kept on this device", whole or absent, once. Where the palette with
its cards would lie on Home's strip it comes down until the cards are under
the strip (`homePlace.ts`, `underStrip`). With a desk the same row stands
behind the desk's tiles (`DeskRoomCards`). In the preview's bar the same
rooms are small buttons (`RoomsBar`), for a mouse and a screen reader: a
press goes there, a cross removes, "+" adds, and the room he stands in is a
field with its name.

**The desk** (`/xr?...&desk=1`; `workspace/desk.ts` the numbers, `Desk.tsx` the
top and the tiles, `useDesk.ts` the state). With the word, a level desk 1.0 m
wide and 0.5 m deep stands in front of the person: by default its top 0.40 m
under the eyes and its near edge 0.35 m ahead. Its near 20 cm hold nothing (the
hands rest there); in the 15 cm after them each Bot of the crew stands at its own
seat (`Crew.tsx` with `desk`: 8 cm tall on a pad 9 cm wide; up to 6 in one row
in the band's middle 10 cm apart, 7 or 8 as a zigzag of two rows 6.8 cm apart
across, at 30.5 and 24.5 cm in, no pad on the rest or on another; `seatPlaces`).
Past 8, seven are seated and the eighth place is a pad with the others' number,
whose press opens the Bots page. A Bot keeps its own seat: the order is kept in
the desk's field of the room's record, a Bot that leaves leaves its seat empty
and a new one takes the first empty seat, and only a roster read whole changes
it (`seatOrder`; `useDesk`, `seating`). A seat shows its Bot's name only while
it is lit. At the band's left lie the palette's tiles in one row (8.5 cm: the
plus, a new chat, the flat preview's start view, set the desk; `tilePlaces`),
and the far 15 cm stay empty. The tiles, the plus's rows and the others' pad open
their page the way the bar's plus does (`openListed`): a panel that shows it
already is turned to. The plus's list stands upright beyond the band, five rows
at a time, so that each row it shows is in the picture from the look that lit
the plus's tile, in the glasses too; a longer list scrolls four rows a press by
the two small tiles at its right. In the flat preview the start view looks down
until the band's near edge is whole over the picture's lower edge
(`deskStartPitch`), and the preview opens on it. The crew's row
under the first page and the palm's palette are not drawn then, and the drawn
room draws no desk of its own (`RoomWalls`, `noDesk`): the set desk is its one
desk, drawn solid while the room is on, and the one that an ask's panel stands
clear of (`AskHost.deskPoints`). A page that opens keeps out of the part of the
view from the desk's near edge up to 3 cm over the Bots' heads (`deskZone`). The
desk's place is an optional field of the room's record at the same version
(`room.ts`, `readDesk`; a reader that does not know it reads the pages as
before); a desk kept in the flat preview is never used in a session, and a desk
that a session kept is drawn dim until a press lands on it. A press on a seat
(the dot and a pinch, a raised hand's ray, the mouse in the preview; the seat's
pad and its Bot's body are one upright target) is a tap on that Bot: its jump,
and 320 ms later its page by round 12's path (an ask's panel when it asks, else
the Bot's page). When that Bot's page stands already, its frame lights for 1 s
(`Panel`, `flash`; `FLASH_MS`) and nothing opens; in the flat preview the
picture turns to it as well. A Bot's page here is a panel that shows that Bot's
page or the chat of one of its sessions now (the session the chat's `sid`
names), or one that was opened with that Bot's page and has not gone to another
Bot's page or another Bot's chat since (`crewRow.isBotsPage`). Without the word
a tap on a Bot finds its page as before: a panel that shows that Bot's page now,
so a panel whose page moved on to another Bot is never it. A seat says its Bot's state as the crew row does (`model.ts`): a turning
ring while it works, the pad lit in the ask's colour with a small blank board
while it asks (the Bot's board, above, stands in its place while one is up), a whole red rim for a cut of it that he has not seen (the dim
mark for one he has, as above), a white
mark while it has something unread (the dashboard's own `unread`), and nothing
but a dim pad while it rests or while the reads are not whole (`known`).
With a desk, a hand is raised only when its ray's origin stands 15 cm over the
desk's top (`pinchRoute.raised`, `PINCH.deskRaisedM`; `pinchRoute.deskY`). A
pinch of the finger tips (a device's own select stays the device's) is a down
only in the frame in which the tips close, and only when the tips were more
than 3 cm apart in every frame of some 150 ms, the last such frame came at most
250 ms before the close, the wrist moved less than 2 cm in the 300 ms before it,
and the hand was tracked in every frame of the 400 ms before it; a flat hand
never pinches (`TipsPinch`, `deskDown`; `PINCH.deskApartMs`, `deskBriskMs`,
`deskStillM`, `deskStillMs`, `deskTrackedMs`). Tips that are closed with no
such frame are never a down, however long they stay closed. So a hand that
comes to rest curled, a slow curl and a hand found as its tips close press
nothing, and neither does a slow, careful pinch: he pinches briskly.
A finger's touch presses a pad or a tile in a session (`deskTouch.ts`,
`DeskTouch.tsx`; every height from skin to desk, each joint less its radius):
the index tip of a pointing hand (the tip at least 6 cm from the palm, the
middle and ring tips at least 2 cm higher than it), tracked in every frame of
the last 200 ms, that comes from more than 3 cm over the desk to within 1.5 cm
of it over a pad or tile and stays 60 ms is the down; past 2.5 cm it is the up;
a down and an up on the same pad or tile within 800 ms press it, as a tap on its
Bot or a press of its tile (`touchTargets`, which `Crew.tsx` and `Desk.tsx` keep
while they are drawn). A touch takes the one press as a pinch does
(`pinchRoute.touch`): the first down of either owns it until its up. While the
index tip of a pointing hand is nearer than 5 cm to a pad's or tile's upright
box, from any side (`nearestM`), the dot is off and no pinch presses
(`pinchRoute.tipNear`); a hand that does not point, such as one resting on the
hands' rest by the first seats, leaves the dot on. A flat hand presses nothing.
The set-up by flat hands (`deskSetup.ts`, `DeskSetup.tsx`) is armed when a
session starts (a session's desk is never confirmed at its start) and by the
tile "set the desk" in a session; in the flat preview that tile puts the desk
back at its default place. While it is armed, both hands that lie flat (the
lowest skin points of the wrist, the palm and the five finger tips, each joint
less its radius, within 1.5 cm in height; `hands.tsx` writes them to
`deskHands`), still (each wrist within 1 cm) and where a desk can be (each
wrist at least 15 cm ahead of the eyes along the head's facing and 20 cm under
them, `onADesk`: hands that hang at the sides are no desk) spread a grid of dots from under
them, and after 1.5 s the desk is set: its top at their mean height, its near
edge's middle under the wrists' middle, facing the way the wrists lie. It moves
the desk only past 2 cm or 3 degrees; a nearer place confirms the desk that
stands (`movesDesk`). Either arming ends at his first press of anything (a
pinch's or a touch's down: `pinchRoute.owner`, `pinchRoute.touch`) or 10 s after it (`SETUP.armedMs`),
whichever comes first, and the desk that stands stays; the grid shows only while
it is armed. At any other time flat hands move nothing, and a kept desk that is
still dim is confirmed by a press only. With `probe=1&hands=1`
a test's AR session paints the hand models, so that a film shows the emulated
hands.
Without the word none of this is read.

**The new chat card** (`workspace/chatCard.ts` the rules, `useChatCard.ts` the
state, `ChatCard.tsx` the card, `chatSpeech.ts` the recognizer, `deskKeys.ts`
and `DeskKeys.tsx` the keyboard). A "new chat" button brings one card out: a
wide tile over the palm's palette (`PaletteCardTile.tsx`), with a desk the tile
`card` in the place that the start view leaves free in a session
(`useDesk.ts`), and in the flat preview a button of the bar. One card is out at
a time. It stands 0.65 m ahead, 25 degrees to the left of where the person
looked; with a desk, upright over the palette's tiles. While it is out or held
the browser's own recognizer listens (the Web Speech API; Chrome may send the
sound to its maker's servers). The microphone's red dot shows on the card
whenever the recognizer has said that it listens, whatever the card's top line
says, with the words "Microphone on" when the line is free for them; final
words and the words still worked on show on it. Where the browser has no
recognizer, or it refuses or has not started after 8 s (`START_MS`), the card
says so and its keyboard comes at once.
The microphone does not listen without end. After 20 s with no words
(`QUIET_MS`, counted from when it first listens and anew from each word, across
the recognizer's own restarts) it is stopped and the card says "Microphone off"
and keeps its words. A hidden page and a session that the system blurs
(`visibilitychange`, not `visible`) turn it off the same way. It comes on again
only by a pinch on the microphone's mark (the top line), which also turns a
listening microphone off. A session's end stops it and puts the card away.
Each mark of the card (close, microphone, language) acts only at the up of its
own down, by the same pointer (`markStep`, `ownUp`): a press that began
elsewhere and is let go over a mark does nothing, and a press that left the
mark or was cancelled (a long press, a lost hand) is dropped, so that no later
up acts for it. The language's mark takes a press over about one and a half
times its drawn frame, inside the card and clear of the other two marks. A
language switch takes the word that the recognizer still worked on into the
card, and a result that a recognizer of another language than the card's
still sends is not taken (`heard` carries its listener's language).
The language is the card's own: it starts as the dashboard's, a pinch on the
mark at the card's lower right (the language's own name: "中文", "English")
goes to the next, and the choice is kept in this browser (`LANG_KEY`) for the
next card. The list is Chinese and English, and the dashboard's own language
when it is another (`speechLangs`). A language that the recognizer does not
know (`language-not-supported`) is not "no speech here": the card goes to the
first language of the list, for that card alone, and keeps its mark
(`langAfterRefusal`); when the first is refused too the microphone is turned
off and the mark stays. The keyboard types Latin letters, digits
and signs only: Chinese comes by speech.
Everything of the card that takes a press carries `userData.xrPress`, which
the readout's plate reads so that it never covers a thing that can be pressed:
the card's group (`'card'`), the keyboard's group (`'keys'`), the palm's tile's
group (`'tile'`), and each mesh of theirs that takes a press (`true`).
A throw: a pinch with the dot on the card (a raised hand's ray, the mouse)
takes it, the head (that ray, the mouse) turns, and letting go throws it where
the aim is (`aimTarget`): nowhere while the aim is within 9 degrees of the
card's own place; to the Bot whose body the aim passes within 4 degrees of;
else into the open. A Bot's reach is its body's own half-width, as its press
box. No hand is raised for it. In the flat preview's quiet time a card is not
taken, and one that is held already is still let go. A pinch held long with the hand
still ends as a cancel for a page (`HeadAim.tsx`); for the card it is a let-go
like any other, and only a lost hand puts the card back. Held, the card rides
over the aim and a mark shows where it would go. Thrown with words it flies
forward to its target and is sent when it lands; thrown with none it is not
sent: it stands at its place with its target written on it, the keyboard
comes, and Enter sends it there. An empty card is never sent
(`cardStep`, and `cardSend` refuses one).
The send is the card's own named write, `writes.ts`'s `cardSend(text, to, id)`,
and `useChatCard.ts` reaches nothing else of that module (`safety.test.ts`
holds the fence). One id is one send: the id is the request's
`Idempotency-Key`; a call while the first is under way gets the first's own
promise; a call after it went through sends nothing. A card whose send failed
comes back frozen, with its words, its target and its id as they were: it takes
no new words, does not listen and shows no keyboard, and a throw of it anywhere,
or a press on it, sends exactly that again, to the same thread. Its top line
names its target all the time, and its lowest line and the room say what is
true whether the request or only its reply was lost (`xr.card_not_sure`:
"Not sure it went. Throw it again to send the same words."). `cardSend`
refuses other words or another target under a used id, since the first request
may have reached the gateway with only its reply lost, and the gateway refuses
a used key with another body for good. To say something else he puts the card
away and takes a new one. A new chat that was started for a send that did not
go through stays in the list of chats (it may hold the words, or nothing), and
the room says so when the card is put away. To a Bot it goes to that Bot's own thread and
no page opens; into the open it starts one new chat (`api.createChatSlot`) and
that chat's page opens on a panel: the chat page with that session's key as
its one query (`streamPath.ts`, `chatSessionPath`: `/chat?sid=<key>`, a key of
letters, digits and `_.:-` and nothing after it), which the gateway opens
while its guard is off and refuses with it on. It is on in the default build,
as a press on a page's own send button is.
The keyboard is one board of ten keys by four rows (letters, Shift for one
capital, a layer of digits and signs, space, Backspace, Enter). With a desk it
lies on the near band where the hands rest, a key every 3.6 cm, and each key is
a target of a finger's touch (`touchTargets`) as well as of the dot and a
pinch; with none it stands 0.8 m ahead under the card, a key every 3.4 cm.
The board takes a near miss: a press between two keys, or within 1.5 cm past
its outer keys, does nothing and does not reach the page or the Bot behind.
While it shows, a real keyboard types on the card too (`Workspace.tsx`,
`onKeyDown`); the arrow keys still turn the head. While a board is in his hand
the keyboard is put away, as a taken 3D dashboard is, since the held board
(0.75 m) stands in front of it (0.8 m); it comes back with the card's words
when the board leaves his hand (`useChatCard`'s `keysAway`). Meanwhile a real
keyboard's typing key (a letter, Enter, Backspace) does nothing: it was meant
for the card, so no page behind gets it (`typed`). The arrow keys still turn
the head.

### Security

| Risk | Control |
|---|---|
| An agent steers a browser signed in as the owner (panel tabs) | the panel browser's relay serves no listener and no daemon; no agent tool, MCP tool or internal-secret route reaches it; the input routes refuse the internal secret; pinned by `test_browser_panel_routes.py` |
| A panel shows or drives another person's page | pictures go to the opening socket only; input needs the same verified access token; a non-owner socket opens nothing |
| A panel tab leaves the dashboard | fixed path shapes on the gateway's own origin; other documents and redirects are failed |
| A stray pinch grants an agent permission | grant controls are marked in the dashboard and a plain press on one is stopped in the page and not sent by the hub |
| The owner's session leaks through a panel | set in memory over the pipe as a session cookie in an incognito context; disposed with the socket; never logged, never in an address or a file |
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
| Local addresses | `navigate` keeps the public-only gate plus `dashboard.browser_local_origins`, applied by one shared predicate (`browser_harness/url_gate.py`): in the shim to the URL asked for, and in the gateway to the URL asked for and the URL the navigation (or `back`) committed on, so a public page that redirects to loopback, private or link-local is refused (`redirect_refused`) and the tab moved to `about:blank`. The gateway adds the origins it trusts as itself (`allowed_origins`), so the dashboard is refused under a tunnel, tailnet or `dashboard.url` name too (`url_blocked` before the request). **Accepted residuals:** DNS rebinding (a public name resolving to a private address) is not seen by either check; names are not resolved. An address that reaches the gateway but is not one of its trusted origins passes |
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
