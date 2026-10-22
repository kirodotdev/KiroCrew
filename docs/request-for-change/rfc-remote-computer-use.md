---
title: Remote Computer Use -- a gateway drives the desktop of the Mac connected to it
status: draft
author: unstablebrainiac
created: 2026-10-18
last-audited: 2026-10-18
audited-at: 310ad48b5f
doc-pr: 21981
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Remote Computer Use

**Status:** `draft`. Nothing here is built. Measured against main `310ad48b5f`.

## Summary

Computer Use drives native apps only on the machine the gateway runs on. The
setup the docs recommend puts the gateway on a headless Linux dev desktop and
the person on a Mac running the desktop app in Remote Tunnel mode. In that
setup Computer Use cannot be turned on at all, and the apps the person wants
driven are on the other machine.

This RFC splits Computer Use at the backend seam. The chokepoint (keystone,
operator app policy, index, fingerprint re-walk, refusals, audit) stays on the
gateway. The native driver runs on the Mac, in a long-lived **executor**
process the desktop app supervises. The desktop app relays each backend call
from the gateway to the executor over the tunnel it already holds, and the
gateway sees a `ComputerUseBackend` like any other.

Six decisions:

1. **Split below the chokepoint.** The gateway gets a `RemoteExecutorBackend`
   that forwards each `ComputerUseBackend` method. `policy` and `gate` keep
   their logic; `tools.dispatch_tool` gains one line that enters the backend's
   per-call scope (§3); the index gains an attachment generation in its key.
2. **The Mac only dials out.** The desktop app pulls requests from the gateway
   over its existing tunnel. No port opens on the Mac.
3. **A scoped executor credential, not the internal secret.** The pull
   routes accept only an owner-bound credential minted over SSH for these
   routes alone. The gateway's internal secret never crosses the tunnel.
4. **Two keys on the honest path, one Mac key against a hostile one.** The
   gateway keystone and a Mac switch, bound to the crew's identity, must both
   be on. A gateway cannot turn on control of a Mac. Against a compromised
   gateway the keystone proves nothing, so the Mac switch is the key that
   holds, and the Mac UI says so.
5. **The Mac enforces every floor that has no config key.** The executor runs
   the same `policy` and `capture_macos` code the gateway does, on a record it
   resolved itself, so a compromised gateway gets no more than an honest one
   with an empty operator policy.
6. **One call, one attachment.** Each tool call is pinned to one attachment
   generation from entry to exit. A generation change mid-call fails the call;
   it never lands half a call on a different Mac.

## Motivation

### Current state

- **The driver runs in the gateway process.** The MCP shim
  (`mcp_computer.py`) posts to `/api/computer-use/invoke`;
  `api_computer_use_invoke` (`dashboard/handlers/computer_use.py`) runs
  `tools.dispatch_tool` on a `subprocess_executor()` worker; the service
  calls `get_shared_backend()`, which `select_default_backend()`
  (`computer_use/backend.py`) builds from the host OS.
- **A Linux gateway is refused outright.** `select_default_backend()` returns
  `LinuxBackend`, an `UnsupportedBackend` carrying `LINUX_REASON`. Settings
  renders that reason and no toggle when `supported` is false
  (`ComputerUsePanel.tsx`). The spec gate `_computer_use_spec_gate()`
  (`agent.py`) also requires `platform_could_be_supported()`, which is
  `platform_id_for_current_os() in _SUPPORTED_PLATFORM_IDS`, a set holding
  `macos` and `windows`.
- **The desktop app already holds a tunnel and a bundled backend.** In Remote
  Tunnel mode the app keeps an SSH forward open by running
  `kirocrew desktop tunnel` from `backend-dist` (`tunnel-keeper.js`) and
  fetches a dashboard link token by running a command over SSH
  (`token-sources.js`, `remote-token.js`). `backend-dist` carries the whole
  Python package, the macOS driver included (`find-bin.js`).
- **The browser panel has an outbound pull channel, but not for remote
  gateways.** `browser-agent-channel.js` long-polls
  `/api/browser/command-drain` and posts `/api/browser/command-result`. Both
  are in `_STRICT_INTERNAL_API_PATHS` (`server.py`), and the app sends a
  secret only to a gateway it launched locally (`local-token.js`).
  `command_bus.py` says a remote gateway falls back to Playwright, and
  `browser-agent-channel.test.js` pins that no secret is pushed to a remote.

### What the code already makes easy

- **The chokepoint consumes plain data.** `AppRef`, `ElementRec`, `Snapshot`,
  `DriverResult`, `ClickRequest` and `DragRequest` are frozen dataclasses of
  primitives (`computer_use/types.py`).
- **The macOS driver holds no native handles between calls.** Each action
  re-walks the tree to the index (`macos_driver.py`,
  `snapshot_macos.resolve_element`), so the gateway can keep the index cache.
  The address is only stable within one attachment: pids and window ids are
  Mac-local and can be reused, which is why decision 6 exists.
- **There is a swap seam.** `register_computer_use_backend()` plus
  `reset_shared_backend()` is how the suite installs `FakeComputerUseBackend`
  (`testing/fake_computer_use.py`).
- **The grant should be the app's.** `computer_use/permissions.py` documents
  that macOS attributes Accessibility and Screen Recording to the responsible
  process, so a `backend-dist` child of `KiroCrew.app` should run under the
  app's grant. This is not yet verified for a client-only packaged app;
  Phase 0 measures it.

## Goals

1. A Linux gateway drives native apps on a Mac whose desktop app is connected
   to it in Remote Tunnel mode, with the same tool surface, refusals and audit
   record as a local macOS gateway.
2. No inbound listener on the Mac. No gateway secret on the Mac.
3. Control of the Mac requires a decision made on the Mac, bound to the crew
   it was made for.
4. A tunnel drop, a sleeping laptop or a quit app produce a typed refusal,
   never a hang, never a silent success and never a lockout.

## Non-goals

- A Linux AT-SPI driver. `linux_driver.py` stays a placeholder.
- Windows executors. The wire is platform-neutral; a Windows app is a
  follow-up with its own consent question (its input moves the real cursor).
- Remote executors for a macOS or Windows gateway. Those keep their local
  backend, including a failed driver import, which keeps its current
  `UnsupportedBackend`.
- Several Macs per gateway (open question 1).
- Gateway-to-gateway peers (`rfc-remote-crew-sidebar`,
  `rfc-outbound-instance-transport`).
- A governance scope. Computer Use is ungoverned by design
  (`rfc-jev-task-executor` cites AGENTS.md); consent stays the keystone plus
  the Mac switch.

## Design

```
 Linux gateway                                     Mac
 ┌───────────────────────────────┐   SSH -L   ┌──────────────────────────────┐
 │ tools.dispatch_tool           │◄──────────►│ KiroCrew.app (Electron main) │
 │  keystone, policy, index,     │            │  computer-executor-channel   │
 │  fingerprint, refusals, audit │  scoped    │   attach / drain / result    │
 │ RemoteExecutorBackend         │  executor  │   heartbeat / reaped / detach│
 │  ExecutorBus + attachment     │  credential│        │ stdio JSON lines     │
 └───────────────────────────────┘            │  kirocrew computer executor  │
                                              │   floors + MacOSBackend      │
                                              └──────────────────────────────┘
```

### 1. Credential and attachment

**The executor credential.** The token the app already fetches over SSH is a
dashboard link token: it expires in minutes and is exchanged for a browser
session cookie (`token_auth_tokens.py`, `token_auth.py`), so it cannot carry a
long-lived channel. The channel gets its own credential the same way the app
gets that token, by running a command over SSH: a new
`kirocrew computer executor-token` subcommand prints a random owner-bound
bearer credential scoped to the executor routes only, with a 12-hour expiry,
plus the gateway's **installation id**, a random value this RFC adds and
persists in the gateway's config directory. Possession of SSH to the gateway
host is the owner check, as it already is for the link token. Gateway logout
(`kirocrew logout`) revokes every executor credential. The channel re-runs
the command before expiry and after a 401.

**The agent cannot mint or read it.** The agent runs a shell on that same
host, so SSH possession alone would let it mint a credential. The fence rests
on where the credential lives, not on how the command is spelled. Issued
credentials are stored as digests only, in a crew-home directory leaf,
`computer-use-executor`, added to `security._CREW_SECRET_LEAVES` (which
fences the agent's file tools), `sandbox_leaves._CREW_HIDDEN_LEAVES` (which
bind-masks it from the agent's sandboxed shell) and
`sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES`. The third list covers
a fresh install. The first mint creates the directory, and the launcher's
mask loop skips a name that does not exist yet, so a sandbox spawned before
that mint would get no mask, and an agent could create the directory itself
and plant a digest for a credential it chose. `_materialize_maskable_dirs`
(`sandbox_staging.py`) creates every leaf in that list, empty and `0o700`,
before each namespace spawn, so the mask always has a directory to bind over.
A mint run from an agent shell therefore cannot write the digest, so the
credential it prints is never accepted, whatever spelling got the command to
run. The command-text floor is a second layer: the verb is
named `executor-token` so the existing `credential-exfil-kirocrew-token` rule
(`security/denied_rules.py`) refuses it from an agent shell, and the argv
floor (`_is_credential_mint` in `security/argv_floor.py`) gains the
subcommand for the interpreter form. A mint run outside the sandbox through
`ssh localhost` would write the digest, so that route is the
`sandbox-escape-ssh-self` floor's (`security/denied_rules.py`, with
`_is_ssh_to_self` in the argv floor). The rule itself records that it is a
command-line subset and that script or interpreter indirection still reaches
sshd until the sandbox gets an OS-level network fence. This RFC does not
widen that gap: an agent that escapes the sandbox already holds the owner's
account, including the dashboard token mint and the gateway's own secrets, so
the executor credential gives it nothing new. Were a credential to leak anyway, its
holder could attach as a fake Mac or take over the attachment: that denies
the real Mac and feeds the agent fabricated trees, but it reaches no input on
the real Mac, whose executor dials out and acts only on its own attachment's
requests.

**Identities.** Three values, all stable:

- the **crew identity**: the remote host's SSH host-key fingerprint plus the
  gateway installation id, both known before the Mac switch is shown (§6);
- the **Mac installation id**: a random value the desktop app creates once and
  keeps in its own store;
- the **attachment**: one executor process serving one gateway.

**Attach.** The client asserts a protocol version, its platform id, its
permission probe (Phase 0 decides what that reads), a display label, the crew
identity it holds consent for, and its Mac installation id. The platform and
probe are validated against fixed vocabularies; the label is bounded,
redacted and display-only; the crew identity must match this gateway's
installation id. The gateway mints the rest: a random `attachment_id`, a
monotonically increasing `generation`, and an attachment token bound to the
credential, the crew identity, the Mac installation id and the generation.

- **Liveness.** The channel posts `heartbeat` every 10 s, independent of
  `drain`, so a long `launch_spawn` (up to the 30 s launch window wait) never
  starves liveness. An attachment with no heartbeat for `EXECUTOR_TTL_S`
  (30 s) is expired. `detach` ends it at once and is idempotent. The executor
  is serial: the bus releases the next request for an attachment only after
  the previous one completes.
- **Same Mac replaces, but only with proof.** A new `attach` with the same
  credential owner, crew identity and Mac installation id evicts the current
  attachment only when it presents that attachment's token (the app process
  still holds it after a wake), or when the current attachment has had no
  heartbeat for `EXECUTOR_TTL_S`. The crew identity and the Mac installation
  id are not secrets, so they alone cannot evict a live, heartbeating Mac. An
  eviction fails the old attachment's in-flight requests with the §5 codes,
  retires its generation, and resets the index. A laptop that wakes with a new
  executor is never locked out by its own ghost for longer than one TTL.
- **Another Mac is refused.** An attach with a different Mac installation id
  while one is live is refused, naming the attached label, unless it sets
  `takeover`, which the app sends only from an explicit "Use this Mac
  instead" menu action. Takeover is possible only for a holder of an executor
  credential, which only SSH to the gateway host can mint, and the displaced
  Mac shows that it was taken over.
- **Kill and reap.** Control rides the heartbeat, not the request path, so a
  stuck driver call cannot block it: a heartbeat response can carry
  `{control: "kill", generation}`. The channel SIGKILLs the executor for that
  generation, waits for it to exit, and posts `reaped` with the attachment
  token.
- **The Mac stops itself when it loses the gateway.** Two independent
  watchdogs, both shorter than the gateway's TTL:
  - the channel SIGKILLs the executor when no heartbeat has been acknowledged
    for `MAC_WATCHDOG_S` (15 s);
  - the executor runs its verbs on a worker thread and a watchdog thread that
    calls `os._exit` when no ping has arrived on stdin for `MAC_WATCHDOG_S`,
    so it dies even if the channel itself hangs, and exits at once when stdin
    closes.
- **Replacement after a kill.** A replacement attach is refused with
  `previous_executor_alive` until either the reap is reported (in `reaped` or
  as `reaped_generation` on the attach) or the retired attachment has gone
  `EXECUTOR_TTL_S` (30 s) with no heartbeat. The second path is safe because
  of the watchdogs: an executor that has not reached the gateway for 15 s is
  already dead, by either of two mechanisms, before the 30 s gateway TTL
  admits its replacement. A retired generation is sent no further requests.
  The gateway cannot verify that a Mac process died; a reap report is the
  owner app's assertion, with the same trust as the rest of the channel.
- **Index.** Every attach, eviction, expiry and detach calls
  `index.reset_shared_index()`, and the generation joins the index key, so an
  index walked on one attachment can never resolve on another, even inside
  the 90-second TTL.

### 2. The wire

A closed codec. Each request is `{generation, request_id, verb, args}` with
a random 128-bit `request_id`; the gateway completes a request only when both
`generation` and `request_id` match an in-flight entry, under one lock.
Validation runs in two stages at both the route and the stdio boundary. Before
parsing: a body or JSON line over its byte cap is rejected by length. After
parsing, before any dataclass or driver call: unknown keys, wrong types,
non-finite numbers and structures over the existing ceilings in
`computer_use/types.py` (`MAX_TYPE_TEXT_LEN`, `MAX_KEY_LEN`, `MAX_ACTION_LEN`,
`MAX_DRAG_STEPS`, `MAX_TREE_NODES_LIMIT`, `MAX_TREE_DEPTH_LIMIT`,
`MAX_TEXT_LIMIT`, `MAX_LAUNCH_QUERY_LEN`, the screen-coordinate bounds) and a
maximum base64 image size are rejected. The request shapes reuse
`validation.MCP_COMPUTER_SCHEMAS` where a verb has a tool twin.

Verbs: `list_apps`, `resolve_app`, `snapshot`, `click`, `drag`, `type_text`,
`press_key`, `set_value`, `scroll`, `perform_action`, `launch_identity`,
`launch_spawn`, `show_pointer_motion`, `probe_permissions`. The ABC's
`platform_id` and `status()` are answered on the gateway from the attachment
(§3); `close()` is gateway-local (§3). The executor rejects any other verb.
Like `rfc-outbound-instance-transport` pins its peer verbs, this table is the
whole command surface: no shell, file or generic RPC verb.

**Handles, not records.** The executor never accepts an `AppRef` from the
gateway. Every app or window the executor reports carries an opaque
`app_handle` it minted, scoped to its generation. Every verb that targets an
app takes the handle; the executor resolves it to a fresh canonical `AppRef`
from the OS window list, refuses a handle from another generation or one
whose window is gone, runs its floors on that record, and passes only that
record to `MacOSBackend`. The gateway keeps the executor's `AppRef` for the
chokepoint and audit; the Mac never trusts it back.

**The element is an address, not a fact.** The gateway's `ElementRec` tells
the executor which index to act on, nothing more. The driver's own identity
check (`_same_identity` in `macos_driver.py`) compares role, subrole and title
only, and `policy.check_input_target` reads `rec.secure` from whatever record
it is handed. So before every verb that takes an element, the executor walks
to the index itself (`snapshot_macos.resolve_element`), refuses when its
record disagrees with the gateway's on role, subrole or title, and runs its
floors on its own record, whose `secure` comes from its own walk. A forged
`secure=False` over a real password field is refused.

**Launch is pinned.** `launch_app` takes two Python callbacks (`permit`,
`refuse_launched`) that cannot cross a wire, and its pre-spawn check is
deliberately the last step before a spawn that cannot be undone
(`backend.py`). The wire splits it without loosening that:

1. `launch_identity(query)` resolves the identity and returns it with an
   opaque launch token that pins the exact resolved target and the
   generation.
2. The gateway runs `permit`, the operator's allow and deny lists, on every
   spelling of that pinned identity. A refusal ends the call.
3. `launch_spawn(token)` accepts only the token, never a query. The executor
   re-resolves the pinned target, refuses if it no longer resolves to the same
   identity, runs the Mac-side launch floors, and spawns.
4. The gateway runs `refuse_launched` on the launched `AppRef`, as the local
   path does after spawning; the executor runs the self-window floor on it too.

**Rewritten on arrival at the gateway, never trusted from the Mac:**

- `Snapshot.captured_at` is the producer's `time.monotonic()`, which is not
  comparable across hosts. The gateway re-stamps it.
- `Snapshot.image_path` is a Mac temp path. The gateway clears it and lets
  `service._persist_image` spool the bytes. The executor deletes its own
  spooled file once the bytes are serialised, so no capture is left behind on
  the Mac.

**Round trips.** Three cases:

- an ordinary mutation costs four tree-only requests: `resolve_app`, the
  fingerprint re-walk `snapshot`, the action, and the settle `snapshot`
  (mutating dispatch forces `want_image=False`, `tools.py`);
- a pointer-moving click or drag with the Mac's overlay option on adds
  `show_pointer_motion`;
- a launch costs `launch_identity`, `launch_spawn` and the post-launch
  `snapshot`, which carries an image by default.

Pixels otherwise cross the wire only on `computer_get_state(screenshot=True)`.
Phase 3 measures each case over a real tunnel and records the numbers here.

### 3. Gateway side

- **`RemoteExecutorBackend(ComputerUseBackend)`** in
  `computer_use/remote_backend.py`. The gateway registers it at startup
  through the existing seam,
  `register_computer_use_backend(lambda: RemoteExecutorBackend(transport, loop))`,
  only on Linux. Its blocking methods run on `subprocess_executor()` workers
  and hand off to the loop with `run_coroutine_threadsafe`; a call made on the
  loop thread, or after the loop closed, is refused rather than deadlocked.
- **Selection.** `select_default_backend()` is untouched. A process that did
  not register the remote backend (`kirocrew computer doctor`, `apps`, `call`
  on a Linux host) still gets `LinuxBackend` and today's answer. An unknown
  platform keeps its current backend.
- **Status.** `BackendStatus` gains `kind` (`local` or `remote`).
  `RemoteExecutorBackend.status()` reports `kind="remote"`, `supported` true
  only while an attachment is live and its executor says supported, and a
  reason that names the fix when not ("no Mac is attached: open Kiro Crew on
  the Mac and allow this crew to use it"). `platform_id` is the attached
  executor's platform, else `linux`.
- **Settings.** `_snapshot()` carries `kind`, and `ComputerUsePanel.tsx`
  renders the toggle when `supported || (kind === "remote" && paired)`, where
  `paired` is true once any executor credential has been minted on this
  gateway. A Linux gateway nobody has paired a Mac with keeps today's
  `LINUX_REASON` text and no toggle, so the remote path stays invisible until
  someone opts in by minting a credential, which only Phase 3's switch or the
  CLI does. Once paired, the keystone can be turned on before a Mac attaches
  and off after it leaves. For a
  remote backend the permission rows come from the attachment's relayed probe
  (refreshed on each attach), not from `_probe_permissions`, which would probe
  the Linux host. Detached, the rows read "no Mac attached". The gateway's
  cursor-motion row is hidden for a remote backend (`cursor_motion_supported`
  is false whenever `kind` is `remote`); the overlay option lives beside the
  Mac switch (§6). Two more panel branches key off `platform` today: the
  permissions section renders only on macOS, and the header badge reads
  "macOS only". For a remote backend the permissions section renders whatever
  `platform` says, and the badge names the attached Mac instead.
- **Spec gate.** `_SUPPORTED_PLATFORM_IDS` is renamed
  `_ADVERTISABLE_PLATFORM_IDS` and gains `linux`, with its comment changed
  from "platform ids with a real driver" to what it now means; the rename also
  drops that comment's claim that a test pins the set, since none does. The gate stays
  attachment-independent: with the keystone on, the tools are advertised and a
  detached call refuses with `no_executor`. Attach and detach do not rebuild
  the spec, so a sleeping laptop does not reset every session. The cost is the
  one `agent.py` names for the gate (about 109 MB of computer-use server per
  chat process); it is paid only once the owner turns the keystone on.
- **One call, one attachment.** `ComputerUseBackend` gains a default no-op
  `call_scope()` context manager, and `dispatch_tool` enters it once per call;
  that one platform-neutral line is the only change to `dispatch_tool`.
  `RemoteExecutorBackend.call_scope()` takes a lease on the current generation
  and every forwarded request inside it carries that generation. If the
  generation changes before the mutating verb is sent, the call fails with
  `executor_changed` and nothing is sent. If it changes after, the call fails
  with `outcome_unknown`. The audit row names the leased attachment.
  `rfc-jev-task-executor`'s second caller of `dispatch_tool` gets the same
  pinning.
- **Audit.** Each gate record gains `executor`: the gateway-issued
  `attachment_id` and the display label captured once at attach, bounded and
  redacted like other desktop text.
- **Live view.** `frame_scope` is thread-local and is published on the
  `subprocess_executor()` worker that runs the dispatch. So the emit happens in
  the blocking backend method body, on that worker, after the re-stamp and
  before returning to `service`, never from the loop callback where the bytes
  arrive, where `active_scope()` is `None` and the frame would be dropped.
- **`close()`** drains and fails the bus's queued requests. It does not
  detach, kill or contact the executor.
- **Timeouts** come from the bound that governs each verb, not from
  `AX_MESSAGING_TIMEOUT_SECS` (2 s, one accessibility call): `MAX_WALK_SECS`
  (10 s) plus a transport margin for `snapshot`; the driver's own action
  bounds plus a margin for the mutating verbs; the launch window wait for
  `launch_spawn`. `list_apps` and `resolve_app` run an untimed CoreGraphics
  window-list and bundle-metadata pass (`apps_macos.py`), so they get a budget
  of their own, proposed at 5 s and checked against Phase 3's measurements.

### 4. Routes

`/api/computer-use/executor/attach`, `/drain`, `/result`, `/heartbeat`,
`/reaped`, `/detach`.

They are not strict-internal; they must be reachable through the tunnel.
Today `token_auth` reads a dashboard credential from a `?token=` query or a
session cookie, recording which with `auth_from_query_token`, and reads the
`X-Internal-Secret` header only for internal (strict or mixed) routes; it has no
`Authorization` branch. This RFC adds one: an `Authorization: Bearer` value is
accepted only if it is a live executor credential (§1), and only on these
routes, and a match sets `request["executor_auth"] = True`. The handlers
require that mark and refuse everything else: a dashboard cookie, a link
token, an app token. Every route but `attach` also requires the attachment
token, so one client cannot post results for another attachment. A retired
attachment's token stays valid for `/heartbeat` and `/reaped` only, until its
reap lands or its TTL passes, so the heartbeat that carries its `kill` is
still answered. A test modelled on `test_browser_command_handler_auth.py` pins
each refusal: cookie, link token, app token, expired credential, credential
after logout, wrong attachment token, stale generation, a retired token on
`/drain` or `/result`; and two acceptances with a retired attachment's token:
a `heartbeat` that receives `kill`, and a `reaped`.

### 5. Failure semantics

| Case | Read verb | Mutating verb |
|---|---|---|
| No attachment | `no_executor` | `no_executor` |
| Generation changed before send | `executor_changed` | `executor_changed`, nothing sent |
| Attachment evicted, expired or detached after send | `executor_detached` | `outcome_unknown`: take a fresh snapshot before acting again |
| No result within the verb's budget | `executor_timeout` | `outcome_unknown`, and the executor is killed (below) |
| Protocol mismatch | refused at attach, both versions named | same |

A forwarded call is never retried by the backend, because a click may have
landed. A late result for a retired generation is dropped.

**Killing a stuck executor.** A synchronous driver call in the executor
cannot be cancelled from the gateway. So a mutating timeout, or transport loss
with a mutating request in flight, retires the generation and returns `kill`
on the next heartbeat. A replacement is admitted only on a reap report or
after the gateway TTL, which the Mac watchdogs make longer than the old
executor can live (§1).

### 6. Mac side

- **`kirocrew computer executor`**, a new subcommand in
  `computer_use/cli.py`. It speaks the §2 codec on stdin and stdout, holds one
  `MacOSBackend`, and makes no outbound network calls. The capture path's
  live-view relay (`screencast.emit_snapshot_frame`) posts only inside a
  published `frame_scope`, and the executor never publishes one, so the relay
  returns at its `active_scope() is None` check; Phase 2 asserts no frame POST
  leaves the executor. It calls backend methods, not `dispatch_tool`, so the
  gateway's index and audit stay authoritative. The two launch verbs reuse
  the existing helpers rather than re-implementing them: `launch_identity`
  calls `launch_macos.resolve_target` and `launch_macos.target_identity`, and
  `launch_spawn` calls the existing `launch_app` helper with the pinned name,
  the Mac identity floor as `permit` and the Mac self-window floor as
  `refuse_launched`, so the already-running refusal and the window wait are
  inherited. `kirocrew computer call` cannot serve here: it returns rendered
  text, keeps its index per process and gates on the Mac's keystone.
- **Floors on the Mac.** Before each verb, on the canonical record it resolved
  from the handle, the executor imports and runs, never copies:
  - the indexless refusal: every verb whose tool twin is in
    `tools._ELEMENT_REQUIRED_TOOLS` is refused without an element, so a
    keystroke can never land in whatever field holds focus;
  - `policy.check_app` with an empty `PolicyConfig`, so the `kirocrew_self`
    floor applies (operator allow and deny lists stay gateway-only: they are
    config, not floors);
  - `policy.check_input_target` with an empty `PolicyConfig` for every tool in
    `tools._SECURE_TARGET_TOOLS` (`type_text`, `set_value`, `press_key`,
    `perform_action`, `scroll`). That covers both its layers: the secure-target
    refusal on a fresh walk, and the text scan (`is_sensitive_bash_command`,
    `audit_bash_exfiltration`, `is_denied` with the full built-in rules);
  - both launch checks `launch_app` applies locally: the identity check before
    `launch_spawn` spawns, and the self-window check on the launched window;
  - `capture_macos` unmodified, so every suppression it applies (secure,
    truncated, depth-truncated, unknown window id) still applies;
  - pointer-moving gestures (`method=global`) are refused unless the Mac
    switch's "allow moving my pointer" option is on. It defaults off.
- **Mac options never travel on the wire.** The channel passes
  `allow_pointer_move` and the overlay option to the executor at spawn time,
  on the command line, outside the request codec. The executor treats them as
  immutable, rejects any request field that names them, and the channel
  restarts the executor when either option changes.
- **`computer-executor-channel.js`** in Electron main, built like
  `browser-agent-channel.js` (injected `fetchFn`, bounded backoff, never
  throws out of its loop). There is one channel per crew identity for the
  whole app, reference-counted by the windows connected to that crew. It runs
  only when the window's gateway reads as remote and the crew's Mac switch is
  on; it refuses to start for a window whose gateway reads as local. It spawns
  the executor from `backend-dist`, attaches, relays drain to stdin and stdout
  to result, heartbeats, pings the executor every 5 s, and on switch-off, last
  window closed, tunnel loss, a missed-heartbeat watchdog or a `kill` on a
  heartbeat: detaches, SIGKILLs and reaps the executor, and reports the reap.
- **The Mac switch.** "Let this crew use this Mac", plus the pointer and
  overlay options, in the app menu and on the Remote Crew form, off by
  default. Its text says that while it is on, this gateway can drive this Mac
  even if the gateway's own Computer Use setting shows off, and that driving
  includes typing into any open app, so a compromised gateway that reaches an
  open terminal can run commands as the Mac user. The executor's typed-text
  scan narrows that, but it is a pattern check, not a boundary, and the text
  does not claim otherwise. It is stored
  against the crew identity (§1), not the local port. The app fetches the
  installation id with the executor credential before it shows the switch,
  and shows it with the host-key fingerprint, so consent is given to a known
  identity and never bound later. `host-config.js` keys remote hosts by port,
  and editing a record keeps unspecified fields, so a port-keyed switch could
  silently carry consent to a different crew. Any change to the crew identity
  clears the switch. The menu shows "Computer Use: this crew is driving this
  Mac" with Stop while an executor runs, and "Use this Mac instead" when
  another Mac holds the attachment.
- **Cursor overlay** is the `show_pointer_motion` verb. The gateway sends it
  where `tools._perform` calls `overlay.show_pointer_motion` today. The
  existing entry point cannot be reused as is: it reads the gateway's
  `cursor_motion` config and schedules onto a loop the invoke handler binds
  (`overlay.py`). So the executor owns a running asyncio loop on its own
  thread and drives `CursorOverlay` through an executor entry that takes the
  spawn-time overlay option instead of reading config. The verb acknowledges
  scheduling only, so ordering stays best effort, as it is locally.

## Migration plan

Each phase is its own PR, independently shippable.

**Phase 0: TCC probe.** On a signed packaged app in client-only mode, run the
executor as a child of `KiroCrew.app` and record whether it inherits
Accessibility and Screen Recording, including after an ad-hoc re-sign.
Exit: a test asserts the recorded verdict is `inherits` or
`does-not-inherit`. Phase 3 is blocked until it is `inherits`, or until a
permission flow for the executor ships.

**Phase 1: gateway backend, routes, attachment.** `remote_backend.py`,
`ExecutorBus`, the executor credential and its subcommand, the attachment
model, the six routes with `executor_auth`, `kind` in status and Settings, the
spec-gate rename, the lease, the failure codes. Tested with a fake executor
transport wrapping `FakeComputerUseBackend` that offsets `captured_at` by
10^6 seconds and sets a bogus `image_path`. Exit:

- the `test_computer_use_*` tool suite passes through the forwarding backend;
- with no credential ever minted, Settings shows `LINUX_REASON` and no
  toggle, and the tools stay unadvertised;
- after a mint and with no attachment, every tool returns `no_executor`, and
  the keystone toggle renders;
- a producer clock 10^6 s off is still inside the index TTL; the model is
  handed a gateway-spooled path;
- re-attaching with a reused pid and window id resolves no old index;
- a generation change before a mutation sends nothing; after, returns
  `outcome_unknown`; a late result is dropped; the audit row names the leased
  attachment, not the one current when the row is written;
- a `launch_spawn` that runs longer than `EXECUTOR_TTL_S` keeps its generation
  live;
- after a mutating timeout, a replacement attach without a reap report is
  refused until the retired attachment's TTL passes, and a late mutation
  result from the old generation is dropped;
- a same-Mac attach replaces when it presents the current attachment token or
  the current attachment is past its TTL, and is refused against a
  heartbeating attachment without the token; a different-Mac attach is refused
  without `takeover`;
- cookie, link-token, app-token, expired-credential, post-logout,
  wrong-attachment-token and stale-generation calls are each refused;
- an oversized body is rejected by length before parsing; an unknown key is
  rejected after parsing and before any dataclass is built;
- `kirocrew computer doctor` on Linux still reports `LINUX_REASON`;
- from an agent shell on the gateway host, `kirocrew computer executor-token`
  and its interpreter form are refused by the mint floor; a mint that runs
  inside the agent sandbox anyway produces a credential the routes refuse,
  because the digest store is hidden from it, and that holds on a fresh home
  where no mint has created the store before the sandbox spawns;
  `ssh localhost` followed by the mint is refused by `sandbox-escape-ssh-self`;
  and the store is unreadable through the agent's file tools and sandboxed
  shell.

**Phase 2: the executor.** `kirocrew computer executor`, the codec, handles,
the Mac-side floors, `launch_identity` / `launch_spawn`. Exit: a client that
skips the gateway and sends raw verbs is refused, one case each, for: a secure
field via each of the five secure-target verbs, a forged `secure=False`
element over a real password field, a denied command typed into a terminal,
the Kiro Crew window, a handle from another generation, a launch of the Kiro
Crew app, a `launch_spawn` whose token no longer resolves to its pinned
identity, a capture of a secure, truncated, depth-truncated and
unknown-window-id window, a `global` click with the pointer option off, an
unknown verb, and an indexless `type_text` or `press_key`. No spooled capture
survives a snapshot, and no frame POST leaves the executor. A request field that
tries to set `allow_pointer_move` or the overlay option is refused, and every
other field a gateway controls cannot make a `global` click move the pointer
while the spawn-time option is off. The executor exits when its stdin closes,
and when no ping arrives for `MAC_WATCHDOG_S` even while a driver call is
blocked.

**Phase 3: Electron channel and the Mac switch.** Blocked on Phase 0. Exit, on
a Mac in Remote Tunnel mode, with the tunnel's round-trip time recorded beside
each number:

- the agent snapshots and clicks in a Mac app; over 20 element clicks the p95
  is within 2 s, over 10 screenshots of a 1440x900 window the p95 is within
  3 s, and neither sample has a timeout; all three §2 cases are measured and
  recorded;
- switch-off ends the executor within one poll, the next call returns
  `no_executor`, and a replay of the old attachment token is refused;
- sleeping the laptop and waking it re-attaches within one TTL;
- quitting the app with a mutation in flight lets a relaunched app attach
  within one TTL;
- a second Mac on the same crew is refused, and its "Use this Mac instead"
  takes over;
- a timed-out mutation kills the executor, and no input from it lands after
  the replacement attaches;
- with the tunnel cut during a driver call blocked past the gateway timeout,
  the executor is dead within `MAC_WATCHDOG_S` and before the gateway admits
  a replacement;
- two windows on the same crew share one executor, and closing one keeps it;
- pointing the same local port at a different crew leaves the switch off;
- the Settings permission rows show the Mac's grants.

**Phase 4: live view and overlay.** Exit: a remote capture's frame reaches
an owner dashboard socket, and with the Mac's overlay option on, a recorded
screen capture of a coordinate click shows the overlay moving; with it off,
no overlay process starts.

## Backward compatibility

A macOS or Windows gateway is unchanged: `select_default_backend()` still
returns its local driver, and no route it serves changes. A Linux gateway
that no one has paired a Mac with is unchanged too: Settings keeps
`LINUX_REASON` and no toggle. Only after an executor credential is minted does
Settings show a toggle and a "no Mac attached" explanation, and with the
keystone on, the tools are advertised and refuse with `no_executor` until a
Mac attaches. If Phase 3 never ships, nothing outside the CLI mints a
credential, so ordinary Linux users never see the remote path. The CLI on a
Linux host is unchanged. An old desktop app never attaches; a new app against
an old gateway finds no `attach` route, keeps the executor stopped and says
the crew is too old.

## Security considerations

- **Compromised gateway.** While a Mac is attached the gateway can drive its
  apps; that is the feature. Against a compromised gateway the keystone is not
  a key: the gateway owns the bus and can send raw verbs with its keystone
  off. The Mac switch is the key that holds, so its text says that leaving it
  on lets that gateway drive this Mac whatever the gateway's own setting
  shows. What a compromised gateway gets is no more than an honest gateway
  with an empty operator policy: every floor without a config key runs on the
  Mac, on a record the Mac resolved, so it cannot type into a password field,
  type a denied command, drive or launch the Kiro Crew window, capture a
  secure window, or move the pointer without the Mac's pointer option, which
  never crosses the wire. Terminals stay drivable, as they are locally
  (`check_app` keeps them off its denylist), but the text scan runs on the Mac
  for every typed string. The scan is a pattern check, not a boundary: a
  compromised gateway that reaches an open terminal can still run commands
  as the Mac user, which is why the switch text names that outcome.
- **Compromised Mac app.** It can already reach the gateway as the owner over
  SSH, so these routes give it nothing it did not have.
- **Route class.** The executor routes sit outside the strict-internal class
  that `invoke` and `frame` are in, because they must cross the tunnel. They
  accept only the scoped executor credential, which no other route accepts,
  and accept results only for requests the gateway issued to that
  attachment's generation. The internal secret stays on the gateway.
- **Consent binding.** The Mac switch is keyed to the crew's SSH host key and
  gateway installation id, shown before consent, and cleared when either
  changes.
- **Data placement.** Screenshot bytes land in the gateway's spool, under the
  same agent-readable screenshot rule; rendered trees and image paths land in
  the chat transcript. The audit log records the invocation or refusal and the
  leased attachment, not pixels or trees. Nothing is left on the Mac.
- **The agent on the gateway host.** It shares the host the credential is
  minted on. The boundary is the credential store, hidden from both its file
  tools and its sandboxed shell, so a mint it runs cannot register a usable
  credential; the command-text floor refuses the mint as a second layer (§1).
  Escaping the sandbox through `ssh localhost` is the existing
  `sandbox-escape-ssh-self` gap, which already exposes everything the owner
  holds; the executor credential adds nothing to it.
  A leaked credential buys denial of the real Mac and fabricated trees, not
  input on it.
- **No new listener.** The Mac only dials the tunnel's local end. The executor
  makes no network calls.

## Alternatives considered

- **Move the whole dispatch to the Mac** (forward MCP calls, run
  `dispatch_tool` there). One gate, on the Mac. Rejected: the gateway's audit,
  its operator policy and `rfc-jev-task-executor`'s second caller of
  `dispatch_tool` would all fork, and `kirocrew computer call` already shows
  the cost (text results, per-process index, Mac keystone).
- **Reverse SSH tunnel to a Mac listener.** Rejected: an inbound port on a
  laptop, and a new credential the gateway would hold for it.
- **Reuse the browser command bus.** Its routes are strict internal and it is
  keyed by chat session; Computer Use is per host. A sibling bus keeps the
  browser contract unchanged.
- **A one-time pairing code on attach.** Rejected: SSH to the gateway host is
  already what mints the executor credential, and the Mac switch is the local
  decision. A pairing code would add a step on every wake without adding a
  party who can say no.
- **Reuse the dashboard link token or session cookie.** Rejected: the link
  token lives minutes and is exchanged for a cookie, and a cookie-reachable
  route would let any dashboard browser session take the executor slot.
- **Run a second gateway on the Mac.** Works today, but splits memory, crons
  and sessions across two crews.

## Open questions

1. One Mac per gateway, or several with a `machine` argument on every tool?
   This RFC proposes one: an attach from a second Mac is refused while the
   first is live, unless the person takes over from the second Mac.
2. Should a macOS gateway also accept a remote executor, choosing per call
   between its own desktop and another Mac? Out of scope here; `kind` leaves
   room for it.
