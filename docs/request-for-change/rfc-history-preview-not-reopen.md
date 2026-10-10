---
title: An Older Sessions row click previews the session, Resume reopens it
status: accepted
author: Pearcekieser
created: 2026-10-09
last-audited: 2026-10-09
audited-at: d4c3a7334d
doc-pr:
implementation-prs: [15730]
tracking-issues: [12257]
supersedes: []
superseded-by: []
---

# RFC: An Older Sessions row click previews the session, Resume reopens it

- Status: accepted. This is the product owner's decision, given 2026-10-09 by
  Pearcekieser on [#15730](https://github.com/kirodotdev/KiroCrew/pull/15730):
  activating an Older Sessions row opens a read-only preview, and reopening the
  session takes a separate, explicit Resume. Claims about today's code were
  checked at `d4c3a7334d` (main).
- Author: Pearcekieser
- Implementation: [#15730](https://github.com/kirodotdev/KiroCrew/pull/15730),
  tracking [#12257](https://github.com/kirodotdev/KiroCrew/issues/12257)
  section 8 ("read-only preview with a separate explicit Resume").

## 1. Summary

A click on a row in the sidebar's Older Sessions pane, or Enter on a focused
row, opens a read-only preview dialog of that conversation. It does not reopen
the session. Reopening is its own action: a Resume session button in the
dialog footer, and a labelled Resume button in the row's hover toolbar.

## 2. Background

At `d4c3a7334d` the row's activation in `website/src/pages/ChatSidebar.tsx`
dispatches `resumeFromHistory`, which posts to
`POST /api/chat/slots/{slot}/resume`. That endpoint clears the transcript's
`closed` flag and publishes a live slot. The pane has no other way to show a
conversation, so reading an old session reopens it: the row leaves Older
Sessions, becomes an open tab, and has to be closed again. Reviewing five old
conversations leaves five new tabs.

`GET /api/sessions/{key}` already reads a transcript without side effects, but
it returns every raw stored row with no display redaction, and no dashboard code
calls it. For an app token it answers only transcripts that record the app as
their owner (`_app_transcript_refusal`, then `_app_owned_messages`).

## 3. Decision

| Action on an Older Sessions row | Before | After |
|---|---|---|
| Click, or Enter on the focused row | reopens the session as a tab | opens a read-only preview; the session stays closed and stays listed |
| Resume session in the preview footer | none | reopens the session, as the old click did |
| Resume in the row's hover toolbar | none | reopens the session without a preview |
| Delete in the row's hover toolbar | deletes | unchanged, set off from Resume by a divider |
| A remote crew's row | switches to that crew | unchanged |

Every other way of reopening a closed session keeps its behaviour: the command
palette's Older Sessions group, ChatPage's "Continue a previous chat" list and a
notification's Resume chat button. Each already names reopening as its action.

## 4. Design

The preview is a modal over the sidebar. It shows the newest 200 rows
(`SESSION_PREVIEW_LIMIT`), drawing user and agent turns and leaving out tool and
system rows. Its text says it is read-only. When the transcript holds more rows
than the page, a muted line says it shows the most recent part. A session with
no chat rows opens to an empty state. A failed read shows the shared error
notice with Try again, and Resume stays available. Both Resume controls are
disabled while the dashboard is disconnected. Each open reads fresh; nothing is
cached between opens.

The preview reads `GET /api/sessions/{key}`. For a dashboard caller that route
answers `{key, title, messages, has_more}`: the newest page, rendered through
`_prepare_messages` (the same redaction and blocked-link records a resumed tab
gets), with the title through `_redact_for_display`. Metadata and rows come from
one transcript revision, and a revision that keeps moving answers the retryable
503 `transcript_changed`. The read writes no metadata and creates no slot. With
no conversation log configured it answers 400 `no_conversation_log`, because an
empty array is not a page the dialog can draw. The route stays an owner
surface behind `guard_owner_surface_routes`.

An app token that owns the transcript keeps the bare array it gets today. That
is the contract `docs/app-kit/api-reference.md` documents for apps, and this
decision does not change it. The dashboard shape is a dashboard-internal answer
on the same route, the way the route already branches on `request["app"]`.

## 5. Compatibility

The dashboard answer of `GET /api/sessions/{key}` changes shape from a bare
array to the page object. No caller in `src/`, `website/src/` or `packages/`
reads the dashboard answer at `d4c3a7334d`: `api.sessionDetail` has no consumer
and the mochi panel calls only `DELETE` on that path. The two tests that read
it move to the object shape in #15730. App callers see no change.

## 6. Alternatives considered

1. Keep the click as resume and add a Preview button to the hover toolbar.
   Rejected: the common action, looking at an old conversation, stays the one
   that costs a tab, and the pane's most prominent gesture keeps a side effect
   the user did not ask for.
2. A new `GET /api/sessions/{key}/preview` route, leaving the existing route's
   answer as it is. Rejected for now: the existing route has no dashboard
   caller to protect, and a new route adds a composition-contract row, a
   trust-gate entry, a bounded-read cap and error-code contract rows for one
   consumer. If a dashboard caller of the bare array appears, splitting the
   route is the follow-up.
3. Open the session in a read-only tab. Rejected: it still adds a tab to close,
   and a tab implies a live slot the read path must not create.

## 7. Open questions

None blocking. Paging inside the preview past 200 rows, and the bulk delete
that #12257 section 8 also asks for, are out of scope and would each be their
own change.
