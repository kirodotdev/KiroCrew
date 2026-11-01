---
title: Local conductor dispatches sessions to a remote crew
status: draft
author: zejiangg
created: 2026-10-06
last-audited: 2026-10-28
audited-at: c6789799df
doc-pr: 17273
implementation-prs: []
tracking-issues: [13476]
supersedes: []
superseded-by: []
---

# RFC: Local conductor dispatches sessions to a remote crew

**Status:** `draft`. Nothing here is built. Measured against main `c6789799df`.
Builds on the accepted [rfc-remote-crew-sidebar.md](rfc-remote-crew-sidebar.md):
a session on a crew is owned by the peer, and this machine is only a window onto
it through `/api/instances/{id}/proxy/...`.

**Revision 2026-10-28.** A design review with the owner settled seven points.
The conductor now picks the crew (it was a non-goal). Placement gates on
headroom, not on a checkout. Workers report a summary only. A lost crew's worker
is retired and redispatched. Sections 2b, 2c, 3b and 5b are new; the Goals,
Non-goals, Failure, Migration and Open questions sections are revised. The
2026-10-06 text also cited a turn relay (`remote_relay.py`, its
`create_peer_slot` and `ensure_version_parity`) that
[#17662](https://github.com/kirodotdev/KiroCrew/pull/17662) deleted; those
references now point at what main serves today.

## Summary

A conductor on this gateway can only dispatch workers onto this gateway. This
RFC lets it create and drive a worker session **on a connected crew**. The peer
owns that session, exactly like a window-view chat: it shows in the crew's
sidebar group, and the person can open it and take over.

Six decisions:

1. The five session tools gain one argument, `crew=`. The hub carries each call
   to routes the peer already serves under `api/chat`. No new proxy prefix.
2. The worker reports to the **peer's** ledger. The hub **pulls** those reports
   when the conductor reads its own ledger. The peer never pushes.
3. A remote session is born bound: `session_create(crew=, work_item=)` creates
   the peer slot and its binding in one call.
4. The conductor picks the crew by headroom. The peer is the real gate and
   refuses a create it cannot hold. `crew=` stays as an explicit override.
5. Workers are self-contained. The seed carries a repo URL and a branch; the
   worker clones into its own scratch, pushes, reports a summary, and deletes
   the clone. Nothing large crosses between machines.
6. A worker whose crew stays down 30 minutes is retired and its item is
   redispatched to another crew on a new branch. Late reports from the retired
   worker are ignored.

## Motivation

### Current state

- **Session tools.** `session_create`, `session_send`, `session_read_message`,
  `session_stop` and `session_close` (`_session_tools` in
  `src/kiro_crew/mcp_dashboard_session_tools.py`) call `/api/session-control/*` on this
  gateway. Those routes are MCP-only and strict (`dashboard/server_internal_paths.py`, the
  *Session control* block of the internal-secret path set). No argument names
  another machine. `session_read_message` returns at most 100 messages
  (`limit`, default 20).
- **The proxy.** `api_instances_proxy` (`src/kiro_crew/dashboard/handlers_instances_peer.py`)
  forwards only paths under `_PROXY_ALLOWED_PREFIXES` = `api/chat`, `api/stream`,
  owner-only, and redacts every reply since
  [#16757](https://github.com/kirodotdev/KiroCrew/pull/16757)
  (`_redact_peer_payload`, `_redact_sse_event`). The fence lives in the HTTP
  handler (`_proxy_canonical_path`); `SshTunnelManager.proxy_request`
  (`instances/ssh_tunnel_manager_peer.py`) checks no path, so a server-side
  caller is not fenced today.
- **What the peer already serves under `api/chat`.** Create
  (`POST api/chat/slots`, `api_chat_slot_create` in
  `dashboard/chat_api/slot_create.py`, which reads `agent`, `model`,
  `memory_mode`; the window view's create in
  `website/src/pages/chat-sidebar/create.ts` sends `memory_mode`), rename
  (`PATCH api/chat/slots/{slot}/title`), send (`POST api/chat?ws=1`, which
  answers at once and queues on a busy slot), read
  (`GET api/chat/slots/{slot}?limit=N`), stop
  (`POST api/chat/slots/{slot}/stop`) and close (`DELETE api/chat/slots/{slot}`,
  `api_chat_slot_delete` in `dashboard/chat_api/slot_lifecycle.py`). The window view
  ([#16757](https://github.com/kirodotdev/KiroCrew/pull/16757)) and the
  per-machine sidebar ([#16756](https://github.com/kirodotdev/KiroCrew/pull/16756))
  use exactly these.
- **Version gate.** `versions_compatible` (`src/kiro_crew/apps/version.py`)
  matches `major.minor`. `api_instances_capabilities` reports it as
  `version_match`.
- **Peer headroom.** `resource_status.probe()` (`src/kiro_crew/resource_status.py`)
  returns a `ResourceStatus`: cgroup-clamped `available_gb`, `cpu_count`,
  `load_per_cpu`, a `posture` and its `pressure_gb` / `critical_gb` thresholds.
  The subagent spawn floor is `check_memory_available` in
  `subagent_parts/host_memory.py`; the adaptive cap is `AdaptiveController`
  (`adaptive/controller.py`), read through `adaptive_exec_cap()`. Nothing
  measures free disk. The `resource_status` MCP tool reports this to an agent;
  no route serves it to another machine.
- **Scratch.** Each agent process gets its own directory under
  `agent_scratch.scratch_root()` (`src/kiro_crew/agent_scratch.py`), exported as
  `KIROCREW_SCRATCH`. An hourly sweep reclaims a directory once its owner is
  dead; one that holds a git repository is quarantined first, not deleted.
- **The work ledger.** `src/kiro_crew/work_ledger.py` stores items per
  conductor; `src/kiro_crew/mcp_work.py` serves `work_brief`, `work_report`,
  `work_ledger_read`, `work_ledger_record`, `work_ledger_rebuild` over
  `/api/work-ledger/*`, also MCP-only. A worker is found by its **binding**
  (`binding_path` in `work_ledger_store.py`, keyed by the worker's own session
  key). `bind` is admitted only for a live LOCAL slot whose `_created_by` names
  the conductor (`dashboard/handlers/work_ledger_conductor.py`). `orphaned` and
  `stale` are derived at read time (`is_orphaned`, `is_stale` in
  `work_ledger_rules.py`) from local slot state (`_slot_running`,
  `_slot_closed` in `dashboard/handlers/work_ledger_read.py`). A report on a
  closed item is refused `item_closed` (`apply_worker_report` in
  `work_ledger_writes.py`). One ledger entry is at most 64 KB
  (`MAX_ENTRY_BYTES` in `crew_log/schema.py`).
- **Wakes.** `WORK_WAKE_STATUSES` = `done`, `blocked`, `question`
  (`src/kiro_crew/work_vocab.py`). Both `conductor_wake.py` (the push) and
  `ledger_wake.py` (the gate) read that one tuple. `progress` wakes nobody, and
  neither does a chat reply that writes no report.

### Problems

| Gap | Where it breaks |
|---|---|
| No crew target | The session tools reach only this gateway's slot table. |
| Unbindable | `bind` looks up a local slot; a peer slot has none, so it answers `unknown_worker_session`. |
| Reports land elsewhere | A worker on the peer calls the PEER's `work_brief`; the peer has no binding, so it answers `not_bound`, and its `work_report` writes nothing the hub can see. |
| Wrong flags | `ledger_wake.worker_running` and `ledger_wake.worker_closed` read local slots. The read rows (`_slot_closed`) and the wake gate (`probes/work_ledger.py`, `slack/gateway_runtime/nudge_wiring.py`) both use them, so a remote worker reads as closed and, once it has reported, wakes the conductor as stalled at once. |
| No placement | The conductor must name a crew. It cannot see any crew's headroom, so it cannot tell a full crew from an idle one. |
| No admission | The peer would accept a create it has no memory, CPU or disk for. |
| Follow-ups are silent | A `done` worker that answers a conductor's follow-up writes a chat turn, not a report, so nothing wakes the conductor. |
| Lost crews strand items | An item whose crew stays down has no path to another crew; `stale` is the only signal. |

The tunnel only opens hub → peer, so the peer cannot call the hub back.

## Goals

- A conductor dispatches one leaf item onto a connected crew and drives it with
  the same five verbs it uses locally.
- By default the conductor picks the crew with the most headroom; `crew=` names
  one explicitly.
- The worker's `work_brief` and `work_report` work unchanged on the peer.
- The person sees the worker in that crew's sidebar group and can take over.
  Chat-on-crew (the window view) stays; the conductor is the default entry
  point.
- An item whose crew is lost finishes on another crew without a person.

## Non-goals

- `spawn_run` on a crew ([#12821](https://github.com/kirodotdev/KiroCrew/pull/12821)).
- Reading arbitrary peer history ([#6874](https://github.com/kirodotdev/KiroCrew/pull/6874)).
- A remote **conductor** (a sub-conductor on the peer). Leaf workers only.
- Pushing anything from the peer to the hub, or any new transport.
- Uploading source to the peer, or moving any large file between machines. The
  worker clones from the repo URL in its seed.
- Sending any credential from the hub, or checking logins before dispatch.
- A per-crew paged "space" view in the sidebar. A separate follow-up.
- Worker-to-worker messages. Every handoff goes through the conductor.

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

`crew=` takes an instance id or exact name. On
`session_create(work_item=)` it is optional: omitted, the hub places the item
(§2b); named, the hub skips placement and sends to that crew, and the peer gate
still applies. On the other four verbs the hub already knows the crew from the
item's current binding, so `crew=` there must match it.

The hub-side handler stays under `/api/session-control/*`; when a crew is set
it calls `proxy_request`, but only with a path that has passed
`_proxy_canonical_path`, so the allowlist binds the conductor as it binds the
browser. Every reply runs `_redact_peer_payload` before it reaches the tool.

`agent` and `model` come from the peer's rosters
(`/api/instances/{id}/capabilities`), and `memory_mode` always rides the create,
as the window view's create does. Omitting `agent` sends nothing, so the peer
applies its own default; the local "inherit the caller's agent" fallback does
not cross.

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
- **Ingest on read.** `work_ledger_read` on the hub fetches the peer copy of each
  open remote item's **current** binding (§5b) and appends any newer report as
  a normal local report event, keyed by `event_id`, so a repeat read adds
  nothing. From there `accept`, wakes from the crew log, and
  `work_ledger_rebuild` work as they do for a local report. A failed fetch keeps
  the last ingested state.
- **`decide` forwards.** `work_ledger_record action=decide` on a remote item
  writes locally, then PATCHes the peer copy. A failed PATCH is reported in the
  tool reply; the local write stands and the next `decide` carries it again.

Why `api/chat/slots/{slot}/work-item` and not a new `api/work-ledger` row: that
subtree is MCP-only, holds every conductor's ledger, and includes `record` and
`rebuild`. A per-slot read and one per-slot write are the whole need.

**Reports carry a summary, never bulk data.** A worker's `summary` and
`artifacts` are pointers: a branch, a commit, a PR number, a path on its own
crew. Each report is one ledger entry, so it must fit `MAX_ENTRY_BYTES` (64 KB)
or the store refuses it `work_entry_too_large`. When the conductor needs more,
it reads the worker's transcript with `session_read_message(crew=)`, bounded at
100 messages per call. Moving a large file between machines is out of scope by
rule: an item that needs it was split wrong, and the conductor re-splits it.

### 2b. Placement and headroom

The conductor picks the crew. Two layers decide, a local estimate and the
peer's own check, because only the peer can measure the peer.

| | (A) Hub estimate only | (B) Peer gate only | (C) Hub reservations + peer gate |
|---|---|---|---|
| Who measures | hub guesses from old snapshots | peer, at create time | peer measures, hub ranks |
| Over-commit | likely: two creates race one snapshot | none: the peer refuses | none: the peer refuses |
| Wasted creates | none | one per full crew tried | rare: the ranking skips full crews |
| New state on the hub | snapshots | none | snapshots + derived reservations |

**Recommend (C), the peer gate plus a local reservation ledger.**

**Eligible crew.** A crew is a candidate when all hold:

| Gate | Source |
|---|---|
| Connected | its `TunnelState` is `CONNECTED` (`instances/ssh_tunnel_manager_child.py`) |
| Compatible | `versions_compatible` holds (`version_match`) |
| Memory | last snapshot's memory free ≥ one estimate |
| CPU | last snapshot's `load_per_cpu` is below the CPU threshold (below) |
| Disk | last snapshot's disk free ≥ one estimate |

There is **no repo or checkout check.** Workers are self-contained (§2c), so any
connected crew can run any item.

**Peer gate.** `POST api/chat/slots` with a `work_item` runs an admission check
before it creates anything. It reuses what the peer already uses for its own
subagents where that exists:

| Resource | Short when | Source |
|---|---|---|
| Memory | `check_memory_available(estimate)` fails, or `posture` is critical | `subagent_parts/host_memory.py`, `resource_status.probe()` |
| CPU | `load_per_cpu` ≥ a new threshold (proposed 1.0), or the agents running already fill `adaptive_exec_cap()` | `resource_status.probe()`, `AdaptiveController` |
| Disk | free bytes under `agent_scratch.scratch_root()` < one estimate | new: `shutil.disk_usage` |

`posture` is the memory posture; nothing on main turns CPU into a posture, which
is why CPU needs its own threshold. A short resource is refused with a typed
`crew_busy` that names it:

```json
{"error": "crew_busy", "short": "disk", "headroom": { ... }}
```

A create without `work_item` (the window view, a person's own chat) is not
gated. The gate binds only conductor dispatch.

**Headroom on every reply.** Every create reply, `ok` or `crew_busy`, carries the
peer's current snapshot:

| Field | Source |
|---|---|
| `mem_free_gb` | `ResourceStatus.available_gb` |
| `cpu_count`, `load_per_cpu` | `ResourceStatus` |
| `posture` | `ResourceStatus.posture` |
| `disk_free_gb` | free space under `scratch_root()` |
| `open_workers` | peer slots bound to a `hub:` mailbox item and not closed |
| `logins` | optional, §4: `{"gh": "valid"}` and the like |
| `measured_at` | the peer's clock |

**Read before the first create.** A new read-only route
`GET api/chat/headroom` returns the same snapshot. It lives under the existing
`api/chat` proxy row, so it needs no new proxy prefix. Owner token, as the rest
of `api/chat`; the reply runs `_redact_peer_payload`. It carries numbers and
status words only.

**Reservation ledger on the hub.** The hub keeps the latest snapshot per crew in
memory and refreshes it from every create reply and from `api/chat/headroom`
on patrol. Reservations are not stored; they are derived from the work ledger.
Each **open** remote binding reserves one estimate on its crew, including a
finished worker the conductor keeps open for questions (§3b). An estimate
starts at 2 GB memory and 10 GB disk. Wave 6 tunes both from measured values.

**Choosing.** For each eligible crew and each of memory and disk, headroom after
reservations is `snapshot free − open remote bindings on that crew × estimate`.
This is deliberately conservative: a running worker is counted once in the
snapshot and once as a reservation. That is safe because the reservations only
rank crews; the peer gate alone admits or refuses. The hub picks the crew whose
scarcer resource has the most headroom after reservations. On `crew_busy` it stores
the reply's snapshot and tries the next crew. When no crew admits the item, the
item stays open and unbound to any crew, and the next patrol tries again.

### 2c. Self-contained workers

The seed names what the worker needs and nothing else:

| Seed field | Meaning |
|---|---|
| `repo` | a clone URL the peer's own credential can read |
| `branch` | the branch to create or resume; new per binding (§5b) |
| `handoff` | earlier workers' summaries and branches (§3b) |

The worker clones into its own `KIROCREW_SCRATCH`, works there, pushes its
branch, and reports `progress` after each step that changed the branch. A
replacement can resume from the pushed branch (§5b). Before it reports `done`
the worker deletes the clone. The scratch sweep's quarantine stays the backstop
for a worker that died first.

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
  runs its gate (§2b), then creates the slot, the mailbox item and the binding
  under one item lock, and echoes `work_item_id`.
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
| peer unreachable | Both answers keep the last ingested values, as every other field does. With no ingest yet, both read `False`. The row adds `crew_state` (from `TunnelState`) and `crew_down_since`, so the conductor can tell "quiet" from "crew down", and §5b can time the retire. |

The row also carries the peer slot's `pending_approval`, so "waiting on a person"
reads differently from "stopped".

### 3b. Handoff between rounds

Workers never talk to each other. The conductor carries everything.

1. **Seed.** Worker B's seed includes worker A's summary, and A's branch when B
   builds on A's code.
2. **Ask.** If the seed is not enough, B reports `question`. The conductor asks A
   with `session_send(crew=)`.
3. **Answer.** A answers with a `reply` report (below). The conductor relays the
   answer to B with `session_send`.
4. **A is gone.** The conductor tells B to rely on the summary. If that is still
   not enough, the conductor re-splits the item.

**The conductor alone closes A.** It may keep A open while later items depend
on it. A kept-open A counts toward its crew's reservations (§2b). While A is
kept open its item stays open too: the conductor rules on A's `done`, then calls
`accept` and `session_close(crew=)` on A together. This keeps A able to report,
since a report on a closed item is refused `item_closed`.

**A waking `reply` report.** Today an answer to a follow-up is a chat turn,
which writes no report, so it wakes nobody. The minimal change adds one worker
status, `reply`, to `WORK_WORKER_STATUSES` and to `WORK_WAKE_STATUSES` in
`work_vocab.py`. Both wake readers derive from that one tuple, so the push and
the gate agree with no other edit. A `reply` needs no evidence, unlike `done`.
It does not claim the item is finished, so the conductor answers it without
running acceptance. The follow-up message asks for it: "answer with
`work_report status=reply`". The change helps local workers as much as remote
ones.

### 4. Auth and safety

- **Owner-only.** A `crew=` call, and any placed create, is admitted only from an
  owner-minted dashboard session. Wave 1 adds this caller rule itself; no such
  rule exists on main today. Channel-born, app, cron and workflow callers are
  refused. The proxy route stays owner-only.
- **Only its own sessions.** `send`, `read`, `stop` and `close` with a crew reach
  only a peer slot that is the `worker_session_key` of an item in the caller's
  ledger with a matching `worker_instance_id`. A conductor cannot drive the
  person's own chats on that crew.
- **Approvals stay the peer's.** The worker runs under the peer's approval mode.
  Its prompts show in the local crew window and are answered there
  (`POST api/chat/slots/{slot}/approve`, shipped in #16757). No tool here
  approves.
- **Redaction on read.** Every peer reply that reaches the conductor runs
  `_redact_peer_payload`, as the window does. Ingested report text is then
  redacted again on the way out (`mcp_work` already does). The headroom route
  is redacted the same way.
- **Credentials stay on each crew.** The worker uses the peer's own credentials
  (for example its `gh` login) to clone and push. No credential is ever sent
  from the hub, and the hub runs no login check before dispatch. A worker stuck
  on a login reports `blocked` with reason `needs_human`. The conductor first
  redispatches the item to another crew (§5b), and asks the person only when no
  crew works.
- **Risk: login status in headroom.** The headroom reply MAY carry per-login
  status flags, one word per named tool (`valid`, `expired`), such as
  `{"gh": "valid"}`. It never carries a token, a user name, a path, or any other
  credential content. The peer fills it from the tool's own status command and
  maps the result to a word; the hub stores only the word.
- **What the remote worker may NOT do.** It holds no hub credential: none is
  sent, and the proxy strips the hub's `?token=`. It cannot reach the hub: the
  tunnel opens hub → peer only, and its ledger writes land in the peer's store.
  It cannot read the hub's ledger, spawn on the hub, or reach a third machine
  (`api/instances` is outside the allowlist).

### 5. Failure

| Event | What happens |
|---|---|
| Tunnel down, short | The peer keeps running; it owns the session. Calls with a crew return the proxy's typed code (`proxy_peer_not_connected`) after one retry. Ledger reads keep the last ingested state and show `crew_state` and `crew_down_since`. On reconnect the next read ingests every report the peer copy holds. |
| Crew down 30 minutes | The conductor retires the binding and redispatches the item (§5b). The timeout is configurable. |
| Peer restart | The peer slot and its ledger are on the peer's disk and rehydrate. A turn in flight dies, so the slot is not `running` and `stale` follows the window. The conductor nudges with `session_send(crew=)`. The worker resumes from its pushed branch. The next `api/chat/headroom` read replaces the stored snapshot. |
| `crew_busy` | The hub stores the reply's snapshot and tries the next eligible crew. With none left, the item stays unbound and the next patrol retries. Nothing is created on a refusing crew. |
| Worker stuck on a login | `blocked`, `needs_human`. The conductor redispatches to another crew first; it asks the person only when none works. |
| Hub restart | Nothing in flight lives on the hub. The item, with its binding history, is on disk; the next read resumes. Snapshots are refetched from `api/chat/headroom`. |
| Version skew | Every call with a crew checks `versions_compatible` first (`major.minor`). A create whose reply lacks `work_item_id` (a peer that ignored the field) is closed at once and refused as `crew_peer_lacks_work_items`, so a worker never runs unbound. |

### 5b. Retire and redispatch

When a remote worker's crew stays unreachable for 30 minutes
(`crew_down_since` older than the timeout), the conductor gives up on that
binding and moves the item.

1. **Retire.** The conductor records a `retire` on the item. The binding is
   marked `retired` with its crew id, its slot key and a reason
   (`crew_unreachable`, `login_blocked`).
2. **Redispatch.** The conductor places the SAME item again (§2b), excluding the
   retired crew. The new binding gets a NEW branch name, so two workers never
   push to one branch. Its seed names the retired binding's last pushed branch,
   and the new worker starts from it.
3. **Clean up.** When the old crew reconnects, the conductor closes the old slot
   with `session_close(crew=)` and logs a `destroyed` event on the binding. The
   peer's own crew log records `session/closed` with reason `destroyed`
   (`crew_log/entry_types_lifecycle.py`), as for any close.

**Only the current binding counts.** The item holds its full binding history,
and exactly one binding is current. Ingest (§2) fetches only the current
binding's peer copy. A report that arrives from a retired binding is ignored,
never applied, so a late `done` cannot close the item. A retired binding's slot
also loses the "only its own sessions" target right (§4) for every verb except
`session_close`.

The new event kind needs a matching entry in `WORK_EVENT_KINDS`
(`work_vocab.py`); the binding history is a list on the item next to
`worker_instance_id`, and each entry carries the crew id, the slot key, the
branch, its state (`current`, `retired`, `destroyed`) and its timestamps.

### 6. Relation to #12821 and #13476

| | This RFC | [#12821](https://github.com/kirodotdev/KiroCrew/pull/12821) `spawn_run` remote |
|---|---|---|
| Unit | a visible session in the crew's group | a silent subagent run |
| Who can take over | the person, from the sidebar | nobody; status lives in a local shadow record |
| Approvals | peer's, answered in the window | peer's, shown only on the peer |
| Source | the worker clones from a repo URL into its scratch | tracked-file snapshot uploaded |
| Placement | most headroom after reservations, peer-gated; `crew=` overrides | least-loaded or pinned |
| Result | ledger report + acceptance | subagent completion event |

Shared parts, no shared code required: a connected peer, `versions_compatible`,
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
| 3 | Peer side: `GET api/chat/headroom`; the admission gate on a `work_item` create; the snapshot on every create reply. | The route answers the snapshot fields for an owner token and refuses any other caller. With a stubbed probe short on memory, CPU or disk, a `work_item` create answers `crew_busy` naming that resource and creates no slot. A create without `work_item` is not gated. Every create reply carries `headroom`. No reply field holds credential content. |
| 4 | Hub side: `worker_instance_id`; `session_create(crew=, work_item=)`; ingest on `work_ledger_read`; `decide` forwards; remote `stale`, `crew_state`, `crew_down_since` in the read rows and in `ledger_wake.worker_running` / `worker_closed`. | A peer report shows on the hub's next read once, not twice. An ingested report from a running remote worker raises no stall wake. `accept` promotes an ingested `pr`. Tunnel down: last state kept, `crew_state` set. Peer missing `work_item_id`: slot closed, refused. |
| 5 | Hub placement: per-crew snapshots; reservations derived from open remote bindings; `session_create(work_item=)` with no crew picks one; `crew_busy` tries the next crew; no crew leaves the item unbound. | With two stub crews, the create lands on the one with more headroom after reservations. A kept-open finished worker still reserves. A `crew_busy` reply falls through to the next crew. With every crew busy the item stays unbound and the next patrol retries. An explicit `crew=` skips ranking and still meets the peer gate. |
| 6 | Estimate tuning: record measured memory and disk per finished remote worker; replace the 2 GB / 10 GB starting estimate. | The estimate is read from config with the measured default; a test pins the fallback when nothing is measured. |
| 7 | The `reply` worker status: add it to `WORK_WORKER_STATUSES` and `WORK_WAKE_STATUSES`; no evidence rule. | A `reply` report on an open item wakes the conductor through both `conductor_wake` and `ledger_wake`. A `reply` without artifacts is admitted. A `reply` on a closed item is refused `item_closed`. |
| 8 | Retire and redispatch: binding history on the item, the `retire` event kind, `current`-only ingest, the configurable timeout (default 30 minutes), new branch per binding, close-on-reconnect with a `destroyed` event. | With a stub crew down past the timeout, the binding reads `retired` and a new binding on another crew is current with a different branch. A `done` from the retired binding is ignored and the item stays open. On reconnect the old slot is closed and the binding reads `destroyed`. |
| 9 | `goal-conductor` skill: a crew item uses `session_create(work_item=)` with no crew, skips `bind`, seeds `repo` / `branch` / `handoff`, relays follow-ups, closes kept-open workers, and redispatches a `needs_human` login block before asking. | The skill's dispatch section names the crew path; a dry run against two stub peers passes. |

Waves 1, 2 and 3 can ship in any order. 4 needs 1 and 2. 5 needs 3 and 4.
6 needs 5. 7 needs nothing. 8 needs 5. 9 needs 5, 7 and 8.

## Backward compatibility

Every argument and field is additive. A call without a crew or `work_item` is
unchanged. A create without `work_item` is never gated, so the window view and
a person's own chats behave as today. An older peer is refused by
`versions_compatible` or by the missing `work_item_id` echo, never driven
unbound. An older peer answers `api/chat/headroom` with 404; the hub treats that
crew as ineligible for placement but still honours an explicit `crew=`. Adding
`reply` to the worker statuses widens what `work_report` accepts and refuses
nothing that passes today.

## Open questions

1. **Remote wakes.** Ingest-on-read means a remote report, `reply` included,
   wakes nobody until the conductor's next patrol. The `reply` status (§3b)
   closes the local half: a follow-up answer now wakes a local conductor. Is
   patrol cadence enough for the remote half, or does a remote item need a
   hub-side poller? Proposed: patrol cadence; add a poller only if a measured
   gap shows up.
2. **Folder on the peer.** Should `session_create(crew=)` accept `folder` and file
   the worker into a peer folder? Proposed: no in wave 1; the crew group already
   says where it is.
3. **Lineage in the crew group.** The peer slot has no `parent.key` on the peer.
   Should the sidebar nest it under the hub conductor? Proposed: no; it is a
   top-level row in the crew's group.
4. **The owner-only caller rule.** Resolved: #6874 closed unmerged, so wave 1
   adds the §4 caller rule itself. Any later crew-scoped read reuses it.
   One deliberate exception (maintainer decision by buluoray, #26651): the wave 4
   ledger-read pull and decide forward may run on a turn the owner did not send,
   such as a patrol wake. Their reach is the one remote item bound on the
   caller's own ledger, and both are off unless `work_ledger.remote_dispatch` is
   on. Later crew-scoped reads still apply the owner-sent rule.
5. **#12821 and the server-side fence.** #12821 calls `proxy_request` for
   `api/spawn`, outside `_PROXY_ALLOWED_PREFIXES`. Wave 1 makes server-side
   calls pass the fence. Does #12821 get its own named row, or a closed-set
   method like `peer_capability`? Not decided here.
6. **Reservation estimate.** 2 GB memory and 10 GB disk per open worker are
   starting guesses. Which measurement replaces them: peak RSS of the worker's
   process tree and the scratch directory's peak size, or a per-repo estimate?
   Proposed: the per-worker peak, as one global default, in wave 6.
7. **Retire timeout.** Is 30 minutes the right default? Shorter wastes work on a
   crew that comes back; longer strands the item. Proposed: 30 minutes,
   configurable, revisited once wave 8 logs real outage lengths.
8. **`reply` on an accepted item.** §3b keeps A's item open while A is kept open.
   Should `reply` instead be admitted on an accepted item, so the conductor can
   accept early? Proposed: no; keeping the item open needs no store change.
