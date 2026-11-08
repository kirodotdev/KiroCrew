---
title: Adaptive warm session pool -- hold one pre-started session while the user is active, return it when idle, and check fit at claim
status: draft
author: iamwhatever, with kirocrew-worker
created: 2026-10-24
last-audited: 2026-10-24
audited-at: 24d4604c45
doc-pr: null
implementation-prs: []
tracking-issues: [23822]
supersedes: []
superseded-by: []
---

# RFC: Adaptive warm session pool

- Status: draft. Nothing built. Acceptance by the maintainer (iamwhatever)
  comes before the first implementation pull request (section 9).
- Tracking issue: [#23822](https://github.com/kirodotdev/KiroCrew/issues/23822).
- Code read at `24d4604c45`. Code is cited by symbol name; every symbol is as of that commit.
- Numbers come from one operator's host: the dashboard chat transcripts of the
  last 14 days (2026-10-10 to 2026-10-24), replayed by a script that reads
  only those files. No model turn was run for this document.

## 1. Summary

The warm pool is a fixed number of pre-started agent sessions
(`session.pool_size`), off by default. This document proposes two things, in
this order:

1. **A fit check at claim time** (phase 1). Before a pooled session is handed
   to a request, the gateway compares a fingerprint taken when the session was
   started with one taken for the request. Any difference discards the pooled
   session and cold-starts. This closes the gaps in section 3.3, where a pooled
   session can today outlive an edit to the thing it was started from.
2. **An adaptive size** (phase 2): 0 while the user is away, 1 while the user
   is active, back to 0 after 30 idle minutes, never above what the host's
   memory posture allows. Opt-in; the default stays off.

The measurements in section 6 are blunt: on this host, real use opened 6
pool-eligible sidebar chats in 14 days, 2 of them sent fast enough for a pool
to help, and a fixed pool of 1 would have saved **7.1 s in total** while
holding 109 MB the whole time. The adaptive policy holds about 6 MB on average,
and it saves time only in bursts (a benchmark burst: 11 of 13 fast sends). The
pool is the right tool for bursts of new default-agent chats. It is not the
lever for crewmate DMs or dispatched workers (section 5).

## 2. Motivation

R8 measured a normal Sessions-sidebar chat on the live gateway
(`research/crewmate-perf/R8-ttft-retro.md`, path B, n 5, Opus 5.5 medium):

| Case | Send to first token, p50 |
|---|---:|
| New chat, sent at once | 11.1 s (p90 15.9) |
| New chat, sent after the eager spawn finished | 3.5 s |
| Warm turn | 1.9 s |

The gap is the session start on the send path: 8.95 s before the prompt
reaches the engine, against 1.0 s when the session already exists. The eager
spawn (`session.eager_spawn`, on by default) starts a slot's session 1.5 s
after the slot is created (`dashboard/chat_turn/eager_spawn.py` `_EAGER_SPAWN_DEBOUNCE_SECS`), so it
hides the start whenever the user takes longer than about 10.5 s to type. A
pooled session hides it for a user who types faster, and for the eager spawn
itself, which claims from the pool like any other caller.

The pool is off by default for a stated reason
(the comment on `DEFAULT_POOL_SIZE` in `config/sections_defaults.py`): each slot is a full kiro-cli process plus
the MCP stdio servers its agent spawns, about 109 MB, and a non-zero
`pool_size` is also reserved out of the TaskRunner's memory-sized parallel cap
(`subagent_parts/host_memory.py` `_host_mem_term`). The user asked for a pool that grows
when new chats are frequent and gives the memory back when idle. The user's
first worry is a subtle wrong hand-off, such as a session started for one agent
being used for another, so safety comes first in this document.

## 3. Current behavior on main

### 3.1 Fill and refill

| Step | Where | What happens |
|---|---|---|
| Size | `WarmSessionPool._state_from_owner` | `pool_size` clamped to `0..POOL_SIZE_MAX` (10, `config/sections_limits.py`); default `DEFAULT_POOL_SIZE = 0` (`config/sections_defaults.py`) |
| Agent | `pool_kiro_agent` | resolves `session.pool_agent`, blank meaning the default agent |
| Start | `WarmSessionPool.start_pool` | fills the pool and starts the health loop only when `pool_size > 0` |
| Fill | `_fill_warm_pool_loop` | one provider per iteration, built with `agent=pool_agent`, `cwd=pool_cwd` and no session key; under the shared start semaphore; identity stamped at spawn |
| Enqueue re-check | the `factory_ok` re-check in `_fill_warm_pool_loop` | enqueue only if the factory is unchanged, the pool is not full, and no identity sweep happened since the spawn |
| Refill after claim | `_get_or_create_impl` in `session_allocation_parts/get_or_create.py` | every claim schedules one replenish |
| Health sweep | `_sweep_warm_pool_once`, every 30 s (`_POOL_HEALTH_INTERVAL` in `session_parts/runtimes.py`) | discards dead or TTL-expired providers (TTL 1800 s) and refills |

### 3.2 Who skips the pool

The decision is the `pool_decision` chain in `_get_or_create_impl` (`session_allocation_parts/get_or_create.py`). The
first matching arm wins and is counted on `kirocrew.session.pool.decision`:

| Outcome | Request |
|---|---|
| `disabled` | `pool_size` is 0 (main's default) |
| `bypass_member_capabilities` | a member whose capabilities were reconciled this call |
| `bypass_resume` | resuming or forking a native session |
| `bypass_stateless` | stateless one-shot |
| `bypass_member` | **every crewmate DM and every crewmate's own worker** |
| `bypass_app_launch` | app-launched agent copy |
| `bypass_restricted_context` | memory mode other than persistent |
| `bypass_member_context` | a native launch document is required |
| `bypass_cwd` | `cwd` differs from the pool's (the default project) |
| `bypass_env` | any extra environment, including an AWS profile bound to the project |
| `bypass_shared_scratch` | a sub-agent joining its parent's work directory |
| `bypass_backend_override` | an explicit backend other than the factory default |
| `bypass_remote_environment` | a session that runs on a remote environment |
| `bypass_effort` | the crew pins a reasoning effort |
| (claim) | everything else calls `_drain_and_claim(agent)` |

Then the claim itself (`_claim_from_pool`) returns nothing unless the
requested agent equals the pool agent. A dispatched
worker running `kirocrew-worker`, or any chat that picked a non-default agent,
misses here. `_drain_and_claim` then discards any entry
that predates an identity change, is past the TTL, or whose process died.

**A mismatched agent, cwd, backend, environment, member route or crew effort
pin never takes a pooled session.** Two fields are not part of the decision
and are adjusted after the claim instead:

- model: switched with `set_model` when it differs from the pool agent's model
  (`client.set_model` in `_get_or_create_impl`);
- a caller's effort override: pushed with `change_effort`
  (`provider.change_effort` in `_get_or_create_impl`).

Approval policy lives on the gateway's session record, not in the child
process (`session.approval_policy` in `_get_or_create_impl`), and the per-session context (memory,
lessons, skills index) is built by the gateway at the first turn, not baked
into the child. Neither can be stale in a pooled process.

### 3.3 What discards a pooled session today

| Change | Drains the pool? | Where |
|---|---|---|
| `config.json` change (model, effort, pool fields, MCP gateway overlay) | yes | config watcher -> `refresh_defaults` (`session_lifecycle_parts/fleet.py`) |
| Crew record save (effort pin) | yes | `_refresh_session_defaults` in `dashboard/agent_admin/crew_records.py` |
| Provider switch or session reset | yes | `_reset_all_sessions` in `dashboard/handlers/sessions_parts/restart.py` |
| MCP edit through the dashboard | yes, or hot-applied to pooled processes too | `_mcp_hot_reload_active` in `dashboard/handlers/mcp_agent_sync.py` |
| Account change | yes, plus an age fence at claim | `mark_identity_epoch`, `_retire_kiro_warm_pool` |
| Gateway restart | yes: the pool is in-process | -- |
| **Agent file edited outside the dashboard** (by hand, a package install) | **no**; only the 1800 s TTL bounds it | no caller found |
| **kiro-cli upgraded in place** | **no**; only the TTL bounds it | no caller found |

The last two rows are the subtle hand-offs the user worried about. They are
rare and bounded by the TTL, but they are silent: a chat could start on an
agent file or binary that no longer exists on disk. Past pool bugs of the same
shape: #2932 (a pooled session leaked `context_pct` into a new chat) and
#16963 (the pool never claimed for the default agent because the names
differed).

## 4. Goals and non-goals

Goals:

- A pooled session is used only when it would behave the same as a cold start
  for that request. When in doubt, cold-start.
- An idle host holds no pooled session.
- A user opening new default-agent chats in a burst gets the warm start.
- Off switch: one config value, applied live.

Not goals:

- Serving crewmate DMs, crewmate workers or dispatched workers from the pool
  (section 5).
- Per-agent pools (#2264, parked).
- Changing the default from off. That is a later decision, with data (phase 3).

## 5. Crewmate DMs and workers

| Who | Today | Recommendation |
|---|---|---|
| Dispatched worker (`session_create`) | G8 #23246 starts its agent at create (`create_session` in `dashboard/session_control_parts/session_create.py`). Create to first send: p50 38.9 s, only 3 of 156 under 10.5 s (14 days) | No pool. G8 already hides the start; a pool would duplicate it and must also match `kirocrew-worker` |
| Crewmate DM | `bypass_member`: a pooled child has no member route (dispatch-tool mount, member backend) | No pool. A DM's new session comes from a context trim; the lever is R8 item 4, starting the trimmed session at the end of the previous turn |
| Crewmate's own worker | `bypass_member`, same reason | No pool |

## 6. Measurements

Script: `research/dynpool/analyze.py` in the operator's workspace (reads
`~/.kiro/crew/sessions/dashboard_chat-*.jsonl` only; output in
`out_14d.txt`). A chat is pool-eligible when its first user row is a human
dashboard message, its agent is the default (`kirocrew`), and its project is
the default workspace. Chats titled `perf-*` are benchmark runs and are counted
separately. A claim is modelled at slot create + 1.5 s (the eager-spawn
debounce); a claim helps only if the first send came within 10.5 s of the
create, and saves `min(7.6 s, 10.5 s - delay)`. One fill takes 9 s.

### 6.1 How often new chats open (14 days)

| Kind | Chats | Per day | Create to first send p50 | Sent within 10.5 s |
|---|---:|---:|---:|---:|
| Human sidebar chats | 10 | 0.7 | 37.2 s | 2 |
| of which pool-eligible | 6 | 0.4 | -- | 2 |
| Benchmark chats (`perf-*`) | 21 | 1.5 | 0.7 s | 11 |
| Dispatched workers | 156 | 11.1 | 38.9 s | 3 |

### 6.2 Policies replayed on real chats (6 eligible, benchmarks excluded)

| Policy | Hits | Helpful hits | Time saved | Average memory | Peak | Process starts |
|---|---:|---:|---:|---:|---:|---:|
| P0 off (main) | 0 | 0 | 0 s | 0 MB | 0 MB | 0 |
| P1 fixed 1 | 6 | 2 | 7.1 s | 109 MB | 109 MB | 673 |
| P2 fixed 2 | 6 | 2 | 7.1 s | 218 MB | 218 MB | 1340 |
| P3 new-chat rate, 0..2 | 1 | 0 | 0 s | 0.8 MB | 109 MB | 6 |
| P4 user presence, 0..1 | 3 | 0 | 0 s | 6.3 MB | 109 MB | 49 |

### 6.3 Same replay with the benchmark bursts included (27 eligible)

| Policy | Hits | Helpful hits | Time saved | Average memory | Peak |
|---|---:|---:|---:|---:|---:|
| P1 fixed 1 | 27 | 13 | 90.7 s | 109 MB | 109 MB |
| P2 fixed 2 | 27 | 13 | 90.7 s | 218 MB | 218 MB |
| P3 new-chat rate, 0..2 | 20 | 9 | 68.4 s | 1.5 MB | 218 MB |
| P4 user presence, 0..1 | 24 | 11 | 81.2 s | 6.3 MB | 109 MB |

What the numbers say:

- A second slot never helped: one fill (9 s) is faster than the gap between
  any two eligible chats, even in an 11-chats-per-hour burst. Max 1 is enough.
- A fixed pool restarts its process every 30 minutes (TTL), 673 starts in 14
  days for 6 claims.
- The two real fast sends each came first after a long idle, so no
  activity-driven policy had a slot ready. The presence policy wins only when
  the user is already active.
- The presence signal here is "a human message in any dashboard chat in the
  last 30 min". A dashboard focus or reconnect event would be an earlier
  signal and might catch a first-of-the-day chat, but it is not in the
  transcripts, so it is **not measured**. Phase 2 counts it (section 9).

## 7. Design

### 7.1 Fit check at claim (phase 1, safety)

At fill, the pool stores beside each provider a `PoolFit` record. At claim,
after the bypass chain and the agent-name match, `_drain_and_claim` builds the
same record for the request, off the event loop, and discards any pooled
provider whose record differs, counted as a new outcome `miss_stale`. Each
field names its source on both sides, so no field compares a value with
itself:

| Field | Pooled side (taken at fill) | Request side (taken at claim) |
|---|---|---|
| resolved agent name | `pool_kiro_agent(cfg)` | the agent the request resolved |
| agent file digest | SHA-256 of the resolved agent file bytes | SHA-256 of the same path, read again |
| cwd | `pool_cwd` | the request's effective cwd |
| backend label | the factory's default backend | the backend the factory would pick now |
| kiro-cli binary stamp | realpath, size and mtime of the resolved kiro-cli binary, read just before the spawn | the same three values, read from disk at claim |
| spawn-config digest | digest of the spawn fields below, from the config the factory was built from | digest of the same fields from the manager's current config |
| identity epoch | already kept (`WarmPoolState.identity_epoch`) | unchanged |

The binary stamp is read from disk on both sides, so an in-place kiro-cli
upgrade changes the request side while the pooled side keeps the old value.
The handshake version is not used: the request side has no child yet.

Spawn fields: the ones a pooled child cannot change after it starts.

- `agent.model`, `agent.reasoning_effort`, `agent.role_efforts`
- `agent.default_agent`, `session.pool_agent`
- `agent.acp_backend`
- the MCP gateway overlay: `mcp_gateway.enabled` and
  `mcp_gateway.stub_servers`, which decide whether the factory passes the
  overlay at all

Phase 1 confirms this list against `build_provider_factory` and pins it with a
test, so a new spawn-time read added later fails the test until it joins the
list. An edit to any other config key is not a miss. The listed changes
already drain the pool through the config watcher (section 3.3), so this
field is the backstop for a missed watcher event.

Cost per claim: one hash of a small JSON file, one `stat`, one digest of a few config values. Failure mode: when a field
cannot be read, the check answers "differs" and cold-starts. A wrong miss
costs one cold start; a wrong hit is the bug this check exists to prevent.

### 7.2 Adaptive size (phase 2)

New keys, all under `session`:

| Key | Default | Meaning |
|---|---|---|
| `pool_mode` | `"fixed"` | `"fixed"` keeps today's `pool_size` behavior; `"adaptive"` uses the keys below |
| `pool_max` | `1` | the most slots the adaptive pool holds, `0..2` |
| `pool_idle_secs` | `1800` | return the slot after this long with no presence signal |

Target size, recomputed on each presence signal and each health sweep (30 s):

```
present = last presence signal within pool_idle_secs
shared  = prewarm_allowance() - live unclaimed eager-spawn entries
target  = min(pool_max, max(0, shared)) if present else 0
target  = 0 while the start queue is busy or the host is under memory pressure
```

- Presence signals: a human dashboard message (any chat), a dashboard
  focus or reconnect (the events that already schedule an eager spawn).
- Memory cap: `resource_status.prewarm_allowance()` maps the host's memory
  posture to how many idle pre-starts it can afford. Today that number bounds
  only the eager-spawn registry (`_prewarm_allowance` in
  `dashboard/chat_turn/eager_spawn.py`). The pool takes what is left of it
  after the live unclaimed eager-spawn entries, so the two together never
  hold more idle processes than the allowance.
- Load backoff: a fill takes the shared start semaphore at background
  priority (the default, `StartPriority.BACKGROUND`), so a foreground start is
  served first. In addition, no fill starts
  while another start is queued.
- Scale down: the health sweep discards ready slots above the target.
- The TaskRunner reservation (`_host_mem_term`) reads the live target,
  not `pool_size`, so an idle adaptive pool reserves nothing.
- `pool_size` keeps working unchanged in `"fixed"` mode.

### 7.3 Policies compared

| | P1 fixed 1 | P3 new-chat rate | P4 user presence (proposed) |
|---|---|---|---|
| Rule | always 1 | 1 for 30 min after a new chat; 2 after 3 in 10 min | 1 while the user is active, 0 after 30 idle min |
| Real saving (14 days) | 7.1 s | 0 s | 0 s |
| Burst saving | 90.7 s | 68.4 s | 81.2 s |
| Average memory | 109 MB | 0.8-1.5 MB | 6.3 MB |
| Catches first chat of a burst | yes | no | yes, if the user was active elsewhere |
| Complexity | none | a rate window | one timestamp |

P4 keeps most of the burst saving for about 6 % of P1's memory, with the
simplest state (one timestamp). P3 waits for the first new chat, which is the
one that needs the slot most.

## 8. Risks and rollback

| Risk | Guard |
|---|---|
| Wrong agent, cwd, backend or member route | unchanged bypass chain and agent match (section 3.2); fit check adds agent file, binary and config (7.1) |
| Stale agent file or binary | fit check; TTL stays as a second bound |
| Pooled state leaking into a chat (#2932 class) | unchanged: claim re-keys the client (`client.rekey` in `_get_or_create_impl`); phase 1 adds a test that a claimed session reports the same initial state as a cold one |
| Memory on a small host | one shared cap: pool target = allowance minus live eager-spawn entries; idle target 0 |
| Start storms | one fill at a time (`fill_active`, `_fill_warm_pool`); background priority |
| Flapping (fill, discard, fill) | target changes only on presence or the 30 s sweep; scale down only after `pool_idle_secs` |

Rollback: `session.pool_mode: "fixed"` with `pool_size: 0` (main's default)
turns everything off; the config watcher applies it live through
`refresh_defaults`, which drains the pool. Phase 1 has no switch of its own:
it can only turn a hit into a cold start.

## 9. Migration plan

| Phase | PR | Exit criteria |
|---|---|---|
| 1 | Fit check at claim, `miss_stale` outcome | a test edits the pool agent's file after fill and the next claim cold-starts; a test replaces the kiro-cli binary file after fill (new mtime) and the claim cold-starts; a test changes each spawn field and the claim cold-starts, and a non-spawn config key does not; an unreadable field cold-starts; existing pool tests unchanged |
| 2 | `pool_mode: "adaptive"`, `pool_max`, `pool_idle_secs`; presence signals; TaskRunner reads the live target | with eager-spawn entries live the pool target drops by that count; with no presence for `pool_idle_secs` the pool holds 0; a presence signal fills 1 within one sweep; under memory pressure it holds 0; a counter reports presence-signal kind so the focus signal of section 6 can be measured |
| 3 | Decide the default | after two weeks of opt-in use: `pool.decision` hit and `miss_stale` counts, average slots held. Off stays the default unless the data shows a saving worth the memory |

Phase 1 is useful alone: it protects anyone who sets `pool_size` today.
Phase 2 depends on phase 1. Phase 3 is a decision, not code.

## 10. Alternatives considered

- **Fixed `pool_size: 1` by default.** Simplest; holds 109 MB and restarts a
  process every 30 minutes on every host, for 7.1 s saved here in 14 days.
- **Per-agent pools (#2264).** Would let workers or a second agent claim, but
  workers are covered by G8 and no other agent had eligible traffic here.
- **Shorter eager-spawn debounce.** Cuts at most 1.5 s, and only for sends
  after the debounce; it does not help a send at once.
- **Pool for crewmate DMs.** Needs a member-route child per crewmate; the DM
  start comes from a trim, which R8 item 4 addresses directly.

## 11. Open questions

1. Is the dashboard focus/reconnect event a good enough presence signal, or
   should phase 2 use messages only? (Measured in phase 2.)
2. Should the fit check also hash each MCP server's own config file, beyond
   the agent file and the gateway overlay fields? The proposal: no. The MCP
   set is named in the agent file and routed by the overlay, and both are in
   the record.
