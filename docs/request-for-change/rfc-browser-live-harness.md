---
title: Live browser in chat -- drive Browser Harness through a gateway CDP relay and show the page in the transcript
status: draft
author: bolichen97
created: 2026-10-22
last-audited: 2026-10-22
audited-at: 2aa6878731
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Live browser in chat

**Status:** `draft`. Nothing here is built. Measured against main `2aa6878731`.
When PR 4 lands, this document supersedes
[rfc-playwright-cli-migration.md](rfc-playwright-cli-migration.md).

## Summary

The agent browses through two stacks today, and the person watches through a
third surface. The `browser` MCP tool drives an Electron view in the side
panel; `playwright-cli` is the fallback and the only way to reach the user's own
Chrome; the side panel's Browser tab shows either one. None of them puts the
page where the person is already looking, which is the chat.

This RFC replaces the agent's browser with one driver and moves the view into
the transcript:

1. **One driver: Browser Harness, local only.** [Browser
   Harness](https://github.com/browser-use/browser-harness) (MIT) drives a
   Chromium-family browser over CDP. Kiro Crew pins it, runs its daemon under
   the gateway, and forces its telemetry and its cloud path off. The `browser`
   tool keeps its name, its governance check and its URL gate; only what sits
   behind it changes.
2. **One CDP connection, owned by the gateway.** A loopback relay in the gateway
   holds the only connection to the browser. Harness's daemon connects to the
   relay as if it were the browser. The relay takes its own screencast and
   input on sessions Harness never sees.
3. **The page lives in the chat.** While the agent browses, its turn shows one
   live card. Clicking the frame hands mouse and keyboard to the person; the
   same card grows. When the turn ends the card collapses to a one-line record.
4. **The browser is whichever one is there.** The user's own Chrome on a
   desktop, Edge on Windows when Chrome is absent, and a Kiro-owned Chromium,
   headless, on a server or a remote gateway.
5. **Passwords never pass the model.** Logins happen during takeover. The
   agent's `type` refuses a password field and the frame masks one.

The side panel keeps one surface: a **Preview** tab for the person's own local
dev server, so front-end work keeps its page annotation. The agent no longer
drives that tab.

## Motivation

### Current state

Verified at `2aa6878731`:

| Surface | What it does | Where |
|---|---|---|
| `browser` MCP tool | posts one op to `/api/browser/command`; Electron drains it and runs it in the panel's `WebContentsView` | `mcp_tools/browser.py`, `browser/command_bus.py`, `website/electron/browser-ops.js` |
| `playwright-cli` | the fallback, the attach path to the user's Chrome, and the `show` view | `browser_cli/` (16 modules), `dashboard/handlers/browser_view_relay.py` |
| Browser tab | renders the native view or the relayed `show` page | `website/src/components/WebPreviewPanel.tsx` (1,441 lines) and its surfaces |
| Prompt and skills | 96 mentions of `playwright-cli` across `config/prompt.md` and 8 builtin skill trees | `config/prompt.md` `{{BROWSER_BEGIN}}` section, `builtin_skills/` |

The backend browser code is 7,862 lines (`browser/`, `browser_cli/`,
`mcp_tools/browser*.py`); the Electron side is 2,788 (`browser-*.js`).

### What breaks for the person

- **The screenshot op hangs when the panel is hidden.** Switching the side
  panel to another tab hides the `WebContentsView` but keeps the page alive, so
  `navigate` and `snapshot` work while `Page.captureScreenshot` never returns
  (`browser-ops.js` documents this). The bus and CDP timeouts are both 15 s, so
  the bus fires first and the agent sees a bare `504 timeout` instead of the
  clearer `cdp_timeout`.
- **The native tool cannot draw.** It addresses elements by ref only: no
  coordinate click, no drag, no scroll by position. `click` accepts
  `doubleClick: true` but the tool description does not say so.
- **The model never sees a native screenshot.** The frame stays in the panel,
  so the agent cannot show the person what it saw.
- **The user's own Chrome is opt-in by prose.** [#19313](https://github.com/kirodotdev/KiroCrew/pull/19313)
  made the agent attach the user's Chrome when a page needs their login, but
  the decision lives in prompt text, and attach goes through the Playwright
  extension, whose token or click is needed each time.
  [#22561](https://github.com/kirodotdev/KiroCrew/issues/22561) asks for the
  same outcome from the other direction: a browser that carries the user's
  logins, that the user can watch and take over, without the agent touching
  their own tabs.
- **Watching means the side panel.** A headless or remote browser is visible
  only through the relayed `show` page, in a panel the person has to open.

### What the code already makes easy

- `computer_use/screencast.py` already relays frames to OWNER websockets as a
  `computer_use_frame` event, loopback-gated. The browser frame follows that
  shape.
- `browser_cli/install_browsers.py` already downloads a Chromium build per OS;
  the headless fallback reuses it.
- `browser_cli/own_browser.py` already detects the user's Chrome; the browser
  order extends it.
- `websockets` is already a runtime dependency (`setup.cfg`), so the relay adds
  none.

### Why this, not what we have

Kiro Crew has had three agent browser paths: the Playwright MCP proxy, retired
by [rfc-playwright-cli-migration.md](rfc-playwright-cli-migration.md) (its "Why"
section holds the numbers below); `playwright-cli`, the shell fallback and the
only road to the user's own Chrome; and the native Electron panel behind the
`browser` tool. The table puts them beside the new stack. Cells say what each
path does at `2aa6878731`, not what it could be made to do.

| | Playwright MCP (retired) | `playwright-cli` | Native panel | Browser Harness + relay (this RFC) |
|---|---|---|---|---|
| Uses the user's logins | extension mode, or a storage-state file Kiro assembled | `attach --extension` to their Chrome | no: own empty profile | yes: their Chrome over CDP; Edge; Kiro's Chromium last |
| Approval for their Chrome | extension prompt or token, per attach | extension click or pasted token, per attach | never reaches it | Chrome's Allow once per gateway connection, after a one-time `chrome://inspect` tick |
| Headless or remote gateway | headless launch | headless `open`; remote watched in the `show` page | desktop app only | Kiro's Chromium headless; the same card from a remote gateway |
| Where the person watches | side panel frame | side panel, `show` iframe | side panel Browser tab | the chat: one live card per turn |
| One-click takeover | panel input over the command bus | `show` page, Escape releases; the agent is not told | click in the panel; the agent keeps going | click the card; the agent waits, Esc hands back, it resumes by itself |
| Click or drag by x,y; double click | refs | refs: `click`, `dblclick`, `drag` | ref only; no drag; `doubleClick` undocumented | ref or x,y; `drag`, `scroll`; `clicks: 2` |
| Screenshot reaches the model | not recorded | file on disk, path on stdout | never; the frame stays in the panel, and hangs (504) when the panel is hidden | PNG path returned; shown in the reply |
| Passwords | storage-state file | `state-save`, `state-load`, cookies; a prose rule | no logins at all | `type` refuses a password field; the frame masks it; take over to sign in |
| Frame rate and adaptation | screencast, unmeasured | live viewport, no adaptation | native view, no stream | cap 60; 57.6 headed, 57.8 headless measured; 8 rungs by link; off when hidden |
| Telemetry and cloud | none; npm registry auth at every gateway start | none; npm registry at install | none | PostHog on by default in Harness, forced off by five env fences; cloud keys removed |
| Extra process or binary | Node 20+, `@playwright/mcp` via `npx`, Kiro's proxy | Node 20+, `npm install -g`, 19 MB, `show` daemon, Chrome extension | none beyond Electron | hash-locked `browser-harness==0.1.13` venv, daemon and helper worker; Chromium only for headless |
| Code Kiro Crew maintains | about 9,700 lines, removed by that migration | 7,862 backend and 2,788 Electron lines, shared with the native panel | same | one change: 382 files, +23,569 / −45,464; the deleted files alone were 35,569 lines |
| Token cost per task | 22 tool schemas, 14,080 bytes, 3.5K to 5K tokens re-sent every turn; vendor-reported 4.4M tokens per task at score 60.0 | one shell tool; about 250 chars of stdout per command | one tool, one enum | one tool, one enum; vendor-reported 2.1M tokens per task at score 60.4 |

The benchmark row is Browser Use's own published number, Codex only,
vendor-reported and not reproduced here.

**Why switch.** Each path misses something the person needs: the native panel
has no logins, no coordinates, and a screenshot that hangs when the panel is
hidden; `playwright-cli` reaches their Chrome only through an extension click
per attach and shows the page in a panel they have to open; none of the three
runs on a remote gateway with a view the person can take over from the chat.
Browser Harness speaks CDP to whichever Chromium-family browser is present,
headed or headless, so their own Chrome, Edge and a server Chromium are one code
path. Because the gateway owns the only CDP connection, the live frame and the
takeover input ride on sessions Kiro controls, which no Playwright path offered
without a second process. One tool with one enum replaces 22 re-sent schemas,
and the whole change removes 45,464 lines while adding 23,569, so the stack we
keep is smaller than the two we drop.

**What it costs.** CDP on the user's Chrome is browser-wide; the relay's tab
filter is the control, and a bug there would reach their tabs. Kiro's Chromium
on Windows keeps a loopback debugging port. Harness is a third-party project
(Browser Use) pinned at 0.x whose telemetry is on by default, so every bump
re-reads `telemetry.py` and `auth.py` before it lands.

## Goals

- One agent browser stack on macOS, Windows and Linux, desktop and headless.
- The agent's page is visible in the chat while it works, at up to 60 frames per
  second on a fast link, and costs nothing while the page is still or the card
  is off screen.
- One click hands control to the person; one key hands it back.
- The user's logins are used where they exist and never typed by the agent.
- No usage data leaves the machine, and no cloud browser is reachable.

## Non-goals

- **120 fps.** Measured below: the CDP screencast tops out near 95 fps headed and
  60 fps headless. More needs native window capture and hardware encoding per
  OS, which is its own project.
- **Browser Use's cloud, its proxy, or its agent library.** The cloud sends pages
  to a third party and costs money; the `Agent` library is an LLM agent inside
  ours.
- **Running arbitrary code through Harness.** Its `run.py`, `browser_js`,
  `browser_cdp` and `http_get` stay unreachable; the agent gets the ops this RFC
  lists and nothing else.
- **Replacing the Playwright library.** The E2E suite, `design_critique` and the
  recording skills keep using Playwright directly. Only `playwright-cli` goes.

## Design

![Live card while the agent works](assets/browser-live-card.png)

### 1. The driver

The gateway installs `browser-harness==0.1.13` into its own virtual environment
at `<data-home>/browser-harness`, from PyPI with `--require-hashes` against a
lock file shipped in the package. Installing from Settings -> Browser replaces
the `playwright-cli` install button. The prefix joins `_CREW_READONLY_LEAVES`, so
the agent can neither read it as a path nor replace it.

The gateway starts the daemon with a scrubbed environment:

| Variable | Value | Why |
|---|---|---|
| `BH_TELEMETRY`, `BROWSER_HARNESS_TELEMETRY`, `ANONYMIZED_TELEMETRY` | `false` | the three switches the package reads |
| `BH_POSTHOG_HOST` | `http://127.0.0.1:9` | a second fence if a switch is missed |
| `BH_TAB_MARKER` | `0` | no badge painted on the page |
| `BU_CDP_URL` | the relay's URL | connects to the relay, not the browser |
| `BROWSER_USE_API_KEY`, `BU_CDP_WS`, `BU_AUTOSPAWN` | removed | the only ways in to the cloud |

A test pins this table: the daemon's environment is built by one function, and
the test asserts the removed keys stay removed even when the gateway's own
environment carries them.

The gateway does not run Harness's CLI, its MCP server or its update check. It
calls the daemon through Harness's own IPC (`_ipc.py`: a Unix socket on POSIX,
loopback TCP with a token on Windows), whose path or token lives in a
`HIDDEN` crew-home leaf.

Audited in Harness 0.1.13: telemetry is PostHog, on by default, and its
`browser_harness_run` event carries the agent's whole script and up to 20,000
characters of output with no scrubbing. That is why every switch above is
forced, and why this RFC pins one version: a bump re-reads `telemetry.py` and
`auth.py` before it lands.

### 2. The relay

```
browser ◀──one CDP websocket──▶ gateway relay ◀── Harness daemon (as if it were the browser)
                                     │
                                     ├── screencast sessions ──▶ browser_frame ──▶ live card
                                     └── input sessions ◀── /api/browser/input ◀── takeover
```

- **One upstream connection.** On the user's Chrome, Chrome 144+ asks "Allow
  remote debugging?" per connection. One connection means one click, which
  lasts until Chrome or the gateway restarts.
- **Disjoint ids.** Every daemon request is renumbered from 1 and mapped back on
  reply, so several daemon clients never collide. The relay's own requests use
  ids from 1,000,000,000 up.
- **Private sessions.** The relay attaches its own flat sessions for screencast
  and input. Every reply and event on those sessions stays in the relay, so
  frames never enter Harness's shared event buffer, which `drain_events`
  empties on each read.
- **Agent tabs stay live.** When the daemon creates a target, the relay
  attaches to it and turns on `Emulation.setFocusEmulationEnabled`. Without
  that, a tab behind another tab paints no frames and Harness's input calls on
  it hang for 5 s (headless: `Runtime.evaluate` too). The user's own tabs are
  never attached.
- **Tab ledger.** The relay records each target the agent creates the moment the
  create returns, whether or not the page then loads, and closes only those at
  session end.
- **Loopback only.** The relay listens on `127.0.0.1:0` and hands its URL to the
  daemon alone. Nothing in the agent sandbox learns the port.

A 199-line spike of this relay drove Harness 0.1.13, unmodified, on all three
platforms. Frames per second on a page under constant change, cap 10 (row 5:
cap 30):

| Phase | macOS Chrome headed | macOS Chrome headless | Linux arm64 Chromium headless | Windows Edge headed | Windows Edge headless | Windows Chromium headless |
|---|---|---|---|---|---|---|
| 1 static, new tab | 0 | 0 | 0 | 0 | 0 | 0 |
| 2 drawing, new tab | 9.9 | 10.0 | 10.0 | 10.5 | 10.5 | 10.5 |
| 3 static, foreground | 0 | 0 | 0 | 0 | 0 | 0 |
| 4 drawing, foreground | 9.9 | 10.0 | 10.1 | 10.3 | 10.6 | 10.6 |
| 5 cap 30 | 29.6 | 29.2 | 28.8 | 27.2 | 31.2 | 30.9 |
| 6 behind another tab | 10.0 | 10.0 | 9.9 | 10.2 | 10.5 | 10.4 |
| 7 behind, focus emulation | 9.9 | 10.0 | 10.0 | 10.3 | 10.6 | 8.3 |
| 8 own window, behind | 10.1 | 10.0 | 10.0 | 9.9 | 10.6 | 10.4 |

The Windows Chromium column is the second run. The first stalled one tab at 0-2
fps and recovered on rerun, which is why section 3 carries a watchdog.

### 3. The stream

- **Source.** `Page.startScreencast` on the relay's session: JPEG, quality 60,
  1280 wide. Chrome emits a frame only when the page changes, so a still page
  costs nothing.
- **Pacing.** The relay acks a frame only after the card has received it, on a
  fixed time grid, so transit time counts toward the interval. With a cap of 60
  this measured 57.6 fps headed and 57.8 headless on macOS, from 53.5 under
  ack-after-sleep.
- **Ceilings measured.** Headed Chrome on a 120 Hz display tops out near 95 fps;
  headless Chromium's compositor is clocked at 60. Lifting the frame-rate limit
  reaches 81-84 fps at about 450% CPU, so the product does not.
- **Adaptive ladder.** Once a second the relay picks a rung:

  | Rung | fps | quality | width |
  |---|---|---|---|
  | 0 | 60 | 60 | 1280 |
  | 1 | 45 | 55 | 1280 |
  | 2 | 30 | 50 | 1280 |
  | 3 | 30 | 45 | 960 |
  | 4 | 20 | 40 | 960 |
  | 5 | 15 | 40 | 800 |
  | 6 | 10 | 35 | 640 |
  | 7 | 5 | 30 | 640 |

  It steps down at once when more than 20% of frames arrive later than 1.5
  periods or the link cannot carry the rung, jumping as far as the measured
  rate needs. It steps up after 2 calm seconds, straight to the best rung the
  link covers with 30% headroom. An fps change is free; a quality or size change
  restarts the screencast on the same session. On a simulated link the stream
  settled one second after each drop and was back at 60 fps three seconds after
  the link recovered.
- **Off screen, off.** The card reports visibility; an off-screen or collapsed
  card stops the screencast.
- **Watchdog.** Input sent with no frame for 3 s restarts the screencast.
- **Wire.** Frames go to OWNER websockets only, as binary messages; the card
  paints them on a `<canvas>` outside React's render cycle.
- **Password mask.** Before a frame is sent, the relay asks the page for the
  boxes of focused or visible `input[type=password]` and the card paints over
  them. The setting can be turned off; the default is on.

### 4. Takeover

![Takeover: the same card, grown](assets/browser-live-takeover.png)

- Clicking the frame posts `POST /api/browser/input/claim`. The route accepts the
  dashboard owner's cookie or token and refuses the internal secret, so an
  agent cannot claim input for itself.
- While claimed, mouse, wheel and key events from the card go to
  `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` on the relay's input
  session, at 15 fps for the stream. Keystrokes are never logged and never enter
  the transcript.
- While claimed, an agent `browser` op does not fail: the tool waits until the
  person hands the browser back (in short polls of a hand-back endpoint, so no
  single call blocks and the turn's keepalive keeps ticking; bounded at ten
  minutes, after which it returns `user_in_control` and the agent stops for
  their word) and then runs. The turn shows "Waiting for you to hand the browser
  back", and the agent resumes on its own the moment they press Esc -- no
  "continue" needed. An op the takeover cut short is not re-run; it answers
  `interrupted` after the hand-back so the agent re-reads the page first.
- Esc or "Hand back to Kiro" releases. "Open in Chrome" focuses the real window
  for things the frame cannot carry: the password manager's autofill popup,
  file pickers, extension pop-ups.

### 5. The card

![Done: the card collapses to one line](assets/browser-live-done.png)

| State | Shows |
|---|---|
| Waiting for Allow | which browser, the one step ("click Allow in Chrome"), no timeout; approval took 215 s in one test |
| Live | address, live dot, which browser, Take over, frame, current action, step strip |
| Hover | "Click to take over" |
| In control | grown card, accent border, "You are in control", Open in Chrome, Hand back (Esc) |
| Done | thumbnail, step count, duration, Replay, Last frame, Open in Chrome |
| Login needed | "Take over to sign in" |

![Waiting for Allow](assets/browser-live-allow.png)

One card per turn, attached to that turn's first browser call. Takeover grows
the same element (`layoutId`); it never swaps in another component. Replay plays
the frames the stream already sent, kept in memory for the life of the page.

### 6. The browser

| Host | Browser | First run |
|---|---|---|
| macOS, Linux desktop, Windows with Chrome | the user's Chrome | enable `chrome://inspect/#remote-debugging` once, then Allow |
| Windows without Chrome | Edge | same |
| Server, no display, remote gateway | Kiro's Chromium, `--headless=new --remote-debugging-port=0 --user-data-dir=<data-home>/browser-profile` | none |
| Chrome's `RemoteDebuggingAllowed` policy is off | Kiro's Chromium | none |

A snap-packaged Chromium is skipped: snap confinement breaks the debugging
port. Kiro's profile is a `HIDDEN` crew-home leaf; cookies from a takeover login
stay there and the agent cannot read the file.

### 7. The tool

`browser` keeps its name, its call-time governance check and its `navigate` URL
gate. Its ops become:

| Op | Change |
|---|---|
| `navigate`, `back`, `wait_for`, `press_key`, `hover`, `select_option`, `console` | same arguments |
| `snapshot` | the existing walker from `browser-ops.js`, run in the page |
| `click` | a ref or `x`,`y`; `clicks: 2` is a double click |
| `drag`, `scroll` | new; ref or coordinates |
| `type` | refuses a password field |
| `screenshot` | writes a PNG under the gateway's snapshot directory and returns its path |
| `tabs` | lists and switches the agent's own tabs only |

The prompt's `{{BROWSER_BEGIN}}` section and the 8 builtin skill trees are
rewritten to this one tool. `browser-auth` becomes "take over to sign in".
`web-preview` changes one word: Browser tab becomes Preview tab.

### 8. Settings -> Browser

First run is a three-step list; once done, or skipped, the page becomes a status
card.

![First run](assets/browser-settings-first-run.png)

![Status](assets/browser-settings-status.png)

| Section | Contents |
|---|---|
| Browser in use | the active browser, connection state, the fallback order, Test, Disconnect |
| Engine | Browser Harness version; Kiro's Chromium version, Redownload |
| Privacy | "No usage data, no cloud browser", locked |
| Live view in chat | show live view, quality (auto by link), max fps, mask password fields |
| Local preview | the Preview tab's `browser_local_origins` |
| Advanced | pick a browser by hand: automatic, your Chrome, Kiro's Chromium, headless |

Every control carries its `configKey`; every string goes to the i18n catalog.

### 9. The Preview tab

The side panel's Browser tab becomes Preview. It keeps the native view
(`browser-view.js`), annotation (`browser-annotate.js`) and `WebPreviewPanel`,
opens only the origins in `dashboard.browser_local_origins`, and answers an
outside URL with "Open this in your Chrome, or ask Kiro". The agent no longer
drives it.

![The Preview tab: a local dev server beside the chat](assets/browser-preview-tab.png)

## Migration plan

There is no switch and no period with two stacks. Each PR replaces what it
touches outright, and no release is cut until all of them have merged, so no
user ever runs a build where the old path and the new one coexist.

| PR | Lands | Removes in the same PR |
|---|---|---|
| 0 | this RFC | -- |
| 1 | Harness install, daemon supervision, relay, environment fence, sandbox leaves; Settings first-run steps 1-2 and the status card | the `playwright-cli` installer, launcher, `show` relay and address-bar launcher in `browser_cli/` and `dashboard/handlers/browser_view_relay.py`; the Settings sections that served them |
| 2 | `browser` tool on Harness; the prompt section and the 8 skill trees rewritten | the command bus routes, `browser-ops.js`, `browser-control.js`, `browser-agent-channel.js`; every `playwright-cli` mention in prompt and skills |
| 3 | live card, stream, adaptive ladder, password mask; Settings live-view section | the panel surfaces that showed the agent's browser |
| 4 | takeover | the side panel's agent browser; the Browser tab becomes Preview |

`use_builtin_browser` and `browser_view_port` go with the PR that removes their
reader. Each removal PR carries `Breaking:` and a `Reader:` list
([take-away-changes](../system-specs/common/take-away-changes.md)). Between
merges `main` may lack a browser for the agent; that is acceptable only because
nothing ships from it until PR 4 is in.

## Backward compatibility

- The release that carries this work is the first and only one that changes
  browsing. It has no `legacy` path to fall back to.
- `use_builtin_browser` and `browser_view_port` in an existing `config.json` are
  dropped by the config coercer with one warning.
- An installed `playwright-cli` prefix is left on disk and reported in
  `kirocrew doctor`; it is not deleted.

## Security considerations

| Risk | Control |
|---|---|
| The relay can reach the user's whole Chrome | the agent gets only the listed ops, on tabs it created; the relay never attaches the user's tabs |
| Harness sends data out | five environment fences, a pinned version, a test on the fence; no CLI, MCP server or update check runs |
| Agent claims input | `/api/browser/input/*` refuses the internal secret |
| Agent types a password | `type` refuses password fields; the frame masks them |
| Frames show logged-in pages | OWNER websockets only; never written to disk except as a requested `screenshot` |
| Another local process uses the relay | loopback, ephemeral port, URL given only to the daemon |
| Agent reads Kiro's profile cookies | `HIDDEN` leaf in every sandbox mode |
| Local addresses | `navigate` keeps the public-only gate plus `browser_local_origins` |

**Accepted residual:** on the user's Chrome, CDP is browser-wide. A bug in the
relay's tab filter would let the agent act on a tab the user opened. Tests pin
the filter, and the card always shows which tab is live.

## Alternatives considered

- **Harness's MCP server directly.** It exposes `browser_js`, `browser_cdp` and
  `http_get`, which bypass our approval model.
- **Harness's own events for frames.** `drain_events` empties one shared buffer;
  frames would push out the events Harness itself waits on. An upstream
  subscribe API would work but waits on their release.
- **Vendoring Harness.** Avoids a dependency but needs about 2,500 lines
  (`daemon.py`, `helpers.py`, `_ipc.py`, `paths.py` and 26 functions of
  `admin.py`) plus its interaction notes, and a manual merge per upgrade.
- **Browser Use Cloud live view.** Hosted, paid, and the page runs on a third
  party's machine.
- **Keep the native panel and fix it.** It still cannot carry the user's logins
  or run headless, and the agent would keep two stacks.

## Open questions

1. Should the card's Replay survive a page reload (frames on disk) or stay
   memory-only?
2. On first launch after the update, should the chat show a one-time card that
   points at Settings -> Browser, or rely on the first live card's "Allow" step?
