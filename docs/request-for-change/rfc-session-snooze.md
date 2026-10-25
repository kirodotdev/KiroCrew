---
title: Snooze a session until a chosen time
status: accepted
author: Pearcekieser
created: 2026-10-24
last-audited: 2026-11-05
audited-at: e84971588e
doc-pr: 23906
implementation-prs: []
tracking-issues: [21448]
supersedes: []
superseded-by: []
---

# RFC: Snooze a Session Until a Chosen Time

> **Status:** `accepted`. bolichen97 accepted it on 2026-11-05, recorded in
> §11. Nothing is on main. Verified at `e84971588e`: a session row's menu
> (`website/src/components/SessionActionsMenu.tsx`, shared by the chat header
> title dropdown, the row's right-click menu and the row's ⋮ menu) offers Pin
> and Mute but no way to set a session aside until a time, and the only
> time-based hiding is the dormant collapse in
> `website/src/pages/staleCollapse.ts`, which reads last activity and never
> brings a row back by itself. This document lands first because it adds a
> per-session state the sidebar hides rows by, and the First Principles lane
> reads that decision from the base branch.

## 1. Summary

A person can snooze a session from any of its three menus: the chat header
title dropdown, right-click on the sidebar row, and the row's ⋮ menu. They pick
a preset (1 hour, 3 hours, tomorrow morning, next Monday morning) or a custom
date and time. The gateway stores the due time in the session's metadata line.
The row then moves into a collapsed "N snoozed sessions" zone at the bottom of
its container, drawn grayed out with a clock marker and its due time.

When the due time arrives, the gateway clears the snooze and records the wake
time. Due times that passed while the gateway was down are handled the same way
on the first sweep after boot. The row returns to its place, is marked unread,
counts as fresh activity so dormant and age filters show it, and raises one
dashboard notification that opens it. The person can also wake it early from
the same menus.

Snooze never closes, deletes, stops or rebinds a session. It is a time-bound
attribute on an open session, like Pin and Mute, and not a new lifecycle state.

## 2. Background

Measured at `e84971588e`.

- The three menus render one item list. `SessionActionsMenu.tsx` picks
  `DropdownMenuItem` or `ContextMenuItem` by its `variant`, and its callers are
  `pages/chat/ChatHeaderMenu.tsx` (the title chevron), and
  `pages/chat-sidebar/SessionRow.tsx` for the right-click menu and both ⋮
  menus. The sidebar board lane draws the same `SessionRow`, so it gets the
  same menus.
- Per-session user flags already persist this way. Mute
  (`api_chat_slot_muted` in `src/kiro_crew/dashboard/chat_folders.py`, through
  the shared `_toggle_slot_user_flag` body) is a
  `PATCH /api/chat/slots/{slot}/muted` route that refuses non-user callers,
  persists the flag through a `Field` in
  `dashboard/slot_persistence/metadata_codec.py` before it changes the live
  slot, and publishes a `slot_patch` frame through `state.push_slot_patch`.
- The dormant collapse (`splitStaleSlots` in `pages/staleCollapse.ts`, driven
  by `useStaleCollapse` in `pages/chat-sidebar/stale.ts`) hides an open session whose last activity
  (`slotActivityTs` in `pages/chat/sessionOrder.ts`: `last_turn_ts`, then
  `last_ts`, then `created`) is older than a threshold. It exempts pinned,
  focused, running, unread and attention-needing rows. It is a pure client
  view and holds no due time.
- Unread is client state. `markSlotUnread` in `store/dashboardSlice.ts` takes a
  string payload for a manual reminder, which only a local read clears.
- The gateway raises dashboard notifications with `state.notify(kind, title,
  body, meta=..., url=...)` through `NotificationCoordinator`.

Two open issues frame where snooze must sit. #18235 asks that the sidebar stop
growing overlapping lifecycle states, and that Pin and Folder stay attributes.
#12601 (timed Do Not Disturb) settled on persisting an until-time and not
trusting a client timer, so the state survives a restart.

## 3. Goals

1. Snooze from all three menus, with presets and a custom time.
2. Snoozed rows are visibly grayed and kept in a collapsible zone below the
   container's dormant zone.
3. The gateway owns the due time and the wake. A wake survives a gateway
   restart, including a due time that passed while it was down, and needs no
   open browser.
4. A woken session reads as unread and as fresh, and raises one notification,
   as #21448 asks.
5. Early unsnooze from the same menus.
6. Snoozing and waking keep the session's transcript, identity, folder, pin,
   tags, mute and running turn exactly as they were.

## 4. Non-goals

- No agent-facing snooze tool. Snooze is a person's choice about their own
  sidebar, the same reason the mute routes refuse agent callers.
- No Slack, phone or email delivery of the wake. The dashboard notification is
  the first version, as #21448 scopes it.
- No change to Dormant, Older sessions, closing, or folder behaviour.
- No recurring snooze.
- No wake for a closed session. The sweep reads live slots only, so a session
  closed while snoozed gets no notification while it stays closed.

## 5. Design

### 5.1 State

One new field on the slot metadata line, `snoozed_until`, an epoch-seconds
float, absent when the session is not snoozed. One more, `snooze_woke_at`,
epoch seconds, written when the gateway ends a snooze at its due time. Both
round-trip through `metadata_codec.FIELDS` like `muted`, are projected by
`SlotProjection`, and join `_SLOT_PATCH_FIELDS` so changes ride the existing
`slot_patch` frame. An older gateway that reads the line ignores the unknown
keys and shows the row as normal.

### 5.2 Route

`PATCH /api/chat/slots/{slot}/snooze` with body `{"until": <epoch seconds>}`
to snooze or `{"until": null}` to wake early. It follows the mute route's
contract: the same ownership and app fences, refusal of agent and
unattributable callers, persist before the live flag changes, and publish
after. The gateway validates the instant. It must be a finite number at least
60 seconds ahead and at most 366 days ahead, or the route returns 400 with
`snooze_until_invalid`. Presets are computed in the browser from local time
and sent as an absolute instant, so the gateway needs no time zone.

### 5.3 Wake

The wake check adds no periodic task of its own. It runs on the gateway's
existing 5-second dirty-slot flush tick (`_flush_loop` in
`src/kiro_crew/dashboard/dashboard_persistence.py`), after that tick's flush,
and is guarded so a failed wake is logged and never stops the flush. Riding
that tick also stops the check cleanly at shutdown, with the flush. It skips
the check until the open-slot restore has run. A slot whose `snoozed_until` is
at or before now is woken: the gateway
clears `snoozed_until`, sets `snooze_woke_at` to now, persists both, publishes
the patch, and calls `state.notify` with a link to the session. The save comes
first on purpose. If the gateway stops after the save and before the notify,
that one wake loses its bell entry, but `snooze_woke_at` is saved, so each
browser still marks the session unread on its next load (§5.4). A notify
before the save would instead repeat the bell entry after such a stop, and on
every tick while the save fails. Because the due
time lives in the metadata line, a restart loses no due time. The first sweep after
boot wakes every session that came due while the gateway was down, each once.
The sweep reads the clock through one injectable function so tests drive it
with a fake clock.

A snooze set on a session that is later closed stays in its metadata line. If
the session is reopened before the due time, it comes back snoozed. If it is
reopened after, the next sweep wakes it and raises the notification then.

### 5.4 What the browser does

- The sidebar removes snoozed rows from each container's list before the
  dormant split and renders them after the dormant expander, behind a
  "Show N snoozed sessions" toggle built like the dormant toggle, with
  `aria-expanded` and `aria-controls`. The toggle's open state persists in
  localStorage, like the crew collapse.
- Snoozed rows are drawn at reduced opacity with a clock marker. Hovering or
  focusing the marker names the due time.
- A snoozed session that needs the person, because it has a pending approval or
  an unanswered question, stays in the normal list with the marker, so a
  blocked turn is never hidden. Unread alone does not do this; hiding unread
  work is what snoozing is for.
- On a `slot_patch` or slot list where `snooze_woke_at` is newer than the last
  wake this browser acknowledged, the browser marks the session unread as a
  manual reminder and records the acknowledgement in localStorage, so a reload
  does not repeat it. `slotActivityTs` takes the later of its current answer
  and `snooze_woke_at`, so the woken row sorts as fresh and leaves the dormant
  collapse.
- Each browser keeps its own acknowledgement, so a person who uses two
  browsers or devices sees the woken session marked unread once in each. This
  is intended: unread state already lives in each browser, and the gateway
  stores none. Moving the acknowledgement to the gateway would add the only
  server-side unread state in the dashboard.
- A browser with no acknowledgement record yet, such as a new device or one
  whose storage was cleared, records the time it first loads and ignores any
  `snooze_woke_at` older than that. Without the cutoff its first load would
  mark every session that ever woke as unread.
- The menus show "Snooze" with a submenu of presets and "Custom…", which opens
  a small dialog with date and time inputs. A snoozed session shows
  "Unsnooze" and its due time instead, with a note that closing it skips the
  reminder at the due time (§5.5).

### 5.5 Interactions

| Case | Behaviour |
|---|---|
| Running turn | Keeps running. Snooze only changes where the row is drawn. |
| Queued messages | Untouched. |
| Pinned | Pin is kept. The row sits in the snoozed zone while snoozed and returns to the pinned band on wake. The pinned flag that patrol tools read does not change. |
| Muted | Mute is kept. The wake notification is still raised, because the person asked for this reminder. |
| Dormant | A snoozed row is never counted as dormant. On wake its activity time is the wake time, so it is fresh. |
| Closed while snoozed | Keeps its due time and raises no notification while closed. Reopened before the due time, it comes back snoozed; reopened after, the next sweep wakes it and the notification fires at the reopen. See §5.3. |
| Focused session | Snoozing the open session keeps it open in the chat pane. The row moves to the snoozed zone. |

## 6. Migration plan

1. **Backend PR.** The two fields, the route, the sweep, the notification,
   `docs/system-specs` and feature-map updates, and tests. Exit criteria:
   a snoozed slot survives a save and reload with its `snoozed_until`; the
   route refuses an agent caller, a past instant and a non-number; with a fake
   clock the sweep wakes a due slot once, persists the clear before it
   publishes, and raises one notification; a slot whose due time passed
   before boot is woken by the first sweep after restore.
2. **Frontend PR.** The menu items, the custom dialog, the snoozed zone, the
   grayed row, the wake handling, i18n in every locale, and a
   `test/gui_user/features.json` entry. Exit criteria: each of the three menus
   snoozes and unsnoozes a row; the zone toggles from the keyboard and
   announces its state; a woken row is unread, fresh and out of the snoozed
   zone; a reload after a wake does not mark it unread again; the layout holds
   on a 375 px viewport.

## 7. Backward compatibility

Additive. Nothing changes for a session that is never snoozed. The new metadata
keys are optional. An older gateway does not read them, so it shows a snoozed
session as a normal row and never wakes it. Its saves keep the keys: a slot save
passes every metadata key it does not own through unchanged
(`carry_unowned_metadata` in `src/kiro_crew/history_schema.py`). After an
upgrade back, the sweep wakes any snooze whose due time passed during the
downgrade, late but once. Nothing else is lost: the transcript and every other
field are untouched, and an older gateway never hides a session because of
snooze.

## 8. Security considerations

The route is user-only and inherits the mute route's fences, so an agent, an
app or a crewmate cannot hide a session from its person. The body is a single
number, validated for type and range. The wake notification carries only the
session's title and key.

## 9. Alternatives considered

- **A browser timer as the source of truth.** Rejected for the reason #12601
  records: it does not survive a restart and does nothing when no tab is open.
- **Hide snoozed rows entirely,** as #21448 first proposes. Rejected because
  the person then has no way to find or wake one before its time. A collapsed
  zone keeps them findable at one row's cost.
- **A new lifecycle state beside Dormant.** Rejected per #18235. Snooze is an
  attribute on an open session.
- **Reset the activity timestamp on the server** by rewriting `last_ts`.
  Rejected because `last_ts` names the newest message and other code reads it
  as a watermark. A separate `snooze_woke_at` keeps that meaning intact.

## 10. Open questions

1. Resolved in review: the wake check rides the existing 5-second flush tick
   (§5.3) in place of a new 15-second loop, so it adds no periodic task. Each
   tick scans at most `MAX_LIVE_SLOTS` (500) in-memory slots, the same slots
   the flush already visits, and a wake lands within about 5 seconds of its
   due time.
2. Should snooze be offered on peer (remote instance) rows? This document says
   no for the first version, because the route acts on the local gateway's
   slots only.

## 11. Acceptance

Proposed 2026-10-24 by Pearcekieser. Accepted 2026-11-05 by bolichen97
(@bolichen97, maintainer). The design brief on 2026-11-05 picked "accept as
written" and asked for two changes: run the wake check on an existing periodic
task, and say in the UI copy what happens to a session closed while
snoozed
([review](https://github.com/kirodotdev/KiroCrew/pull/23906#pullrequestreview-5778994182)).
Both are in §5.3, §5.4 and §10. The re-read at `855253c7ee` kept that pick
([review](https://github.com/kirodotdev/KiroCrew/pull/23906#pullrequestreview-5781665398)),
and the maintainer signed the direction: snooze rides the existing flush tick,
and the Unsnooze menu carries the note for sessions closed while snoozed
([comment](https://github.com/kirodotdev/KiroCrew/pull/23906#issuecomment-6653075739)).
