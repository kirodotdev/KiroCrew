---
title: Live browser in chat -- Browser Harness behind a gateway CDP relay, watched and taken over from the transcript
status: draft
author: bolichen97
created: 2026-10-31
last-audited: 2026-10-31
audited-at: 0301f8b34f
doc-pr:
implementation-prs: []
tracking-issues: [22561]
supersedes: []
superseded-by: []
---

# RFC: Live Browser in Chat

**Status:** `draft`. Nothing here is on main. Main was read at `0301f8b34f`.
A prototype of the whole design is on the `feat/browser-live-harness` branch,
read at `278f08eec6`. This document records the decisions that prototype makes
so a maintainer can accept or reject them before any of its code lands. It is
`draft`, not `in-progress`, because the decision has not been made. When its last
implementation PR merges, this document supersedes
[rfc-playwright-cli-migration.md](rfc-playwright-cli-migration.md).

## Summary

An agent that browses today does it out of sight. The page is in a side panel
the person has to open, or in a headless browser nobody sees, and stepping in
means leaving the chat. This RFC proposes one agent browser, shown where the
person already looks:

1. **One driver.** [Browser Harness](https://github.com/browser-use/browser-harness)
   (MIT), pinned and run locally by the gateway, drives a Chromium-family
   browser over CDP (the Chrome DevTools Protocol). Its telemetry and its cloud path are forced off.
2. **One CDP connection, owned by the gateway.** A loopback relay holds the only
   connection to the browser. Harness connects to the relay as if it were the
   browser. The relay takes the screencast and the person's input on sessions
   Harness never sees.
3. **The page lives in the chat.** Each browsing turn shows one live card. A
   click on the picture hands mouse and keyboard to the person, on desktop or
   phone. Esc hands it back and the agent carries on by itself.
4. **Quick asks.** When the agent needs a sign-in, a code or a "not a robot"
   check, it asks. The person answers in a small sheet in the chat, and the
   gateway types the values into the page. The agent gets back one word, never
   the value.
5. **Replay.** A finished card plays the turn back, with each click, key and
   run of typing marked on a timeline.
6. **Playwright-CLI goes.** The `browser` tool keeps its name, governance check
   and URL gate. The shell path, its side-panel view and its installer are
   removed in the same release. There is no flag to bring them back.

## Motivation

### What happens today

Read at main `0301f8b34f`:

| Path | What the agent does | What the person sees |
|---|---|---|
| `browser` MCP tool (`mcp_tools/browser.py`) | posts ops over a command bus to the Electron view in the side panel | the page in the side panel's Browser tab, if they open it |
| `playwright-cli` (`browser_cli/`, 16 modules) | runs shell commands: `open`, `click`, `snapshot`; `attach --extension` to reach the person's Chrome | a relayed `show` page in the side panel; Escape releases control, and the agent is not told |
| Prompt and skills | `config/prompt.md` names `playwright-cli` on 7 lines; 7 builtin skills name it | nothing |

The problems that follow:

- **Invisible work.** On a remote or headless gateway the only view is a
  relayed page in a panel. Nothing in the chat says what the agent is doing on
  the page.
- **No clean way to step in.** The person can click inside the `show` page, but
  the agent keeps going and is never told it was interrupted.
- **Their logins cost a click each time.** Reaching the person's own Chrome goes
  through the Playwright extension: a click or a pasted token per attach. The
  decision to attach lives in prompt text.
- **Sign-ins have no safe path.** The agent either types a password itself or
  stops and asks in prose. Issue
  [#22561](https://github.com/kirodotdev/KiroCrew/issues/22561) asks for a
  browser with the person's logins that they can watch and take over.
- **Two stacks.** The native panel and the shell CLI do the same job with
  different gaps. The panel has no logins and no coordinates; the CLI has no
  in-chat view.

## Goals

- One agent browser on macOS, Windows and Linux, headed or headless, local or
  remote gateway.
- The person sees the agent's page in the chat while it works.
- One click hands control to the person; one key hands it back; the agent
  resumes without a "continue".
- A value the person types into a quick ask reaches only the page field it was
  meant for. The gateway never hands it to the agent, a log or a file. What the
  site itself shows afterwards is page content (D1).
- No usage data leaves the machine and no cloud browser is reachable.

## Non-goals

- **Browser Use's cloud, proxy or agent library.** The cloud runs the page on a
  third party's machine. The agent library is an LLM agent inside ours.
- **Arbitrary code through Harness.** Its `run.py`, `browser_js`, `browser_cdp`
  and `http_get` stay unreachable. The agent gets the tool's ops and nothing
  else.
- **Replacing the Playwright library.** The E2E suite and the recording skills
  keep using it. Only the `playwright-cli` agent path goes.
- **A password manager.** The sheet does not read or fill a saved password.

## Design

```
browser ◀──one CDP connection──▶ gateway relay ◀── Harness daemon (as if it were the browser)
                                      │
                                      ├── screencast session ──▶ live card in the chat
                                      └── input session ◀── takeover and quick asks
```

The spec that owns each part is
[`browser.md`](../system-specs/modules/browser.md) on the branch. This section
names only the shape a reviewer has to agree to.

### 1. The driver and the relay

- The gateway installs `browser-harness==0.1.13` into its own virtual
  environment from a hash-locked requirements file. The install path is
  read-only to the agent's sandbox.
- The daemon starts with an environment built by one function and pinned by one
  test: three telemetry switches forced off, the telemetry host pointed at a
  dead loopback port, the update check off, and every cloud key removed even
  when the gateway's own environment carries it.
- The relay listens on `127.0.0.1:0`. Every path carries a per-start secret
  that only the daemon is given; a request without it is 404. It renumbers the
  daemon's requests so its own never collide, keeps its screencast and input
  sessions private, and records each tab the agent creates. The agent can list
  and switch only those tabs, and only those are closed when the session ends.

### 2. Which browser

| Host | Browser |
|---|---|
| macOS, Linux desktop, Windows with Chrome | the person's own Chrome, after a one-time `chrome://inspect` switch and Chrome's "Allow" |
| Windows without Chrome | Edge |
| Linux with no display (a server, a remote Linux gateway), or Chrome's debugging policy off | Kiro's own Chromium, headless, with its own profile |

Kiro's Chromium speaks CDP over a pipe on macOS and Linux, so it opens no port.
On Windows it keeps a loopback port (decision D5). The person can pick a
browser by hand in Settings.

### 3. The live card

- One card per browsing turn. It shows the address, which browser, the current
  step, and the page at up to 60 frames per second. Chrome sends a frame only
  when the page changes; when it stops, the gateway asks for one sharp still. An
  off-screen card stops the stream.
- The stream adapts to the link on two axes: frame rate (60 down to 5 fps) and
  picture (1280 px down to 640 px). It steps down at once. Frame rate climbs
  back after two calm seconds, picture after five.
- Frames go only to the dashboard owner's websockets.
- **Takeover.** A click on the picture claims input. The claim route refuses the
  agent's own credential, so only the person can take control. While they hold
  it, an agent op waits in the tool (polling in short calls, up to ten minutes),
  then runs. An op the takeover cut short is not re-run; the agent is told to
  look at the page again first. Esc or Hand back returns control, with an
  optional one-line note ("Done", "Wrong site", or free text) that the agent
  receives as the person's own words.
- **Phone.** On a phone the card opens a full-screen sheet with the same
  takeover. On desktop it can grow into the side panel.

### 4. Quick asks

The agent calls `browser` op `ask` for one thing it must not handle itself: a
sign-in, one short text field, a one-time code, a "not a robot" check, or a
confirm before a purchase. The person sees a sheet in the chat:

- **A standalone sign-in sheet.** The sheet names the site's origin, read-only,
  and has its own username and password boxes. The person does not hunt for
  fields in the small picture.
- **The gateway fills the page step by step.** For a sign-in the gateway finds
  each field itself, screen by screen (username, then password on the next
  screen), and stops the moment the tab leaves that origin. A passkey, a puzzle
  or a "not a robot" check goes to takeover instead.
- **Bound before, re-checked after.** A field ask is bound, when it is made, to
  the page's top frame, its origin and each target field. A sign-in ask is bound
  to the origin alone, and each field is found and checked on its own screen.
  Before writing, the gateway checks again: same document, same origin, the
  field still attached, visible, not covered and of its type. Anything else answers `origin_changed` and
  nothing is written. Page reads for these checks run in an isolated world (a
  separate script context the page's own code cannot touch), so page script
  cannot fake them.
- **Values held for one ask, never saved by the gateway.** The answer arrives in
  one owner-only POST. The gateway writes it into the bound field and drops it.
  It is not logged, audited, recorded in the replay, kept in card state or
  returned to the agent; the agent gets one outcome word such as `submitted`,
  `declined` or `timed_out`. The person may tick "Remember on <site>" to keep a
  username, name or email in their own browser's storage; passwords and codes have no
  slot there.
- **Iframes.** A field inside a same-site or cross-origin frame (an embedded
  sign-in form) can be bound and filled. A see-through frame over a decoy is
  refused.

### 5. Replay

The gateway keeps one recording per turn: frames at up to 5 fps, plus each
action's label and time. Before a frame is written, every secret field is
painted out: password boxes, one-time-code fields, and every field a quick ask
filled. A frame whose mask cannot be computed is dropped, not kept. The live
card's picture covers the same fields. The finished card's Replay shows a timeline with one marker per action,
play and pause, step by step, and 1x, 2x and 4x.

### 6. The tool

`browser` keeps its name, its call-time `capabilities.browse` check and its
navigate gate (public HTTP(S), plus the operator's exact loopback origins). Its
ops are `navigate`, `back`, `snapshot`, `click` (a ref or x,y; `clicks: 2`),
`drag`, `hover`, `scroll`, `type`, `press_key`, `select_option`, `wait_for`,
`console`, `screenshot`, `tabs`, `ask` (section 4) and `reflex` (a fast loop of
the other ops, only with the optional decision model, D4). `type` and printable
`press_key` refuse a password field. The navigate gate runs twice: on the URL
asked for, and on the URL the page actually landed on.

### 7. Settings -> Browser

The gateway installs Harness on its own at start, in the background, when it is
missing or out of date. It adds Kiro's Chromium only when that browser would be
used. The page shows the browser in use, its connection, Install and
Redownload, a locked "No usage data, no cloud browser" line, and the live view's
quality and frame-rate caps. It replaces the `playwright-cli` installer.

## Decisions

Each decision is the branch's default. Each trade-off is what the decision gives
up.

| # | Decision | Trade-off |
|---|---|---|
| D1 | **No post-fill masking.** After a quick ask fills a field, the gateway adds no mask over the page. Sites already draw password boxes as dots. The snapshot outline leaves out a filled field's value, and `screenshot` is refused while a filled value is still on the page. | Anything the site itself shows afterwards, such as the email echoed on the next screen, is ordinary page content: the agent can read it, and the replay records it. |
| D2 | **The browser is on by default.** `capabilities.browse` defaults to allow, and the gateway installs Harness at start without a click. | A fresh install downloads a Python package (and, on a host with no Chrome, a Chromium build) the person did not ask for by name. |
| D3 | **One governance row turns the whole browser off.** A deny on `capabilities.browse` is checked in the tool and again, fail-closed, at every browser route, the engine start and every helper call. It never falls back to another driver. The first refusal stops a running browser. | An organisation cannot allow the live view but forbid driving, or the reverse; it is one switch. |
| D4 | **The decision model is optional and off by default.** A decision model is a small, fast model, separate from the chat model, that answers narrow questions. An owner may arm one to pick an element from a description, check a step, or run a fast `reflex` loop. Providers come from one closed registry; nothing is sent until the owner consents, and `capabilities.decisions` withdraws it with the rest of that feature. With it off, every op runs exactly as listed above. | Two paths per op (with and without the model) to test and keep equal. |
| D5 | **Windows keeps a loopback debugging port for Kiro's Chromium.** Python cannot hand a Windows child the pipe file descriptors CDP's pipe mode needs. | Any local process on that Windows host can find the port and drive Kiro's Chromium. The person's own Chrome is not affected: Chrome asks "Allow" for every new connection. |
| D6 | **Playwright-CLI is removed outright, with no flag.** The CLI path, its `show` relay, the native-panel command bus, and the `dashboard.use_builtin_browser` and `dashboard.browser_view_port` keys go in the same release that adds the new stack. | No fallback if Harness breaks on some host; the fix is a new release. An installed CLI is left on disk and reported by `kirocrew doctor`, not deleted. |

## Security model

What the gateway trusts, and what it does not:

| Actor | Can | Cannot |
|---|---|---|
| The agent | call the `browser` tool's ops on tabs it created; read page text and the outline | claim input, answer an ask, reach the relay or the daemon socket, read Kiro's profile, read a value typed into an ask |
| The page | run its own script; put text in front of the agent | fake the ask's checks (isolated world); send the agent past the navigate gate |
| Other local processes | find the relay's port | open the relay without its secret; on macOS and Linux, reach Kiro's Chromium at all |
| The dashboard owner | watch, take over, answer asks, replay | -- |

| Threat | Control |
|---|---|
| The relay reaches the person's whole Chrome | the agent's ops act only on tabs the relay recorded as the agent's; the person's tabs are never attached |
| Harness sends data out | the pinned version, the environment fence and its test; no Harness CLI, MCP server or update check runs |
| The agent claims input or answers an ask | the input, live and ask-answer routes refuse the internal credential (403) and require the owner |
| The agent types or spells a password | `type` and printable `press_key` refuse password fields, through open shadow roots |
| An ask writes into the wrong place | binding at ask time, re-check before write, origin pinned, opacity check on frames |
| A public page redirects to a private address | the navigate gate runs again on the committed URL; a refused landing blanks the tab |
| Frames show logged-in pages | owner websockets only; on disk only as a requested `screenshot` or a replay frame with secret and quick-ask fields painted out |

**Accepted residuals.** Page text is untrusted input and can sway the agent
(prompt injection); the navigate gate bounds where a navigation can land, but
not what the agent decides to do on a public page. CDP on the person's Chrome is
browser-wide, so a bug in
the relay's tab filter would reach their tabs; tests pin the filter and the card
shows which tab is live. DNS rebinding (a public name resolving to a private
address) is not seen, because names are not resolved. D1 and D5 above.

## Alternatives considered

- **Keep `playwright-cli` and add a card.** Its live view is a separate `show`
  server; the card would be a second view over a process the gateway does not
  own, and takeover still would not tell the agent.
- **Fix the native panel.** It still has no logins and no headless or remote
  mode, and the agent keeps two stacks.
- **Harness's own MCP server.** It exposes `browser_js`, `browser_cdp` and
  `http_get`, which bypass the approval model.
- **Frames from Harness's event buffer.** That buffer is shared and emptied on
  each read; frames would push out the events Harness waits on. Hence the
  relay's private sessions.
- **Vendoring Harness.** About 2,500 lines plus a manual merge per upgrade,
  against a pinned, hash-locked install.
- **A hosted live view.** Paid, and the page runs on someone else's machine.
- **Mask the field after a fill (the opposite of D1).** Painting over a field in
  the picture does not stop the page from echoing the value elsewhere, and a
  site's password box is already dots.
- **Keep `playwright-cli` behind a flag (the opposite of D6).** Two stacks, two
  prompt sections and two sets of skills, for a path the new one covers.

## Rollout

- **PR 0:** this document, docs only. It must merge to main and be accepted
  first, because the First Principles review lane reads RFC status from the
  base commit.
- **Then a stack of implementation PRs,** each under 1 MB of diff so every
  review lane reads it whole, each green on its own: driver and relay; the tool
  on Harness with the prompt and skills rewritten; the live card and stream;
  takeover; quick asks; replay; Settings; the removal of what this replaces.
- **No release between the first and the last.** Main may have a gap in the
  agent browser while the stack lands. Nothing automatic enforces this: the
  maintainer who accepts this RFC holds the release cut until the last PR
  merges, so no user runs both stacks or neither.
- The removal PR carries `Breaking:` and a `Reader:` list
  ([take-away-changes](../system-specs/common/take-away-changes.md)) and flips
  [rfc-playwright-cli-migration.md](rfc-playwright-cli-migration.md) to
  `superseded`, filling `superseded-by` there and `supersedes` here.

### Backward compatibility

- `dashboard.use_builtin_browser` and `dashboard.browser_view_port` in an
  existing `config.json` still load, log one warning naming them, and are
  dropped on the next save.
- An installed `playwright-cli` prefix is left on disk and reported by
  `kirocrew doctor`.
- Skills and prompts that name `playwright-cli` are rewritten in the same stack;
  a user skill that shells out to it stops working (it is no longer installed).

## Evidence

- **Sign-in coverage.** The author ran a read-only survey of the sign-in pages
  of the top 100 sites by traffic against the prototype: the quick ask handled
  76 of 100. The rest need a passkey, a puzzle or a page the sheet cannot bind,
  and go to takeover. The per-site table is not in this repository.
- **CI pre-flight.** The whole prototype ran through CI as draft PR
  [#26129](https://github.com/kirodotdev/KiroCrew/pull/26129) (head
  `50416b6450`, since closed). 78 checks passed. The 9 that did not are the AI
  review lanes and their roll-ups, which need this document on main first.
- **Frame rate.** In the author's relay spike on macOS, with a cap of 60, the
  stream measured 57.6 fps headed and 57.8 fps headless. A still page sent no
  stream frames on macOS, Linux and Windows.

## Open questions

1. **The recording write queue has no bound.** Each session's writes wait in a
   queue with no cap (`LiveRecorder._submit` in `recording.py`), so a slow disk
   grows gateway memory. Proposed: a fixed cap that drops the oldest frame and
   marks the gap in the timeline.
2. **Cleanup can delete the next session's recording.** Deleting a session's
   recordings waits for its queued writes, then removes the directory by session
   key (`delete_session`). A new session that reuses that key and starts
   recording during the wait loses its recording. Proposed: tie the delete to the
   recording generation it started from, not the key alone.

Both are follow-ups to the replay part and block nothing else.
