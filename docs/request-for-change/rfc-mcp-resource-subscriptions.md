---
title: MCP resource subscriptions -- wake a session when an MCP resource it subscribed to changes
status: draft
author: unstablebrainiac
created: 2026-10-13
last-audited: 2026-10-13
audited-at: de2d143dc
doc-pr: null
implementation-prs: []
tracking-issues: [19694]
supersedes: []
superseded-by: []
---

# RFC: MCP resource subscriptions

- Status: draft. Nothing is built. Code facts are measured at `de2d143dc`.
- Author: unstablebrainiac
- Tracking issue: [#19694](https://github.com/kirodotdev/KiroCrew/issues/19694)
- Related: [rfc-webhook-subscriptions.md](rfc-webhook-subscriptions.md) (this RFC
  adds a source to it and uses its buffer, subscription and wake),
  [rfc-consolidated-monitor.md](rfc-consolidated-monitor.md) (no new wake path),
  [rfc-agent-started-turns.md](rfc-agent-started-turns.md) (the other way a turn
  starts without a prompt),
  [mcp-shareability.md](../system-specs/modules/mcp-shareability.md) (today's
  per-URI routing),
  [mcp-gateway-backend-replacement.md](../system-specs/modules/mcp-gateway-backend-replacement.md)
  (respawn and replay),
  [mcp-gateway-daemon-lifecycle.md](../system-specs/modules/mcp-gateway-daemon-lifecycle.md).

## 1. Summary

MCP lets a server tell a client that a resource changed. The client sends
`resources/subscribe` for a URI, and the server sends
`notifications/resources/updated` when that resource changes. Kiro Crew's MCP
gateway already routes those notifications to the stub that subscribed. Nothing
reaches a chat, though. The subscription belongs to the chat's kiro-cli
connection, so it ends when Kiro Crew stops an idle chat's kiro-cli, and an update
that does arrive has no way to start a turn.

This RFC lets a session subscribe to a resource on one of its MCP servers and be
woken when it changes, whether or not its kiro-cli is running. Kiro Crew holds the
subscription itself, through an ordinary gateway stub it opens for the session
(the **holder**). Each update becomes an event in the webhook-subscriptions
buffer, and that RFC's `session` consumer delivers the wake. The change adds an
event source, not a wake path.

## 2. Motivation

### 2.1 Watch loops mostly find nothing

A session that waits on something outside Kiro Crew polls for it on a timer. On
one operator's gateway between 2026-10-06 and 2026-10-13, twelve such loops
watched pull requests and code reviews. Together they woke about 2,100 times:

| Outcome | Share of wakes |
|---|---|
| Acted: pushed, commented, messaged or spawned | about 22% |
| Found nothing | about 39%, costing about 34 agent-hours and 6,750 tool calls |
| Reported status without acting | the rest |

Loops on code reviews found nothing on 80% of their wakes. Twelve one-shot crons
existed only to re-arm loops past their seven-day runtime cap. These are one
gateway's numbers, not a fleet measurement.

A server that pushes its changes removes the empty wakes, and MCP already defines
that push.

### 2.2 What the gateway already does

- **Per-URI routing.** `Backend._resource_subscriptions` maps each URI to the
  stubs that subscribed to it (`src/kiro_crew/mcp_gateway/backend.py`), and
  `Backend._route_backend_message` sends `notifications/resources/updated` only to
  those stubs. An update for a URI nobody subscribed to is dropped.
- **Per-stub subscribe and unsubscribe.** A server that does not accept caller
  identity gets one coalesced upstream lease per URI. A server that does gets a
  grant per caller (`Backend._handle_resource_subscription`).
- **Replay on respawn.** A respawned backend gets the stub's subscriptions
  replayed (`Backend.replay_resource_subscriptions`).

### 2.3 What is missing

| Gap | Evidence |
|---|---|
| A subscription ends with the chat's kiro-cli connection | `Backend.detach_stub` releases the upstream subscription when the last subscriber leaves. A reaped kiro-cli detaches its stub, and the backend is evicted `mcp_gateway.idle_timeout_secs` later (default 300, `src/kiro_crew/config/integration_sections.py`). |
| A subscription does not survive a daemon restart | The subscription tables live in the daemon process. A kiro-cli stub that ever subscribed refuses to reconnect (`has_live_subscriptions` in `src/kiro_crew/mcp_gateway/stub_bridge.py`). The daemon exits when the Kiro Crew server that spawned it exits (`_owner_liveness_sweeper` in `src/kiro_crew/mcp_gateway/daemon/sweepers.py`), so every server restart is also a daemon restart. |
| An update cannot start a turn | On the kiro-cli backend a turn starts only when Kiro Crew sends a prompt. Nothing turns an update into one. |

## 3. Goals and non-goals

Goals:

- A session subscribes to a (server, URI) pair on an MCP server it already has,
  and is woken when the server reports a change.
- The subscription survives the session's kiro-cli being stopped, the backend
  process dying, and a Kiro Crew restart.
- Updates are coalesced and budgeted, and reach the model as untrusted data.
- A subscription that cannot be kept up pauses with a reason. It never stops
  silently.
- Nothing changes for a session that does not subscribe.

Not a goal:

- Carrying resource contents in the wake. The wake names the URI, and the session
  reads what it needs with its own tools.
- MCP servers the gateway does not front. Without a pooled backend there is
  nothing for a holder to attach to.
- Sessions without a dashboard slot, in the first version. `dashboard_slot_key`
  returns an empty string for them (`src/kiro_crew/dashboard/chat_utils_slots.py`).
- Changing how kiro-cli's own subscriptions behave.

## 4. Design

### 4.1 The holder: a stub Kiro Crew opens for the session

For each (session, server) pair with at least one subscription, the Kiro Crew
server opens a gateway stub connection of its own, the holder. It registers with
the session's own stub's Register payload for that server, so it attaches to the
same pooled backend, but under a `stub_uuid` of its own. The daemon keys attach,
refcount and routing on that id, and `Backend.attach_stub` refuses one that is
already attached, so the holder mints a fresh `stub_uuid` for every registration,
as the stub's own `_reconnect` does (`src/kiro_crew/mcp_gateway/stub_link.py`). It then
sends `initialize`, sends `resources/subscribe` for each URI, and reads updates on
its own socket.

The holder shares the stub's backend only when that server is pooled. A Register
without `poolable`, or a server the daemon has retreated from sharing after it
behaved per-client, gets a private backend keyed on the connection's `stub_uuid`
(`exclusive_stub_uuid` in `_handle_connection`,
`src/kiro_crew/mcp_gateway/daemon/connection.py`). There the holder runs a backend
of its own: its subscription stands alone, §4.4 does not apply, and that backend
counts against `max_pinned_backends`.

The holder gets that Register payload from a new one-shot control frame,
`describe-session`. Given a session key and a server name, it returns the Register
payload, with the session token and the stub's `stub_uuid` removed, and the
captured `initialize` request of
the session's live stub for that server. The daemon keeps neither today:
`_StubConn.__slots__` has no Register (`src/kiro_crew/mcp_gateway/daemon/identity.py`),
and the captured `initialize` is `captured_init`, a local of `_handle_connection`
(`src/kiro_crew/mcp_gateway/daemon/connection.py`). Phase 2 stores both on the
connection. The subscription record keeps them, so the holder can register again
after a restart with no kiro-cli present. Persisting a Register is not new:
prewarm already writes Register payloads, token removed, to `hot-keys.json`.

The holder's identity is the session key, not a stored token. Kiro Crew mints a
stub token for the holder and pushes a `claim` frame that binds it to the stored
session key, the same frame a kiro-cli client gets. `build_claim_frame` builds it
(`src/kiro_crew/mcp_gateway/claim.py`): the frame
carries the session key, the session type derived from it, the gateway process's
`principal_id` and the session's `channel_id`, and the daemon's `_apply_claim`
sets the holder connection's caller from it (`src/kiro_crew/mcp_gateway/daemon/control.py`,
`_caller_from_register` in `daemon/identity.py`). The session token does not need
to survive a restart. It is minted per client in `AcpClient.__init__`
(`mint_stub_session_token` in `claim.py`, called from `src/kiro_crew/acp/client_class.py`),
so a session's kiro-cli started after a restart is a fresh client with a fresh
token, and the daemon's token-to-caller bindings live only in the daemon process
(`_TOKEN_BINDINGS` in `daemon/identity.py`) and are gone after a restart. What
makes the holder the same caller as the session's own stub is the claim's session
key, which the subscription record stores. §4.4 shows why the token value never
enters the comparison on a server the gateway fronts.

§8.1 explains why the holder is a stub rather than code inside the daemon.

### 4.2 Initialization

A backend that no client has initialized cannot take a subscribe, and after a
restart the holder may be its first client. The holder therefore sends the
`initialize` request that the session's own stub sent, as `describe-session`
returned it. If the backend is already initialized, the daemon answers from its
cached result, as it does for any later stub. Either way the backend sees the
`initialize` it would have seen from the session's kiro-cli. Phase 0 checks that
this request carries nothing secret and nothing that differs per process.

### 4.3 Liveness

Each failure has an owner:

| Failure | What notices it | What happens |
|---|---|---|
| The backend process dies | The holder forwards an MCP `ping` request every 60 s, and the forward raises `BackendGone` | The daemon's existing respawn path rebuilds the backend, re-primes it from the holder's captured `initialize` and replays the holder's subscriptions (the `BackendGone` handler in `_handle_connection`). |
| The daemon or the Kiro Crew server restarts | The holder's socket reaches end of file, or the server starts with stored subscriptions | The holder reconnects once the manager has a daemon, registers from the stored Register under a fresh `stub_uuid`, is claimed with the stored session key through a fresh `claim` frame (binding a token it mints for itself; no token from before the restart is reused), sends the stored `initialize` and subscribes to every URI again. |
| Updates are lost during a gap | Any re-subscribe | One "may have changed" event per URI, because updates sent while nobody held the subscription are gone. |
| The backend cannot be started again | Registering or subscribing fails, for example because the launch approval changed | The subscription pauses with the reason, and the pause is reported. |

The daemon answers its own in-band `ping` frame without touching the backend, so
that frame cannot detect a dead backend. The MCP `ping` request is forwarded to
the backend, so it can.

### 4.4 Sharing a grant with the session's own stub

A server that accepts caller identity holds one grant per caller. The holder and
the session's own stub are meant to be the same caller, so they must share one
grant. The gateway already guards a shared grant: when one stub leaves, it
releases upstream only if no other routed stub holds a grant from the same caller.
Three places make that check: `Backend.evict_stub_subscriptions`, which the
`claim` frame's handler calls when it rekeys or clears a connection's identity
(`daemon/control.py`), `Backend.detach_stub` for a stub that leaves, and
`Backend._handle_resource_subscription` for an unsubscribe. All three compare the
whole `CallerContext` with `==`.
`CallerContext` is a frozen dataclass, so equality covers `session_type`,
`principal_id`, `channel_id`, `from_gateway`, `raw`, `session_token` and
`identity_denial` as well as `session_key` (`src/kiro_crew/mcp_caller.py`). A holder
whose caller differs from the kiro-cli stub's in any of those fields is not counted
as a sharer. If the difference is in a field the upstream server never sees
(`build_caller_meta` sends neither `from_gateway` nor `raw`), the server holds one
grant for both. The kiro-cli stub's unsubscribe or exit then releases that grant
upstream, and the holder stops receiving updates while it still looks subscribed.

The right fix is to make the holder present a `CallerContext` equal to the
session's kiro-cli stub's, not to loosen the check. The whole-`CallerContext` `==`
is correct: the upstream server tells callers apart by more than a session key, so
a check that merged two callers the server keeps apart would release or leak a
grant on a server that keys by principal or token. The holder is already set up to
be that same caller. It attaches to the same pooled backend, and the daemon
attributes it with a `claim`, the same mechanism that attributes the session's own
kiro-cli stub.

The grant caller the three checks compare is the caller recorded at grant time,
`pending.caller` (`Backend._on_resource_subscription_response` writes
`_grant_callers[(uri, stub_uuid)]`). That caller is what the daemon forwards for
the connection, `_caller_for_backend(backend, conn.caller, conn)`
(`daemon/connection.py`, `daemon/control_plane.py`). Field by field, for a holder
and a kiro-cli stub of the same session on a server the gateway fronts:

- `session_key`, `session_type`, `principal_id`, `channel_id`: these come from the
  `claim` frame. `_caller_from_register` builds the caller from it
  (`daemon/identity.py`), and `build_claim_frame` fills the frame with the session
  key, the session type derived from the key by `classify_session_type`, the
  gateway process's `principal_id`, and the session's `channel_id`
  (`src/kiro_crew/mcp_gateway/claim.py`). The holder is claimed by the same gateway
  process, so `principal_id` matches, and a claim carrying the same session key and
  `channel_id` makes all four match. `channel_id` is the one field the holder's
  claim must be given explicitly; the other three follow from the session key.
- `from_gateway`: `_caller_from_register` sets it `True` for every claimed caller.
- `raw`: `_caller_from_register` leaves it the empty default. It is populated only
  by `CallerContext.from_meta`, which parses a forwarded request on the backend
  side, not the daemon-side caller the grant records. So it is empty for both.
- `session_token`: `_caller_for_backend` attaches the token only to a Kiro Crew
  control-plane backend; for a third-party server it returns the caller unchanged,
  and `_caller_from_register` never set a token, so the field is empty for both.
  The token value never enters the comparison on a server the gateway fronts. On
  a Kiro Crew control plane the holder carries its own token, so it is a separate
  caller there with its own grant, and the gateway and the server agree.
- `identity_denial`: set only on a frame forwarded to a denied control-plane
  backend (`_caller_for_backend`). Empty for a third-party server, for both.

So the holder's grant caller equals the stub's once the holder is claimed with the
session's own session key and `channel_id`. No field needs a loosened check. Phase
0(b) measures which field, if any, actually differs. Phase 1 proves the existing
checks share the grant for two connections that present the same caller, and Phase
2 builds the holder to carry that field in its claim, changing no survivor check. A server that does not
accept caller identity uses one lease per URI, which stubs already share safely.

### 4.5 From update to wake

The holder turns each `notifications/resources/updated` into an event in the
webhook-subscriptions event buffer
([rfc-webhook-subscriptions.md](rfc-webhook-subscriptions.md) §5.2):

| Field | Value |
|---|---|
| source | `mcp` |
| kind | `resources/updated` |
| key | `<server name> <uri>` |
| data | nothing beyond the URI |

The subscription is a webhook subscription on one built-in `mcp` source. It gets
that RFC's consumer, coalescing, budget, pause and approval posture unchanged. Its
`session` consumer delivers a `[Webhook wake]` envelope with the URI inside the
framed, redacted excerpt and the server name as the label. No other wake path is
added.

Two things depart from the webhook RFC, and this RFC records them as decisions:

- **Who creates the source.** That RFC makes every source operator-only (§5.1).
  The `mcp` source has no secret and no ingress, so there is nothing for an
  operator to create. Instead the operator turns it on with
  `mcp_gateway.resource_subscriptions.enabled`. Sessions never create a source.
  They create subscriptions on it targeting themselves, which that RFC's §9
  already allows.
- **Who writes the instruction.** That RFC calls the `instruction` operator-owned
  text (§5.3). A subscription a session creates for itself carries an instruction
  the session wrote, which is the authority `monitor_start` gives a session today:
  its message is re-injected as the session's next turn. Three gates stay with the
  operator: the subscription shows on the Webhooks page, where the operator can
  edit or pause it, and the `kirocrew webhook sub` CLI removes it; a subscription a
  session creates always gets that RFC's default `interactive` posture (§7.6), and
  only the operator can change it to `trusted`; and the operator setting above turns
  the whole source off.

### 4.6 Limits

- 20 resource subscriptions per session. Beyond that, a subscribe is refused.
- Coalescing per key, using the webhook RFC's `window_secs`. Proposed default:
  60 s.
- `budget.wakes_per_day`. Proposed default: 48. A subscription that exhausts it
  pauses and reports, as the webhook RFC §13 requires.
- The gateway's own limits still apply: 1,024 subscriptions per stub
  (`_RESOURCE_SUBSCRIPTIONS_MAX_PER_STUB`) and 8,192 characters per URI
  (`_RESOURCE_URI_MAX_LEN`), both in `backend_leases.py`.
- A holder keeps its backend's refcount above zero, so the backend is never evicted
  while subscribed. A backend with refcount above zero is never an eviction
  target, and a spawn into a full pool is refused with a `capacity` rejection
  (`BackendPool` in `src/kiro_crew/mcp_gateway/pool.py`). Unbounded holders could
  therefore fill `mcp_gateway.max_backends` (default 64) and refuse backends to
  sessions that hold no subscription. Holders may pin at most
  `mcp_gateway.resource_subscriptions.max_pinned_backends` backends. Proposed
  default: 16, a quarter of the default pool. A holder shares the backend of every
  session with the same pool key, so the cap counts backends, not sessions. A
  subscribe that would pin one more backend than the cap is refused with a reason.
- The holder drains its inbox without blocking. The gateway detaches a stub whose
  inbox overflows (`_STUB_INBOX_MAXSIZE = 4096`), and a detached holder would lose
  every subscription it carries.

### 4.7 Lifecycle

A session's resource subscriptions and holders are removed when the session is
closed, trashed or emptied. The cleanup sits beside `_retire_slot_nudge_loop` in
`_close_slot` (`src/kiro_crew/dashboard/chat_api/slot_lifecycle.py`) and in the
session-storage routes. A conversation reset keeps the session key, so it keeps
the subscriptions. A wake for a session that can no longer be resolved deletes the
subscription.

### 4.8 Surfaces

- **MCP tool.** The webhook RFC's `webhook_subscribe` accepts a resource source,
  `{"mcp": {"server": "<name>", "uri": "<uri>"}}`. The calling session comes from
  `mcp_core.require_strict_session_key`, and a session can subscribe only itself.
  The call is synchronous: it checks that the session has that server and that the
  server declares `resources.subscribe`, and it returns the server's answer to the
  subscribe. A session cannot set the approval posture. `webhook_list`,
  `webhook_pause`, `webhook_resume` and removal treat a resource subscription like
  any other.
- **Config.** `mcp_gateway.resource_subscriptions.enabled`, default false, and
  `mcp_gateway.resource_subscriptions.max_pinned_backends`, default 16.

## 5. Phases

| Phase | Delivers | Exit criteria | Blocked on |
|---|---|---|---|
| 0 | This RFC and two probes | Verdicts recorded under §9: (a) a holder that replays a captured `initialize` against `@modelcontextprotocol/server-everything`, and against one other server that declares `resources.subscribe`, gets the same capabilities as the original client, and the request carries no secret; (b) a gateway test that compares the `CallerContext` a second connection carries, after it registers and is claimed with the session's own session key and `channel_id`, against the session's kiro-cli stub's grant caller on a server that accepts caller identity, and names any field that still differs | nothing |
| 1 | A gateway test proving the second connection shares the grant | With the second connection claimed as in 0(b), the existing survivor checks (`evict_stub_subscriptions`, `detach_stub`, `_handle_resource_subscription`) count it as a sharer, so the kiro-cli stub's detach, unsubscribe or `claim` rekey does not release the shared grant. No survivor check is changed. The existing subscription tests pass unchanged | Phase 0 (b) |
| 2 | `describe-session`, the holder, its store and its liveness, behind the config flag. The holder is claimed with the session's own session key and `channel_id`, carrying whatever field 0(b) named, so its grant caller equals the stub's | `test/fake_pool_mcp_server.py` is extended to accept a subscribe and send a scripted update. The update reaches the holder after (1) the session's kiro-cli stub detaches, (2) the backend process is killed, and (3) the daemon restarts. A changed launch approval pauses the subscription with a reason. A subscribe that would pin one more backend than `max_pinned_backends` is refused with a reason | Phase 0 (a), Phase 1 |
| 3 | Holder events into the event buffer, and wakes through the `session` consumer | One update produces one `[Webhook wake]` per coalescing window, with the URI only inside the framed excerpt | rfc-webhook-subscriptions PR 1 and PR 2 |
| 4 | The resource source on `webhook_subscribe` | A session can subscribe, list, pause and remove its own resource subscriptions, and is refused another session's | rfc-webhook-subscriptions PR 3 |

Phases 1 and 2 need nothing from the webhook RFC. Phase 1 asserts a property of the existing
checks rather than changing them: two connections of one session that present the
same caller already share one grant, so no code path is loosened.

## 6. Backward compatibility

Compatible. Nothing changes unless `mcp_gateway.resource_subscriptions.enabled`
is set and a session subscribes. Sharing a grant between a session's connections
is unchanged: the survivor checks keep comparing the whole `CallerContext`, and a
holder shares the session's grant by presenting the same caller.

## 7. Security considerations

- **No new trust path.** The daemon already accepts a Register from a same-uid
  peer that names a session key (`_caller_from_register`, and `needs_identity` in
  `_resolve_register_identity`, both in `daemon/identity.py`). The holder
  uses that path. Phase 2 also attributes the holder with the session's session
  key through the existing `claim` frame, so the daemon sets its caller the way it
  sets a kiro-cli stub's. Like a kiro-cli client's claim, it binds a stub token
  the holder mints for itself, and that token does not reach a third-party
  backend's grant caller (§4.4).
- **The server controls only a URI.** An update carries a URI the server chose. It
  reaches the model only redacted and inside the webhook RFC's framed excerpt,
  never as an instruction. The subscription's instruction, which the session
  wrote, is the only part of a wake that instructs.
- **A server cannot subscribe a session.** Subscriptions come only from the
  session's own tool call, and the existing routing drops an update for a URI no
  holder subscribed to.
- **Unattended turns.** A wake runs under the subscription's own approval posture
  (webhook RFC §7.6) and never inherits trust from an interactive turn.
- **Flooding.** Coalescing and the daily budget bound the wakes a server can
  cause, and the inbox drain bounds the holder's memory.

## 8. Alternatives considered

### 8.1 The daemon holds the subscription itself

An internal stub inside the daemon, as `app_call` uses for one-shot requests, was
the first design. It needs three mechanisms that do not exist:

- A fresh backend sits at `Backend._init_state = "unsent"`. The daemon can drive
  `initialize` on it only through `Backend.prime_initialize`, with a stub's
  captured request, and it holds that request only while the stub's connection is
  live. The docstring of `_acquire_backend` (`src/kiro_crew/mcp_gateway/gatewayd.py`)
  states the consequence: "nothing will initialise a warm backend until a stub
  attaches".
- Respawn and replay run only from a live stub connection's forward loop (the
  `BackendGone` handler in `_handle_connection`). An internal stub would need its
  own copy of that path.
- Updates would need a stream from the daemon to the server. Every control frame
  is one-shot (the module docstring of `daemon/control.py`, and
  `GatewayManager._control_roundtrip` in `src/kiro_crew/mcp_gateway/manager.py`),
  and none of them can push.

The holder avoids all three. It initializes like any client, the existing respawn
path serves it, and updates arrive on its own socket.

### 8.2 Keep kiro-cli running while a chat is subscribed

This costs a kiro-cli process for every waiting chat, and it still leaves the
update with no way to start a turn.

### 8.3 Keep polling

The status quo described in §2.1.

### 8.4 Extend `monitor_watch`

A structured monitor watches one target per session through probes written for
each provider. It cannot watch an arbitrary MCP server, and it still polls.

## 9. Open questions

1. **Order against the webhook RFC.** Phase 3 needs that RFC's PR 1 and PR 2,
   which are not built. If they will not land soon, should Phase 3 deliver through
   the MCP Apps injection path (`api_mcp_apps_message` in
   `src/kiro_crew/dashboard/handlers/mcp_apps.py`) with an in-memory coalescer per
   key, replaced by the buffer when PR 2 lands?
2. **Probe verdicts.** Phase 0 records verdicts (a) and (b) here.
3. **Reading the resource.** kiro-cli may not expose `resources/read` to the model.
   Should the core server offer a read through the holder, so a woken session can
   read the resource on any server?
4. **Sessions without a dashboard slot.** Channel-born sessions are outside the
   first version. Should a later phase cover them?
5. **Session-written instructions.** §4.5 departs from the webhook RFC's
   operator-owned instruction and rests on the `monitor_start` precedent. Is that
   enough, or should a resource subscription carry no instruction and wake with the
   URI alone?
