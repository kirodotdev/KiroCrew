---
title: Remote Computer Use -- a gateway drives the desktop of the Mac connected to it
status: draft
author: unstablebrainiac
created: 2026-10-18
last-audited: 2026-11-20
audited-at: 1c37e326a2
doc-pr: 21981
implementation-prs: []
tracking-issues: [40197]
supersedes: []
superseded-by: []
revision: 2
---

# RFC: Remote Computer Use

**Status:** `draft`. Nothing here is built. Measured against main `1c37e326a2`.

**Revision 2 (2026-11-20).** Revision 1 gave the Mac a transport of its own: a
scoped bearer credential minted over SSH, six pull routes through the tunnel,
and an Electron channel. Since then the gateway half of connected computers
([#29949](https://github.com/kirodotdev/KiroCrew/issues/29949)) landed on main
([#34858](https://github.com/kirodotdev/KiroCrew/pull/34858)): one authenticated
socket per computer, dialled out over the same SSH forward, with a credential
minted over the same SSH login. Two tunnelled credential schemes for one Mac
would be two things to secure and revoke, so this revision carries the executor
on a channel of that socket instead (§1, §4). It also adds two rules revision 1
lacked: the remote path stays invisible until a Mac is enrolled for it (§3,
"Paired"), and a local Linux driver, which
[#35378](https://github.com/kirodotdev/KiroCrew/pull/35378) and
[#25222](https://github.com/kirodotdev/KiroCrew/pull/25222) propose, is never
hidden by it (§3, "Selection"). The wire (§2), the Mac floors (§6) and the
failure codes (§5) are unchanged. Revision 1's transport is kept, condensed, as
the fallback (§7).

## Summary

Computer Use drives native apps only on the machine the gateway runs on. The
setup the docs recommend puts the gateway on a headless Linux dev desktop and
the person on a Mac running the desktop app in Remote Tunnel mode. In that
setup Computer Use cannot be turned on at all, and the apps the person wants
driven are on the other machine.

This RFC splits Computer Use at the backend seam. The chokepoint (keystone,
operator app policy, index, fingerprint re-walk, refusals, audit) stays on the
gateway. The native driver runs on the Mac, in an **executor** process that the
Mac's connected-computer connector starts on request. Each backend call travels
from the gateway to the executor on a `computer-use` channel of the Mac's
connector socket, and the gateway sees a `ComputerUseBackend` like any other.

Seven decisions:

1. **Split below the chokepoint.** The gateway gets a `RemoteExecutorBackend`
   that forwards each `ComputerUseBackend` method. `policy` and `gate` keep
   their logic; `tools.dispatch_tool` gains one line that enters the backend's
   per-call scope (§3); the index gains an attachment generation in its key.
2. **The Mac only dials out, on the socket it already holds.** The executor's
   traffic is one channel of the connected-computer connection. This RFC adds
   no route, no listener and no bus of its own.
3. **No new credential.** The Mac is enrolled the way connected computers
   enrolls every computer: over the SSH login the tunnel rides, with the
   connector credential (`connector_token.py`). Its revocation is that
   computer's device record.
4. **Two keys on the honest path, one Mac key against a hostile one.** The
   gateway keystone, and a separate "use this Mac's desktop" capability on the
   Mac's own connected-computer grant, given in its native consent. A gateway
   cannot grant the capability. Against a compromised gateway the keystone
   proves nothing, so the Mac capability is the key that holds, and the Mac UI
   says so.
5. **The Mac enforces every floor that has no config key.** The executor runs
   the same `policy` and `capture_macos` code the gateway does, on a record it
   resolved itself, so a compromised gateway gets no more than an honest one
   with an empty operator policy.
6. **One call, one attachment.** An attachment is one `computer-use` channel.
   Each tool call is pinned to one attachment generation from entry to exit. A
   generation change mid-call fails the call; it never lands half a call on a
   different Mac or a different executor.
7. **A local driver is never hidden.** The remote backend is installed only
   while a computer holding the desktop capability is enrolled, and when the
   gateway also has a working local driver, the local driver stays the default.

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
  (`agent_parts/spec_build.py`) also requires
  `platform_could_be_supported()`, which is
  `platform_id_for_current_os() in _SUPPORTED_PLATFORM_IDS`
  (`computer_use/backend.py`), a set holding `macos` and `windows`.
- **Two open PRs give Linux a local driver.** #35378 runs Computer Use on the
  upstream `cua-driver` for Linux X11 and Windows, with a `driver` choice
  (`auto`, `native`, `upstream`) in the keystone file; #25222 adds a native
  AT-SPI/X11 driver. Both add `linux` to `_SUPPORTED_PLATFORM_IDS` and make
  `select_default_backend()` return a real backend on Linux.
- **The Mac already holds an authenticated outbound socket.** With the
  `connected_computers` preview on, a computer's connector opens
  `GET /api/connected-computers/connect` (`token_auth_paths.CONNECTOR_CONNECT_PATH`) through
  the SSH local forward. The socket authenticates every frame after the
  handshake with a per-connection MAC and sequence (`connected_computers/authenticated.py`),
  pings every `HEARTBEAT_SECS` (15 s) and drops after `LIVENESS_DEADLINE_SECS`
  (45 s) of silence (`connected_computers/protocol.py`). It multiplexes byte channels
  (`broker.DeviceConnection.open(kind, session_key)`), and the
  gateway never names a path or argv: `kind` is `workspace` or `mcp:<name>`
  (`protocol.kind_of`), and the connector maps it to a process of its
  own choosing.
- **The credential is scoped and minted over SSH.** `connector_token.mint`
  issues a credential with audience
  `kirocrew-connected-computer`, bound to the gateway id, the device, its
  install fingerprint and the device record's credential generation, for 20 h
  (`CREDENTIAL_TTL_SECS`). Every dashboard path refuses it. Disconnect, Remove
  and a local Off bump the generation and end the socket.
- **The local side is in flight.** The connector, native consent, grant store
  and SSH enrollment (`kirocrew connected-computer enroll --stdin`) are
  [#39409](https://github.com/kirodotdev/KiroCrew/pull/39409), Linux only; the
  desktop-app menu and dialogs are
  [#39410](https://github.com/kirodotdev/KiroCrew/pull/39410); the dashboard
  surfaces are [#39411](https://github.com/kirodotdev/KiroCrew/pull/39411);
  macOS and Windows sharing (a Seatbelt plan and a Keychain store) is
  [#40191](https://github.com/kirodotdev/KiroCrew/pull/40191). All four are
  open and stacked. Its connector starts only the workspace tool server or a
  local MCP server the person exposed, every child under folder confinement.
- **Connected computers is gated.** `flag.remote_refusal()`
  (`connected_computers/flag.py`) also requires Session Environments
  (`session_environments_enabled()`, the `KIROCREW_DEV_MODE=1` developer
  flag), and the preview is off on a Shared Crew.

### What the code already makes easy

- **The chokepoint consumes plain data.** `AppRef`, `ElementRec`, `Snapshot`,
  `DriverResult`, `ClickRequest` and `DragRequest` are frozen dataclasses of
  primitives (`computer_use/types.py`).
- **The macOS driver holds no native handles between calls.** Each action
  re-walks the tree to the index (`macos_driver.py`,
  `snapshot_macos.resolve_element`), so the gateway can keep the index cache.
  The address is only stable within one attachment: pids and window ids are
  Mac-local and can be reused, which is why decision 6 exists.
- **There is a swap seam.** `register_computer_use_backend()` plus `reset_shared_backend()` is how the
  suite installs `FakeComputerUseBackend` (`testing/fake_computer_use.py`).
- **A channel is a byte stream to a child process.** The executor speaks JSON
  lines on stdin and stdout (§2), which is exactly what a channel carries, so
  the codec needs no change to ride one.
- **The grant should be the app's.** `computer_use/permissions.py` documents
  that macOS attributes Accessibility and Screen Recording to the responsible
  process. Whether an executor started by the connector, under the desktop
  app's own host, inherits the app's grant is not yet verified; Phase 0
  measures it.

## Goals

1. A Linux gateway drives native apps on a Mac enrolled with it as a connected
   computer, with the same tool surface, refusals and audit record as a local
   macOS gateway.
2. No inbound listener on the Mac. No gateway secret on the Mac. No credential
   beyond the connector credential the Mac already holds.
3. Control of the Mac requires a decision made on the Mac, bound to the
   enrollment it was made for.
4. A tunnel drop, a sleeping laptop or a quit app produce a typed refusal,
   never a hang, never a silent success and never a lockout.
5. A Linux gateway nobody enrolled a Mac with, and a Linux gateway with a
   working local driver, behave exactly as they would without this RFC.

## Non-goals

- A Linux driver. #35378 and #25222 own that; this RFC only keeps out of
  their way (§3, "Selection").
- Windows executors. The wire is platform-neutral; a Windows executor is a
  follow-up with its own consent question (its input moves the real cursor).
- Remote executors for a macOS or Windows gateway. Those keep their local
  backend (open question 3).
- Several Macs per gateway at once (open question 4).
- Placing a chat on the Mac. That is connected computers' Session Environments
  provider; Computer Use is per gateway, not per chat.
- Gateway-to-gateway peers (`rfc-remote-crew-sidebar`,
  `rfc-outbound-instance-transport`).
- A governance scope. Computer Use is ungoverned by design
  (`rfc-jev-task-executor` cites AGENTS.md); consent stays the keystone plus
  the Mac capability.

## Design

```
 Linux gateway                                       Mac
 ┌────────────────────────────────┐  SSH -L   ┌───────────────────────────────┐
 │ tools.dispatch_tool            │◄─────────►│ connected-computer connector  │
 │  keystone, policy, index,      │ connector │  (local gateway, or the       │
 │  fingerprint, refusals, audit  │  socket:  │   desktop app's own host)     │
 │ RemoteExecutorBackend          │  channel  │        │ stdio JSON lines     │
 │  lease + generation            │ computer- │  kirocrew computer executor   │
 │ ConnectionBroker channel       │    use    │   floors + MacOSBackend       │
 └────────────────────────────────┘           └───────────────────────────────┘
```

### 1. Enrollment, consent and the channel

**Enrollment is connected computers'.** The Mac enrolls with the gateway over
the SSH login the tunnel rides (`kirocrew connected-computer enroll --stdin`,
#39409) and holds the resulting connector credential under its OS keyring
(#40191 adds the macOS Keychain store). Possession of SSH to the gateway host
is the owner check, as it already is for the dashboard link token. This RFC
mints, stores and fences nothing of its own: revision 1's executor credential,
its digest store, its installation id and their three sandbox-leaf entries are
not needed.

**The desktop capability.** Connected computers' native consent gains a second,
separate grant: "Let this crew use this Mac's desktop", off by default, with
the cursor-overlay option beside it. It is independent of sharing a folder: a Mac can grant the
desktop with no folder, a folder with no desktop, or both. Its text says that
while it is on, this gateway can drive this Mac even if the gateway's own
Computer Use setting shows off, and that driving includes typing into any open
app, so a compromised gateway that reaches an open terminal can run commands as
the Mac user. The executor's typed-text scan narrows that, but it is a pattern
check, not a boundary, and the text does not claim otherwise. The capability is
stored on the local grant, which binds it to that enrollment (the gateway id
and the device record), so consent cannot move to another crew. Disconnect,
Remove and a local Off end it with the rest of the grant. The capability and
its overlay option are local: the gateway learns only whether the capability is
on (`ready`, below), never the option. There is no pointer option: as on a local
gateway, one enable covers the feature, and only a `click_method: "global"` the
model names moves the real pointer (`gate.require_pointer_move`), so the Mac adds
no pointer permit of its own.

**The channel.** A new channel kind, `computer-use`, beside `workspace` and
`mcp:<name>` in `protocol.kind_of`. The gateway opens it; the connector refuses
it unless the grant holds the desktop capability, and otherwise starts one fixed
program, `kirocrew computer executor`, with the overlay option from its own
grant on the command line. The gateway never names the program or its arguments,
exactly as for the other kinds.

- **Not folder-confined.** The executor must reach every window under the
  app's Accessibility and Screen Recording grants. A folder fence such as
  #40191's Seatbelt plan would deny that, so this one kind runs with no folder
  plan and an environment built from nothing, like every connector child. The
  Mac floors (§6) are its boundary. This is the one exemption from connected
  computers' "no unconfined run" rule, it applies only to that fixed argv, and
  it is a decision for the feature's owner (open question 7).
- **Host-scoped.** A `computer-use` channel belongs to the gateway's remote
  backend, not to a chat. `ConnectionBroker.open_channel` takes a chat's `session_key` and counts channels per chat; the remote backend
  opens at most one `computer-use` channel per connection, counted against the
  connection-wide `MAX_CONNECTION_CHANNELS` only.
- **A grant with no folder.** The `ready` frame (`protocol._FIELDS`) requires a
  `root` and a `confinement` plan. A desktop-only grant has neither, and needs
  to say it holds the desktop capability. So version 2 of the protocol gives
  `ready` an optional `desktop: true`, with `root` and `confinement` optional
  when it is set. Today both ends refuse a `hello` or `ready` whose `v` is not
  exactly `protocol.PROTOCOL_VERSION`, and both refuse unknown fields, so the
  version is negotiated rather than bumped:
  - a new connector adds an `X-Connector-Features: computer-use` header to the
    upgrade request; the route reads only the headers it names, so an older
    gateway ignores it;
  - a new gateway answers with a `hello` at `v: 2` only when that header is
    present, and at `v: 1` otherwise, so an older connector still gets the
    `hello` it accepts;
  - `decode`'s exact-match check becomes membership in `{1, 2}` on both ends,
    and a `ready` at `v: 2` is accepted only on a connection whose `hello` was
    `v: 2`; a `ready` at `v: 1` is accepted after either `hello`;
  - a new connector sends the header only for a grant that holds the desktop
    capability, so a folder-only grant always speaks version 1;
  - a new connector that receives a `v: 1` `hello` knows the gateway is older:
    it sends a version 1 `ready` without the desktop capability, so any folder
    share still works, and it tells the person that this crew is too old for
    Computer Use.

  Neither the header nor `hello.v` is authenticated (the `hello` proof covers
  the gateway, device and nonces, and the session MAC starts after `hello`), so
  a relay can only downgrade the connection to version 1, which drops the
  desktop and nothing else; the connector then reports the crew as too old,
  which is wrong but harmless. A relay that raises `v` gets its `ready`
  refused, a denial of service it could cause anyway.

**Identities.** Two, both from connected computers:

- the **enrollment**: the gateway id and the device id with its install
  fingerprint and credential generation. It replaces revision 1's crew
  identity (SSH host key plus installation id);
- the **attachment**: one `computer-use` channel, that is one executor process
  serving one gateway.

**Attach.** The remote backend opens the channel on the live connection of
the computer whose `ready` said `desktop: true`. The executor's first line
asserts the codec version, its platform id, its permission probe (Phase 0
decides what that reads) and a display label. The platform and probe are
validated against fixed vocabularies; the label is bounded, redacted and
display-only. The gateway mints the generation, a monotonically increasing
counter, and stamps it on the channel.

- **Liveness.** The connection's own authenticated ping (15 s) and deadline
  (45 s) cover the channel. The executor is serial: the backend releases the
  next request only after the previous one completes. The watchdog `ping`
  below is the one exception: it carries no `request_id`, is never an
  in-flight entry, and its answer is read and dropped.
- **Kill.** A `close` frame on the channel stops the executor's process tree
  (the connector's own contract for `close`). The connector's `closed` frame is
  the reap report. A stuck driver call cannot block it, because the connector,
  not the executor, handles `close`.
- **The Mac stops itself when it loses the gateway.** The executor exits at once
  when stdin closes, and a watchdog thread calls `os._exit` when no `ping`
  line has arrived on stdin for `MAC_WATCHDOG_S` (15 s), so it dies even if the
  connector itself hangs. The gateway's remote backend writes a `ping` request
  every 5 s while attached. It writes whole lines, so a `ping` always lands at a
  line boundary; the connector relays the channel's bytes unchanged and inserts
  nothing. The executor's reader thread answers `ping` while a driver call runs
  on its worker, so a long call does not starve the watchdog.
- **Replacement after a kill.** A new `computer-use` channel is opened only
  after the old one's `closed`, or after the connection that carried it has
  ended. The second path relies on the connector stopping every child process
  tree when its socket ends, within a bound shorter than the gateway's 45 s
  deadline (open question 8). A retired generation is sent no further requests.
- **One Mac at a time.** Connected computers allows several enrolled
  computers. When more than one live connection says `desktop: true`, the
  backend refuses with `executor_ambiguous`, naming their labels, until the
  person turns the capability off on all but one (open question 4).
- **Index.** Every attach, end and replacement calls
  `index.reset_shared_index()`, and the generation joins the index key, so an
  index walked on one attachment can never resolve on another, even inside
  the 90-second TTL.

### 2. The wire

A closed codec, carried as JSON lines inside the channel's `data` frames
(each frame at most `MAX_DATA_BYTES`, 32 KiB, so one line can span frames and
is reassembled before parsing). Each request is
`{generation, request_id, verb, args}` with a random 128-bit `request_id`
(the watchdog `ping`, §1, is `{generation, verb}` alone); the
gateway completes a request only when both `generation` and `request_id` match
an in-flight entry, under one lock. Validation runs in two stages at both ends.
Before parsing: a line over its byte cap is rejected by length. After parsing,
before any dataclass or driver call: unknown keys, wrong types, non-finite
numbers and structures over the existing ceilings in `computer_use/types.py`
(`MAX_TYPE_TEXT_LEN`, `MAX_KEY_LEN`, `MAX_ACTION_LEN`, `MAX_DRAG_STEPS`,
`MAX_TREE_NODES_LIMIT`, `MAX_TREE_DEPTH_LIMIT`, `MAX_TEXT_LIMIT`,
`MAX_LAUNCH_QUERY_LEN`, the screen-coordinate bounds) and a maximum base64
image size are rejected. The request shapes reuse
`validation.MCP_COMPUTER_SCHEMAS` where a verb has a tool twin.

Verbs: `list_apps`, `resolve_app`, `snapshot`, `click`, `drag`, `type_text`,
`press_key`, `set_value`, `scroll`, `perform_action`, `launch_identity`,
`launch_spawn`, `show_pointer_motion`, `probe_permissions`, and `ping`, which
only feeds the watchdog and has no tool twin. The ABC's
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
Each channel direction has a 1 MiB credit window (`WINDOW_BYTES`), so a large
screenshot streams under backpressure rather than queueing. Phase 4 measures
each case over a real tunnel and records the numbers here.

### 3. Gateway side

- **`RemoteExecutorBackend(ComputerUseBackend)`** in
  `computer_use/remote_backend.py`, over a small transport interface whose one
  production implementation is a `computer-use` channel from
  `connected_computers.broker`. Its blocking methods run on
  `subprocess_executor()` workers and hand off to the loop with
  `run_coroutine_threadsafe`; a call made on the loop thread, or after the loop
  closed, is refused rather than deadlocked.
- **Paired.** The gateway registers the remote backend through the existing
  seam, `register_computer_use_backend(...)`, only on a Linux gateway, only
  while connected computers is servable there (`flag.remote_refusal()` is
  `None`), and only while at least one active device record is paired for the
  desktop. `DeviceRecord` gains a `desktop_paired` field for that. The gateway
  sets it the first time an admitted connection's `ready` says `desktop: true`,
  and only revoke and remove clear it, so pairing outlives sleep, reconnects
  and the toggle. Whether the desktop is on right now is a separate, live
  fact: the `desktop` value of each live connection's `ready`. `ready` is sent
  once per connection, so the connector makes a change visible by redialling:
  turning the capability on or off on the Mac closes its connection and opens
  a new one, whose `ready` carries the new value. Turning it off therefore ends
  the `computer-use` channel with the connection, the record stays paired, and
  the next call returns `no_executor`. A grant that held only the desktop has
  nothing left once it is off, so turning it off is that grant's local Off: the
  connector revokes it and does not redial, the record is revoked, and the
  gateway falls back to `LINUX_REASON` once no paired record remains.
  `executor_ambiguous` counts live connections whose current `ready` said
  `desktop: true`. Unregistering restores `select_default_backend()`'s answer. So a
  Linux gateway nobody enrolled a Mac with keeps today's `LINUX_REASON` and no
  toggle, and a process that registered nothing (`kirocrew computer doctor`,
  `apps`, `call`) keeps today's answer.
- **Selection.** When `select_default_backend()` returns a backend whose
  `status().supported` is true (a local Linux driver from #35378 or #25222),
  that backend stays the default and the remote backend is not registered. The
  remote Mac is used on a Linux gateway whose local backend reports
  unsupported. A person who wants the Mac on a gateway that also has a local
  desktop needs an explicit choice; the natural home is #35378's `driver`
  choice, gaining `remote` (open question 2). Until a choice exists, the local
  driver wins, which is the direction that hides nothing that works today.
- **Status.** `BackendStatus` gains `kind` (`local` or `remote`).
  `RemoteExecutorBackend.status()` reports `kind="remote"`, `supported` true
  only while an attachment is live and its executor says supported, and a
  reason that names the fix when not ("no Mac is attached: open Kiro Crew on
  the Mac and let this crew use its desktop"). `platform_id` is the attached
  executor's platform, else `linux`.
- **Settings.** `_snapshot()` carries `kind` and `attached_label`, and
  `ComputerUsePanel.tsx` renders the toggle when `supported || kind === "remote"`;
  `kind` is `remote` only while the paired rule above holds. Once paired, the
  keystone can be turned on before a Mac attaches and off after it leaves. For
  a remote backend the permission rows come from the attachment's relayed
  probe (refreshed on each attach), not from `_probe_permissions`, which would
  probe the Linux host. Detached, the rows read "no Mac attached". The
  gateway's cursor-motion row is hidden for a remote backend; the overlay
  option lives beside the Mac capability (§1). The permissions section renders
  whatever `platform` says, and the header badge names the attached Mac. The
  attached Mac also shows in connected computers' own device list (#39411).
- **Spec gate.** `linux` joins `_SUPPORTED_PLATFORM_IDS`, which #35378 and
  #25222 also do; whichever lands first carries the edit, and its comment then
  says that a Linux gateway is advertisable through either a local driver or a
  paired Mac. The gate stays attachment-independent: with the keystone on, the
  tools are advertised and a detached call refuses with `no_executor`. Attach
  and detach do not rebuild the spec, so a sleeping laptop does not reset every
  session. The cost is the one `agent.py` names for the gate (about 109 MB of
  computer-use server per chat process); it is paid only once the owner turns
  the keystone on.
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
- **Audit.** Each gate record gains `executor`: the device id, the generation
  and the display label captured once at attach, bounded and redacted like
  other desktop text.
- **Points and frames.** The Mac's native hit test confines points, so the
  remote backend declares that it confines points natively (#35378's
  `confines_points_natively`, when that lands). The live view's frame is
  emitted once per snapshot on the dispatch worker: from `service.snapshot` if
  #35378's `floors.emit_live_frame` is on main, otherwise in the blocking
  backend method body after the re-stamp, never from the loop callback where
  the bytes arrive, where `active_scope()` is `None`.
- **`close()`** fails the backend's queued requests. It does not close the
  channel or contact the executor.
- **Timeouts** come from the bound that governs each verb, not from
  `AX_MESSAGING_TIMEOUT_SECS` (2 s, one accessibility call): `MAX_WALK_SECS`
  (10 s) plus a transport margin for `snapshot`; the driver's own action
  bounds plus a margin for the mutating verbs; the launch window wait for
  `launch_spawn`. `list_apps` and `resolve_app` run an untimed CoreGraphics
  window-list and bundle-metadata pass (`apps_macos.py`), so they get a budget
  of their own, proposed at 5 s and checked against Phase 4's measurements.

### 4. Routes

None. The executor's traffic rides `GET /api/connected-computers/connect`,
which already authenticates itself before the upgrade and every frame after it
(`ws_route.py`). `token_auth` gains no `Authorization` branch, and the
internal secret stays on the gateway. Revision 1's six pull routes are the
fallback's (§7).

### 5. Failure semantics

| Case | Read verb | Mutating verb |
|---|---|---|
| No attachment | `no_executor` | `no_executor` |
| More than one Mac offers its desktop | `executor_ambiguous` | `executor_ambiguous` |
| Generation changed before send | `executor_changed` | `executor_changed`, nothing sent |
| Channel or connection ended after send | `executor_detached` | `outcome_unknown`: take a fresh snapshot before acting again |
| No result within the verb's budget | `executor_timeout` | `outcome_unknown`, and the channel is closed (below) |
| Codec or protocol mismatch | the channel is refused, both versions named | same |

A forwarded call is never retried by the backend, because a click may have
landed. A late result for a retired generation is dropped. This matches
connected computers' own rule that a channel is never resent or moved (B08).

**Killing a stuck executor.** A synchronous driver call in the executor cannot
be cancelled from the gateway. So a mutating timeout, or a lost connection with
a mutating request in flight, retires the generation and closes the channel. A
replacement channel opens only after its `closed`, or after the connection
ended (§1).

### 6. Mac side

- **`kirocrew computer executor`**, a new subcommand in
  `computer_use/cli.py`. It speaks the §2 codec on stdin and stdout, holds one
  `MacOSBackend`, and makes no network calls. The capture path's live-view
  relay (`screencast.emit_snapshot_frame`) posts only inside a published
  `frame_scope`, and the executor never publishes one, so the relay returns at
  its `active_scope() is None` check; Phase 2 asserts no frame POST leaves the
  executor. It calls backend methods, not `dispatch_tool`, so the gateway's
  index and audit stay authoritative. The two launch verbs reuse the existing
  helpers rather than re-implementing them: `launch_identity` calls
  `launch_macos.resolve_target` and `launch_macos.target_identity`, and
  `launch_spawn` calls the existing `launch_app` helper with the pinned name,
  the Mac identity floor as `permit` and the Mac self-window floor as
  `refuse_launched`, so the already-running refusal and the window wait are
  inherited. `kirocrew computer call` cannot serve here: it returns rendered
  text, keeps its index per process and gates on the Mac's keystone.
- **Started by the connector.** The connector runs in the Mac's local gateway
  or, with no local gateway, in the desktop app's own host (#39409's
  `desktop_host.py`, supervised by the app in #39410). Either way the
  executor is that process's child, and Phase 0 measures which of the two
  carries the app's TCC grants.
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
    truncated, depth-truncated, unknown window id) still applies, and, once
    #35378's `floors.harden_snapshot` is on main, that same predicate on the
    snapshot before it is serialised;
  - `policy.resolve_click_method` on the gateway's request, so `auto` never
    resolves to `global` on the Mac either; a pointer-moving gesture still
    needs the model to name `global`, exactly as locally.
- **The Mac option never travels on the wire.** The connector passes the
  overlay option to the executor at spawn time, on the command line, outside
  the codec. The executor treats it as immutable and rejects any request field
  that names it; the connector restarts the executor when it changes.
- **Cursor overlay** is the `show_pointer_motion` verb. The gateway sends it
  where `tools._perform` calls `overlay.show_pointer_motion` today. The
  existing entry point cannot be reused as is: it reads the gateway's
  `cursor_motion` config and schedules onto a loop the invoke handler binds
  (`overlay.py`). So the executor owns a running asyncio loop on its own
  thread and drives `CursorOverlay` through an executor entry that takes the
  spawn-time overlay option instead of reading config. The verb acknowledges
  scheduling only, so ordering stays best effort, as it is locally.
- **The Mac menu** shows "Computer Use: this crew is driving this Mac" with
  Stop (turning the capability off) while an executor runs. That sits with
  connected computers' own menu items (#39410).

### 7. Fallback transport

If connected computers does not ship on macOS, or a maintainer rules that
Computer Use must not depend on its preview and its Session Environments gate
(open question 1), revision 1's transport applies in place of §1's enrollment
and channel and §4. Everything else in this RFC is unchanged by the choice: the
remote backend sits behind a transport interface for this reason. In short:

- **Credential.** A `kirocrew computer executor-token` subcommand, run over
  SSH like the dashboard link token, prints a random bearer credential scoped
  to the executor routes, valid 12 h, revoked by `kirocrew logout`. Only its
  digest is stored, in a crew-home leaf `computer-use-executor` listed in
  `security._CREW_SECRET_LEAVES`, `sandbox_leaves._CREW_HIDDEN_LEAVES` and
  `sandbox_precreate._CREW_PRECREATE_HIDDEN_DIR_LEAVES`, so a mint run inside
  the agent sandbox writes a digest the gateway never reads. The mint verb is
  also refused from an agent shell by `credential-exfil-kirocrew-token` and the
  argv floor. A random gateway installation id, read-only to the sandbox,
  joins the SSH host key as the crew identity the Mac switch is bound to.
- **Routes.** `/api/computer-use/executor/attach`, `/drain`, `/result`,
  `/heartbeat`, `/reaped`, `/detach`, outside the strict-internal class.
  `token_auth` accepts an `Authorization: Bearer` executor credential on that
  prefix only and on no other path, and no other credential there. Every route
  but `attach` also needs the attachment token.
- **Liveness and kill.** A heartbeat every 10 s, an attachment TTL of 30 s, a
  `kill` carried on the heartbeat response, a `reaped` report, and a 15 s Mac
  watchdog, so a killed executor is dead before the gateway admits its
  replacement.
- **Mac side.** A `computer-executor-channel.js` in Electron main supervises
  the executor, with a Mac switch keyed to the crew identity.

## Migration plan

Each phase is its own PR, independently shippable, each naming
[#40197](https://github.com/kirodotdev/KiroCrew/issues/40197).

**Phase 0: TCC probe.** On a signed packaged app in client-only mode, run the
executor as a child of the desktop app's host, and as a child of a local
gateway, and record for each whether it inherits Accessibility and Screen
Recording, including after an ad-hoc re-sign. Exit: a test asserts the
recorded verdict is `inherits` or `does-not-inherit`. Phase 5 is blocked until
it is `inherits`, or until a permission flow for the executor ships.

**Phase 1: the backend seam.** `call_scope()`, `audit_executor()`,
`BackendStatus.kind`, the generation in `AppRef`'s window key, the lease in
`dispatch_tool`, the overlay hint routed by `kind`, the audit `executor` field.
No transport. Exit: every existing `test_computer_use_*` test passes unchanged;
a fake backend whose generation changes before a mutation sends nothing and
after returns `outcome_unknown`; the audit row names the leased generation, not
the one current when the row is written.

**Phase 2: the codec and the executor.** `executor_wire.py`,
`kirocrew computer executor`, handles, the Mac-side floors,
`launch_identity` / `launch_spawn`. Exit: a client that skips the gateway and
writes raw verbs to the executor's stdin is refused, one case each, for: a
secure field via each of the five secure-target verbs, a forged `secure=False`
element over a real password field, a denied command typed into a terminal,
the Kiro Crew window, a handle from another generation, a launch of the Kiro
Crew app, a `launch_spawn` whose token no longer resolves to its pinned
identity, a capture of a secure, truncated, depth-truncated and
unknown-window-id window, an `auto` click that would resolve to `global`, an
unknown verb, and an indexless `type_text` or `press_key`. An over-cap line is
rejected by length before parsing, and an unknown key after parsing and before
any dataclass is built. No spooled capture survives a snapshot, and no frame
POST leaves the executor. A request field naming the spawn-time overlay
option is refused. The executor exits when its stdin closes, and when no ping arrives
for `MAC_WATCHDOG_S` even while a driver call is blocked.

**Phase 3: the forwarding backend.** `remote_backend.py` over the transport
interface, with an in-memory fake transport wrapping `FakeComputerUseBackend`
that offsets `captured_at` by 10^6 seconds and sets a bogus `image_path`; the
paired and selection rules; `kind` in status and Settings. Exit:

- the `test_computer_use_*` tool suite passes through the forwarding backend;
- with no Mac enrolled for its desktop, Settings shows `LINUX_REASON` and no
  toggle, and the tools stay unadvertised;
- with a supported local Linux backend registered, the remote backend is not
  installed, whatever is enrolled;
- once paired and with no attachment, every tool returns `no_executor`, and
  the keystone toggle renders; with two live Macs offering their desktop,
  every tool returns `executor_ambiguous`;
- a producer clock 10^6 s off is still inside the index TTL; the model is
  handed a gateway-spooled path;
- re-attaching with a reused pid and window id resolves no old index;
- a late result is dropped; a mutating timeout retires the generation and
  closes the channel, and no replacement opens before its `closed`;
- `kirocrew computer doctor` on Linux still reports `LINUX_REASON`.

**Phase 4: the `computer-use` channel.** In connected computers: the channel
kind, the negotiated protocol version 2 with `ready.desktop`, the host-scoped open, the
connector's fixed executor argv with no folder plan and the refusal without
the capability, and the desktop capability in the grant and its native consent.
Blocked on #39409 merging, on #40191 or another macOS local side, and on the
feature owner's answers to open questions 5 to 11. Exit, on a Mac in Remote
Tunnel mode, with the tunnel's round-trip time recorded beside each number:

- the agent snapshots and clicks in a Mac app; over 20 element clicks the p95
  is within 2 s, over 10 screenshots of a 1440x900 window the p95 is within
  3 s, and neither sample has a timeout; all three §2 cases are measured and
  recorded;
- the connector refuses a `computer-use` channel while the grant lacks the
  capability;
- the four pairings of old and new gateway with old and new connector each
  complete the handshake, a folder share works in all four, and only the
  new-with-new pairing offers the desktop;
- turning the capability off on a grant that also shares a folder closes the
  connection, the executor exits when its stdin closes, and the next call
  returns `no_executor`; turning it off on a desktop-only grant revokes the
  grant, and Settings returns to `LINUX_REASON`;
- sleeping the laptop and waking it re-attaches after the connection returns;
- quitting the app with a mutation in flight lets a relaunched app attach;
- a timed-out mutation kills the executor, and no input from it lands after
  the replacement attaches;
- with the tunnel cut during a driver call blocked past the gateway timeout,
  the executor is dead within `MAC_WATCHDOG_S`;
- enrolling the same Mac with a different crew leaves that crew without the
  capability;
- the Settings permission rows show the Mac's grants.

**Phase 5: live view and overlay.** Blocked on Phase 0. Exit: a remote
capture's frame reaches an owner dashboard socket, and with the Mac's overlay
option on, a recorded screen capture of a coordinate click shows the overlay
moving; with it off, no overlay process starts.

Phases 1 to 3 need none of connected computers' open PRs and can land first.
Under the fallback (§7), Phase 4 becomes revision 1's routes, credential and
Electron channel, with revision 1's exit criteria for them.

## Backward compatibility

A macOS or Windows gateway is unchanged: `select_default_backend()` still
returns its local driver, and no route it serves changes. A Linux gateway
nobody enrolled a Mac with for its desktop is unchanged: Settings keeps
`LINUX_REASON` and no toggle. A Linux gateway with a working local driver keeps
that driver. Connected computers without the desktop capability is unchanged:
no `computer-use` channel is ever opened. Version negotiation (§1) keeps every
pairing working: an old connector never sends the features header, so a new
gateway speaks version 1 to it and an old Mac never runs an executor; a new
connector against an old gateway receives a version 1 `hello`, keeps any folder
share on version 1, and tells the person the crew is too old for Computer Use.
The CLI on a Linux host is unchanged.

## Security considerations

- **Compromised gateway.** While a Mac is attached the gateway can drive its
  apps; that is the feature. Against a compromised gateway the keystone is not
  a key: the gateway owns the channel and can send raw verbs with its keystone
  off. The Mac capability is the key that holds, so its text says that leaving
  it on lets that gateway drive this Mac whatever the gateway's own setting
  shows. What a compromised gateway gets is no more than an honest gateway
  with an empty operator policy: every floor without a config key runs on the
  Mac, on a record the Mac resolved, so it cannot type into a password field,
  type a denied command, drive or launch the Kiro Crew window, capture a
  secure window, or move the real pointer except through a `global` gesture
  it names, which is the local rule too. Terminals stay drivable, as they are locally
  (`check_app` keeps them off its denylist), but the text scan runs on the Mac
  for every typed string. The scan is a pattern check, not a boundary: a
  compromised gateway that reaches an open terminal can still run commands
  as the Mac user, which is why the capability text names that outcome.
- **The confinement exemption.** Every other connector child runs inside a
  folder fence. The executor does not, because it must reach every window.
  What bounds it instead is the fixed argv (the gateway names a kind, never a
  program), the closed codec, and the Mac floors. Whether that exemption is
  acceptable is the feature owner's call (open question 7).
- **Compromised Mac app.** It can already reach the gateway as the owner over
  SSH, so the channel gives it nothing it did not have.
- **Credential.** The only credential is connected computers' connector
  credential: scoped by audience, gateway, device, fingerprint and generation;
  never sent on the socket (the connector proves possession); revoked on the
  device record. A leaked one lets its holder pose as that computer: it can
  offer fabricated trees and deny the real Mac, but it reaches no input on the
  real Mac, whose executor acts only on its own channel's requests.
- **The agent on the gateway host.** It shares the host enrollment runs on.
  Whether it can enroll a computer of its own through
  `kirocrew connected-computer enroll --stdin` is connected computers'
  question (open question 9); at worst such a computer is the leaked-credential
  case above.
- **Data placement.** Screenshot bytes land in the gateway's spool, under the
  same agent-readable screenshot rule; rendered trees and image paths land in
  the chat transcript. The audit log records the invocation or refusal and the
  leased attachment, not pixels or trees. Nothing is left on the Mac.
- **No new listener and no new route.** The Mac only dials the tunnel's local
  end. The executor makes no network calls.

## Alternatives considered

- **A transport of its own** (revision 1): a scoped executor credential and six
  pull routes. It works and is kept as the fallback (§7), but it is a second
  tunnelled credential scheme for the same Mac and crew, with its own store,
  sandbox fences, revocation and Mac switch, beside a socket that already
  authenticates every frame and a consent flow that already exists.
- **Expose Computer Use to the gateway as one of the Mac's MCP servers**
  (`mcp:<name>`). Rejected: it moves the whole dispatch to the Mac (below), and
  #39409's connector refuses to expose Kiro Crew's own servers.
- **Move the whole dispatch to the Mac** (forward MCP calls, run
  `dispatch_tool` there). One gate, on the Mac. Rejected: the gateway's audit,
  its operator policy and `rfc-jev-task-executor`'s second caller of
  `dispatch_tool` would all fork, and `kirocrew computer call` already shows
  the cost (text results, per-process index, Mac keystone).
- **Reverse SSH tunnel to a Mac listener.** Rejected: an inbound port on a
  laptop, and a new credential the gateway would hold for it.
- **Run a second gateway on the Mac.** Works today, but splits memory, crons
  and sessions across two crews.

## Open questions

For a maintainer:

1. Should remote Computer Use depend on connected computers, inheriting its
   preview, its Session Environments developer gate (`flag.remote_refusal()`)
   and its Shared Crew exclusion? Yes means §1 and §4. No means the fallback
   (§7), or a second gate in `connected_computers/flag.py` that serves only the
   `computer-use` channel without Session Environments.
2. On a Linux gateway that has both a working local driver and a paired Mac,
   is the owner's choice a `remote` value in #35378's `driver` setting, or
   something else? Until it exists the local driver wins (§3).
3. Should a macOS gateway also accept a remote executor, choosing per call
   between its own desktop and another Mac? Out of scope here; `kind` leaves
   room for it.
4. One Mac per gateway, or several with a `machine` argument on every tool?
   This RFC proposes one, refusing with `executor_ambiguous` while two offer
   their desktop.

For connected computers' author, CrysisDeu
([#29949](https://github.com/kirodotdev/KiroCrew/issues/29949), #39409 to
#40191), who owns the protocol, the connector and the consent this RFC builds
on:

5. Will the protocol take a `computer-use` channel kind and a version 2 `ready`
   with `desktop: true` and an optional `root` and `confinement`?
6. Will the broker take a host-scoped open, one per connection, outside the
   per-chat count?
7. Is the confinement exemption for the one fixed executor argv acceptable, with
   the Mac floors (§6) as its boundary?
8. Does the connector stop every child process tree when its socket ends,
   within a bound shorter than the gateway's 45 s deadline? §1's replacement
   rule depends on it.
9. Can an agent on the gateway host run `kirocrew connected-computer enroll
   --stdin` and enroll a computer of its own?
10. Does the desktop capability belong in the same native consent dialog as the
    folder share, or in its own menu item?
11. Will the device record take a `desktop_paired` field set from `ready`, and
    will the connector redial when the capability changes, revoking a
    desktop-only grant instead (§3, "Paired")?
