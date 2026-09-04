---
title: Session Tag-Change Event as a server-side signal on lane transitions
status: draft
author: (issue #7663 author)
created: 2026-09-02
last-audited: 2026-09-02
audited-at: 6581a04ee
doc-pr: null
implementation-prs: []
tracking-issues: [7663]
supersedes: []
superseded-by: []
---

# RFC: Session Tag-Change Event as a server-side signal on lane transitions

The kanban board records that a session moved between lanes; nothing
server-side reacts to the move. This RFC proposes a fire-and-forget event that
fires when a session's **status** tags change, so an automation can respond to a
lane transition, the motivating case being a close-out prompt that runs when a
session enters **Done**. It argues for reusing the existing script-hook engine
rather than adding a subsystem, and settles the four design questions the issue
flagged as expensive to reverse once anything subscribes. It is a design of
record only: nothing here is on main.

## Summary

Fire a server-side event when a session's status tags change so automation can
react to a lane transition (e.g. run a close-out prompt when a session enters
Done), reusing the existing user-authored script-hook mechanism rather than
adding a subsystem. The positions this RFC takes, one line each:

1. **Payload** carries the *delta* (`added` / `removed` status tag ids) **plus**
   the resulting status-tag list and the session key, so a consumer needs no
   prior-state file and does not re-invent the polling diff.
2. **Scope** is **status tags only** (tags whose definition carries
   `status: true`); non-status auto-tags, written routinely, do not fire.
3. **Failure posture is fail-open / informational, never vetoing**: unlike
   `PreToolUse` (which blocks on exit 2), a tag hook cannot veto a tag write -
   the user already performed the drag and a broken hook must not make the board
   unusable. This mirrors `rfc-mcp-lifecycle-event-log.md`'s "observability, not
   audit" posture and answers, for tag hooks, the same fail-open question
   [#7339](https://github.com/kirodotdev/KiroCrew/issues/7339) answers for
   `PreToolUse`.
4. **Re-entrancy** is settled now, not deferred: the event carries **advisory
   provenance**, following the in-repo precedent that `Stop` self-limits by
   advisory `hook_continuation_count` / `stop_hook_active`
   (`src/kiro_crew/hooks.py:4213-4221`) rather than an enforced cap.
5. **Placement** is a **sixth `HOOK_EVENTS` entry** (a `SessionTagsChanged`
   script-hook event), because the ask is to *run an automation now* on the
   transition, which is precisely what the script-hook engine does, not to
   fold a log after the fact, which is what `src/kiro_crew/events/` is for.

## Motivation

### Current state

- `HOOK_EVENTS` (`src/kiro_crew/hooks.py:93-99`, with the constants at
  `:86-90`) and `ALLOWED_HOOK_EVENTS`
  (`src/kiro_crew/validation.py:92-94`) carry **exactly five** events -
  `AgentSpawn`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`. All
  five are agent-turn lifecycle events; none fires when a tag moves outside a
  turn. A tag change has no agent and no turn.
- The tag writers mutate `slot.tags`, persist, and call `push_slots_update()` to
  the browser. That push is a client render signal; nothing server-side consumes
  it. Verified: `api_chat_slot_tags` (`src/kiro_crew/dashboard/chat_tags.py:507`,
  writes `slot.tags` at `:562`) and `api_chat_slot_drop` (`:902`, writes at
  `:983`) both end in a `push_slots_update()` with no server-side consumer of the
  transition.

### Problems

- The only way to react to a tag change today is to poll
  `GET /api/chat/slots` on a timer and diff the result against a remembered
  snapshot. That makes reaction latency equal to the poll interval, and forces
  every consumer to reimplement the same two things: the diff, and the first-run
  baselining (so the first poll after a restart does not treat every existing tag
  as "just added").
- There is no natural home for "Done now means something." The board is a
  display of state, not a trigger for it.

### Prior art, and why this is a third thing

Three neighbouring issues sit around this one; none asks for it:

- **[#3456](https://github.com/kirodotdev/KiroCrew/issues/3456)** (open), expose
  session tagging as an MCP tool. That is the **write** direction: let an agent
  set its own tag. This RFC is the **read** direction: let something react once a
  tag is set. They compose (#3456 lets an agent mark itself Done, this lets Done
  mean something) but neither implies the other.
- **[#1487](https://github.com/kirodotdev/KiroCrew/issues/1487)** (open) -
  project-level `.kiro/hooks/*.kiro.hook` with new `promptSubmit` / `agentStop` /
  `userTriggered` triggers. Same *category* of ask (an event surface), and its
  reaction raised a trust question about executing hook definitions that arrive
  inside a project checkout. **That concern does not apply here**: hook
  definitions live in a single global, user-authored store
  (`_HOOKS_FILE = "hooks.json"`, `src/kiro_crew/hooks.py:3939`), not per-agent and
  not from a checkout.
- **[#1861](https://github.com/kirodotdev/KiroCrew/issues/1861)** (closed,
  completed), an agent auto-tags its own session from context, shipped as
  `src/kiro_crew/dashboard/chat_auto_tag.py`. Relevant because that path
  **deliberately never applies status/workflow tags**: the `NEVER apply
  status/workflow tags` guard sits at `chat_auto_tag.py:101-108` (comment at
  `:102`, the `if existing.get("status"): return` guard immediately below, and
  `create_tag_definition(..., status=False)` for new tags), and its own
  `slot.tags` write is at `:135`. This RFC does not disturb that rule: it only
  *observes* status tags; it does not write them.

### Why this is cheap and needs no new trust or consent decision

- **Global, user-authored store, not per-agent.** A tag change has no agent and
  no turn, so a per-agent config would have been a blocker; the global
  `hooks.json` store (`hooks.py:3939`) is not.
- **No new capability surface.** Script hooks are already governance-gated by
  `capabilities.script_hooks`, default OFF, via
  `_script_hooks_capability_denied` (`src/kiro_crew/hooks.py:3671-3690`, checked
  inside `run_script_hook` at `:3743`). A tag-change hook rides that same gate;
  the capability surface does not widen.
- **Dispatch from an HTTP handler with no agent turn is already supported.**
  `api_hook_test` (`src/kiro_crew/dashboard/handlers/hooks.py:307`) calls
  `run_script_hook` (`:337`) with a synthesized payload, and `run_script_hook`
  (`src/kiro_crew/hooks.py:3728`) builds a default `hook_event` itself when passed
  `None` (`hooks.py:3761`), requiring no session and no agent config. A tag-write
  handler firing a hook is the same shape.

## Goals

- A **fire-and-forget** server-side notification on **status-tag** change,
  carrying the delta so a consumer needs no prior-state file.
- **Cover the full status-tag write surface**, not only the two handlers the
  issue names.
- **Reuse the existing script-hook mechanism** (engine, dispatch, capability
  gate, global store) rather than adding a subsystem.

## Non-goals

- **Not a write/tagging capability.** Letting an agent set its own tag is
  [#3456](https://github.com/kirodotdev/KiroCrew/issues/3456).
- **Not a change to `chat_auto_tag`'s never-status rule**
  (`chat_auto_tag.py:101-108`). This RFC observes status tags; it does not make
  the auto-tagger write them.
- **Not project-checkout hook trust.** New trigger sources arriving with a
  checkout are [#1487](https://github.com/kirodotdev/KiroCrew/issues/1487); this
  reuses the existing global store and adds no new trust decision.
- **Not an audit trail.** This is observability that *runs an automation*, not a
  tamper-evident record. SEL remains the audit trail and is untouched.

## Design

### Question 1: Payload shape is delta plus resulting list plus key

The event carries:

- `session_key`: the slot's session key (opaque; the existing session key
  convention).
- `added`: status tag ids gained in this transition.
- `removed`: status tag ids lost in this transition.
- `status_tags`: the resulting status-tag id list after the write.

A hook that wants "Done was just added" reads `added`; it never has to keep its
own prior-state file. Carrying only the resulting list would push the diff back
onto every consumer, relocating the polling problem rather than solving it. The delta is
computed at the emit point from the pre-write and post-write status-tag sets
(the write sites already hold both: e.g. `prior_tags` / `new_tags` at
`chat_tags.py:561-562`).

### Question 2: Scope is status tags only

The event fires only when the **status** subset of `slot.tags` changes. A tag is
a status tag when its definition carries `status: true`, the same predicate the
drop handler already uses to separate lanes from labels
(`kept = [t for t in slot.tags if t in tag_index and not tag_index[t].get("status")]`,
`chat_tags.py:980`), and the same flag `chat_auto_tag.py` checks at
`:101-108`. Firing on every tag would make the event chatty for the case most
people want, because `maybe_auto_tag` writes **non-status** tags routinely
(`chat_auto_tag.py`, which never writes status tags by construction). So the emit
point diffs the *status* subset only: if `added` and `removed` are both empty
after filtering to status tags, no event fires.

### Question 3: Failure semantics are fail-open, informational, never vetoing

`PreToolUse` can block a tool call on exit 2. A tag-change hook must **not** be
able to veto a tag write. Two reasons: the user has already performed the drag
(the write has committed and `push_slots_update()` has told the browser), and a
broken hook that could veto would make the board unusable. So the dispatch is
fire-and-forget: the hook's exit code and output are recorded for the hook-test
surface but never gate the write, and a hook that raises, times out, or is
denied by the capability gate leaves the tag write exactly as it landed.

This is the same posture `rfc-mcp-lifecycle-event-log.md` takes for the
lifecycle log, *"this log is observability, not audit"*, and it answers, for
tag hooks, the same fail-open question that
[#7339](https://github.com/kirodotdev/KiroCrew/issues/7339) answers for
`PreToolUse`. An emitter that needs fail-closed semantics belongs in SEL, not
here.

### Question 4: Re-entrancy uses advisory provenance, settled now

A hook that reacts to a tag change by setting a tag can retrigger itself. Rather
than leave each hook to defend itself, the event carries **advisory
provenance**, an `origin` field naming what caused the write
(`user` for a drag/API call, `agent` for an MCP/auto path, `folder-inherited`
for the inheritance path, `hook` for a write a tag-change hook itself made) and a
`hook_reentry_depth` counter (0 on a user- or agent-driven transition, incremented
when the write originates from within a tag-change hook's own reaction).

This follows the settled in-repo precedent: `Stop` controls its own re-entrancy
with **advisory** signals, not an enforced cap. `hook_continuation_count` (the
depth of the current continuation run) and `stop_hook_active` (its boolean
shorthand) are stamped on the `Stop` payload unconditionally
(`src/kiro_crew/hooks.py:4213-4221`) precisely so a hook *may* self-limit while a
real gate hook checks its own condition and ignores them. The comment there is
explicit: *"Kiro's Stop contract defines no cap ... a hook may self-limit,
diagnose, or surface the count."* We adopt the same stance: the runtime provides
the depth and origin; the hook decides. Enforced suppression is rejected because
it would silently swallow a legitimate second transition (Done → In Review → Done
in quick succession is a real workflow), and because it would be the *only*
enforced re-entrancy control in the hook engine, contradicting the `Stop`
precedent.

### Placement: a sixth `HOOK_EVENTS` entry, not an `events/` kind

Two homes exist for this signal.

**Option A: a sixth `HOOK_EVENTS` entry** (a `SessionTagsChanged` script-hook
event). This reuses the existing script-hook engine, its HTTP-handler dispatch
path (`api_hook_test` → `run_script_hook`), its capability gate
(`capabilities.script_hooks`, default OFF), and its global user-authored store
(`hooks.json`). It is additive: `SessionTagsChanged` joins the `HOOK_EVENTS`
tuple (`hooks.py:93-99`) and the `ALLOWED_HOOK_EVENTS` frozenset
(`validation.py:92-94`); existing hooks are unaffected.

**Option B: a session-domain kind in `src/kiro_crew/events/`.** That package
already has a `session` domain (`session/message`, `events/kinds.py:40`), and
`rfc-mcp-lifecycle-event-log.md` is settling its first-emitter precedents right
now (per-`key` monotonic `seq`, one gateway append writer, fail-open because "the
log is observability, not audit"). A `session/tags-changed` kind would fit its
envelope (`{v, kind, src, key, ts_ms, data}`, additive-only, opaque `key`,
`RawEvent` tolerance, `events/base.py`).

**Recommendation: Option A.** The decisive difference is what the ask *does*. The
`events/` log is an **observe-after-the-fact** surface, a fold/read that a
consumer polls or replays (its own contract says ordering and the write path
"arrive with the first emitter", and its consumers "fold the log"). The request
here is to **run an automation now**, at the moment of transition, exactly the
script-hook engine's job. Routing through the event log would still require a
separate consumer that folds the log and *then* dispatches a hook, which is
strictly more machinery than firing the hook at the write site. The event log and
this event are not mutually exclusive: a later change may *also* emit a
`session/tags-changed` kind for history/observability once the lifecycle log has a
live writer (that is the mcp-lifecycle RFC's Phase 1), and this RFC does not block
that. But the run-now automation belongs on the hook engine.

**Where the RFC line falls.** `GOVERNANCE.md` reserves an RFC for a change to a
public interface that other parts build around and that is expensive to reverse.
A *new event other automations subscribe to* is exactly that, the payload shape
and semantics are the contract, and reversing them after anyone subscribes is the
cost the issue itself flags. That is why this is an RFC and not a plain feature
PR, even though the *mechanism* is small. The contrast is
[#3952](https://github.com/kirodotdev/KiroCrew/pull/3952), which landed
contract-level hook-*matcher* work as a plain feature PR with no RFC: matcher
semantics tune an existing event's dispatch; they do not add a new event surface
for others to build on. The design questions (payload, scope, failure, re-entrancy)
are the reversible-once-subscribed decisions, which is precisely what needs
agreeing before code.

### The full status-tag write surface, and which sites emit

The issue names two handlers; the true surface is larger. Verified sites that
write `slot.tags` at HEAD `6581a04ee`:

| Site | Location | Writes | Emits? |
|---|---|---|---|
| `api_chat_slot_tags` | `chat_tags.py:507` → `slot.tags` at `:562` | user set-tags API | **yes** |
| `api_chat_slot_drop` | `chat_tags.py:902` → `slot.tags` at `:983` | user drag-to-lane API | **yes** |
| folder inheritance | `chat_handlers.py:2259-2260` (`slot.tags.append`), ids from `validate_folder_tag_ids` (`chat_tags.py:60`) | a new slot inherits a folder's tags | **yes** |
| channel first-file / restore | `channel_slots.py:372-373` and `:392-393` (`slot.tags.append`) | channel slot filing | **yes** |
| slot restore (bulk) | `chat_handlers.py:6394` / `:6402` (`slot.tags = ...`) | reconstruct from stored value | **no** (load-time) |
| fork | `chat_fork.py:699` (`new_slot.tags = list(slot.tags)`) | fork inherits parent tags | see below |
| persistence load/prune | `chat_persistence.py:990` / `:1002` and `:1470` / `:1482` | load-time reconstruction | **no** (load-time) |
| auto-tag | `chat_auto_tag.py:135` | never writes status tags (`:101-108`) | **n/a**, no status delta ever |

The folder-inherited path is the one the issue's two-handler framing misses:
`validate_folder_tag_ids` (`chat_tags.py:60`) screens a folder tag id's **shape**
(must be a list of strings) and **vocabulary** (intersected with the live
`state._tags` when authoritative) but **not** `status: true`. So an inherited
folder status tag can put a session into a lane through
`chat_handlers.py:2259-2260` **without either named handler running**. Any design
that emits only from the two handlers would miss lane entries. This site must
emit.

**Load-time reconstruction must not emit.** The persistence and bulk-restore
sites (`chat_persistence.py:990/1002/1470/1482`, `chat_handlers.py:6394/6402`)
rebuild `slot.tags` from the stored value on boot/reload; emitting there would
replay the entire tag history on every restart and treat every existing Done as
"just entered Done." These sites are silent. This is the same reasoning the
lifecycle-log RFC uses to keep segment loading out of the emit path.

**Fork** copies the parent's tags to a new slot; it is a genuine new lane entry
for the child, so it *may* emit, but the `origin` is `fork` and consumers that
only care about human transitions can filter it. Left as an open question below.

**Recommendation: a single choke-point emitter, not per-handler emits.** Because
the write surface is this wide and shares one invariant (`slot.tags` mutated under
`tags_write_lock`, per the comment at `chat_handlers.py:2250-2256`), the emit
belongs at a single helper that every write site funnels through, it takes the
pre-write and post-write status-tag sets, computes the delta, and dispatches the
hook only if the status subset changed. A single choke point is the only way to
guarantee the folder-inherited path (and any future write site) cannot silently
skip the event, which is exactly the failure mode `validate_folder_tag_ids`'s own
docstring warns about for its consolidation. Per-site emits are rejected: they are
the shape that let review miss call sites twice during #7366 (as the
mcp-lifecycle RFC records) and would re-open that finding here.

## Migration plan

Design of record; nothing on main. Phases are independently shippable.

### Phase 1: the event constant and its emit choke-point (backend only)

Add `SessionTagsChanged` to `HOOK_EVENTS` (`hooks.py:93-99`) and
`ALLOWED_HOOK_EVENTS` (`validation.py:92-94`), and add one status-tag-delta emit
helper that the two `chat_tags.py` handlers, the folder-inheritance path
(`chat_handlers.py:2259-2260`), and the channel filing path
(`channel_slots.py:372-373` / `:392-393`) call. Fire-and-forget through
`run_script_hook`; load-time sites stay silent.

**Exit criteria:** a hook registered for `SessionTagsChanged` fires with a
populated `added` when a slot is dragged to Done via the API, via a folder-inherited
status tag, and via a channel file; it does **not** fire on server restart, on a
non-status auto-tag, or when the status subset is unchanged; a hook that raises or
times out does not affect the tag write (assertable on `slot.tags` after the call).

### Phase 2: payload provenance and re-entrancy signals

Add `origin` and `hook_reentry_depth` to the payload; stamp `origin=hook` and
increment depth when the write originates inside a tag-change hook's reaction.

**Exit criteria:** a hook that sets a status tag in reaction observes
`hook_reentry_depth > 0` and `origin = "hook"` on the retriggered event; a
same-turn Done → In Review → Done sequence still delivers both transitions (no
enforced suppression).

### Phase 3: documentation and the hook-test surface

Extend the events documentation and let `api_hook_test`
(`dashboard/handlers/hooks.py:307`) synthesize a `SessionTagsChanged` payload so
the event is testable through the existing test endpoint, mirroring how it
already synthesizes the `Stop` payload.

**Exit criteria:** the hook-test endpoint returns a hook's output for a synthesized
tag-change payload; the events doc lists the new event and its payload keys.

*Blocked-on-open-question:* the exact payload key names (Open question 1) gate
Phase 1's public shape; fork emission (Open question 3) gates whether Phase 1 wires
`chat_fork.py:699`.

## Backward compatibility

Additive throughout. `SessionTagsChanged` is a new entry in an existing tuple
(`HOOK_EVENTS`) and frozenset (`ALLOWED_HOOK_EVENTS`); existing hooks match on
their own event and are unaffected. No store or interface is reshaped: `slot.tags`,
`hooks.json`, and the persistence format are untouched. A hook registered for the
new event on an older build simply never fires. Rollback is deletion of the constant
and the emit helper.

## Security considerations

- **No new capability surface.** The event rides the existing
  `capabilities.script_hooks` gate (default OFF,
  `hooks.py:3671-3690`); a deployment that has not enabled script hooks sees no
  new behavior.
- **No new trust decision.** Hook definitions remain in the global user-authored
  `hooks.json` (`hooks.py:3939`); nothing executes definitions that arrive with a
  project checkout (the [#1487](https://github.com/kirodotdev/KiroCrew/issues/1487)
  concern does not apply).
- **Minimal payload.** The event carries the session key and status tag ids
  only: no transcript, no tool arguments, no environment.
- **Fail-open cannot brick the board.** Because a hook failure never gates the
  write, a broken or hostile hook degrades an automation, not the board.
- **The never-status auto-tag rule is untouched** (`chat_auto_tag.py:101-108`):
  this event observes status tags; it does not cause them to be written.

## Alternatives considered

- **Poll `GET /api/chat/slots` and diff (status quo).** Rejected: reaction
  latency equals the poll interval, and every consumer reimplements the diff and
  first-run baselining. This is the problem, not a solution.
- **A `session/tags-changed` kind in `src/kiro_crew/events/` as the primary
  mechanism.** Rejected as *primary* because the ask is run-now automation, and the
  log is an observe-after-the-fact fold; routing through it needs a separate
  consumer that then dispatches a hook. May be added later as a *secondary*
  history/observability emit once the lifecycle log has a live writer (that RFC's
  Phase 1); this RFC does not block it.
- **A vetoing / blocking variant (`PreToolUse`-style exit-2 block).** Rejected per
  Question 3: the user already performed the drag, and a broken hook must not make
  the board unusable.
- **Firing on all tags, not just status tags.** Rejected per Question 2:
  non-status auto-tags are written routinely, making the event chatty for the case
  most people want.
- **Enforced re-entrancy suppression.** Rejected per Question 4: it would swallow
  legitimate rapid transitions and would be the only enforced re-entrancy control
  in the hook engine, contradicting the advisory `Stop` precedent
  (`hooks.py:4213-4221`).

## Open questions

1. **Exact payload key names.** `added` / `removed` / `status_tags` /
   `session_key` / `origin` / `hook_reentry_depth` are proposed; the final spelling
   is the reversible-once-subscribed decision and should be locked before Phase 1
   ships.
2. **Coalescing.** Should a remove-then-re-add of the same status tag within a
   short window coalesce into no event (net-zero delta), or deliver both
   transitions? Proposed: deliver both (no debounce), matching the no-suppression
   stance in Question 4; revisit only if a real chatty case appears.
3. **Fork emission.** Should `chat_fork.py:699` emit for the child slot's inherited
   lane, or is a fork a load-like reconstruction that stays silent? Proposed: emit
   with `origin = "fork"` so consumers can opt in or out, but this is the least
   certain of the write-surface calls.
4. **`events/`-package alignment.** If maintainers prefer this be *observable* in
   the lifecycle log too, a `session/tags-changed` kind can be added as a secondary
   emit once that log has a live writer. This RFC recommends the hook event as the
   run-now mechanism regardless; the log emit is complementary, not a substitute.
