---
title: Terminal session ownership - one browser owner per PTY
status: draft
author: Pearce Kieser, with Codex
created: 2026-09-01
last-audited: 2026-09-09
audited-at: 2188f029d
doc-pr: 7649
implementation-prs: [8863]
tracking-issues: [7638, 5656]
supersedes: []
superseded-by: []
---

# RFC: Terminal session ownership - one browser owner per PTY

Everything measured below was rechecked at `2188f029d`. Paths are repo-relative.

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
independent correctness and authorization fix: every terminal route becomes
dashboard-owner-only, and displaced WebSocket handlers are fenced and audited.
The next PR changes ownership end to end: attach protocol, tab-local state, and
explicit popout transfer. A separate PR adds predecessor crash recovery after
the core ownership protocol is proven. A final PR completes typed lifecycle
handling, observability, and cleanup.

## Decision

Kiro Crew will enforce this invariant:

> A terminal session has exactly one active browser owner and one current
> connection generation.

The server is the authority. Browser storage and cross-window messages carry
recovery material, but they cannot grant ownership by themselves.

Before the generation protocol lands, every terminal management route and the
WebSocket upgrade require the existing dashboard-owner identity. This closes
cross-user listing, replay, mutation, and deletion without treating a terminal
ID as authority. Within that authenticated owner boundary, Phase 1 retains
newest-socket ownership as a compatibility bridge.

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

`loadPersisted` in `website/src/hooks/useBottomTerminal.ts` restores terminal tab
IDs from `localStorage`. Its `storage` event listener adopts that complete state
in every other same-origin window. This is useful for the popout, but it also
gives ordinary tabs the identifiers of live PTYs.

Each window has a separate JavaScript realm. The module-level `registry`,
`readyListeners` and `conns` maps in `website/src/utils/terminalRegistry.ts`
deduplicate a connection inside one realm only. They cannot see a connection
created by another window. Every window that mounts the shared tab list
therefore calls `ensureTerminalConnection` for the same session.

The popout contract acknowledges the intended replacement behavior: the module
comment in `website/src/utils/terminalPopout.ts`, above
`TERMINAL_POPOUT_CHANNEL`, says both windows share tab membership through
`localStorage`, and that the backend replaces a WebSocket during handoff. Timing
and liveness beacons suppress overlap on the expected path, but they do not make
the popout the only other window that knows the IDs.

### Current backend replacement is asymmetric

The backend replays scrollback and then assigns `existing.ws = ws` in
`api_terminal_ws` (`src/kiro_crew/dashboard/handlers/terminal.py`). PTY output is
sent only to the socket currently stored in `sess.ws`.

Disconnect cleanup correctly checks identity before clearing the current socket.
The input and resize paths do not perform the matching check. Any displaced
handler that is still draining its socket remains able to write binary input or
resize the PTY.

The result is an asymmetric terminal: one context submits input while another
context receives output. Reconnect backoff then changes which context owns
output without changing which visible context the operator is using.

### Failure states are collapsed into transport failure

The frontend reconnects every close with exponential backoff (the `ws.onclose`
redial inside `connect` in `website/src/utils/terminalRegistry.ts`, bounded by
`MAX_RETRIES`, `BASE_DELAY_MS` and `MAX_DELAY_MS`). That is correct for a broken
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
- Require the dashboard owner for every terminal management and WebSocket route
  before introducing per-tab credentials.

## Non-goals

- Persisting PTY processes across a gateway restart. Issue
  [#5656](https://github.com/kirodotdev/KiroCrew/issues/5656) asks for that
  durable, first-class terminal; this RFC and PR #8863 relate to it (they fix
  ownership of the in-memory PTY that survives a browser reload) but do not
  complete it, and neither closes that issue.
- Sharing one interactive terminal concurrently across multiple viewers.
- Replaying unacknowledged keyboard input after reconnect.
- Building a general browser leader-election or distributed-lock framework.
- Replacing dashboard authentication or Origin validation. This RFC tightens
  terminal authorization by requiring the existing dashboard-owner identity.
- Changing the terminal's sandbox posture, shell selection, or scrollback size.
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

A `resume` is eligible only after the server has observed that the current
owner socket is disconnected. It never displaces a live socket, even when the
caller presents the current resume credential and generation. A live owner
returns `ownership_conflict` without advancing generation or rotating
credentials. The frontend closes its previous transport before attempting a
resume; a duplicated tab that copied `sessionStorage` therefore cannot use
`resume` as an implicit transfer. A post-disconnect race with copied material
still has one winner because the first successful rotation invalidates the
credential for every loser.

A successful attach advances `generation` and returns:

```json
{
  "type": "ready",
  "resumed": true,
  "generation": 7,
  "resume_credential": "next-opaque-secret",
  "shell": "/bin/bash",
  "fence_shells": {"bash": "/bin/bash"}
}
```

Ownership metadata extends the existing `ready` payload. It does not replace
`shell` or `fence_shells`; clients still need those fields to choose the
interpreter for run-in-terminal handoffs without re-resolving a bare executable
name in a project-controlled working directory.

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

Phase 1 requires the shared dashboard-owner gate for every terminal route:
create, list, WebSocket attach, completion, selection redaction, and deletion.
The gate runs before session lookup or WebSocket preparation. An authenticated
non-owner therefore cannot discover a terminal ID, receive scrollback, displace
the operator, mutate the PTY, enumerate through completion, or terminate a
session.

Phase 2 narrows authority within that dashboard owner. The current WebSocket
may mint a short-lived, single-use management proof bound to its session,
generation, and one operation. The proof is kept only in page memory and is
invalidated by use, expiry, or owner replacement. A copied resume credential
and generation cannot mint one without first winning the attach race.

`GET /api/terminal/sessions` does not return live terminal IDs to an ordinary
tab owned by that user. The caller presents current-socket proofs and sees only
the corresponding sessions, or receives configuration and capacity information
with no live resource identities.

Closing a terminal is also an owned operation.
`DELETE /api/terminal/sessions/{id}` requires a single-use delete proof minted
by the current owner socket. The server validates its session, generation,
operation, expiry, and nonce before terminating the PTY. A stale duplicated
tab, displaced socket, expired credential, or predecessor recovery capability
cannot close the current owner's terminal.

`POST /api/terminal/complete` requires a single-use completion proof minted by
that same current socket before it resolves the session working directory or
returns entries. A duplicated or displaced tab therefore cannot use copied
`sessionStorage` or a retained terminal ID to enumerate filesystem metadata from
the winner's live PTY. Selection redaction does not read session state, but
still remains dashboard-owner-only.

Owner-scoped proofs narrow *attachment*; they must not lock the operator out of
their own machine. A PTY whose owning tab is gone — crashed with no surviving
credential, or a transfer target that failed after acceptance — would otherwise
be invisible and unkillable from every tab for the full orphan timeout while a
runaway shell keeps running. Phase 2 therefore keeps one coarse, proof-free
administrative path behind the same dashboard-owner gate:
`GET /api/terminal/sessions?scope=admin` returns, for every PTY with no live
owner socket, an opaque handle, the shell name, the process state, and the
seconds since its owner disconnected — never a terminal ID usable for attach,
never scrollback, never the working directory — and
`DELETE /api/terminal/sessions/admin/{handle}` terminates that PTY. Both refuse
a session whose owner socket is live (the operator closes that one from its
owning tab, or takes it over with an explicit transfer), both are SEL-audited,
and neither mints or accepts a management proof. The existing orphan reaper is
unchanged; the administrative path only lets the operator act before it does.
Phase 1 does not need this path: its listing already returns every session to
the dashboard owner.

Credentials and management proofs remain in authenticated request bodies or
headers that are redacted by the existing HTTP logging boundary. They never
appear in URLs.

### 5. Fence every client-to-PTY side effect by generation

Each accepted handler captures its generation. Binary input and resize share one
session input lock. Their ordering is:

1. acquire the input lock;
2. confirm both `sess.ws is ws` and `sess.generation == generation`;
3. perform the write or resize; and
4. release the lock.

Owner replacement serializes through the separate reconnect and output
publication locks described below; it never waits for a blocked PTY write. An
input operation that passed its ownership check before replacement may complete.
A queued write revalidates after acquiring the input lock, observes the advanced
generation, and is rejected. The first rejected input or resize from that
handler emits one coarse denial audit and ends the handler. The audit contains
the session ID and stale-owner classification, never terminal input, output,
paths, or credentials.

Phase 1 applies this fence to today's `sess.ws` identity before generations
exist. Reconnect candidates serialize independently from PTY writes. They replay
the bounded scrollback, use a monotonic byte count to catch output produced
during replay, and publish only after reaching the current count. Replay and
ready sends are bounded. A failed, overtaken, or non-converging candidate leaves
the previous owner authoritative. Publication cancels a blocked send to the
displaced socket and rotates the transport lock so old flow control cannot stall
PTY draining. Output, title, cwd, readiness, pong, and disconnect cleanup
capture and revalidate the current socket. Phase 2 adds the generation check to
the same boundaries.

### 6. Transfer popout ownership explicitly

The current owner asks the backend to prepare a transfer. In Phase 2, the
backend returns one short-lived, single-use target credential. It is bound to
the session, source generation, and intended target. It is invalidated when it
expires, is consumed, the source socket disconnects or loses ownership, or a
newer transfer is prepared. A delayed target can therefore never use an older
credential to displace a later owner.

The source sends terminal metadata and the target credential directly to the
specific popout through a transferred `MessagePort` established from its
`WindowProxy`. The source verifies the expected same-origin target before
sending the credential. `BroadcastChannel`, storage events, and `localStorage`
carry no terminal IDs or ownership credentials, so unrelated dashboard tabs
cannot observe or race the handoff.

The target opens its WebSocket with `mode: "transfer"`. Only after the backend
accepts the target, advances the generation, and returns `ready` does the target
acknowledge the handoff to the source window. The source then disposes its local
connection and xterm view. If the popup is blocked, closes before backend
acceptance, or never attaches, the transfer credential expires and the source
remains owner while its socket is current. A source disconnect before target
acceptance revokes the transfer credential; the source may later use ordinary
`resume` only after the server confirms that no live owner remains. If the
target closes after backend acceptance but before
acknowledging the source, ownership has already advanced: Phase 2 leaves the PTY
to the ordinary orphan timeout and the source offers Start New. Phase 3 adds the
bounded predecessor recovery path for that case.

Returning the panel performs the same protocol in reverse. The operation is a
transfer, not a second attachment.

### 7. Recover a crashed popout without preempting it

This recovery path is not required to establish exclusive ownership or explicit
popout transfer. It lands after those invariants are proven in production. Until
then, a transferred owner that crashes after the handoff completes follows the
existing orphan-reaper path, and the predecessor offers Start New rather than
attempting to reclaim the PTY.

Phase 3 extends transfer preparation to mint a predecessor recovery credential.
The source retains that credential with dormant terminal metadata while the
popout owns the PTY. It becomes eligible only when all of these are true:

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
ready { resumed, generation, resume_credential, shell, fence_shells }
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

### Phase 1 - authorize terminal access and fence displaced handlers

Call the shared `is_owner_dashboard_request` gate (through
`require_owner_dashboard_request`) on every terminal route and WebSocket upgrade
before session lookup or WebSocket preparation. Within that owner boundary the
newest accepted connection is authoritative: add the current-socket identity
check under the input lock for binary input and resize. Keep replacement
independent from potentially blocked PTY writes, bound replay, send the
displaced socket one coarse `error` frame before closing it, and audit the
first stale mutation before ending the displaced handler; later stale frames
from that socket are never read, so one denial audit is the per-socket
ceiling. Advisory `title`, `cwd`, and `pong` frames re-confirm ownership under
the transport lock and are bounded, so they neither reach a displaced socket
nor park the poller behind one. Add deterministic tests with two handlers for
one PTY and with an authenticated non-owner.

Phase 1 does not mint a per-session reconnect credential. The owner gate
decides *who* may contend (the dashboard owner alone); *which* of that owner's
windows holds the PTY is decided by the newest connection. A resume credential
that can also refuse a live displacement is the Phase 2 protocol below.

**Exit criteria:**

- An authenticated non-owner cannot create, list, attach, replay, complete,
  redact, delete, or displace a dashboard owner's terminal.
- A handler displaced by a reconnect cannot write bytes or resize the PTY.
- A frame already linearized before replacement completes.
- A blocked PTY write or displaced-socket send cannot block reconnect forever.
- A candidate cancelled after publication is detached again rather than left
  as a dead owner the orphan reaper never sees.
- Output produced during replay is delivered before owner publication, or the
  candidate is rejected and the previous owner remains authoritative.
- The displaced socket receives exactly one coarse `error` frame, carrying no
  session identifier or terminal content.
- Multiple queued stale input or resize frames produce one sanitized denial
  audit for that displaced handler.
- Output, advisory control frames, and disconnect cleanup still target the
  current socket.
- No frontend or protocol change is required.

### Phase 2 - migrate ownership end to end

Phase 2 adds per-terminal create, resume, and transfer capabilities inside the
Phase 1 dashboard-owner boundary. A resume credential may reconnect only after
the current owner socket has disconnected; it cannot displace a live owner.
Live takeover requires an explicit generation-bound transfer, and every
successful attach rotates the credential and advances the generation.

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
- Listing returns a live ID only with a fresh single-use proof minted by that
  session's current socket.
- Deletion requires a fresh single-use delete proof minted by the current
  socket.
- Completion requires a fresh single-use completion proof before any
  working-directory probe or entry listing.
- A stale or duplicated tab cannot delete the winner's PTY.
- A stale or duplicated tab cannot enumerate through the winner's live PTY.
- The dashboard owner can list and terminate every PTY with no live owner
  socket from any tab, without a proof, and without receiving an attachable
  ID, scrollback, or working directory; the same path refuses a PTY whose
  owner socket is live.
- Two clients racing copied recovery material produce one owner and one typed
  conflict.
- A resume while the current owner socket is live returns
  `ownership_conflict` without changing generation or credentials.
- A reconnect after confirmed owner disconnection rotates recovery material and
  fences its predecessor.
- Loss at each credential-rotation step remains recoverable by one client.
- Unknown resume IDs never spawn a new shell.
- Five ordinary dashboard tabs create one terminal WebSocket for a terminal
  opened in one tab.
- Reload in the owning tab resumes its terminal and scrollback.
- Duplicating the tab produces at most one successful owner.
- Popup creation failure before target attach leaves the source as owner.
- Target failure before backend acceptance leaves the source as owner.
- Source disconnection or owner replacement before target acceptance revokes
  the pending transfer credential.
- Preparing a newer transfer invalidates every older target credential.
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

Extend transfer preparation to mint the dormant predecessor recovery credential
and add the three server-side eligibility conditions in Section 7. The frontend
attempts recovery only after the transferred owner disappears and its ordinary
resume grace elapses. This phase changes crash recovery only; it does not change
attach, rotation, storage, or clean transfer semantics established in Phase 2.

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
- The dashboard-owner check is the Phase 1 authorization floor for every
  terminal route. Resume and transfer credentials later control ownership
  within that authenticated owner; they do not replace dashboard authentication.
- A resume credential never authorizes displacement of a live owner socket.
  Live-owner replacement requires an explicit, generation-bound transfer.
- Listing, completion, and deletion require short-lived, single-use proofs
  minted by the current socket; copied recovery material alone is insufficient.
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
- Transfer credentials are bound to their source generation and target, and are
  revoked by source disconnection, owner replacement, or newer transfer state.
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
