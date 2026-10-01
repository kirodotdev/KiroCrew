---
title: Jev task executor — closed browser and desktop subtasks without an LLM in the loop
status: accepted
author: Ray Xu
created: 2026-09-28
last-audited: 2026-09-29
audited-at: df0ea7909c
doc-pr: 14839
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Jev task executor — closed browser and desktop subtasks without an LLM in the loop

- Feature preview; default off
- Related: [`rfc-wake-judge`](rfc-wake-judge.md) §3.1, which also needs the
  `noul` and `score` wire types this design adds.
- Prior art: [JevOnly](https://github.com/buluoray/JevOnly), a standalone harness
  that drives a browser and a desktop app with Jev as the only model.

Status: accepted on 2026-09-29, recorded by the maintainer approval of the PR
that sets this status. The acceptance answers §7 questions 1 and 2 as proposed:
the executor sits below the tenet 8 line as a trust-boundary component, and the
browser surface waits on its own driver design section (§3.6), with desktop as
the phase 1 surface. §7 records both decisions; question 3 stays open and gates
step 5, and question 4 is deferred with no rollout step. Nothing of this design
is on main. Code references were read at `699083f906` and re-read at `df0ea7909c`.

## 1. Problem

When the agent needs a web page or a desktop app, every step goes through the
chat model. It reads a snapshot, often several thousand tokens of element tree,
picks one ref, calls `browser` or `computer_click`, and reads the next snapshot.
A ten-step errand such as "open the second search result and note its price and
rating" costs ten full model turns. Most of those turns involve no reasoning.
The model is picking one line out of a list the harness already wrote.

Kiro Crew already has a cheap model for that kind of question. The decisions
seam (`src/kiro_crew/decisions/`, spec
[`decisions.md`](../system-specs/modules/decisions.md)) sends Jev a closed menu
and gets back one pick with a calibrated probability. Seven points use it today
(`skills.select`, `message.steer`, `model.route`, `memory.recall`, `tool.risk`,
`compaction.keep`, `nudge.wake`), and none of them touches the browser or
Computer Use. Each point answers one decision inside a turn. None of them runs a
loop of twenty steps that observes, acts, checks the result and undoes a wrong
move.

JevOnly shows the loop works on its own. On its regression set (Google Flights,
Wikipedia, Hacker News, GitHub, two online-store tasks), seven of eight tasks
pass on every run; the eighth is stopped by a bot wall. A typical task takes
about 20 steps, 80 Jev questions and 30 seconds, and costs about one cent. Jev
never writes text: every word the loop types comes from the task, a given fact
or the page.

## 2. Idea

Give the chat agent one tool per surface that hands a closed subtask to a Jev
loop and returns what the loop read:

```
jev_browser_task(goal, start_url, facts?, max_steps?)  -> {success, answer, values, stopped}
jev_computer_task(goal, app, facts?, max_steps?)       -> {success, answer, values, stopped}
```

The chat model still plans, decides when a subtask is closed enough to hand
over, and reads the result. The loop does the clicking. When Jev cannot finish,
the loop says so (`dead_end`, `max_steps`, `budget`, `needs_human`,
`caller_gone` when the calling session ended or the request was aborted, or
`not_permitted` when a prerequisite is missing or the gateway refused to ask
Jev) and the agent carries on with its ordinary tools.

The executor is a **core module** behind a Feature Preview, off by default. It
runs inside the gateway process, asks Jev only through the existing decisions
gate, and drives the desktop only through the existing Computer Use dispatch.
It works only when the owner has set up all of the following, each explicitly:

| Prerequisite | Where the owner sets it | Surface |
|---|---|---|
| A Jev API key in the secrets vault as `TYPESAFE_API_KEY`, and decisions consent bound to the configured endpoint | Settings › Secrets, and the Decisions card | both |
| The Jev task executor preview, per surface: **Browser** and **Desktop apps** | a new row on the Decisions card | each separately |
| Computer Use turned on | Settings › Computer Use (`computer_use.json`) | desktop |
| Browsing not denied by the governance ceiling (`capabilities.browse`) | the fleet policy and profile | browser |
| The session sampled in, i.e. `decisions.bucket` at 100 or covering it | `config.json` (`decisions.bucket`) | both |

A missing prerequisite never degrades into a partial run. When Computer Use or
the surface's scope is off, the tool is not listed at all, as `computer_*`
tools are not. When a prerequisite is withdrawn after the tool was listed, the
call returns `not_permitted` and names the one thing to set up.

## 3. Design

### 3.1 Placement: why this is core, not an app

Tenet 8 (`TENETS.md`) puts anything that renders or interprets in an app and
keeps the trust boundary in core. This RFC asks maintainers to place the
executor below that line, which §7 question 1 records as decided. The argument is that the
executor is itself a trust-boundary component, not a surface:

- **It decides what leaves the machine.** Every step chooses which page lines,
  control labels and field values go to a third-party endpoint. That is the
  same job the decisions gate does for its seven points, and it needs the gate's
  consent, endpoint binding, vault key and scrub on every question.
- **It acts on the owner's desktop.** It drives Computer Use through the same
  dispatch as the agent's own `computer_*` calls, so the keystone
  `computer_use.json` enable and `policy.check_app`'s app denylist bound it the
  same way. Computer Use carries no governance ceiling (`computer_use/gate.py`
  only audits), so the session key is the audit identity, not a permit.
- **As an app it would need two new crossings into core.** An app backend
  would need an app-token route to ask Jev and another into Computer Use
  dispatch, each with its own caller gate, task lease and ceiling, because no
  caller-proof seam exists between an app's MCP tool call and its backend's
  HTTP request. In-process execution needs neither: the caller is the session
  that called the tool, proved the way every other internal route proves it.
- **It renders nothing.** It adds no page and no panel. Its only UI is a row on
  the existing Decisions card and the existing tool card in chat.

The cost is release coupling: the loop's heuristics can no longer change
without a Kiro Crew release. Core takes only the generic loop. Site-specific
rules stay out, and JevOnly stays the place to experiment before a rule is
generic enough to land.

The module is `src/kiro_crew/jev_executor/`. It imports `decisions.gate`,
`computer_use.tools` and the browser runtime, and nothing imports it on the
gateway boot path (`no-new-work-on-gateway-boot-path`): the route handlers
import it inside the handler, as `dashboard/handlers/decisions.py` already does
for the seam.

### 3.2 Wire types

The loop asks `noul` questions (is this page off the path, did the last move
work, is this line the value asked for) and `score` questions (QA thresholds)
as well as `Choice`. `decisions/types.py` declares `Choice` alone, and
`impl_jev._to_wire` refuses anything else. On the base commit `rfc-wake-judge`
§3.1 assigned the widening to its own PR D, whose rollout entry does not list
it; this commit re-points §3.1 here, so this RFC owns the widening. It adds `Noul` and `Score` dataclasses, their wire mapping
in `impl_jev.py` and the LLM lane, and tests for a malformed or partial answer.
The fail-closed domain check is `gate._answers_are_valid`, which today requires
a string value drawn from `question.options` and runs outside `decide`'s
`try`. Each new type therefore gets its own branch there: a `Noul` answer is a
probability in [0, 1] and a `Score` answer a number inside its declared range,
and a type with no `options` never reaches the `Choice` branch. A malformed
answer of either type yields `None`, never an exception. The wake judge can use
them as soon as they land.

### 3.3 Enabling: the preview row is the consent

Browser page text and desktop control text are new kinds of egress. Today the
seam sends message excerpts and skill descriptions under the base consent, and
four scopes widen it (`decisions/consent.py`): `tool_args` sends tool
arguments, `compaction` a whole slot transcript with every tool input in it,
`memory_text` the text of recalled memories, and `nudge_evidence` a watched
session's new evidence. Every one of those is text the agent itself produced
or stored. None of them has ever sent the text of a page or the value of a field in a
desktop app, and a desktop field can be more sensitive than a public page. So
the keystone `decisions_consent.json` gains two scopes, `browser_text` and
`desktop_text`, placed exactly the way `tool_args`, `memory_text` and
`nudge_evidence` are placed. Each has a strict reader (only a literal `true`
consents, and absent means no), its own `KEEP_*` sentinel in the locked
read-modify-write, a `POINT_SCOPE_KEYS` entry, an SEL verb and a switch on the
Decisions card. A consent recorded before the scopes existed stays inert for
both points, and turning decisions consent off clears both.

These two switches are the Feature Preview. There is no separate preview flag:
a localStorage flag is presentation only, and a `config.json` flag is writable
by an auto-approved agent shell, so neither could be the thing that lets page
text leave the machine. The card draws the row only while decisions consent is
on, because both scopes need the vault key and the bound endpoint beneath them.

`browser_text` stays inert until the browser driver section (§3.6) is
accepted. Each scope is a standing grant that lasts until the owner turns it
off. Each scope covers everything its surface's tasks send, not only the
observed text: the task goal, the target (the app name, or for the browser the
start URL), the caller's `facts`, and the visible text, control labels and field
values the loop reads. `tool_args` is neither needed nor consulted, so a
surface scope granted with `tool_args` off still covers the task's own
arguments, and no task argument leaves the machine without that scope. The
card says so plainly: while the switch is on, any task the agent hands to the
executor sends its goal, target and facts, and the page or desktop text it
reads, to the configured endpoint. The copy lives in the i18n catalog.
`browser.task` and
`computer.task` are decisions point names. They are not `computer_use.*`
governance scopes, and Computer Use stays ungoverned as AGENTS.md requires.

The two MCP tools are listed only in a session that starts while their scope is
granted, so an install that never opts in pays no tool-schema tokens. A tool
already listed checks every prerequisite again on each call.

### 3.4 Running a task

`jev_computer_task` lives in the `kirocrew-computer` MCP server, not in
`kirocrew-core`. `@kirocrew-core` is blanket-approved in `config/defaults.json`,
while `kirocrew-computer` is registered only when Computer Use is enabled and
is deliberately kept out of `allowedTools`, so a task call gets the same
per-call approval prompt a single `computer_click` does rather than one silent
approval for a whole budget of desktop actions. The browser tool's server is
set by its driver section (§3.6) under the same rule: never a blanket-approved
server. The tools are stateless, like every MCP tool. Each
call posts to `POST /api/jev-executor/{browser,desktop}/run`, a loopback route
that requires `internal_auth` exactly as `dashboard/handlers/computer_use.py`
does. Both run paths are listed in `server._STRICT_INTERNAL_API_PATHS`, since the
auth middleware sets `internal_auth` only for a listed path, and the handler
re-asserts it; so a dashboard cookie or an app token cannot reach it and cannot choose
the session key. The tool resolves its own session key with the strict resolver
(`mcp_core._resolve_session_key_strict`), the one that refuses rather than
walking up the process tree. It declares that key in `X-Session-Key` together
with `mcp_core._session_token_header()`, and the route is authorized by
`internal_auth` and the route's own attestation of that declared key through
`member_memory_auth.session_key_is_attested`, which accepts the Unix-socket
peer attestation or a verifying `X-Session-Token` and so holds on the TCP
fallback a Windows host uses. The
route refuses an undeclared or unverified key and never trusts a key named in
the body alone, where `dashboard/handlers/computer_use.py` uses the body key
only as the audit and namespace identity. The
lenient ladder is not used, because its proc-ancestor walk resolves a
sub-agent to its parent's slot. When the strict resolver returns no key, the
tool does not call the route and the task ends `not_permitted` with the reason
`session_unresolved`; it never runs a task against a guessed or parent
identity. The route then maps the path to the
point: `/browser/run` asks as `browser.task` and
`/desktop/run` as `computer.task`. The body cannot name an arbitrary point.

The route checks the prerequisites in the order the table in §2 lists them,
then the governance ceiling on browsing: a `/browser/run` call is refused when
`capabilities.browse` is denied for the calling session. The executor calls
`vet_and_audit` itself under its own tool name, `jev_browser_task`, rather than
reusing `mcp_core._vet_browse_governance`, because that helper fails open on an
evaluation error and audits every call as `browser`. Here an evaluation error
refuses: an unattended loop that sends page text off the machine must not run
on a policy it could not read. It refuses with `not_permitted` and the first missing one.
It then runs the loop
in the gateway under a per-task budget: an absolute wall-clock limit, a maximum
number of Jev questions, and one in-flight Jev request at a time. The gateway
sets the limits and the caller's `max_steps` only lowers them. A per-session
and a gateway-wide ceiling on concurrent tasks sit above the per-task budget.
The wall-clock limit is checked before each dispatch starts, never by
cancelling one: a worker-thread dispatch cannot be stopped mid-action, so the
task waits for an in-flight dispatch to return and only then reports its stop.
No action is issued after the task has decided to stop.
The tool call blocks until the task ends, within the MCP call timeout. The
task's lifetime is tied to its caller by two bounds that do not depend on a
cancellation reaching the gateway. The wall-clock limit is always set below
the MCP call timeout, so a task cannot outlive the call that started it. And
the run request is the task's lease: the shim closes its HTTP connection when
its call is cancelled or its session ends, and the route watches that
connection, so the next pre-dispatch check after it closes stops the task with
`caller_gone`. No action is issued once either bound has passed. The
step driver is async and stays on the event loop, so it can await
`decisions.gate.decide` directly. Only the blocking legs of a step leave it:
the Computer Use dispatch (its post-action settle sleep and accessibility
walk) runs on the module's own executor pool, never on the shared
`subprocess_executor()` pool that PTY teardown and `kiro-cli` spawns rely on,
so the chat turn and the gateway's liveness heartbeat keep running while a task
does.

Every step asks Jev through `decisions.gate` as one bounded batch, the way
`compaction.keep` already batches, so every question re-checks consent, the
endpoint binding, the surface's scope, sampling, the scrub and the fleet
capability. `decide` reports every refusal, provider error, timeout and
unreadable config as the same bare `None`. The executor does not pass that
ambiguity on. Before each batch it awaits `gate.is_enabled`, which runs the same
refusals without asking Jev, and then `gate.scrub_reason` on the batch it is
about to send, since `is_enabled` does not run the scrub. `is_enabled` reads
the keystone, so it runs through `asyncio.to_thread`, as the chat runner
already does for the preview check. `scrub_reason` does no IO but runs regex
scans that hold the GIL, so a thread hop does not relieve it; instead the state
in a batch is capped in bytes before it reaches the scan (the cap is open
question 3). A batch either check refuses ends the task with `not_permitted`.
A scrub refusal carries the category `scrub_reason` returns:
`scrubbed:credential` or `scrubbed:exfiltration-url` when the page carried
such a string, `scrubbed:provider-model` when the configured `provider.model`
is not a model id, and `scrubbed:scan-failed` when a scanner raised. An
`is_enabled` refusal stays unnamed, because it returns a bare `bool` that also
covers an unreadable config. A scrub refusal writes exactly one decisions-log
row with that category as its error, as `decide` does when it scrubs, so a
seam that refuses every time is never read as one that is not firing.
A `None` after both pre-checks pass is a provider failure and ends the task
with `dead_end`. Sampling belongs here: an operator `decisions.bucket`
below 100 leaves some sessions permanently unsampled, so §2's table lists it.
The result is that "the scope is not granted" never reads as "Jev could not do
it".

Decision logging stays best-effort, as `decisions.md` §5 specifies. What the
owner can count on is one task-summary row per task: task id, surface, steps,
questions, stop reason and elapsed time. It carries no page or field text.
Phase 1 persists no per-step trace. Captured page and desktop text lives only
in the task's memory and is dropped when the task ends, so no stored copy can
outlive the sandbox setting that was in force when it was captured. A
persisted trace is open question 4.

The shared decisions log is agent-readable by design, so nothing captured from a
page or a desktop reaches it either. The executor's questions offer opaque
option keys (a candidate's index in the step's snapshot), never the captured
label or value, so the answer values `decide` logs identify a position, not
text. `decisions.outcomes` is not used, since it holds one receipt per point
per turn.

### 3.5 Computer Use: one executor, two output adapters

`computer_use/tools.py` has one ordered chokepoint, `_dispatch`, reached through
`dispatch_tool` and its async wrapper `dispatch`. Its order is a security
property the module docstring states: it validates the call, runs the enable
check before anything else (a disabled feature must not even enumerate the
operator's windows), then resolves the target's OS identity, runs the audit
gate, the target policy and the SEL audit, and enters `_run`. `_run` checks the
window fingerprint for drift (`svc.verify_fingerprint`), refuses a secure input
target (`policy.check_input_target`), then performs the action, settles,
snapshots again and renders the result. The split below keeps every one of
those steps, in that order, inside the shared executor. Rendering carries a
security role, not only a display one. `render._render_record` drops a secure
element's title, value, actions, traits and frame. The raw records keep some of
those: on macOS a secure `ElementRec` blanks its value but keeps its title and
frame (`snapshot_macos.py`). Handing raw `Snapshot` or `ElementRec` records to
the executor would therefore widen what reaches the Jev wire.

The change instead splits `_dispatch`, with the `_run` it enters, into one
internal executor returning a private `DispatchResult` and two adapters over
it. Every in-line refusal site in `_dispatch` (the `_static_refusal` and
`_refusal` returns) returns a typed refusal that both adapters render. `dispatch_tool` renders text as it
does today. `dispatch_structured` returns a new export type, never the raw
records, and applies the same output boundary the text path applies:

- A secure element keeps only its index, role, subrole and `secure: true`. Its
  title, value, actions, traits, focus and frame are dropped.
- Selected text, screenshot bytes and screenshot paths are omitted.
- A non-secure element exports one label, chosen and clipped exactly as
  `render._render_record` chooses it (`title or value`, cut to `text_limit`),
  so a field whose title and value differ never exports its value where the
  text path hides it.
- Every exported string passes the canonical redactor and the
  exfiltration-URL pass.
- Every refusal and typed exception goes through the same `_refusal` shaping
  and `policy.redact_result` the text adapter applies in `dispatch_tool`. A
  drift refusal such as `StaleIndex`, which carries `render.describe_record`
  text for the cached and the fresh element, is never exported raw.

Tests seed secure records with a non-empty title, value and frame and assert
none of those bytes appear in either output. They seed a non-secure record
with distinct title and value and assert the value is absent from both, and
credential-shaped text on a non-secure element and assert both outputs redact
it. Each mutation is run: removing the secure branch or the label rule from an
adapter must fail its test.

`dispatch_structured` is a Python entry point with no HTTP route of its own.
The executor calls it in-process with the session key the run route resolved,
so the enable check, target policy and SEL audit apply with the same identity
as the agent's own `computer_*` calls. Nothing outside the gateway process can
reach it.

Desktop undo is weaker than browser undo, and the contract says so. The loop
may press Escape to close a menu it opened. It never restores a value it set,
because the exported label is clipped and may be the title rather than the
value, so the prior value never reaches it intact. Anything else, a set value
included, reports `restored=False`, and the loop stops with `needs_human` instead of
claiming the step was reversed. `Cmd+Z` is never used, because it can undo the
user's own work.

### 3.6 Browser: required properties, driver deferred

Desktop is the phase 1 surface. The browser surface is proposed and gated the
same way (the `browser_text` scope, the preview switch, `capabilities.browse`),
but it does not ship until a separate design section for its driver is
accepted. That section must specify a gateway-owned browser driver and show
how it holds these properties; this RFC does not choose the mechanism.

- **Isolated state.** An empty, ephemeral profile per task: no user cookies,
  local files, uploads, downloads, clipboard or extension APIs, no popups, and
  closed on every stop reason.
- **Network-pinned.** Every request, including links and redirects, reaches
  only public HTTP(S) addresses. The connection is bound to the address that
  was vetted, with no second resolution, so a rebinding name cannot reach
  loopback, private, link-local or cloud metadata addresses.
- **Nothing in process arguments.** No part of a URL and no typed text ever
  reaches a process's argv, where other accounts on the host can read it.
- **Read-only without approval.** The RFC makes no read-only claim for any
  request method. Any step that could change state, including following a
  link, stops with `needs_human` unless a gateway-owned approval record
  authorizes it, and no such record exists yet.
- **Governed.** The browse ceiling is evaluated by the executor itself under its
  own tool name, and an evaluation error refuses.

Until that section is accepted `browser_text` stays inert: granting it enables
nothing, and `jev_browser_task` is not listed.

## 4. Cost

JevOnly, measured on 2026-09-21 from a tree whose content was later merged as
commit `9746743`: about $0.014 per browser task (20 steps, 80 questions) and a
0.15 s median per Jev request. On a four-case QA suite run twice, 8 of 8 passed
in 70 seconds for about seven cents. The chat model is called once to delegate
and once to read the result, instead of once per step, so the saving grows with
task length. For a two-step task it is zero, and the tool description says not
to hand those over.

These browser figures are indicative only: they were measured by JevOnly's
own driver, not the gateway-owned driver §3.6 defers, and the driver section
carries its own measurement. The implementation PR for the desktop surface
checks its own task list, repetitions, failures and cost accounting into a
result manifest.

## 5. Security

Each property names where it is enforced. All of them are in core.

- **Nothing runs until the owner sets it up.** The vault key, decisions
  consent, the per-surface scope and, for the desktop, the Computer Use
  keystone are each owner-only writes that an agent cannot make.
- **No browser egress in phase 1.** The browser surface is inert until its
  driver section is accepted, and that section must prove the §3.6 properties
  (isolated state, network-pinned, nothing in argv, no state change without an
  approval record).
- **Structured desktop output is no wider than rendered output.** One sanitized
  export type, pinned by mutation-tested secure-field, label and redaction
  tests.
- **Every task call is approved like a desktop action.** `jev_computer_task`
  lives on `kirocrew-computer`, which is never blanket-approved, not on
  `kirocrew-core`, which is.
- **Only the calling session can start a task.** The run route requires
  `internal_auth` and a declared session key the route attests with
  `member_memory_auth.session_key_is_attested` on every transport; the tool
  resolves that key through the strict
  resolver only, and ends the task when it resolves none.
- **Captured text is not agent-readable.** Nothing captured is persisted:
  the task-summary row carries no page or field text, and answer values in the
  agent-readable decisions log are opaque option keys, never captured text.
- **Egress is scoped per surface and bound to an endpoint.** Both scopes live on
  the keystone, stay absent until granted, and are cleared on revoke. Questions
  go through the same scrub and governance ceiling as every other point.
- **The Jev key never leaves the gateway.** It resolves from the vault only
  inside `impl_jev`, as it does for every point.
- **Third-party retention.** TypeSafe publishes no data-retention policy. The
  consent copy does not say or imply that page content is kept only briefly.

## 6. Alternatives considered

- **An external app with a small core surface.** Rejected for this design. The
  app would still need every owner step in §2, plus installing and enabling it,
  and core would still need an app-token route into the decisions gate and
  another into Computer Use dispatch, each needing a caller gate, a task lease
  and an app-wide ceiling. That is more trust-boundary surface than running the
  loop in-process.
- **A decision point per step inside the agent turn.** Each `browser` call would
  ask Jev to pre-rank the snapshot for the model. That shrinks the tokens per
  step but keeps one model turn per step. In an offline test on three research
  questions, Jev's top 40 of 255 to 858 passages held 93% to 100% of the
  hand-labelled answers, which makes a snapshot pre-filter a good later point.
  It does not replace the executor.
- **A separate preview flag in `config.json` or localStorage.** Rejected. The
  first is agent-writable and the second is presentation only; the keystone
  scopes are the only switch an agent cannot flip.
- **Screenshots and OCR for the desktop.** Rejected. Pixels would leave the
  governed layer, and Jev does not read images.

## 7. Questions, and how they were answered

1. Do maintainers accept the executor below the tenet 8 line, as a
   trust-boundary component? (`no-new-builtin-apps` covers only
   `src/kiro_crew/apps/builtins/**`, which this module does not touch, so no
   exemption is requested.)

   **Decided 2026-09-29 by buluoray: accepted as proposed. The executor is a
   trust-boundary component in core.**
2. Do maintainers accept deferring the browser surface to its own driver design
   section (§3.6), with desktop as the phase 1 surface?

   **Decided 2026-09-29 by buluoray: accepted as proposed. Desktop is the phase 1
   surface; the browser waits on the §3.6 driver design section.**
3. — open, gates step 5. What are the per-task defaults, the per-batch state
   byte cap and the per-session and gateway-wide ceilings, and do they belong
   on the keystone or in config?
4. — deferred, no rollout step. Is a persisted per-step trace wanted at all? If
   so, it needs its own design for a store that stays unreadable when an
   operator later turns the sandbox off, which this RFC does not attempt.

## 8. Rollout

1. **This document**, as a proposal. Before any executor implementation PR
   (steps 3 to 7) opens, a maintainer accepts it and a separate base commit changes `status` to
   `accepted`. An implementation PR points at that record and never accepts the
   RFC in its own diff.
2. **Wire types.** `Noul` and `Score` in `decisions/types.py`, Jev and LLM wire
   mapping, a per-type branch in `gate._answers_are_valid`, malformed and
   partial-answer tests (including one proving a malformed answer returns
   `None` rather than raising), and `decisions.md` updated. This step is
   surface-agnostic and does not wait on this RFC's acceptance: it may land on
   `rfc-wake-judge`'s need alone, citing that document, and it stays valid if
   the executor is rejected. Only steps 3 to 7 are gated on step 1.
3. **Consent and the preview row.** The two scopes, the two point names, the
   Decisions card row with its two switches, and the SEL verbs. Updates
   `decisions.md`, every locale catalog and the i18n tests.
4. **Structured Computer Use.** The executor split and `dispatch_structured`
   with its export type. Updates `computer-use.md`. Mutation tests cover
   secure-field removal, the label rule, redaction and refusal shaping on both
   adapters.
5. **The executor and the desktop tool.** The per-task budget and ceilings
   wait on open question 3.
   `src/kiro_crew/jev_executor/`, the run routes, `jev_computer_task`, the
   per-task budget and ceilings, with a new system spec
   under `docs/system-specs/modules/`. Tests cover each missing prerequisite
   returning `not_permitted`, `internal_auth` and caller identity on the run
   route, an undeclared or unverified session key being refused, both run paths being listed in `_STRICT_INTERNAL_API_PATHS`, the
   blocking legs running off the event loop, the tool being absent without the
   scope, one decisions-log row per scrub refusal, the tool being registered
   on `kirocrew-computer` and never matched by
   the default `allowedTools`, the budget stops, the wall-clock limit being
   below the MCP call timeout, and `caller_gone` stopping the task at the next
   dispatch once the run request's connection closes.
6. **Browser driver design section.** A new section of this document (or a
   follow-up RFC) that specifies the gateway-owned driver and proves the §3.6
   properties; `jev_browser_task` lands only after it is accepted.
7. **Signed-in Browser panel.** Blocked on the gateway-owned approval record.
   Any action in the user's own signed-in panel waits on that record.
