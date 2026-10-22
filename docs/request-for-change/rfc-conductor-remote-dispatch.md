---
title: Local conductor dispatches sessions to a remote crew
status: draft
author: zejiangg
created: 2026-10-06
last-audited: 2026-10-06
audited-at: ebf647989a
doc-pr: 17273
implementation-prs: []
tracking-issues: [13476]
supersedes: []
superseded-by: []
---

# RFC: Local conductor dispatches sessions to a remote crew

**Status:** `draft`. Nothing here is built. Measured against main `ebf647989a`.
Builds on the accepted [rfc-remote-crew-sidebar.md](rfc-remote-crew-sidebar.md):
a session on a crew is owned by the peer, and this machine is only a window onto
it through `/api/instances/{id}/proxy/...`.

## Summary

A conductor on this gateway can only dispatch workers onto this gateway. This
RFC lets it create and drive a worker session **on a connected crew**. The peer
owns that session, exactly like a window-view chat: it shows in the crew's
sidebar group, and the person can open it and take over.

Three decisions:

1. The five session tools gain one argument, `crew=`. The hub carries each call
   to routes the peer already serves under `api/chat`. No new proxy prefix.
2. The worker reports to the **peer's** ledger. The hub **pulls** those reports
   when the conductor reads its own ledger. The peer never pushes.
3. A remote session is born bound: `session_create(crew=, work_item=)` creates
   the peer slot and its binding in one call.

## Motivation

### Current state

- **Session tools.** `session_create`, `session_send`, `session_read_message`,
  `session_stop` and `session_close` (`_session_tools` in
  `src/kiro_crew/mcp_dashboard_session_tools.py`) call `/api/session-control/*` on this
  gateway. Those routes are MCP-only and strict (`dashboard/server_internal_paths.py`, the
  *Session control* block of the internal-secret path set). No argument names
  another machine.
- **The proxy.** `api_instances_proxy` (`src/kiro_crew/dashboard/handlers_instances_peer.py`)
  forwards only paths under `_PROXY_ALLOWED_PREFIXES` = `api/chat`, `api/stream`,
  owner-only, and redacts every reply since
  [#16757](https://github.com/kirodotdev/KiroCrew/pull/16757)
  (`_redact_peer_payload`, `_redact_sse_event`). The fence lives in the HTTP
  handler (`_proxy_canonical_path`); `SshTunnelManager.proxy_request` itself
  checks no path, so a server-side caller is not fenced today.
- **What the peer already serves under `api/chat`.** Create
  (`POST api/chat/slots`, which takes `agent`, `model`, `memory_mode`; see
  `create_peer_slot` in `remote_relay.py`), rename
  (`PATCH api/chat/slots/{slot}/title`), send (`POST api/chat?ws=1`, which
  answers at once and queues on a busy slot), read
  (`GET api/chat/slots/{slot}?limit=N`), stop
  (`POST api/chat/slots/{slot}/stop`) and close (`DELETE api/chat/slots/{slot}`,
  the same `close_slot` sequence session-control's close runs). The window view
  ([#16757](https://github.com/kirodotdev/KiroCrew/pull/16757)) and the
  per-machine sidebar ([#16756](https://github.com/kirodotdev/KiroCrew/pull/16756))
  use exactly these.
- **The work ledger.** `src/kiro_crew/work_ledger.py` stores items per
  conductor; `src/kiro_crew/mcp_work.py` serves `work_brief`, `work_report`,
  `work_ledger_read`, `work_ledger_record`, `work_ledger_rebuild` over
  `/api/work-ledger/*`, also
  MCP-only. A worker is found by its **binding** (`binding_path`, keyed by the
  worker's own session key). `bind` is admitted only for a live LOCAL slot whose
  `_created_by` names the conductor (`handlers/work_ledger.py`). `orphaned` and
  `stale` are derived at read time (`is_orphaned`, `is_stale`) from local slot
  state (`_slot_running`, `_slot_closed`).

### Problems

| Gap | Where it breaks |
|---|---|
| No crew target | The session tools reach only this gateway's slot table. |
| Unbindable | `bind` looks up a local slot; a peer slot has none, so it answers `unknown_worker_session`. |
| Reports land elsewhere | A worker on the peer calls the PEER's `work_brief`; the peer has no binding, so it answers `not_bound`, and its `work_report` writes nothing the hub can see. |
| Wrong flags | `ledger_wake.worker_running` and `ledger_wake.worker_closed` read local slots. The read rows (`_slot_closed` in `handlers/work_ledger.py`) and the wake gate (`probes/work_ledger.py`, `slack/gateway_runtime/nudge_wiring.py`) both use them, so a remote worker reads as closed and, once it has reported, wakes the conductor as stalled at once. |

The tunnel only opens hub → peer, so the peer cannot call the hub back.

## Goals

- A conductor dispatches one leaf item onto a named, connected crew and drives it
  with the same five verbs it uses locally.
- The worker's `work_brief` and `work_report` work unchanged on the peer.
- The person sees the worker in that crew's sidebar group and can take over.

## Non-goals

- Automatic placement (pick a crew for me). The conductor names the crew.
- `spawn_run` on a crew ([#12821](https://github.com/kirodotdev/KiroCrew/pull/12821)).
- Reading arbitrary peer history ([#6874](https://github.com/kirodotdev/KiroCrew/pull/6874)).
- A remote **conductor** (a sub-conductor on the peer). Leaf workers only.
- Pushing anything from the peer to the hub, or any new transport.
- Uploading source to the peer. The worker uses the peer's own checkout.

## Design

### 1. Tools: a `crew=` target

| | (a) `crew=` on the five tools, hub routes to `api/chat` | (b) New proxy row `api/session-control` | (c) New `crew_session_*` tool family |
|---|---|---|---|
| New proxy prefix | none | one | none |
| Peer auth | the owner token the proxy already holds | session-control is internal-secret only, so the hub would need the peer's internal secret: a hub-held credential the peer can replay | as (a) |
| Conductor skill | one argument | one argument | a second set of verbs and branches |
| Tool count | unchanged | unchanged | +5 |

**Recommend (a).** Each call becomes:

| Tool | Peer call | Row |
|---|---|---|
| `session_create(crew=, work_item=, title, agent, model)` | `POST api/chat/slots` (+ `work_item`, §3), then `PATCH api/chat/slots/{s}/title` | `api/chat` |
| `session_send(crew=, target, message)` | `POST api/chat?ws=1` `{message, slot}` | `api/chat` |
| `session_read_message(crew=, target)` | `GET api/chat/slots/{s}?limit=N` | `api/chat` |
| `session_stop(crew=, target)` | `POST api/chat/slots/{s}/stop` | `api/chat` |
| `session_close(crew=, target)` | `DELETE api/chat/slots/{s}` | `api/chat` |

The `api/chat` row alone suffices. `api/stream` is not used: no tool streams.

`crew=` takes an instance id or exact name, the selector shape
[#6874](https://github.com/kirodotdev/KiroCrew/pull/6874) uses. The hub-side
handler stays under `/api/session-control/*`; when `crew` is set it calls
`proxy_request`, but only with a path that has passed `_proxy_canonical_path`,
so the allowlist binds the conductor as it binds the browser. Every reply runs
`_redact_peer_payload` before it reaches the tool.

`agent` and `model` come from the peer's rosters
(`/api/instances/{id}/capabilities`), and `memory_mode` always rides the create,
as in `create_peer_slot`. Omitting `agent` sends nothing, so the peer applies its
own default; the local "inherit the caller's agent" fallback does not cross.

### 2. Work ledger: where remote reports land

The worker's MCP server talks to its own gateway, so its reports land on the
peer. The question is how the hub learns them.

| | (a) Hub pulls the peer copy | (b) Peer pushes to the hub |
|---|---|---|
| Tunnel direction | hub → peer, as today | needs peer → hub: a new transport |
| Peer reach into hub | none | the peer holds a hub credential and a hub route |
| Wakes | on the conductor's read (patrol cadence) | immediate |
| Hub down | nothing lost; the peer copy waits | reports fail or queue on the peer |

**Recommend (a).** (b) breaks both the tunnel shape and §4's "no reach back".

How (a) works:

- **The peer copy is a mailbox, not a second ledger.** The peer stores the item
  under a conductor key that names the hub's conductor (`hub:<conductor key>`),
  so its own `work_brief` and `work_report` resolve unchanged. The peer never
  patrols it.
- **Disjoint writers, as `WorkItem` already splits them.** The hub writes the
  conductor-owned fields (`title`, `acceptance`, `decision`, `round`); the peer
  worker writes the worker-owned ones (`status`, `summary`, `artifacts`, `pr`,
  `last_report_at`). Nothing is written from both sides.
- **Peer routes, per slot, under `api/chat`.**
  `GET api/chat/slots/{slot}/work-item` returns the worker-owned fields plus the
  event tail. `PATCH api/chat/slots/{slot}/work-item` takes a `decision`. Owner
  token, the same auth as the rest of `api/chat`.
- **Ingest on read.** `work_ledger_read` on the hub fetches each open remote
  item's peer copy and appends any newer report as a normal local report event,
  keyed by `event_id`, so a repeat read adds nothing. From there `accept`,
  wakes from the crew log, and `work_ledger_rebuild` work as they do for a local
  report. A failed fetch keeps the last ingested state.
- **`decide` forwards.** `work_ledger_record action=decide` on a remote item
  writes locally, then PATCHes the peer copy. A failed PATCH is reported in the
  tool reply; the local write stands and the next `decide` carries it again.

Why `api/chat/slots/{slot}/work-item` and not a new `api/work-ledger` row: that
subtree is MCP-only, holds every conductor's ledger, and includes `record` and
`rebuild`. A per-slot read and one per-slot write are the whole need.

### 3. Binding

| | (a) Two steps: create, then `bind(crew=)` | (b) Born bound: `session_create(crew=, work_item=)` |
|---|---|---|
| Ownership proof | a hub-side "this conductor created that peer slot" record, then a peer-side bind | the slot and its binding are made in one peer call |
| Unbound window | between create and bind, the person can type into the slot | none |
| Skill change | none | steps 2 and 3 of dispatch merge for a crew item |

**Recommend (b).** It keeps the local rule "bind BEFORE seeding" true by
construction, and needs no cross-gateway ownership record.

- The hub checks the item is the caller's own and open, then sends
  `work_item: {item_id, title, acceptance, decision}` with the create. The peer
  creates the slot, the mailbox item and the binding under one item lock, and
  echoes `work_item_id`.
- The hub item records the pair: a new `worker_instance_id` field next to
  `worker_session_key` (the peer's slot key). The pair is the session's identity,
  as `sessionRowIdentity` is in the sidebar. No local binding file is written:
  no local worker exists.
- `bind` with a remote key is refused with a pointer to `session_create(work_item=)`.

Flags for a remote item:

| Flag | Derived from |
|---|---|
| `orphaned` | Unchanged. The conductor is local. |
| `stale` | `worker_running` = the peer slot's `running` (`read_peer_slots`, already parsed in `_PEER_SLOT_BOOL_FIELDS`); `worker_closed` = the slot missing from a successful read. Both answers are stored with the last ingest. `ledger_wake.worker_running` / `worker_closed` answer an item with `worker_instance_id` from that stored state and never call the peer, so the read rows and the wake gate agree. |
| peer unreachable | Both answers keep the last ingested values, as every other field does. With no ingest yet, both read `False`. Either way the window still runs, so a crew that stays down past the window flags `stale` unless its last state was `running`. The row adds `crew_state` (from `TunnelState`), so the conductor can tell "quiet" from "crew down". |

The row also carries the peer slot's `pending_approval`, so "waiting on a person"
reads differently from "stopped".

### 4. Auth and safety

- **Owner-only.** A `crew=` call is admitted only from an owner-minted dashboard
  session, the caller rule #6874 writes (`_authorize_crew_read`). Channel-born,
  app, cron and workflow callers are refused. The proxy route stays owner-only.
- **Only its own sessions.** `send`, `read`, `stop` and `close` with `crew=` reach
  only a peer slot that is the `worker_session_key` of an item in the caller's
  ledger with a matching `worker_instance_id`. A conductor cannot drive the
  person's own chats on that crew.
- **Approvals stay the peer's.** The worker runs under the peer's approval mode.
  Its prompts show in the local crew window and are answered there
  (`POST api/chat/slots/{slot}/approve`, shipped in #16757). No tool here
  approves.
- **Redaction on read.** Every peer reply that reaches the conductor runs
  `_redact_peer_payload`, as the window does. Ingested report text is then
  redacted again on the way out (`mcp_work` already does).
- **What the remote worker may NOT do.** It holds no hub credential: none is
  sent, and the proxy strips the hub's `?token=`. It cannot reach the hub: the
  tunnel opens hub → peer only, and its ledger writes land in the peer's store.
  It cannot read the hub's ledger, spawn on the hub, or reach a third machine
  (`api/instances` is outside the allowlist).

### 5. Failure

| Event | What happens |
|---|---|
| Tunnel down mid-item | The peer keeps running; it owns the session. `crew=` tools return the proxy's typed code (`proxy_peer_not_connected`) after one retry. Ledger reads keep the last ingested state and show `crew_state`. On reconnect the next read ingests every report the peer copy holds. |
| Peer restart | The peer slot and its ledger are on the peer's disk and rehydrate. A turn in flight dies, so the slot is not `running` and `stale` follows the window. The conductor nudges with `session_send(crew=)`. |
| Hub restart | Nothing in flight lives on the hub. The item, with its `worker_instance_id`, is on disk; the next read resumes. |
| Version skew | Every `crew=` call runs `ensure_version_parity` first (`major.minor`). A create whose reply lacks `work_item_id` (a peer that ignored the field) is closed at once and refused as `crew_peer_lacks_work_items`, so a worker never runs unbound. |

### 6. Relation to #12821 and #13476

| | This RFC | [#12821](https://github.com/kirodotdev/KiroCrew/pull/12821) `spawn_run` remote |
|---|---|---|
| Unit | a visible session in the crew's group | a silent subagent run |
| Who can take over | the person, from the sidebar | nobody; status lives in a local shadow record |
| Approvals | peer's, answered in the window | peer's, shown only on the peer |
| Source | peer's own checkout | tracked-file snapshot uploaded |
| Placement | named crew | least-loaded or pinned |
| Result | ledger report + acceptance | subagent completion event |

Shared parts, no shared code required: a connected peer, `ensure_version_parity`,
owner-only. The split is by verb: `spawn_run` is #12821's, the session tools are
this RFC's.

[#13476](https://github.com/kirodotdev/KiroCrew/issues/13476) asks for both
halves (`spawn_run(instance=)` and `session_send(instance=)`). This RFC is the
session half; #12821 is the spawn half.

## Migration plan

Each wave is one PR, shippable alone.

| Wave | Change | Exit criteria |
|---|---|---|
| 0 | Accept this RFC. | First Principles lane reads it from base. |
| 1 | `crew=` on the five session tools; server-side calls pass `_proxy_canonical_path` and redact; owner-only caller rule; target fence (an item's own peer slot). | A test drives create/send/read/stop/close against a stub peer and each lands on the `api/chat` route in the table. A path outside the allowlist is refused server-side. A planted credential in a peer reply reads redacted. A non-owner caller and a non-dispatched peer slot are refused. |
| 2 | Peer side: `POST api/chat/slots` accepts `work_item` and binds atomically; `GET`/`PATCH api/chat/slots/{slot}/work-item`. | On the peer, `work_brief` from that slot returns the item; `work_report` updates the copy; `GET` returns it. For a slot created without `work_item`, both routes answer 404. |
| 3 | Hub side: `worker_instance_id`; `session_create(crew=, work_item=)`; ingest on `work_ledger_read`; `decide` forwards; remote `stale` and `crew_state` in the read rows and in `ledger_wake.worker_running` / `worker_closed`. | A peer report shows on the hub's next read once, not twice. An ingested report from a running remote worker raises no stall wake. `accept` promotes an ingested `pr`. Tunnel down: last state kept, `crew_state` set. Peer missing `work_item_id`: slot closed, refused. |
| 4 | `goal-conductor` skill: a crew item uses `session_create(crew=, work_item=)` and skips `bind`. | The skill's dispatch section names the crew path; a dry run against a stub peer passes. |

Waves 1 and 2 can ship in either order; 3 needs both; 4 needs 3.

## Backward compatibility

Every argument and field is additive. A call without `crew=` is unchanged. An
older peer is refused by `ensure_version_parity` or by the missing
`work_item_id` echo, never driven unbound.

## Open questions

1. **Wakes.** Ingest-on-read means a remote report wakes nobody until the
   conductor's next patrol. Is the patrol cadence enough, or does a remote item
   need a hub-side poller? Proposed: patrol cadence; add a poller only if a
   measured gap shows up.
2. **Folder on the peer.** Should `session_create(crew=)` accept `folder` and file
   the worker into a peer folder? Proposed: no in wave 1; the crew group already
   says where it is.
3. **Lineage in the crew group.** The peer slot has no `parent.key` on the peer.
   Should the sidebar nest it under the hub conductor? Proposed: no; it is a
   top-level row in the crew's group.
4. **#6874's caller rule.** If #6874 merges first, wave 1 reuses
   `_authorize_crew_read`; if not, wave 1 adds it and #6874 rebases onto it.
5. **#12821 and the server-side fence.** #12821 calls `proxy_request` for
   `api/spawn`, outside `_PROXY_ALLOWED_PREFIXES`. Wave 1 makes server-side
   calls pass the fence. Does #12821 get its own named row, or a closed-set
   method like `peer_capability`? Not decided here.
