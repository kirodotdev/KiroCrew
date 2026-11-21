---
title: An app with permissions.chatSlots may start, change, fire and stop goal loops on its own chat slots over /api/autonudge
status: draft
author: tilakputta
created: 2026-11-16
last-audited: 2026-11-16
audited-at: 529da1ee
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
Status line. Every code reference below was read at `529da1ee`.

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
- Changes to `ChatSlotSDK`, the read routes, or the owner's `/api/monitors` routes.
- Stopping an app's loops when the app is disabled. That applies to the
  in-gateway SDK too and is listed as an open question (§9).

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
`banner` and `max_runtime_secs`. `gate` and `judge`, which `_AUTONUDGE_START_FIELDS`
accepts from the owner, answer `400 unknown_field` for an app.

### 4.3 Creating the chat

An app already creates its own slot with `POST /api/chat/slots`; the host stamps
`slot._app` from the token. For a name that already holds a transcript another
caller made, `_app_slot_acquisition_denial`
(`src/kiro_crew/dashboard/chat_api/slot_create.py`) refuses with 404, by
design. This RFC changes nothing there; it only lets the app-made slot carry a loop.

## 5. Rollout

One implementation PR, after acceptance:

- the helper and the four call sites in `src/kiro_crew/dashboard/handlers/autonudge.py`;
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
- app request carrying `gate` or `judge`: `400 unknown_field`;
- owner request: behaviour unchanged.

## 6. Backward compatibility

No caller loses anything. An app without `chatSlots` gets the same 403 it gets
today, with an added `cause` field. The app token still needs `/api/autonudge`
in `permissions.api`, as for any shared route. An app that already declares both
gains the routes with no new prompt, because the trust prompt it was granted
under already lists AutoNudge loops on its slots. An app that adds either to
reach these routes goes through the existing widening-update consent.

## 7. Security considerations

- **Who may do what.** The owner: everything, as today. An enabled app holding
  `chatSlots`: start, update, fire and stop loops on slots stamped with its own
  name and running on their own session. Nobody else gains anything.
- **No new reach.** A loop's message becomes the next turn of a chat the app can
  already drive directly with `POST /api/chat`. A loop adds timing, not reach.
- **No approval escalation.** The turns run under the slot's own approval mode.
  Nothing here sets or widens an approval mode.
- **Revocation.** The grant is read from the live manifest on every call, so
  disabling the app or removing the flag stops new arms at once.
- **Audit.** Each allowed and denied decision writes one SEL row under
  `app:<name>`, the caller that decided it, the shape `_refuse_foreign_app_job`
  uses for cron (`src/kiro_crew/dashboard/handlers/cron_shared.py`).
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

1. Should disabling an app also stop the loops on its slots? Proposed: yes, in a
   follow-up that covers the in-gateway SDK and these routes together. Nothing in
   §5 waits on it.
