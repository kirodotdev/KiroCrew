---
title: An Older Sessions row click previews the session, Resume reopens it
status: accepted
author: Pearcekieser
created: 2026-10-09
last-audited: 2026-11-03
audited-at: 66db87b900
doc-pr: 18535
implementation-prs: [15730]
tracking-issues: [12257]
supersedes: []
superseded-by: []
---

# RFC: An Older Sessions row click previews the session, Resume reopens it

- Status: accepted 2026-11-06 by the maintainer Bolin Chen (@bolichen97), at
  `9d5d7bb` on [#18535](https://github.com/kirodotdev/KiroCrew/pull/18535). It
  records the product owner's decision, given 2026-10-09 by Pearcekieser on
  [#15730](https://github.com/kirodotdev/KiroCrew/pull/15730): activating an
  Older Sessions row opens a read-only preview, and reopening the session takes
  a separate, explicit Resume. Claims about today's code were
  checked at `85dcaeed2f` (main) and re-checked at `66db87b900` on 2026-11-03.
- Author: Pearcekieser
- Implementation: [#15730](https://github.com/kirodotdev/KiroCrew/pull/15730),
  tracking [#12257](https://github.com/kirodotdev/KiroCrew/issues/12257)
  section 8 ("read-only preview with a separate explicit Resume").

## 1. Summary

A click on a row in the sidebar's Older Sessions pane, or Enter or Space on a
focused row, opens a read-only preview dialog of that conversation. It does not reopen
the session. Reopening is its own action: a Resume session button in the
dialog footer, and a labelled Resume button in the row's hover toolbar.

## 2. Background

At `85dcaeed2f` the row's activation (`activateRow`) in
`website/src/pages/chat-sidebar/HistoryPane.tsx` dispatches `resumeFromHistory`, which posts to
`POST /api/chat/slots/{slot}/resume`. That endpoint clears the transcript's
`closed` flag and publishes a live slot. The pane has no other way to show a
conversation, so reading an old session reopens it: the row leaves Older
Sessions, becomes an open tab, and has to be closed again. Reviewing five old
conversations leaves five new tabs.

`GET /api/sessions/{key}` already reads a transcript without side effects, but
it returns every raw stored row with no display redaction, and no dashboard code
calls it. For an app token it answers only transcripts that record the app as
their owner (`_app_transcript_refusal`, then `_app_owned_messages`). Since
#26334 a hub also calls it on a peer: `api_crew_sessions_read` in
`handlers_instances_crew_reads.py` sends `exclude_incognito=1` with no app token,
and `_shape_read_messages` refuses any answer that is not a list.

## 3. Decision

| Action on an Older Sessions row | Before | After |
|---|---|---|
| Click, or Enter or Space on the focused row | reopens the session as a tab | opens a read-only preview; the session stays closed and stays listed |
| Resume session in the preview footer | none | reopens the session, as the old click did |
| Resume in the row's hover toolbar | none | reopens the session without a preview |
| Delete in the row's hover toolbar | deletes | unchanged, set off from Resume by a divider |
| A remote crew's row | switches to that crew | unchanged |

Every other entry point that dispatches `resumeFromHistory` keeps its behaviour,
for example the command palette's Older Sessions group and its recents provider,
ChatPage's "Continue a previous chat" list, the sessions drawer's closed-session
link, the short-name match in the chat page session controller and a
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

The preview reads a new route, `GET /api/sessions/{key}/preview`. It answers
`{key, title, messages, has_more}`: the newest page, rendered through
`_prepare_messages` (the same redaction and blocked-link records a resumed tab
gets), with the title through `_redact_for_display`. Metadata and rows come from
one transcript revision, and a revision that keeps moving answers the retryable
503 `transcript_changed`. The read writes no metadata and creates no slot. With
no conversation log configured it answers 400 `no_conversation_log`, because an
empty array is not a page the dialog can draw. The new route is an owner
surface behind `guard_owner_surface_routes` and refuses app tokens. #15730 adds
the rows a new route needs: the composition-contract row, the trust-gate entry,
the bounded-read cap and the error-code contract rows.

`GET /api/sessions/{key}` itself does not change. Every caller keeps the bare
array it gets today, including an owning app (the contract
`docs/app-kit/api-reference.md` documents) and a hub's crew transcript read
(the `[]` it gets for an incognito or missing transcript included).

## 5. Compatibility

No existing answer changes shape, so no caller breaks. That covers a hub and a
peer on different versions: `api_crew_sessions_read` keeps calling the
unchanged route, and an older dashboard never calls the new one. The dashboard
gains one new call, the preview read. At `66db87b900` no dashboard code reads
`GET /api/sessions/{key}`: `api.sessionDetail` is defined but has no consumer.

## 6. Alternatives considered

1. Keep the click as resume and add a Preview button to the hover toolbar.
   Rejected: the common action, looking at an old conversation, stays the one
   that costs a tab, and the pane's most prominent gesture keeps a side effect
   the user did not ask for.
2. Answer the page object from `GET /api/sessions/{key}` for every caller
   without an app token, with no new route. This was the design until
   2026-11-03. Rejected: since #26334 a hub's crew transcript read calls that
   route on a peer with no app token and requires the bare array, so every
   remote-crew transcript read would fail as a malformed reply. Carving the hub
   out by its `exclude_incognito=1` parameter would tie the answer's shape to a
   filter flag. A separate route costs four contract rows and breaks no one.
3. Open the session in a read-only tab. Rejected: it still adds a tab to close,
   and a tab implies a live slot the read path must not create.

## 7. Open questions

None blocking. Paging inside the preview past 200 rows, and the bulk delete
that #12257 section 8 also asks for, are out of scope and would each be their
own change.
