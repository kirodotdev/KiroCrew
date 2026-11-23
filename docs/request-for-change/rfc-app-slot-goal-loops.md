---
title: An app with permissions.chatSlots may start, change, fire and stop goal loops on its own chat slots over /api/autonudge
status: draft
author: tilakputta
created: 2026-11-16
last-audited: 2026-11-23
audited-at: b6e2c8a6
doc-pr: null
implementation-prs: []
tracking-issues: [37388]
supersedes: []
superseded-by: []
---

# RFC: Goal loops on an app's own chat slots over HTTP

Status: draft. Nothing is built and this document changes no code. It answers
[#37388](https://github.com/kirodotdev/KiroCrew/issues/37388), which a
maintainer labelled `tier:T3` and parked as a design-level change, pointing at
this process. Acceptance is requested from a maintainer, who records it on this
Status line. Every code reference below was read at `b6e2c8a6`.

It lands on its own because it widens four routes that are owner-only today to
a second kind of caller, an app token. That changes who may arm work that runs
in a chat session, which is a security decision before it is a code change.

## 1. Summary

An app whose installed manifest declares `permissions.chatSlots: true`, and
which the owner has enabled, may call `POST /api/autonudge`,
`PATCH /api/autonudge/<id>`, `DELETE /api/autonudge/<id>` and
`POST /api/autonudge/<id>/fire` with its app token, but only for loops on chat
slots it owns. Every other caller and every other slot keeps today's owner gate.
The arm goes through the same chokepoint the in-gateway `ChatSlotSDK.arm_nudge`
already uses, so the HTTP surface gains nothing the in-gateway surface lacks.

## 2. Problem

### 2.1 What an app sees

An external app (a backend process that calls the gateway with its app token)
can already create its own chat slot and send turns to it. It cannot keep a goal
loop on that slot. Every write route answers `403 dashboard_owner_required`:

| Route | Handler in `src/kiro_crew/dashboard/handlers/autonudge.py` | Gate |
|---|---|---|
| `POST /api/autonudge` | `api_autonudge_start` | `_require_monitor_owner(request, "autonudge_start")` |
| `PATCH /api/autonudge/<id>` | `api_autonudge_update` | `_require_monitor_owner(request, "autonudge_update")` |
| `DELETE /api/autonudge/<id>` | `api_autonudge_delete` | `_require_monitor_owner`, on both the structured-monitor and the legacy branch |
| `POST /api/autonudge/<id>/fire` | `api_autonudge_fire` | `_require_monitor_owner(request, "autonudge_fire")` |

`_require_monitor_owner` passes only when
`is_owner_dashboard_request` (`src/kiro_crew/dashboard/handlers/source_providers.py`)
is true, and that is false for any request whose `request["app"]` is non-empty.
The two read routes, `GET /api/autonudge` (`api_autonudge_list`) and
`GET /api/autonudge/<id>` (`api_autonudge_get`), have no owner gate and are not
changed here.

### 2.2 Why the in-gateway path does not cover it

An app that runs inside the gateway already has this. `ChatSlotSDK.arm_nudge`
(`src/kiro_crew/apps/chat_slot_sdk.py`) checks that the slot is the app's own
live slot (`ChatSlotSDK._require_live`) and calls `authorize_and_add_nudge`
(`src/kiro_crew/autonudge_authz.py`) with `source="app:<name>"`. The SDK is
built only when the manifest declares `chatSlots` as the literal `true`
(`src/kiro_crew/apps/context.py`), and the manifest text for `chatSlots`
already promises "AutoNudge loops on those slots"
(the `chatSlots` field of the app permissions model in `src/kiro_crew/apps/manifest_sections.py`).

An app whose worker is a separate backend process has no `ctx`. Today it can arm
a loop only by having a dashboard page open that arms it with the owner's
session. One task-board app that gives each card one worker chat and one goal
loop saw no loop start for about ten hours on a day nobody had its page open,
then 47 start within an hour of the page opening.

Cron jobs are not a substitute. An app may run its own cron jobs, but a cron
run's chat is its own session, pruned after the run unless `persistent_session`
is set, and only the owner may give it approval mode `auto`
(`_refuse_app_auto_approval`, `src/kiro_crew/dashboard/handlers/cron_jobs.py`).
The card's chat and the worker's chat become two different things.

## 3. Goals and non-goals

Goals:

- An app holding `chatSlots` arms, updates, fires and stops goal loops on its own
  slots over HTTP, with no dashboard page open.
- The HTTP surface enforces exactly the rules the in-gateway `arm_nudge` does.
- Every decision, allowed or denied, leaves one audit row naming the app.

Non-goals:

- Loops on any slot the app does not own, including the owner's own chats and
  another app's slots.
- Any change to approval modes, auto-approve, or what a loop's turn may do.
  `sessionApproval` and `workerAutoApprove` keep their own consent.
- Channel-bound loops (Slack, Discord) for apps.
- Changes to `ChatSlotSDK`'s arm API, the read routes, or the owner's
  `/api/monitors` routes.
- A per-app count of live loops. The count is already bounded by the slots the
  app owns, one loop per slot (§4.2).

Disabling an app stopping its loops is in scope (§4.4). It covers loops armed
through the in-gateway SDK too, because both surfaces leave the same kind of loop.

## 4. Design

### 4.1 One gate for the four write routes

Each write route replaces its bare `_require_monitor_owner` call with one helper,
`_require_monitor_owner_or_own_app_slot(request, operation, slot_key)`:

1. **Owner request.** Unchanged: `_require_monitor_owner` passes it.
2. **App request** (`request["app"]` non-empty):
   1. Read the live manifest and app state, as `session_approval_denial_cause`
      does (`src/kiro_crew/apps/permissions.py`): manifest first, then the
      installed record. If `chatSlots` is not `true` or the app is off, answer
      today's `403 dashboard_owner_required` with a `cause`
      (`chat_slots_not_declared`, `disabled`, `unavailable`) whose text names
      who clears it, in the shape `session_approval_refusal` uses.
   2. Resolve the slot: the body's `slot_key` (or `session_key`) on start, the
      loop record's `slot_key` on update, delete and fire.
   3. Call `deny_app_slot_session_access(app, slot, key, operation)`
      (`src/kiro_crew/dashboard/slot_ownership.py`). It passes only when
      `slot._app == app` and the slot still runs on its own session and
      transcript; a foreign, unscoped or missing slot gets the uniform
      not-found answer the chat-slot routes already give, and an
      app-isolation audit row.
3. **Any other caller** (internal secret with no app claim, a non-owner person):
   unchanged.

### 4.2 What each route then does for an app

- **Start** calls `authorize_and_add_nudge(..., source=f"app:{app}")`, as
  `arm_nudge` does. Message limits, credential and URL redaction, the sensitive
  stop-sentinel refusal, the external-arm refusal on `crew` and `member` slots
  (`_EXTERNAL_ARM_REFUSED_MODES`, `src/kiro_crew/autonudge_authz.py`) and the
  audit-or-deny write all apply unchanged.
- **Update** calls `authorize_and_update_nudge` with the same source.
- **Delete** calls `authorize_and_stop_monitor` or the legacy removal,
  after the ownership check.
- **Fire** runs the existing fire path after the ownership check.

An app request may send only the fields `arm_nudge` takes (`slot_key` or
`session_key`, `message`, `idle_secs`, `max_cycles`, `stop_sentinel_path`) plus
`banner` and `max_runtime_secs`. `gate`, `judge` and `watch`, which the owner may
send, answer `400 unknown_field` for an app. Without `watch` an app loop never
watches a work ledger, so `_extend_for_open_ledger`
(`src/kiro_crew/autonudge_service/firing.py`) never raises its spent bounds.

**Bounds.** The owner's limits apply to an app unchanged, with one tightening:

- Every app loop has a finite runtime. An app may send `max_runtime_secs`; it is
  checked by `validate_runtime_secs` (`src/kiro_crew/monitoring/limits.py`) with
  `allow_unbounded=False`, so it must be between 1 and the owner's configured
  `monitoring.max_runtime_secs` ceiling (`runtime_ceiling_secs`). When the app
  leaves it out, the loop gets that ceiling. The owner's own calls keep
  `allow_unbounded=True`, so `0` still means unbounded for them.
- `max_cycles` may be `0` (no cycle cap), as for the owner. The required runtime
  already ends the loop, so a second app-only ceiling would add a number to tune
  and no extra protection.
- Live loops per app: at most one per slot the app owns. The service keeps one
  loop per slot and replaces any existing one (`_find_by_slot` in
  `src/kiro_crew/autonudge_service/mutations.py`). No separate per-app count is
  added. The owner's stop for all of an app's loops at once is disabling the
  app (§4.4).

The same rule applies to `ChatSlotSDK.arm_nudge`, which today passes no runtime,
so `authorize_and_add_nudge` arms the loop with its default `0`, unbounded. The
SDK gains an optional `max_runtime_secs` keyword with the same check and the
same default, so the two surfaces stay alike and no existing caller breaks.

**Fire.** `POST /api/autonudge/<id>/fire` from an app is no faster than
`POST /api/chat`, the route it could use to send the same turn. Neither route
has a per-app rate limit today: there is no 429 path in
`src/kiro_crew/dashboard/chat_api/chat_send.py`. `api_autonudge_fire` refuses a
busy session with `409 session_busy` rather than queueing, so at most one fired
turn runs on a slot at a time. This RFC adds no rate limit to either route.

### 4.3 Creating the chat

An app already creates its own slot with `POST /api/chat/slots`; the host stamps
`slot._app` from the token. For a name that already holds a transcript another
caller made, `_app_slot_acquisition_denial`
(`src/kiro_crew/dashboard/chat_api/slot_create.py`) refuses with 404, by
design. This RFC changes nothing there; it only lets the app-made slot carry a loop.

### 4.4 Disabling the app stops its loops

To the owner, disabling an app means "stop this app's work". Today a loop keeps
firing turns on the app's slots after a disable, until its own bounds run out.
That is true for loops armed through the in-gateway SDK as well. Two changes
close it:

1. **In the disable request.** `notify_app_disabled`
   (`src/kiro_crew/apps/teardown.py`) already runs first in the disable
   teardown, before `disable_app` writes the `enabled` flag, and it already ends
   the app's worker auto-approve grants before the app's own hook runs. It also
   stops every AutoNudge loop whose slot carries the app's owner stamp
   (`slot._app == app`), with `stopped_reason="app_disabled"`, before calling
   the app's hook. This is a core step, so an app that registered no hook, or
   whose hook fails, still loses its loops.
2. **At every tick, as the backstop.** Before a loop fires, the service reads the
   owner stamp of the slot under `loop.slot_key`. If the stamp names an app that
   `is_app_enabled` (`src/kiro_crew/apps/manager_lookup.py`) reports as not
   enabled, the loop is deactivated with the same `stopped_reason` and fires no
   turn. This covers a disable this process never saw: another process, a
   hand-edited `installed.json`, or a restart that emptied the hook registry.

A stopped loop stays stopped. Enabling the app again does not re-arm it; the app
arms again through the routes in §4.1, which re-check the grant. The chat itself
is kept, so the card still opens.

## 5. Rollout

One implementation PR, after acceptance:

- the helper and the four call sites in `src/kiro_crew/dashboard/handlers/autonudge.py`;
- the loop stop in `notify_app_disabled` and the tick check in
  `src/kiro_crew/autonudge_service/firing.py` (§4.4);
- the bounded `max_runtime_secs` on the app path and on `ChatSlotSDK.arm_nudge`;
- `docs/app-kit/api-reference.md` and `docs/app-kit/manifest-reference.md`: the
  `/api/autonudge` rows say which calls an app token may make;
- a new `test/test_autonudge_app_slot_scope.py`.

Exit criteria, each a test that fails on `main`:

- granted app, own slot: start, update, fire and stop succeed, and the audit row
  names `app:<name>`;
- granted app, foreign slot (another app's, an unscoped person's chat, a missing
  key): the uniform not-found answer, and nothing armed;
- granted app, own slot in `member` mode: refused by the chokepoint;
- `chatSlots` not declared, and declared but the app disabled: `403`, nothing armed;
- app request carrying `gate`, `judge` or `watch`: `400 unknown_field`;
- app start with `max_runtime_secs` of `0` or above the owner's ceiling: `400`,
  nothing armed; with it left out: armed with the ceiling. The same three cases
  through `ChatSlotSDK.arm_nudge`;
- disable the app: every loop on its slots is inactive with
  `stopped_reason="app_disabled"` when the disable request returns, for loops
  armed over HTTP and through the SDK alike, and no further turn fires;
- app marked disabled without the disable request (only `installed.json`
  changed): the next tick deactivates its loop and fires no turn;
- enable the app again: the stopped loops stay stopped;
- app `PATCH` that changes `message` and `idle_secs`: the audit row's `fields`
  lists exactly those two;
- owner request: behaviour unchanged, including `max_runtime_secs=0`.

## 6. Backward compatibility

No caller loses anything. An app without `chatSlots` gets the same 403 it gets
today, with an added `cause` field. The app token still needs `/api/autonudge`
in `permissions.api`, as for any shared route. An app that already declares both
gains the routes with no new prompt, because the trust prompt it was granted
under already lists AutoNudge loops on its slots. An app that adds either to
reach these routes goes through the existing widening-update consent.

Two behaviours change for apps that use `ChatSlotSDK.arm_nudge` today. A call
that sends no runtime now gets a loop bounded by the owner's ceiling instead of
an unbounded one, with no change to the call itself. And disabling the app now
stops those loops (§4.4). Both narrow what an app's loop does after the owner
stops watching it, which is the point of the change.

## 7. Security considerations

- **Who may do what.** The owner: everything, as today. An enabled app holding
  `chatSlots`: start, update, fire and stop loops on slots stamped with its own
  name and running on their own session. Nobody else gains anything.
- **No new reach.** A loop's message becomes the next turn of a chat the app can
  already drive directly with `POST /api/chat`. A loop adds timing, not reach.
- **No approval escalation.** The turns run under the slot's own approval mode.
  Nothing here sets or widens an approval mode.
- **No faster turns.** A fired turn is one the app could already send with
  `POST /api/chat`, and fire refuses a busy session, so fire changes when a turn
  runs, never how many run at once on a slot (§4.2).
- **Bounded without a page open.** Every app loop has a finite runtime no longer
  than the owner's ceiling (§4.2), so a loop nobody watches still ends.
- **Revocation.** The grant is read from the live manifest on every call, so
  disabling the app or removing the flag stops new arms at once. Disabling the
  app also stops its armed loops, in the disable request and again at the next
  tick (§4.4).
- **Audit.** Each allowed and denied decision writes one SEL row under
  `app:<name>`, the caller that decided it, the shape `_refuse_foreign_app_job`
  uses for cron (`src/kiro_crew/dashboard/handlers/cron_shared.py`). On update,
  `authorize_and_update_nudge` already writes its critical `invoked` row before
  the change, and fails closed when it cannot. That row's `fields` names every
  field the call changes (`message`, `idle_secs`, `max_cycles`,
  `max_runtime_secs`, `active`, `banner`, `watch`), so an owner sees that a
  message changed. It records names, not values; the new message is on the loop
  record. A loop stopped by §4.4 writes one row with
  `stopped_reason="app_disabled"`.
- **Uniform denial.** A slot the app does not own reads the same as a missing
  one, so the route does not reveal other sessions' keys.

## 8. Alternatives considered

- **A new `permissions.loops` flag.** Rejected: `chatSlots` already promises
  loops on the app's slots. A second flag would ask the owner twice for one
  capability and let the HTTP and in-gateway surfaces drift apart.
- **Cron jobs with `persistent_session`.** Rejected for the reasons in §2.2.
- **Keep a dashboard page open.** The workaround today; it fails whenever no one
  has the page open.

## 9. Open questions

None open. The first draft asked whether disabling an app should stop the loops
on its slots, as a follow-up. Review on
[#37981](https://github.com/kirodotdev/KiroCrew/pull/37981#issuecomment-7051343568)
asked for it in this RFC, and it is now §4.4 with exit tests in §5.
