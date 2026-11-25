---
title: Search all chats, later stages -- message links, matches by meaning, tool output
status: accepted
author: RohanK6
created: 2026-11-24
last-audited: 2026-11-24
audited-at: 50fe0f6cf2
doc-pr: null
implementation-prs: []
tracking-issues: [38023]
supersedes: []
superseded-by: []
---

# RFC: Search all chats, later stages

**Status:** accepted on 2026-11-25 by maintainer bolichen97's sign-off on
[#42015](https://github.com/kirodotdev/KiroCrew/pull/42015#pullrequestreview-6063857145)
("Bolin accepts this RFC as written"). Nothing is built yet.
On 2026-11-16 KiroCrewCaptain wrote on
[#38023](https://github.com/kirodotdev/KiroCrew/issues/38023) that the deferred
stages are not on the current plan and that a design goes through this
request-for-change process. That decision is now recorded in
[§13](#13-decision); nothing here is built yet. Every main-side
reference was read at `50fe0f6cf2`.

Credit: stages 1 and 2 of Search all chats shipped in
[#38024](https://github.com/kirodotdev/KiroCrew/pull/38024) (part of
[#38022](https://github.com/kirodotdev/KiroCrew/issues/38022)); the later stages
were parked in #38023. The cost review of archive search on
[#38222](https://github.com/kirodotdev/KiroCrew/issues/38222) (CrysisDeu,
2026-11-17) and the open fix for it,
[#39672](https://github.com/kirodotdev/KiroCrew/pull/39672), set the cost rules
§7 applies to every stage that adds text to the search index.

## 1. Summary

Search all chats (⌘⇧F / Ctrl+Shift+F) finds the messages that match across
every chat and jumps to the one the person picks. Three things it does not do
yet, in the order this document proposes them:

- **Stage A, message links (the concrete first stage):** copy a link to a hit
  from the result list, the same link a chat message's "Copy link to message"
  gives. Frontend only; no new route, no new index text.
- **Stage B, matches by meaning:** list the messages of a chat that matched by
  meaning, once session search has its meaning leg. Blocked on
  [rfc-session-semantic-search.md](rfc-session-semantic-search.md) phase 3.
- **Stage C, tool output:** let a search match what a tool returned (`tool`
  rows), behind its own default-off setting, under the cost rules of §7.
  Attachment text stays out (§4).

Each stage can be accepted, changed or dropped without the others.

## 2. What main has today

| Piece | Where | What it does |
|---|---|---|
| The dialog | `website/src/components/globalChatSearch/GlobalChatSearchDialog.tsx` | Lists hits grouped by chat, filters by Bot, speaker and date, and on Enter or click resumes the chat (`resumeFromHistory`) and files a `pendingMessageJump` with the hit's `ts` and `mid`. |
| The route | `GET /api/sessions/search/messages` (`dashboard/handlers/sessions_parts/message_search.py`) | Ranks chats with `search_sessions`, then reads each ranked chat's paging corpus (`read_messages_chained_full`) and lists the `user` and `assistant` messages whose string `content` matches (`message_hits`). Each hit is `{index, role, ts?, mid?, snippet}`; at most `MAX_HITS_PER_SESSION` (5) excerpts per chat and `MAX_SESSIONS` (50) chats. |
| Message links | `website/src/utils/shareUrl.ts` (`buildShareableUrl`, `copySessionLink`) | Builds `/chat/<slug>?sid=<key>&msg=<ts>[&mid=<mid>]`. Used by a message's "Copy link to message" (`AssistantMessage.tsx`, `UserMessage.tsx`, without `mid`) and by the pinned messages panel (`PinnedMessagesPanel.tsx`, with `mid`). `resolveMsgIndex` prefers `mid` and falls back to `ts`. |
| A link to a closed chat | `pages/chat/useChatPageSessionController.ts` (`sidClosed`, `resumeSidClosed`) | A `?sid=` naming a chat that is not open shows a notice with a Resume action instead of opening it. |
| Search by meaning | [rfc-session-semantic-search.md](rfc-session-semantic-search.md), status `partial` | Phase 2 (the chunk store, behind `session.semantic_search`, default off) is on main. Phase 3, the vector leg `search_sessions` reads, is not; it is open as [#39791](https://github.com/kirodotdev/KiroCrew/pull/39791). A chunk records `message_index`, its message's position among the texts `_iter_message_texts` yields. |
| Searchable text | `SessionCatalogProjection._iter_message_texts` (`history_search.py`) | The non-empty string `content` of every row except the metadata header and a host-sandbox refusal row. The fold, the snippet, the SQLite index row (`history_index.py`) and the chunk store all read it. `message_hits` then keeps `user` and `assistant` rows only, so a `tool` row's text can rank a chat but is never listed as a hit. |
| Attachments | a user row's `meta.files` | The names of the files a turn attached. Their contents are not in the transcript. |

## 3. Goals

- Copy a link to one hit without opening the chat first.
- List hits that matched by meaning, marked as such, once the meaning leg exists.
- Let a person who wants it find a message by what a tool returned, without
  making search slower or larger for anyone who does not.
- Keep every cost rule the #38222 review set (§7).

## 4. Non-goals

- Extracting text from attached files (PDFs, images, documents). That is an
  indexing pipeline of its own, with its own storage and privacy review.
- Changing how `search_sessions` ranks chats, or its query syntax.
- Building the meaning leg itself. Stage B only reads what
  [rfc-session-semantic-search.md](rfc-session-semantic-search.md) phase 3 builds.
- Searching archive segments of reasons other than `rotate`; #39672 keeps them
  out on purpose.

## 5. Design

### Stage A: copy a link to a hit (concrete first stage)

- **Where:** each hit row in the dialog gets a "Copy link" action (an icon
  button, and the same item in the row's keyboard context: a shortcut is open
  question 1). It calls `copySessionLink(result.key, result.title, hit.ts, mode,
  hit.mid)`, the same function the pinned messages panel calls, so the link has
  the same shape and resolves through `resolveMsgIndex` the same way.
- **When it is offered:** only for a hit that carries `ts`. A hit with no `ts`
  has nothing the deep link can resolve, so it gets no action, as a message
  without `ts` gets none today.
- **What the link opens:** an open chat jumps to the message. A chat that is not
  open shows main's existing closed-chat notice with Resume (`sidClosed`); the
  phase exit test checks that the jump still lands after Resume.
- **Feedback:** the copied / failed icon and `ErrorNotice` the message actions
  already use (`flashCopy`, `copyOutcomeIcon`). Strings through the i18n
  catalog.
- **Server:** no change. The route already returns `key`, `ts` and `mid`.
- **Not in Stage A:** a link that opens the dialog with a query filled in.

### Stage B: matches by meaning

Entry condition: rfc-session-semantic-search phase 3 is on main and its
`match` field ships on `search_sessions` rows.

- **Rows:** a chat the meaning leg returned and the keyword gate did not lists
  its best chunk as one hit: `{index, role, ts?, mid?, snippet, match:
  "meaning"}`. `index` comes from the chunk's `message_index` mapped onto the
  paging corpus the route already reads; the snippet is the chunk, redacted, as
  that RFC's §7.4 already cuts it. A keyword hit carries `match: "text"`.
- **Mapping the index:** `message_index` counts the texts `_iter_message_texts`
  yields, while the dialog's `index` counts rows of `read_messages_chained_full`.
  The route maps one onto the other by walking the same corpus once, keeping the
  rows `_iter_message_texts` would yield. A chunk whose message cannot be found
  (the chat changed since it was chunked) is dropped, never pointed at the wrong
  row. With #39672, both lists start with the archived head, and its
  `CORPUS_VERSION` re-chunks rows stored before.
- **Dialog:** a meaning hit is labelled "Related", the label
  rfc-session-semantic-search §8.2 gives a meaning row in the sidebar, and lists
  below the text hits of the same chat. No mode switch; with the setting off the dialog is unchanged.
- **Gates:** every gate a keyword hit passes (app ownership through
  `_AppOwnedKeys`, the role and date filters, redaction, incognito and temporary
  chats left out).

### Stage C: tool output (default off)

- **Setting:** `session.search_tool_output`, `false` by default.
- **What is searched:** the string `content` of `tool` rows, already in
  `_iter_message_texts` today. With the setting on, `message_hits` also lists
  `tool` rows, and the speaker filter gains "Tool output". With it off, nothing
  changes: a `tool` row's text still ranks a chat (as on main) but is not
  listed.
- **Excerpt:** the same redaction as every hit (`redact` on the whole text before
  the cut), plus the refusal-row skip. A tool row is often large, so its excerpt
  is cut around the match like any other, and the row is capped at the same
  record bound the index reads with (`jsonl_util.bounded_records`).
- **Index:** no new text enters the SQLite index or the chunk store; both already
  read `tool` rows through `_iter_message_texts`. Stage C changes which matches
  are listed, not what is stored. If the team wants tool text kept OUT of the
  index for people who leave the setting off, that is a larger change and is
  open question 3; its cost is in §7.
- **Attachments:** a hit may show the names in `meta.files` beside a user hit
  (no file contents, §4).

## 6. Migration plan

| Phase | PR-sized change | Exit criteria |
|---|---|---|
| A | "Copy link" on a dialog hit. Frontend only. | A copied link opens the chat at that message, by `mid` when the hit has one; for a closed chat, Resume then lands the jump; a hit without `ts` has no action; a refused clipboard write shows the error. Screenshots attached to the PR. |
| B | `match` on hits, the index mapping, the "Related" label. Blocked on rfc-session-semantic-search phase 3. | With `session.semantic_search` off, the route's reply equals main's. With it on, every meaning hit's `index` opens the message its chunk came from; a stale chunk is dropped; an app sees only its own chats. |
| C | `session.search_tool_output`, the role, the speaker filter entry. | With the setting off, the reply equals main's. With it on, a word only a tool row holds lists that row, redacted, and no index or chunk row grows. |

Each phase can be dropped without the later ones.

## 7. Costs (the #38222 rules)

The #38222 review named the costs a search change must not add. Each stage,
against each:

| Cost from #38222 | Stage A | Stage B | Stage C |
|---|---|---|---|
| Reading archive or transcript files on the query path | none | none new: the route already reads each listed chat's paging corpus | none new: the same read |
| Work under the transcript lock (`index_session` reads the whole corpus while it holds `_locked(key)`) | none | none: chunks are read from the store | none: no index change |
| Retention's drop under a writer's lock, its `BEGIN IMMEDIATE` and WAL truncate | none | none | none |
| Index or chunk store growth | none | none | none, unless open question 3 is taken |

If open question 3 is answered "keep tool text out of the index unless the
setting is on", Stage C then changes what `_iter_message_texts` yields. That
means an `_INDEX_VERSION` and `CORPUS_VERSION` bump (a one-time rebuild of every
index row and chunk), and a setting flip has to rebuild again. It adds no work
under the lock beyond the rebuild itself, and no retention change.

## 8. Backward compatibility

- Stage A adds a button; no route or field changes.
- Stage B adds `match` to each hit. A client that ignores it sees the same
  fields as today. With `session.semantic_search` off, nothing changes.
- Stage C adds a setting, default off, and `tool` as a value of the route's
  `role` parameter. With the setting off, `role=tool` answers the same
  `400 invalid_role` an unknown role value answers today, and every other
  reply is unchanged.

## 9. Risks

- **Stage A:** a link copied from a hit names a chat the receiver may not be able
  to open (another person's Crew, a deleted chat). That is true of every message
  link today; the receiver gets main's existing "not found" or closed-chat
  notice.
- **Stage B:** a wrong index mapping would jump to the wrong message. The mapping
  drops anything it cannot match exactly, and its exit test checks every hit.
- **Stage C:** tool output holds what the person never typed: file contents,
  command output, API answers. Redaction is the same as for every hit, but a
  person may be surprised that a search lists it. That is why it is default off
  and labelled "Tool output".

## 10. Security and data considerations

- No stage adds a route, a token path or a store.
- Every hit passes `redact` before it is cut, and the refusal-row skip, as today.
- Incognito and temporary chats stay out of every stage.
- An app caller keeps seeing only the chats it owns (`_AppOwnedKeys`), in every
  stage.

## 11. Alternatives considered

- **Stage A as "open in a new tab" instead of "copy link".** A new tab loses the
  dialog; a link is what the pins panel and message menus already give.
- **Stage B by running a second, per-message vector query in the route.** It
  would embed every listed message on each search. The chunk store already
  holds them.
- **Stage C by indexing attachment text.** It needs file extraction, storage and
  a privacy review of its own (§4).

## 12. Open questions

1. Should Stage A also bind a key (for example ⌘C on the selected hit), or only
   the button?
2. Stage B: should a chat with only meaning hits list at all when the person
   filtered by speaker or date? This document proposes yes, with the same
   filters applied to the chunk's message.
3. Stage C: is listing tool rows that are already indexed enough, or should tool
   text leave the index for people who leave the setting off (§7)?
4. #38023 also lists the Bot filter, which today offers only the Bots seen in
   results. Listing every Bot needs the agents list; is it wanted as a small
   change of its own, outside this document?

## 13. Decision

Accepted as written on 2026-11-25 by maintainer bolichen97 in his
[review of #42015](https://github.com/kirodotdev/KiroCrew/pull/42015#pullrequestreview-6063857145)
("Bolin accepts this RFC as written"). Implementation PRs may now open
against this design.
