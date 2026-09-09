# Advisor Module

## Overview

The advisor module (`kiro_crew/advisor/`) is an opt-in, asynchronous,
cross-model session reviewer. When enabled for a session, it observes the
primary agent's work at host-owned checkpoints, reviews it on an isolated
reviewer session with read-only evidence tools, and returns severity-aware
advice. It never impersonates the primary agent, never blocks the primary
turn, and is fully inert when disabled: no reviewer process, no event
buffering, no model cost, no UI noise.

It is a native session service owned by dashboard/session lifecycle code,
keyed by the effective parent session identity. It is not an app, not a peer
session, not a dashboard slot, and not an ordinary subagent.

Related feature requests: serial artifact review and iterate-until-clean, and
a clean-context review gate for final responses. The advisor implements the
asynchronous, opt-in, checkpoint-observing contract; those workflows can
later compose on the same subsystem.

## Package layout

| File | Responsibility |
|---|---|
| `observation.py` | Normalized, bounded, redacted primary event records; checkpoint batching; observation epochs. |
| `service.py` | Parent-session registry, effective config, enable/disable, lifecycle attachment, reset/dispose, the async review pump. |
| `runtime.py` | Shared reviewer runtime pool: per-parent reviewer sessions, crash self-heal, release/reap/shutdown (PID protection is `AcpRuntime`'s own). |
| `output.py` | Strict reviewer envelope validation (`AdvisorNote`). |
| `guard.py` | Emission guard: dedupe, non-blocker budget (4 per update), interruption cooldown (120 s), interruption cap (3 per epoch; a blocker past it is preserved as a card, never steered) -- module constants; reset with the epoch. |
| `delivery.py` | Advisory envelope over the shared steer ledger; preserve-on-unconsumed; staged next-turn context. |
| `composition.py` | Dispatcher policy, prompt rendering, packaged-agent materialization, runtime factory over the agent SDK. |
| `read_gate.py` | kiro-cli `preToolUse` hook for the reviewer's builtin reads: judges each `fs_read`/`grep` with `advisor_permission_gate` before execution; exit 2 blocks, any failure blocks; self-tested by the installer before every spawn. |
| `usage.py` | Reviewer usage attribution helpers. |

## Observation contract

Checkpoints derive from facts the host owns — never from transcript polling
(edits, rewinds, regeneration, compaction, forks, and transfers make JSONL
polling unsafe):

1. Completed tool results coalesce (in order) into an `in_progress=True`
   `ObservationUpdate`.
2. A text segment finalized before a tool group is included in the next
   in-progress update.
3. The turn terminal produces exactly one `in_progress=False` final update
   per epoch; completion is idempotent, so a replayed terminal cannot emit a
   duplicate. A host-fabricated terminal carries `synthetic=True` and is
   never treated as a genuine provider completion.
4. Every update carries the parent session key, parent turn identity, the
   observation epoch, and a monotonically increasing sequence.

Records are bounded to `OBSERVATION_PAYLOAD_MAX_CHARS` characters (truncation
is flagged, not silent) and pass through the owner-supplied redactor before
buffering. Reasoning content is never written into the parent transcript for
the advisor's sake.

Neither the primary model's reasoning nor the user's messages are observed:
the records are the assistant's visible text segments and tool results only,
so the observer never holds anything rawer than the transcript surface and
no user-authored text reaches the second model. The reviewer therefore
judges the work on its own terms (wrong logic, risky actions, claims the
tool results contradict, changes that break the surrounding code) and goal
or requirement review is scoped out of v1 -- its agent prompt says so. Feeding (redacted) reasoning or the user's
messages to the reviewer is a follow-up, to be taken up when a review that
missed something for lack of them is named -- or when a goal-blind blocker
proves to be a false positive in practice (a `blocker` on a destructive
action the user explicitly asked for interrupts a turn that was doing what
it was told; the primary weighs the note as advice, and the per-review
outcome log records each steer, so such a case can be named).

### Epochs and turns

A lifecycle boundary — session remove/reset/reload, model/agent/workspace/
provider switch, native conversation reset, compaction/history rewrite,
fork/transfer destination creation, gateway shutdown/recycle — starts a new
observation epoch via `AdvisorObserver.begin_epoch()`. Pending records and
dedupe state (the emission guard resets with the epoch) never cross an epoch
boundary. Recording after a completed epoch raises until a new epoch begins.

A new TURN on the same session re-primes a sealed epoch via `begin_turn()`:
turn N+1 records land instead of raising, while a completed update the pump
has not consumed yet survives the re-prime. Attachment re-resolves the
per-session override every turn, so switching a live session to `off` drops
its observer and guard at the next turn boundary rather than being ignored.

Wiring: switch/reload resets notify through the dashboard's single reset
helper; slot close notifies at the synchronous tombstone; a successful
auto-compaction notifies through the session manager's compact callback (a
failed compact rewrote nothing and does not touch the epoch); gateway
shutdown disposes every observer via an `on_cleanup` hook. A fork or transfer
destination is a NEW session key with no observer, and the source is left
untouched by design, so both are fresh without a dedicated notification.

### Coverage boundary

Observation hooks live in the dashboard chat runner (`_run_chat`), so every
turn that runs through a dashboard chat slot is observed: interactive
chat, dashboard-bound goal loops and babysit wakes, and subagent-completion
injections. Turns that do not run through it -- task-executor steps,
channel `TurnDriver` turns, cron job turns, and subagent worker turns --
are NOT observed in v1; extending the hooks to those loops is a follow-up.

## Service contract

`AdvisorService` owns per-parent-session observers behind an enablement gate:

- Disabled (`enabled=False`, the default): `attach()` returns `None`, no
  observer exists, `observer_count()` is 0.
- Enabled mid-session: the observer starts empty at the current boundary;
  historical work is not backfilled or reviewed.
- Opted out mid-session: the next attach drops the live observer and guard.
- `detach()` disposes a session's observer. A terminal boundary also releases
  the parent's reviewer session on the pool; gateway disposal shuts the pool
  down so the shared subprocess dies with the gateway.

## Delivery contract

An advisory rides the same steer ledger as a user send through an additive
envelope parameter on the chat delivery seam: same pending registration, same
delivery-id reconciliation, same consumption evidence. The persisted row
carries the `advisor` role and provenance meta (`advisorSeverity`,
`advisorUpdateId`, `advisorState`); the injected text tells
the primary to weigh the evidence, never to obey it. The reviewer's text is
model output: besides outbound redaction, any reserved advisor delimiter it
carries (`[Advisor]`, `[Advisor context]`, `[End advisor context]`) is
neutralized where the steer text and the `[Advisor context]` frame are rendered (so a restored entry is covered too) -- together with the primary's own structural boundary markers via the platform's span-local neutralizer -- and a note cannot close or forge a frame
early and land its remainder as bare instructions. An advisory the turn
never consumed is PRESERVED at teardown -- a visible Advisor card plus context
staged for the next primary turn -- and never enters the user queue or runs
as a user-authored turn. The staged context is drained exactly once, at the
next turn's start, prepended to the outgoing message with the same
weigh-not-obey framing. Both the staged context and the per-session override
are SESSION-scoped even though they live on slots: every live slot sharing
the effective session key (a channel-stem slot and a dashboard tab linked to
the same channel) is staged, read, drained, cleared, and overridden together
(staged in memory on every alias, since the list is persisted into the one
shared transcript from whichever alias saves next; only the acting slot is
dirtied -- on stage, drain and clear alike -- so a stale alias is never forced
to flush its own copy of the shared metadata over the active one's), so an
opt-out on one alias cannot be undone by a sibling's stale setting and advice
preserved on one alias reaches the session's next turn wherever it runs.
A slot object can also change conversation mid-turn (a cron/workflow swap of
`linked_session_key`): the observer key is pinned at attach and every later
checkpoint and pump drops on a mismatch, staged context is tagged with the session it
was staged for (re-bound on restore, after the slot's link is restored) and a
mismatch clears it unread, and the close boundary fires
only when the last slot fronting a session closes -- an idle alias closing
never drops a sibling's live observer.
User sends without an envelope keep byte-identical
row and payload shapes. A hard kill discards pending advisories alongside
pending user steers.

## Usage attribution

A reviewer turn is real spend but not the parent's turn. Its usage row is
keyed by the reviewer session's stable synthetic key (`advisor:<parent session key>`, so spend stays traceable across restarts) and
tagged `surface="advisor"` -- the two existing token-record fields attribute
the spend, and the token-record schema is untouched. The parent link lives on
the advisory row itself (`advisorUpdateId`), not on the usage row.

## Configuration

Effective enablement composes the global default with a per-session override
(`inherit` / `on` / `off`): `on` and `off` win in both directions, and
an absent value is `inherit`, and anything else unrecognized on disk
collapses to `off` rather than to a default that could enable review. The override endpoint mirrors the value onto every slot of the
session in memory and persists it ONCE, through the authorized slot, the way
every other slot-metadata route does -- a forced save confirmed before the
200 (the periodic dirty flush skips message-less slots). Siblings are not
dirtied: the aliases share one transcript, and a sibling's full save would
rewrite the shared metadata from its own copy. The transaction is serialized
on the transcript's keyed lock (alias slots of one session share it, so a
losing request's rollback cannot undo the winner's acknowledged write) and
the save is pinned to the transcript key the request was authorized against,
so a rebind mid-save makes the write refuse; a rebind detected after the
write leaves the acknowledged value on the authorized transcript (200, applied
to that session's observer): every slot still bound to that transcript carries
the written value in memory (including an alias the channel reconciler created
during the save, which loaded the pre-write value), and only the rebound slot
gets its in-memory value back, since it now fronts another conversation.
On failure nothing reached disk and every member is restored in memory before
the coded error returns.
While the reviewer is unavailable (a non-kiro `agent.acp_backend`), `on`
is refused with the same reason the settings toggle gives (`409
advisor_unavailable`, shown inline by the control); `off` and `inherit`
always land.

## Review pump

Observation flows to review asynchronously, DURING the turn as well as at
its end. The chat runner feeds the observer at host-owned checkpoints (turn
attach, redacted tool results, finalized segments, one idempotent final
update at the terminal event) and schedules a fire-and-forget pump at each
checkpoint the primary turn never awaits — so in-progress advice can arrive
while a mistake is still cheap to fix. Each update carries the epoch's
CUMULATIVE evidence (bounded to `EPOCH_MAX_RECORDS`, newest wins): a live
run proved slice-at-a-time reviews myopic — twenty reviews each saw one
innocuous fragment and missed what one whole-turn review caught. Drains are
gated on genuinely new records, and in-progress reviews are throttled per
session (`review_min_interval_secs`); the final update always reviews. A
review that a reset, compaction, or opt-out raced is discarded before
dispatch (observer identity and epoch are re-checked), and the same live
check runs again before every note of one review -- a blocker steer awaits
the running turn, so a revocation can land between two notes, and the
remaining notes are then dropped (`revoked`) rather than persisted, staged,
or steered -- so stale advice never
crosses into a replacement conversation. The pump drains one update, renders it as the reviewer
prompt, runs one bounded review on the shared pool, decodes the JSON
envelope from the reviewer's final text with the platform's `parse_llm_json`
(bare, fenced, or prose-embedded; validation stays strict), and dispatches through the per-session emission
guard. Every failure leg — no observer, empty drain, unbound pool, reviewer
error, malformed output — ends the pump quietly; a lost review logs its
cause at WARNING, and every completed review logs one INFO line with its note count and dispatch
outcomes, so a silent reviewer and a broken dispatcher are distinguishable
in production. Degradation is not yet surfaced in the dashboard UI; #10135
tracks a `degraded` state on the per-session control.
Enablement is decided by the ATTACHED OBSERVER, never re-checked against
the global flag: attach composed the global default with the per-session
override, so a session opted `on` under a global-off default reviews.
The reviewer's own text passes outbound redaction (credentials,
exfiltration URLs) before it is injected or persisted — reviewer output is
model output. A `blocker` steered into a turn that never consumed it flips
its existing card to preserved rather than duplicating it.

The reviewer runs as the packaged read-only `kirocrew-advisor` agent
(`advisor/agents/kirocrew-advisor.json`; tools exactly `fs_read` and
`grep`) on one shared runtime. That spec is a kiro-cli agent definition, so
the reviewer runtime is kiro-cli ONLY in v1: enabling the advisor is
refused by the config PATCH surface (visible reason; the check reads the
config watcher's in-memory snapshot, never the disk, and fails closed before
the watcher has started) when `agent.acp_backend` selects another harness, and `configure_from_config`
marks the reviewer unavailable under one (one WARNING; the advisor stays
disabled, every observer -- a per-session `on` included -- is detached and
its in-flight review discarded, and the availability flag is composed into
every later enablement decision), so a harness the operator never selected
is never spawned; a live `agent.acp_backend` PATCH re-applies the section.
Serving the reviewer on the Claude/Codex backends is a follow-up.

The spec is a managed artifact: on every pool
build the PACKAGED spec is parsed, its `allowedTools` filtered through the
governance ceiling, and written over the managed file in the kiro agents
directory (tmp+rename; nothing on disk flows into the spec), so a stale grant,
a hand edit or a planted link cannot widen the boundary. A regular file at the
reserved name that is not the managed spec (its description does not open with
the managed sentence) is somebody's configuration: the install refuses with a
name-collision error and leaves it intact, and the pool degrades visibly. The reviewer's reads are
gated BEFORE execution: kiro-cli approves its builtin reads (`fs_read`, `grep`)
natively without raising a permission request, but it runs the agent spec's
`preToolUse` hooks first and blocks the call on exit 2, so the managed spec
carries one hook per read tool pointing at `kiro_crew.advisor.read_gate` (the
gateway's own interpreter), which boots the platform context and judges the
call with `advisor_permission_gate` -- the same read-only ceiling, governance
hook and sensitive-path checks that judge a permission request -- and exits 2
on a denial or on any failure (kiro-cli treats other exit codes and a missing
command as a hook error and runs the tool, so the installer refuses when the
hook interpreter is not executable, and it executes the complete hook command
against a known-denied and a known-allowed read before every spawn, refusing
unless both verdicts are right -- an editable install runs the hook from the
source tree, so the module's verdicts are proven, not assumed). Under that gate the OS sandbox is the
kernel-enforced floor: the reviewer runtime runs under the STRICT sandbox tier
(credential directories masked) and the install refuses on a host whose
sandbox cannot apply that mask to a kiro-cli child: no sandbox backend, sandboxing off, or a platform where the
child's isolation is delegated to kiro-cli's own sandbox (macOS with that
sandbox enabled, Windows), since Crew's tier is never applied there. That
host is detected once at gateway startup (off-loop) and composed into
`reviewer_available`, so enabling is refused up front with the reason -- the
settings toggle's PATCH validator and the per-session control's 409
(`advisor_sandbox_unavailable`) -- the same way a non-kiro backend is. The
reviewer's child additionally hides the crew-home leaves the tier leaves
read-write for a primary's in-sandbox MCP servers (the SEL trust root and key,
the security-event log, the dashboard secret; `sandbox.mcp_only_crew_leaf_targets`),
since the reviewer runs no MCP server. The spec carries no `allowedTools`, every observed tool call
is recorded on the SEL trail (`auto_approved` unless a permission decision
already covered it; a call that cannot be recorded abandons the review, since
the read has already run and an unaudited one must not contribute), and any tool call that DOES raise a permission request
is judged by `advisor_permission_gate` -- the read-only tool ceiling
(keyed on the trusted tool NAME only; a request without a name is denied,
never authorized by its self-declared kind),
`is_sensitive_path` / `path_contains_sensitive` on every path argument (a
pathless `grep` is judged at the reviewer cwd), and the platform's
PreToolUse hook -- approving once or denying, fail-closed. Path arguments
are extracted with the platform's exhaustive nested walker (`target_paths`),
so the batched `fs_read` shape (`operations[].path`) is checked like a
flat one; a read exposing no target, or a walk the platform had to
truncate, is denied as unverifiable. kiro-cli resolves
`--agent` against `<cwd>/.kiro/agents` before the global directory, so the
reviewer process is spawned from a crew-owned directory under the config dir
(`<config>/advisor`: mounted read-only inside every sandbox -- pre-created so
the mount exists, its name never resolved through a symlink -- and on the
agents' file-edit write-protected list, with the installer refusing to spawn
while anything sits under its `.kiro` tree; outside the agents tree, which the
delegated sandboxes refuse as a workspace); the
observed workspace is only ever the SESSION cwd.
The reviewer model is the `advisor` role pin, `agent.role_models.advisor`
-- `agent.role_models` is the only sanctioned place to pin a model for a
class of work, and the pin passes the same entitlement validation as every
other role; `auto` or no pin keeps the runtime default, and a pin the
runtime's model-id grammar rejects falls back to the default with a warning
instead of failing every review. Each reviewer session runs with the
OBSERVED slot's project as its working directory, so evidence tools read the
right tree even when one shared runtime serves parents in different
workspaces. Reviewer spend persists as an attributed usage row per review
(see Usage attribution). Gateway startup applies the `advisor.*` config
section on a background task off the bind path; pool binding follows
enablement (a disabled advisor constructs nothing, and disabling live
unbinds and shuts the pool down), and no process spawns until an enabled
session's first review. The one `advisor.*` key (`enabled`) and the
reviewer's `agent.role_models.advisor` pin are editable from the dashboard settings (Settings → Chat → Advisor) through the
config PATCH surface, which re-applies to the live service on success.

## Testing

The `test/test_advisor_*.py` suites pin the contracts above: observation and
epochs, runtime pool lifecycle, envelope and guard, delivery and staged
context, lifecycle boundaries, config resolution, per-slot override, usage
attribution, turn hooks, dispatch policy, and the pump end to end. The
feature was additionally validated live against an isolated dev gateway: a
weak primary model paired with a stronger reviewer produced real mid-work
`[concern]` interventions on a genuine transcript.
