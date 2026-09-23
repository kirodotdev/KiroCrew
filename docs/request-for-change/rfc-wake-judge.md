---
title: Wake judge — a System One model screens auto-nudge ticks
status: partial
author: Raymond Chen
created: 2026-09-22
last-audited: 2026-09-23
audited-at: 2fdadc71ac
doc-pr: 12735
implementation-prs: [12776]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Wake judge — a System One model screens auto-nudge ticks

- Feature preview; default off
- Companion: [`rfc-conductor-work-ledger`](rfc-conductor-work-ledger.md) Phase 3
  (typed wake gate). The judge is the other half: it screens evidence only a
  model can read.

Status: partial. The LLM lane is on main: `decisions/impl_llm.py` answers the
judge's questions over one tool-less model call, `gate.py` carries `nudge.wake`
in `DECISION_POINT_NAMES` beside `JUDGE_POINT` and the two lane names, and
`decisions.nudge_wake.provider` and `.llm_model` are live config keys the config
route may edit. The judge core is in flight on
[#12787](https://github.com/kirodotdev/KiroCrew/pull/12787) rather than on main:
`decisions/points/nudge_wake.py`, the `nudge_evidence` entry in
`POINT_SCOPE_KEYS`, the evidence collectors, the autonudge tick hook, `judge` on
`monitor_start` / `monitor_update` and the transcript notice are all absent from
main, and so are the skills that author a brief. No `kirocrew-judge` agent
template is coming: the landed lane runs on the bundled `kirocrew-lite`
(`JUDGE_AGENT_NAME`), because a judge template would differ from it only in the
model, which the runner passes per call, and a standing prompt the call already
states. Code references below were read at `2fdadc71ac`. The sections describing
the core describe that branch, read at `08f5787fb8`, and it is a subset of them:
it carries the transcript-tail and pull-request-probe evidence kinds, states that
a comment-BODY kind is deliberately absent because its reader publishes only a
digest, and carries no work-ledger or self collector at all. Its `default_spec()`
carries the two criteria and not §3.3's untrusted-evidence clause, and its scope
text is the opt-in wording §7 Q4 and Q5 are about. Re-audit §3.2, §3.3 and §3.6
at merge.

## 1. Problem

An auto-nudge loop (`monitor_start`) fires a full model turn on its owning
session every interval. For a pull request named by URL, `PrWatchProbe` already
turns unchanged ticks into free re-arms; for everything else — a conductor
patrolling worker transcripts, a babysitter reading bot comment bodies, a loop
watching a log — every tick costs a turn on the main session whether or not the
new evidence needs it. Measured on this repository today: a conductor watching
three workers at 45 min spends ~48 turns over two days to act on about five
events; ten patrol loops died together on a gateway restart and nobody noticed
for hours because the only reader was the loop itself. A second measurement, from
this document's own review cycle: one patrol ran 9 cycles at 45 minutes across 5
workers, and roughly two thirds of those reads found nothing new.

The typed wake gate ([`rfc-conductor-work-ledger`](rfc-conductor-work-ledger.md)
§Wake gate, Phase 3) removes the turn for facts that arrive as typed fields
(item status, PR checks). It cannot read a transcript tail, a review comment, or
a log line. Those still need judgment, and today the only judge available is the
main session at full price.

## 2. Idea

Keep the loop. Keep the poll. On each tick, do not wake the main session; wake
a **judge** — a fast, cheap, typed decision — that reads the new evidence and
answers one question: does the owner need to act now? Only a yes fires the
main session. The judge is a decision point on the shipped `decisions` seam
(`src/kiro_crew/decisions/`, PR #11490 and successors), the same seam Joe used
for sampled skill selection (`skills.select`), risky-tool flags and model-tier
routing. Jev (TypeSafe's System One model: state + typed questions in,
calibrated probabilities out, ~100 ms, no strings, no hallucinated shapes) is
the intended judge. Where Jev is not consented, the same interface is served by
a text-only small model behind a strict parser, so the mechanism is one and the
provider is a setting.

Kahneman's split is the design: the judge is System One (a gut check in
milliseconds), the main session is System Two (slow, deliberate, expensive).
The judge never acts; it only decides whether System Two is needed.

## 3. Design

```
autonudge tick ─▶ evidence collectors ─▶ state (bounded, scrubbed)
                                             │
                                             ▼
                              decide("nudge.wake", state, questions)
                              Jev (consented)  |  LLM lane (kirocrew-lite)
                                             │
                     ┌───────────────────────┴──────────────────────┐
                     ▼                                              ▼
                 QUIET: re-arm,                               WAKE: fire the
                 no turn, notice                               main session
                                                                    ▲
        provider failure / invalid answer / timeout ─▶ FALLBACK ─────┘
```

TERMINAL is not a judge verdict. Only a typed probe (a merged or closed pull
request, a work ledger with every item closed) ends a loop; the judge can never
stop one, so no prose can buy permanent silence.

### 3.1 The decision point `nudge.wake`

State (one object, ≤ 8 000 chars after scrubbing, oldest evidence dropped first):

- `loop`: the owner's instruction, truncated to 1 500 chars, and the
  `wake_when` / `quiet_when` text of the brief in force — the built-in one, or
  the `judge` spec's where the loop arms one.
- `since_last_tick`: a list of evidence items, each `{source, kind, age_s, text}`
  where `source` names the collector and the target (`session:chat-1751`,
  `pr:kirodotdev/KiroCrew#12735`, `work-ledger:it_42`), `kind` is a closed set
  (`transcript_tail`, `pr_checks`, `pr_comment`, `ledger_event`, `probe`),
  and `text` is bounded per item (1 000 chars).
- `last_verdict`: the previous tick's answer and fingerprint, so a judge can see
  it already passed on this evidence once.

Questions, asked in parallel, each atomic. The shipped seam speaks Jev's
`choice` type only (`decisions/types.py`; `_to_wire` refuses anything else), so
every question is a Choice; widening the wire to `noul`/`score` is a later PR.
That widening is PR D's: the seam must gain `noul` and `score` wire types before
a question here can ask for a bare probability or a bounded number instead of
encoding one as a Choice.

| id | type | instructions | options |
|---|---|---|---|
| `needs_owner` | choice | Does the new evidence require the owning session to act now? | `wake`: the loop's `wake_when` text; `quiet`: its `quiet_when` text |
| `outcome` | choice | What state is the watched work in? | `nothing_new`, `progress_only`, `needs_action`, `needs_human`, `finished`, `broken` |
| `urgency` | choice | How urgent is any action? | `none`, `next_tick_is_fine`, `now` |

The probability of `wake` plays the yes/no role below.

Mapping, in code, not in the model:

- `finished` or `broken` with `confidence ≥ 0.6` → **WAKE** with the verdict
  attached, so the woken session can report and decide to stop; the judge itself
  never ends the loop.
- `P(wake) ≥ 0.5` or `outcome ∈ {needs_action, needs_human}` → **WAKE**.
- otherwise → **QUIET**: re-arm, no turn.
- Low confidence (`< 0.4` on `outcome`) → WAKE. A judge that is unsure hands
  the call to System Two; it never guesses quiet.
- Any provider failure, timeout, or answer that fails validation → **FALLBACK**:
  fire, exactly as the ungated timer would. The judge can only remove turns
  that were safe to remove; it can never silence a loop.
- Quiet-streak floor: after N consecutive QUIET verdicts the tick fires anyway,
  so a miscalibrated judge cannot starve a loop forever. N defaults to the
  shipped `_MAX_QUIET_STREAK` (10 today), the same floor the PR probe already
  has, and is configurable per point.

Thresholds are constants in one module with tests, tuned from the JSONL log,
not prompt text.

### 3.2 Evidence collectors

The tick runs in the gateway (in `AutoNudgeService`, same thread the PR probe
uses), so collectors read in-process state; no tool call, no model.

- `pr`: for each PR URL in the message or `judge.targets`, the existing
  `PrWatchProbe` observation plus new review/issue comment bodies since the
  last tick (bounded).
- `session`: for each `chat-*` key in `judge.targets`, the transcript rows
  appended since the last tick, assistant and tool rows only, last status line
  first. Creator-only: the same check `session_read_message` applies. A target
  the owner may not read is dropped and noted, never fetched.
- `work-ledger`: when the owning session is a conductor with a work ledger,
  the new events per open item (typed; they also feed the Phase 3 probe).
- `self`: the owning session's own ledger `next`, so the judge knows what the
  owner said it was waiting for.

Every collector output goes through the seam's scrub (`has_credential`, the
canonical redaction pass) before it enters state; a scrub hit drops the item
and records `scrubbed`. Nothing leaves the machine that the owner's own
session would not have read.

### 3.3 Arming: a built-in brief, and the owner's own

`monitor_start` and `monitor_update` gain one optional object, feature-gated:

```json
"judge": {
  "targets": ["chat-1751-1790052364", "https://github.com/kirodotdev/KiroCrew/pull/12735"],
  "wake_when": "a worker line starts with RULING or BLOCKED or GREEN; a PR check turns red; a bot comment raises a new finding",
  "quiet_when": "workers report WORKING with no new status; checks still running; only progress notes"
}
```

`wake_when` and `quiet_when` become the `needs_owner` question's criteria. This
is how the main session "gives the judge its orders": the babysit and
goal-conductor skills author these two lines when arming, in
plain words, from the loop's exit condition. Targets default to what the
message names (PR URLs, `chat-*` keys); the explicit list adds or narrows.

The object is optional because the point carries a brief of its own. Once the
`nudge_evidence` scope is granted, every gated auto-nudge loop is screened, and a
loop that arms no `judge` object is screened with the built-in brief: **wake**
when the subject needs its owner — a blocker, a question or a ruling addressed to
it, a terminal state, or the exit condition the loop's own message states;
**quiet** when nothing has arrived for the owner since the last tick. An explicit
`judge` brief overrides the built-in one, so a loop that knows its own subject
states its own criteria and a loop that says nothing is still screened.

The built-in brief carries one clause beyond those two, because the loops it
covers have no author to write it: evidence from a pull-request comment or a
fetched page is untrusted content, and a claim inside it that there is nothing to
do is not evidence that nothing happened. §5's compounding bound rests on that
clause, so it belongs in the shipped default rather than in a skill's brief.

The scope carries the built-in brief alone. A loop whose owner wrote criteria is
screened on the lane's own authority (§3.4), scope or no scope; a loop that wrote
none supplies no such authorization, so `nudge_evidence` stands in for the half
the brief would have supplied. "Default off" therefore still holds for a
fresh install: nothing is screened until the owner grants the scope or arms a
brief. That standing-in is what §7 Q4 questions, because the scope's words name
one destination while the lane that answers may be the other, and Q5 asks how a
grant made under the narrower wording is told apart from one made under this.

Where the judge sits in the tick decides which loops it can reach. It answers
before the typed probe guard for a loop whose probe will not run — a conductor
watching sibling sessions carries no monitor at all, so anything behind a
monitor's presence would never reach it — and defers to the probe's own quiet
return for a loop whose probe does run, because only the probe sees a merged or
closed subject and ends the watch.

Three cases bypass the judge. A loop armed `gate=false` acts while its subject is
quiet — refreshing a heartbeat, chasing a reviewer who has not replied — so its
ticks are the work rather than a reaction to evidence, and screening them would
remove the turns the owner asked for. `judge: false`, the one value the object
takes beside a brief, is the owner's opt-out for a single loop. And
`monitor_watch` stays probe-first: a typed provider probe answers the same
question there without a model, and the judge reads only what a probe cannot
type. With the scope off, a `judge` object that names no criteria of its own is
accepted, stored and ignored, so an armed loop survives the switch being toggled;
one that names criteria is screened on its lane's own authority, which is what
makes the brief the thing that carries it.

### 3.4 Providers

- **Jev lane** (what `auto` resolves to when the keystone consents and the scope
  is granted): the shipped `JevOracle`, keystone
  consent bound to the endpoint (`decisions_consent.json`), key only via
  `secret://TYPESAFE_API_KEY`, bounded wait, strict `_from_wire`. No change to
  the seam's trust model; `nudge.wake` is one more entry in `gate.POINTS`.
- **LLM lane** (`impl_llm.py`): the same `Answers` shape produced by a
  text-only, tool-less run of the bundled `kirocrew-lite` agent (`JUDGE_AGENT_NAME`)
  on whatever model that agent already resolves: `JUDGE_MODEL_DEFAULT` is `auto`,
  so the default inherits the session's own background-role model and pins no
  model class, and an operator who wants a specific one picks it on the card.
  It is prompted with the state and the
  questions and required to
  answer in one JSON object with exactly the question ids, each value a
  probability. The parser is as strict as `_from_wire`: any extra key, missing
  id, non-finite number or prose → invalid answer → FALLBACK. The LLM lane is
  what makes the interface real for people without a Jev key; it is slower
  (seconds) and costs a small model call, still far below a main-session turn.
  No judge-specific agent template ships: it would differ from `kirocrew-lite`
  only in the model, which the runner passes per call, and in a standing prompt
  that `render_prompt` already states more completely on every call.
- **Provider selection** is a per-point setting, `decisions.nudge_wake.provider
  ∈ {auto, jev, llm}` (`auto` = Jev when the keystone consents, else LLM),
  written through the config route like the `decisions.model_route.<tier>`
  pins. The keystone is consent to send state to the Jev endpoint, and its
  scopes are categories of that egress, so the Jev lane requires the main
  switch plus the `nudge_evidence` scope (§3.6). The LLM lane adds no
  destination (the model provider the owner's sessions already send every
  turn to) and no data class (the owner's own children's transcripts,
  creator-only), decides only quiet-or-fire, and is fail-open; it is
  authorised by `provider = llm` plus the `judge` spec the owner's session
  arms, with no keystone involvement. What authorises a loop that arms no spec is
  Q4: the built-in brief rests on `nudge_evidence` today, a scope whose words
  describe the Jev endpoint rather than this lane's destination.
  `gate.py`'s `_judge_authority` and `config/sections.py` both state
  the spec half as shipped rationale and predate the built-in brief, so each owes
  one sentence; they sit in files this document does not change.

### 3.5 Rendering

Every verdict is visible without a turn:

- a transcript notice on the owning session: `Wake judge · quiet (needs_owner
  0.08, outcome progress_only 0.91) · 2 new rows in chat-1751, checks pending
  on #12735` — one line, so a human reading the tab sees why nothing fired;
- the monitor popover shows the last verdict and the quiet streak;
- the decisions JSONL log records call metadata and the verdict (no state
  text), under `point=nudge.wake`, for calibration.

### 3.6 Where the switch lives: a row on the Decisions card

The Decisions (Jev) card in Settings → Developer → Feature Previews is a
list-and-detail over the gateway's own point registry (PR #12598): every entry
in `gate.POINTS` draws a row with a status chip, and a point that declares a
scope gets a consent switch on its detail panel, with no frontend edit. The
judge uses exactly that:

- `nudge.wake` is registered in `gate.POINTS` with a new scope
  `nudge_evidence` in `POINT_SCOPE_KEYS`. The switch text bounds what the grant
  buys, and the built-in brief makes that population wider than the words a
  reviewer of an opt-in feature would have read: not "the loops I arm" but every
  gated loop the owner runs. Two consequences follow that this document does not
  settle; §7 carries them as questions rather than answers. The switch names the
  Jev endpoint, yet a briefless loop with the scope granted and no endpoint
  consent is screened on the LLM lane, whose destination is the owner's own model
  provider (Q4). And a consent authorizes the words it was recorded against and
  nothing wider — the rule `gate.py` states for the `tool_args` scope — while the
  keystone records one boolean per scope and no wording, so nothing distinguishes
  a grant made under narrower text (Q5).
  For the Jev lane that scope switch is the on switch; there is
  no separate feature toggle. Scope off → the Jev lane is off and a `judge` spec
  naming no criteria is stored and ignored, so a tick with no brief of its own
  behaves exactly as today; a spec that names criteria still runs on the LLM
  lane, which this scope does not govern (§3.4).
- The row's detail panel carries the point's own settings: provider
  (auto / Jev / LLM), the LLM model (from the advertised model list, "keep the
  session's own model" by default), and the quiet-streak floor. All three write
  `decisions.nudge_wake.*` keys registered in the config route's editable set.
  Choosing LLM is what turns the LLM lane on (§3.4).
- Status is the server's verdict, as for every row, and for this point it is read
  from the lane the gate would actually pick rather than from the keystone alone.
  The row reports that lane as running where it can answer, and where the picked
  lane cannot run it says which grant is missing rather than a flat off. The
  vocabulary is the card's and this document does not restate it; what belongs
  here is the consequence: a machine that grants the scope without an endpoint
  consent is screening on the LLM lane, and its row says so rather than reading
  as switched off.

Default off, in the sense a fresh install has no scope granted and screens
nothing. Turning the scope off mid-loop returns a loop running the built-in brief
to plain timer behaviour on its next tick. It is not a kill switch for the judge
as a whole: a loop whose owner wrote criteria was never screened on that grant and
keeps running on its lane's own authority, so `judge: false` is what stops one
(§3.3).

## 4. Cost

| loop | today | with judge (Jev) | with judge (LLM) |
|---|---|---|---|
| conductor, 3 workers, 45 min, 48 h | 64 main turns | ~6 main turns + 64 judge calls (~$0.01) | ~6 main turns + 64 small-model calls |
| PR babysit with bot-comment reading, 5 min | 1 turn / tick | wake only on a new finding | same, seconds slower |
| judge provider down | — | identical to today (fallback fires) | identical to today |

## 5. Security

- Egress: the Jev lane is behind the existing keystone consent, endpoint-bound;
  the LLM lane reuses the session's own model provider. Both receive scrubbed,
  bounded state; the decisions scanners run first; redirects are refused.
- Authority: the judge decides only QUIET vs fire; it cannot inject text into
  a turn, choose a target, or write anything. A single wrong answer costs one
  delayed or one extra turn.
- Compounding: a QUIET answer sustained across ticks compounds, and once the
  scope is granted the loops exposed to it are every gated one, including those
  that arm no brief. The evidence sustaining a QUIET is untrusted content
  crossing a trust boundary (a
  PR comment, a fetched page). Worst case, an input crafted to read as
  "nothing to do" suppresses delivery for `quiet_streak_floor × interval_secs`:
  with the default floor (`_MAX_QUIET_STREAK`, 10) and a 45-minute interval,
  7.5 hours. That is the silence a gated loop already tolerates today when
  its subject does not change; what is new is that text can now buy it. The
  bound is enforced by the floor itself, and it is the judge's own counter rather
  than the probe's: a per-loop `judge_quiet_streak` counts every tick the judge
  answered QUIET, resets only on a delivered turn, and is compared against
  `decisions.nudge_wake.quiet_streak_floor` (clamped, never off). A per-loop
  counter is what makes the bound reach a loop carrying no monitor at all, which
  the monitor's own streak field could not. Owners watching public repositories
  should set a lower floor.
  Evidence is labelled per source in `state`, and the warning that a comment or a
  fetched page is untrusted is part of the built-in brief (§3.3), so it reaches
  every screened loop rather than only the ones whose owner wrote criteria. The
  babysit skill's brief states it again for the loops that write their own.
- Targets: `session` collection is creator-only; a `judge.targets` entry the
  owner may not read is dropped and logged, never fetched.
- Fail-open by construction: every error path is the ungated timer.
- Injection: evidence is data; it is placed in `state`, never in
  `instructions`. The question texts come only from the owner's `judge` spec
  and the point's fixed strings.

## 6. Alternatives considered

- **Event-driven wakes only** ([`rfc-conductor-work-ledger`](rfc-conductor-work-ledger.md)
  Phase 3, and the earlier draft of this RFC's sibling). Correct for typed
  facts; blind to prose evidence. The two compose: probe first, judge on what
  the probe cannot type.
- **[`rfc-token-efficient-monitors`](rfc-token-efficient-monitors.md)**
  (in-progress). That RFC's monitor controller is what adds "fingerprints,
  budgets, terminal outcomes, and completed-turn accounting", and it leaves the
  AutoNudge timer in place as "the durable scheduling primitive" rather than
  replacing it. Terminal outcomes are therefore its vocabulary and not the
  judge's, which is why the mapping in §3.1 gives the judge no terminal verdict.
  The judge adds one decision at the tick and changes none of that controller's
  typed verdicts; where a typed probe can answer, it answers alone.
- **[`rfc-consolidated-monitor`](rfc-consolidated-monitor.md)** (draft). It
  merges three streams, one of which is `autonudge.py`, and states "the target
  shape, what gets deleted, and the order". The judge is a field on an existing
  loop's record (`judge`) rather than a second loop, so consolidation has
  nothing extra to merge. One dependency does follow: that document's deletion
  list includes "irq's own delivered-cycle counting and quiet-streak floor",
  on the grounds that "the structured budgets subsume them". §5's compounding
  bound is enforced by a quiet-streak floor, so when that consolidation lands
  the bound has to be restated against whatever subsumes the floor.
- **Let the main session decide with a cheaper model tier.** Still a full turn
  with the whole context; the model-tier route (#12267) shows the seam can
  pick a tier, but the cost is the context, not the model.
- **Rules (regex on status prefixes).** That is the status quo for conductor
  tails (`GREEN:` / `BLOCKED:` prefixes) and it is exactly what the typed work
  ledger replaces; regex cannot read a review comment.
- **Always-on shadow arm first.** #11490 deliberately shipped no shadow arm; the
  fallback-fires design means the live arm is already safe to run, and the
  JSONL log gives the calibration curve without a second arm.

## 7. Open questions

1. Threshold defaults (0.5 / 0.6 / 0.4) and the streak floor: tune from the
   first two weeks of logs on this repository's own loops.
2. Should the LLM lane require its own consent row on the keystone? **Ruled: no**,
   for the reason in §3.4. `auto` resolves to the Jev lane only when the keystone
   consents and the `nudge_evidence` scope is granted, otherwise to the LLM lane;
   a pinned `jev` without the scope stores the brief and runs the plain timer.
   Revisit if the lane ever sends more than the session itself would.
3. Name. Mechanism: wake judge (decision point `nudge.wake`). The fallback lane
   runs the bundled `kirocrew-lite` agent rather than a judge-specific template.
   User-facing metaphor in the toggle copy: "a secretary who
   screens the interruptions".
4. Which switch authorizes the LLM lane for a briefless loop. The built-in brief
   rests on `nudge_evidence`, whose text describes the Jev endpoint, but a loop
   with the scope granted and no endpoint consent is screened on the LLM lane,
   which sends to the owner's own model provider instead. Either that switch's
   words cover both destinations, or the built-in brief is restricted to one lane.
   Restricting it to the LLM lane adds no destination and needs no re-consent, at
   the cost of the Jev lane never serving a loop whose owner wrote nothing;
   restricting it to Jev keeps the words honest and leaves installs without a key
   unscreened by default. Unresolved, and it decides code in PR D, not prose here.
5. How a widened consent is recognised. A consent authorizes the words it was
   recorded against and nothing wider, but the keystone records one boolean per
   scope and no wording or version, so an install that granted `nudge_evidence`
   under opt-in wording is indistinguishable from one that granted it under the
   wider population. Either the scope is versioned, or the wider population needs
   a scope key of its own, which is the mechanism the seam already uses to keep an
   already-consented install inert for a category it never reviewed.

## 8. Rollout

1. PR D — judge core: `decisions/points/nudge_wake.py`, `nudge.wake` in
   `gate.POINTS` with the `nudge_evidence` scope, collectors, the autonudge
   tick hook with fail-open mapping, `judge` on `monitor_start` /
   `monitor_update`, transcript notice, `decisions.nudge_wake.*` keys, tests.
   Jev lane only. The day-one path for a Jev key holder is to grant the scope and
   have their gated loops screened with the built-in brief, with a `judge` spec
   stating criteria of a loop's own. Blocked on Q4: which switch authorizes a
   briefless loop on each lane is undecided, and both this entry's scope text and
   its default follow whatever Q4 settles.
2. PR E — LLM lane: `decisions/impl_llm.py` running the bundled `kirocrew-lite`
   agent, the provider setting, and on the Decisions card (after #12598) the
   row label for `nudge.wake` plus the provider and LLM-model pickers in its
   detail panel. Depends on D's point name and on #12598's card shape.
3. PR F — skills: `babysit` and `goal-conductor` author `wake_when` /
   `quiet_when` when arming; after D merges.
4. This RFC lands as `docs/request-for-change/rfc-wake-judge.md`.
