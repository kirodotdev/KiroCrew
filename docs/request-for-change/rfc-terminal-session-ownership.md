---
title: Terminal session ownership - one browser owner per PTY
status: draft
author: Pearce Kieser, with Codex
created: 2026-09-01
last-audited: 2026-09-01
audited-at: 1ee69f225c
doc-pr:
implementation-prs: []
tracking-issues: [7638]
supersedes: []
superseded-by: []
---

# RFC: Terminal session ownership - one browser owner per PTY

Everything measured below was read at `1ee69f225c`. Paths are repo-relative.

## Summary

Give each terminal session one server-authoritative browser owner. An owner
attaches with a tab-local identity and a rotating resume credential. Every
accepted reconnect or transfer advances a connection generation, and the
backend rejects input and resize frames from every older generation.

Keep terminal IDs, owner identity, and credentials in `sessionStorage`. Keep
only harmless panel preferences in `localStorage`. A popout receives ownership
through an explicit, acknowledged, single-use transfer instead of learning live
terminal IDs from an app-wide storage event.

This preserves the Kiro Crew behaviors that a smaller terminal design omits:

- reload in the owning tab reconnects to a retained PTY;
- backend scrollback is replayed after reconnect;
- the whole tabbed terminal moves to a popout and back; and
- a crashed popout has a bounded recovery path.

It also adopts the useful constraint from T3 Code's smaller design: a terminal
surface owns the terminal it created. Ordinary browser tabs neither discover nor
attach to another tab's live PTYs.

This work lands as a stack. PR 0 is this RFC. The first implementation PR is an
independent correctness fix that fences displaced WebSocket handlers. The next
PR changes ownership end to end: attach protocol, tab-local state, and explicit
popout transfer. A separate PR adds predecessor crash recovery after the core
ownership protocol is proven. A final PR completes typed lifecycle handling,
observability, and cleanup.

## Decision

Kiro Crew will enforce this invariant:

> A terminal session has exactly one active browser owner and one current
> connection generation.

The server is the authority. Browser storage and cross-window messages carry
recovery material, but they cannot grant ownership by themselves.

The terminal WebSocket will use an authenticated attach handshake before any PTY
is created, replayed, read, resized, or written. A successful handshake returns a
typed `ready` outcome and fresh recovery material. A failed handshake returns a
typed, non-retryable outcome when retrying the same request cannot succeed.

The reconnect policy remains bounded exponential backoff for transport failure.
It does not retry ownership conflict, session expiry, terminal disablement, or
spawn failure. Kiro Crew never replays keyboard input. Backend scrollback replay
remains safe.

## Motivation

### The observed delay was ownership loss, not terminal latency

Issue [#7638](https://github.com/kirodotdev/KiroCrew/issues/7638) records a
controlled reproduction:

| Event | Local time | Elapsed from Enter |
|---|---:|---:|
| Enter reached the gateway WebSocket | 12:29:40.087 | 0 ms |
| PTY output began | 12:29:40.089 | 2 ms |
| Response and prompt completed | 12:29:40.128 | 41 ms |
| The watched browser displayed replayed output | 12:31:28 (browser observation) | 108 s |

One newly created terminal received five WebSocket attachments in less than
500 ms. Packet capture showed no loss, retransmission pattern, or shell delay
that explained the visible wait. The PTY completed in milliseconds; output went
to a different browser context and appeared in the watched context only after a
later reconnect replayed scrollback.

### Current browser state distributes live resource identities

`website/src/hooks/useBottomTerminal.ts:11-15` persists terminal tab IDs in
`localStorage`. Its storage listener at `:104-113` adopts that complete state in
every other same-origin window. This is useful for the popout, but it also gives
ordinary tabs the identifiers of live PTYs.

Each window has a separate JavaScript realm. The module-level maps in
`website/src/utils/terminalRegistry.ts:5-8` and `:162` deduplicate a connection
inside one realm only. They cannot see a connection created by another window.
Every window that mounts the shared tab list therefore calls
`ensureTerminalConnection` (`:365-379`) for the same session.

The popout contract acknowledges the intended replacement behavior:
`website/src/utils/terminalPopout.ts:24-34` says both windows share tab
membership through `localStorage`, and that the backend replaces a WebSocket
during handoff. Timing and liveness beacons suppress overlap on the expected
path, but they do not make the popout the only other window that knows the IDs.

### Current backend replacement is asymmetric

The backend replays scrollback and then assigns `existing.ws = ws` at
`src/kiro_crew/dashboard/handlers/terminal.py:681-703`. PTY output is sent only
to the socket currently stored in `sess.ws` (`:901-918`).

Disconnect cleanup correctly checks identity before clearing the current socket
(`:1008-1016`). The input and resize paths do not perform the matching check.
Any displaced handler that is still draining its socket remains able to write binary input
at `:938-965` or resize the PTY at `:975-1001`.

The result is an asymmetric terminal: one context submits input while another
context receives output. Reconnect backoff then changes which context owns
output without changing which visible context the operator is using.

### Failure states are collapsed into transport failure

The frontend reconnects every close with exponential backoff
(`website/src/utils/terminalRegistry.ts:292-357`). That is correct for a broken
network path. It is wrong when the server has rejected ownership, the PTY has
exited, the orphan timeout has expired, or the gateway restarted.

Those outcomes need different actions. Repeatedly opening another WebSocket
does not resolve an ownership conflict and recreates the same contention this RFC
removes.

## Goals

- Enforce one active browser owner and one current connection generation per
  terminal session.
- Make input, resize, output, and disconnect cleanup obey the same generation.
- Preserve reload recovery for the owning tab and backend scrollback replay.
- Keep ordinary same-origin tabs from discovering or attaching to live PTYs.
- Transfer popout ownership explicitly and only after the target is ready.
- Recover from a crashed transfer target without allowing a live target to be
  preempted.
- Distinguish retryable transport failure from terminal lifecycle and ownership
  outcomes.
- Preserve the existing bounded reconnect backoff and manual reconnect action.
- Land in independently useful and independently abandonable PRs.
- Keep credentials out of URLs, logs, metrics, and audit resources.

## Non-goals

- Persisting PTY processes across a gateway restart.
- Sharing one interactive terminal concurrently across multiple viewers.
- Replaying unacknowledged keyboard input after reconnect.
- Building a general browser leader-election or distributed-lock framework.
- Changing the terminal's authentication, origin validation, sandbox posture,
  shell selection, or scrollback size.
- Making a terminal resume across a complete browser restart when its tab-local
  recovery credential is gone.
- Allowing an arbitrary tab to take a live terminal without an explicit transfer
  or an eligible crash-recovery capability.
- Replacing xterm.js or the existing reconnect timing.

## Design

### 1. Separate preferences from live ownership state

`localStorage` retains only cross-window-safe preferences:

- panel height and width;
- dock position;
- font and visual preferences; and
- other values that do not identify a live backend resource.

Each browser tab stores the following in `sessionStorage`:

```text
client_id
terminal_id
active_tab_id
tab_order
cwd
resume_credential
connection_generation
```

`client_id` is a random tab identity, not a credential. Browsers clone
`sessionStorage` when a tab is duplicated, so neither `client_id` nor storage
is authoritative. A duplicated tab begins with copied recovery material, but
the server accepts only one connection generation. The winner receives the next
credential; the loser receives `ownership_conflict`.

A gateway restart already destroys every in-memory PTY. The storage migration
therefore preserves preferences but does not migrate terminal IDs out of the old
app-wide record. A new frontend starts new terminals instead of interpreting
stale shared IDs as resumable shells.

### 2. Create a terminal reservation before opening its WebSocket

`POST /api/terminal/sessions` becomes the creation authority. It accepts the
tab's `client_id` and optional working directory, reserves a terminal ID, and
returns:

```json
{
  "session_id": "opaque-id",
  "attach_credential": "opaque-secret"
}
```

The reservation has a bounded expiry and does not consume a PTY process until
the WebSocket attach succeeds. Expired, unattached reservations do not count
against the live PTY limit indefinitely.

The server, not the browser, mints the session ID. A client must
explicitly choose `create` or `resume`; an unknown ID on a resume path returns
`session_expired` and never silently starts a new shell.

### 3. Negotiate ownership inside the authenticated WebSocket

The WebSocket URL continues to contain only the terminal ID. After the existing
cookie authentication and Origin check succeed, the client sends one JSON
control frame before any other frame:

```json
{
  "type": "attach",
  "mode": "create",
  "client_id": "tab-id",
  "credential": "opaque-secret"
}
```

`mode` is one of `create`, `resume`, or `transfer`. The server does not spawn a
PTY, replay scrollback, assign `sess.ws`, or accept input before validating this
frame. The attach frame has a short timeout and a strict size bound.

A successful attach advances `generation` and returns:

```json
{
  "type": "ready",
  "resumed": true,
  "generation": 7,
  "resume_credential": "next-opaque-secret"
}
```

The client writes the successor credential to `sessionStorage` and acknowledges
that generation. Rotation is a two-step commit: until the acknowledgement, the
server retains enough pending state to recover when the `ready` frame or its
acknowledgement is lost. At no point are two generations current. A retry
recovers an unacknowledged successor only after the pending socket is gone.

Credentials are single-owner capabilities with at least 256 bits from the
operating system's cryptographic random source. The server stores only digests
and compares them in constant time. During an unacknowledged rotation it keeps
the predecessor and successor digests. After the pending socket closes, either
credential completes one recovery attempt and atomically invalidates the other.
Every acknowledged resume invalidates the predecessor. Knowledge of a terminal
ID, a copied `client_id`, or an expired credential is insufficient to attach.

### 4. Scope management operations to the owner

The ownership boundary applies to HTTP management routes as well as the
WebSocket. `GET /api/terminal/sessions` does not return live terminal IDs to an
ordinary dashboard tab. A caller either presents its owner credential and sees
only sessions it owns, or receives configuration and capacity information with
no live resource identities.

Closing a terminal is also an owned operation.
`DELETE /api/terminal/sessions/{id}` requires the current owner credential and
generation. The server validates both before terminating the PTY. A stale
duplicated tab, displaced socket, expired credential, or predecessor recovery
capability cannot close the current owner's terminal.

Credentials remain in authenticated request bodies or headers that are
redacted by the existing HTTP logging boundary. They never appear in URLs.

### 5. Fence every client-to-PTY side effect by generation

Each accepted handler captures its generation. Replacement, binary input, and
resize share one session input lock. The ordering is:

1. acquire the input lock;
2. confirm both `sess.ws is ws` and `sess.generation == generation`;
3. perform the write, resize, or owner replacement; and
4. release the lock.

An input operation that linearizes before replacement is permitted to complete.
Once replacement linearizes, no displaced handler affects the PTY. This closes
the current gap without trying to cancel an executor write already in progress.

PTY output, title, cwd, readiness, pong, and disconnect cleanup retain their
current capture-and-revalidate pattern and add the generation check where
needed. The server closes a displaced socket after advancing ownership.

Phase 1 applies this fence to today's `sess.ws` identity before credentials
exist. That bug fix does not wait for the rest of the RFC.

### 6. Transfer popout ownership explicitly

The current owner asks the backend to prepare a transfer. The backend returns:

- a short-lived, single-use target credential; and
- a predecessor recovery credential that is not immediately eligible.

The source sends terminal metadata and the target credential directly to the
specific popout through a transferred `MessagePort` established from its
`WindowProxy`. The source verifies the expected same-origin target before
sending the credential. `BroadcastChannel`, storage events, and `localStorage`
carry no terminal IDs or ownership credentials, so unrelated dashboard tabs
cannot observe or race the handoff.

The target opens its WebSocket with `mode: "transfer"`. Only after the backend
accepts the target, advances the generation, and returns `ready` does the target
acknowledge the handoff to the source window. The source then disposes its local
connection and xterm view. If the popup is blocked, closes, or never attaches,
the transfer credential expires and the source remains owner.

Returning the panel performs the same protocol in reverse. The operation is a
transfer, not a second attachment.

### 7. Recover a crashed popout without preempting it

This recovery path is not required to establish exclusive ownership or explicit
popout transfer. It lands after those invariants are proven in production. Until
then, a transferred owner that crashes after the handoff completes follows the
existing orphan-reaper path, and the predecessor offers Start New rather than
attempting to reclaim the PTY.

The source retains dormant terminal metadata and the predecessor recovery
credential while the popout owns the PTY. That credential becomes eligible only
when all of these are true:

1. the transfer target's socket is disconnected;
2. the target's ordinary resume grace has elapsed; and
3. no newer transfer or generation has superseded the predecessor.

The current owner's resume credential wins a race during the grace period. The
predecessor is limited to recovering an orphaned transfer and never preempts a
live socket. Browser heartbeat and visibility signals decide when the UI attempts
recovery, but the backend conditions decide whether it succeeds.

A clean return uses a new explicit transfer and invalidates predecessor recovery
state. A complete browser crash with no surviving predecessor or resume
credential leaves the PTY to the existing orphan reaper.

### 8. Model protocol outcomes, not one disconnected state

The server emits machine-readable outcomes:

```text
ready { resumed, generation, resume_credential }
ownership_conflict
session_expired
exit { code, signal }
spawn_failed
terminal_disabled
protocol_required
```

The frontend classifies them:

| Outcome | Automatic retry | User action |
|---|---|---|
| Network close or transient transport error | bounded exponential backoff | Reconnect after exhaustion |
| `ownership_conflict` | no | Focus owner or perform an explicit transfer |
| `session_expired` | no | Start a new terminal |
| `exit` | no | Inspect exit state or start a new terminal |
| `spawn_failed` | no | Inspect error and retry creation |
| `terminal_disabled` | no | Close the unavailable terminal |
| `protocol_required` | no | Refresh the dashboard |

The existing `online` and foreground visibility listeners rearm only a
transport retry chain. They do not rearm terminal outcomes.

The browser never replays input because it cannot know whether a frame reached
the PTY before the connection failed. The backend replays scrollback because
that stream is observational and already retained by the server.

### 9. Keep observability free of credentials and terminal content

SEL events record coarse transitions:

```text
terminal.owner.attach
terminal.owner.resume
terminal.owner.transfer
terminal.owner.reject
terminal.owner.recover
```

Resources include terminal ID, generation, transition, and rejection code.
They never include credentials, terminal input, terminal output, URLs containing
credentials, or cross-window message payloads.

Metrics count concurrent owners rejected, resumes, transfers, recovery
attempts, and protocol outcomes. Terminal ID and `client_id` are forbidden as
metric labels.

## Comparison with current Kiro Crew and T3 Code

| Concern | Current Kiro Crew | T3 Code reference | Proposed Kiro Crew |
|---|---|---|---|
| Session identity | Live IDs copied to every window | Unique per mounted surface | Unique to an owning browser tab |
| Live-state storage | `localStorage` | Component memory | `sessionStorage` |
| Reload recovery | Every window has an attach path | New terminal after reload | Owning tab resumes retained PTY |
| Cross-tab sharing | Automatic | None | None |
| Duplicate prevention | Per JavaScript realm | Per surface realm | Server-authoritative |
| Backend owner | Latest socket wins | Surface owns its socket | Credential plus generation |
| Stale input | Accepted | No expected competing owner | Rejected after generation change |
| Popout | Timing-based socket replacement | No shared live session | Acknowledged transfer |
| Transport retry | Bounded backoff | Bounded backoff | Bounded backoff |
| Ownership error | Looks like disconnect | Absent by construction | Typed, non-retryable |
| Crash recovery | Another window steals ownership | New terminal | Current owner first, predecessor fallback |
| Complexity | High and incorrect | Lowest | Moderate and explicit |

T3 Code is simpler because it does not promise that a browser reload or separate
popout reclaims the same PTY. Kiro Crew copies its ownership invariant without
copying its full lifecycle tradeoff. Dropping all recovery reduces implementation
cost and regresses established terminal behavior.

The proposed design is the smallest compromise that preserves those behaviors:
one owner, one explicit transfer path, and an independently staged bounded
predecessor recovery path. It does not introduce general leases, shared workers,
browser elections, or multi-view terminal fanout.

## Migration plan

Every phase is independently shippable and independently abandonable.

### Phase 1 - fence displaced handlers

Add the current-socket identity check under the input lock for binary input and
resize. Serialize socket replacement through that lock. Add deterministic tests
with two handlers for one PTY.

**Exit criteria:**

- A handler displaced by a reconnect cannot write bytes or resize the PTY.
- A frame already linearized before replacement completes.
- Output and disconnect cleanup still target the current socket.
- No frontend or protocol change is required.

### Phase 2 - migrate ownership end to end

Extend session creation with an attach reservation. Add the initial attach
frame, owner state, generations, credential rotation, typed `ready`,
`ownership_conflict`, `session_expired`, and `protocol_required` outcomes.
Update the bundled frontend to use the protocol, move live state to
`sessionStorage`, and replace popout handoff with explicit transfer. A transferred
owner that crashes after accepting ownership is not reclaimed by its predecessor
in this phase; the existing orphan reaper cleans up the PTY and the predecessor
offers Start New.

These changes land together because separating tab-local IDs from popout
transfer leaves the popout unable to discover its terminals. Sharing a resume
credential through the old app-wide store as an intermediate state recreates
the bug at a more sensitive layer. Predecessor crash recovery is not part of
that atomic transition and lands separately.

**Exit criteria:**

- No PTY operation occurs before an attach succeeds.
- Session listing never reveals another tab's live terminal IDs.
- Deletion requires the current owner credential and generation.
- A stale or duplicated tab cannot delete the winner's PTY.
- Two clients racing copied recovery material produce one owner and one typed
  conflict.
- A same-owner reconnect rotates recovery material and fences its predecessor.
- Loss at each credential-rotation step remains recoverable by one client.
- Unknown resume IDs never spawn a new shell.
- Five ordinary dashboard tabs create one terminal WebSocket for a terminal
  opened in one tab.
- Reload in the owning tab resumes its terminal and scrollback.
- Duplicating the tab produces at most one successful owner.
- Source failure before target attach leaves the source as owner.
- Target failure before backend acceptance leaves the source as owner.
- Target failure after backend acceptance never restores the predecessor in this
  phase; the PTY follows the orphan timeout and the predecessor offers Start New.
- A successful transfer advances generation exactly once.
- Transfer credentials travel only through the intended popout's point-to-point
  `MessagePort`; broadcast channels remain credential-free.
- Popout return uses the same transfer protocol in reverse.
- Harmless visual preferences remain shared.
- Migration from the old record preserves preferences but drops stale live IDs.
- Old frontend code receives `protocol_required` and cannot steal an owned PTY.

### Phase 3 - add bounded predecessor crash recovery

Add the dormant predecessor recovery credential and the three server-side
eligibility conditions in Section 7. The frontend attempts recovery only after
the transferred owner disappears and its ordinary resume grace elapses. This
phase changes crash recovery only; it does not change attach, rotation, storage,
or clean transfer semantics established in Phase 2.

**Exit criteria:**

- A live target cannot be preempted by predecessor recovery.
- The current owner's ordinary resume credential wins during its grace period.
- A crashed, disconnected target is recoverable only after the resume grace.
- A newer transfer or generation permanently invalidates older predecessor
  recovery state.
- Concurrent current-owner resume and predecessor recovery attempts produce
  exactly one owner.
- Loss at each predecessor credential transition leaves at most one eligible
  recovery path.
- A predecessor without an eligible credential offers Start New and cannot
  fall back to terminal-ID discovery.

### Phase 4 - complete lifecycle UX and cleanup

Add `exit`, `spawn_failed`, and `terminal_disabled` handling, visible recovery
actions, protocol metrics, and cross-window browser coverage. Remove superseded
timing and compatibility code.

**Exit criteria:**

- Each typed outcome maps to the retry behavior in the design table.
- Retry exhaustion offers Reconnect and Start New actions.
- Gateway restart and orphan expiry never look like a transient reconnect.
- Tests prove keyboard input is never replayed.
- Browser tests cover reload, ordinary tabs, popout transfer and return, source
  crash, target crash, and PTY exit.
- The dashboard system specification describes the final protocol.

## Backward compatibility

The terminal WebSocket is an internal dashboard protocol served with its matching
frontend bundle. It is not a documented third-party API.

During Phase 2, a terminal session is either legacy or owned for its lifetime.
An owned session never accepts a legacy attachment. A legacy session retains the
Phase 1 stale-writer fence until the gateway restarts.

A gateway restart destroys all PTYs, so the frontend migration does not promise
to recover IDs from the old `localStorage` record. It preserves visual
preferences and starts clean terminal sessions. A stale browser bundle receives
`protocol_required`; the existing dashboard version-change mechanism refreshes
it instead of letting it repeatedly attach.

No persisted backend data format changes. Owner state, credential digests,
generations, reservations, and transfer state live with the in-memory PTY
session.

## Security considerations

- The existing authenticated-session and strict Origin checks remain mandatory
  before attach negotiation.
- Resume and transfer credentials control terminal ownership but do not replace
  dashboard authentication. Both checks are required.
- Listing and deletion enforce the same owner credential and generation
  boundary as attach, input, and resize.
- Credentials travel only in authenticated WebSocket control frames or
  authenticated request bodies. URLs never contain them.
- Server state stores credential digests, not plaintext credentials.
- Audit logs, application logs, metrics, exception text, and browser diagnostics
  must redact or omit credentials.
- Input and resize fencing is a security boundary. A hidden or displaced browser
  context must not execute commands or alter an interactive terminal after it
  loses ownership.
- `sessionStorage` reduces accidental cross-window distribution; it is not an
  XSS boundary. Existing CSP, sanitization, authentication, and Origin controls
  remain responsible for hostile script prevention.
- Popout messages use a target-specific `MessagePort`; broadcast channels never
  carry ownership material. The transport is not authority: possession and
  server validation of a single-use transfer credential authorize the move.
- Predecessor recovery fails closed while the current owner is connected or
  inside its resume grace.
- The terminal remains the operator's intentionally unsandboxed interactive
  shell. This RFC neither grants terminal access to agents nor changes that
  trust model.

## Alternatives considered

### Use T3 Code's component-lifetime model unchanged

Give every mounted surface a new terminal and abandon reload and popout
continuity. This has the smallest implementation and the strongest local
ownership rule. It regresses behaviors Kiro Crew already exposes, so this RFC
uses the invariant without adopting the lifecycle limitation.

### Keep latest-connection-wins and add more browser timing

Add longer disposal delays, stronger heartbeats, or more `BroadcastChannel`
coordination. Rejected because ordinary windows still know the live IDs, browser
events are delayed or dropped, and the backend still cannot distinguish a
valid reconnect from an ownership steal.

### Elect a browser leader

Use `BroadcastChannel`, `localStorage`, a SharedWorker, or a Service Worker to
elect one connection owner. Rejected as the authority: browser lifecycle differs
across platforms, duplicated tabs clone state, and a process crash loses the
election state while the PTY survives. Browser coordination remains useful for
UX but cannot replace server fencing.

### Broadcast output and accept input from every socket

Treat all attached windows as collaborative terminal viewers. Rejected because
multiple interactive writers are surprising and dangerous, resize ownership is
undefined, hidden contexts execute commands, and terminal content is
distributed more broadly than the operator requested.

### Use a fixed owner ID without rotating credentials

Allow any context presenting the same `client_id` to reconnect. Rejected because
duplicated tabs clone `sessionStorage`; either tab retakes ownership
forever. Rotation makes the server choose one current generation.

### Use the session ID as the sole capability

Mint one unguessable session ID and require it with a generation counter for
every attach. Rejected because the ID is copied when a tab is duplicated. Both
tabs retain the same permanent capability: a generation check can choose one
winner for a single race, but the loser can keep presenting the same capability
on later generations and recreate latest-connection-wins livelock. Preventing
that requires rotating or attenuating the capability after each accepted
attach, which is the separate resume credential in this design.

Keeping resource identity separate from authority also lets terminal IDs remain
in WebSocket paths, audit resources, and coarse diagnostics without turning
those locations into secret-bearing surfaces.

### Persist ownership credentials across browser restarts

Store credentials in `localStorage` or durable browser storage. Rejected because
it recreates broad cross-window discovery and lengthens the lifetime of a
capability for a PTY that the gateway itself does not persist.

## Open questions

1. Does the first release offer an explicit "Move terminal here" action for an
   ownership conflict, or only identify that another window owns it?
2. What resume grace and transfer-expiry durations balance frozen mobile tabs
   against popout crash recovery? The implementation PR must justify and
   single-source each value.
3. Does a predecessor recovery capability survive a main-window reload in
   `sessionStorage`, or exist only in memory while the source page remains open?
4. Does PTY exit signaling land with Phase 2's protocol outcomes or remain in
   Phase 4?
5. After reload recovery is reliable, does the current 15-minute orphan timeout
   stay unchanged or become shorter?
6. Does Phase 2 use one protocol version field or infer legacy mode from the
   absence of an attach frame? This RFC recommends an explicit version because
   it has a defined removal point.
