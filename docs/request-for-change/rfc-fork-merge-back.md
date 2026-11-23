---
title: Fork merge-back, bring a fork's results home as an edited summary card
status: draft
author: unstablebrainiac
created: 2026-11-23
last-audited: 2026-11-23
audited-at: 3b076d02ff
doc-pr:
implementation-prs: [17165, 34274, 15957]
tracking-issues: [3816]
supersedes: []
superseded-by: []
---

# RFC: Fork merge-back

- Status: draft. Acceptance is requested from a maintainer; the status flips to
  `accepted` when one records it here. The backend half is already on main:
  [#17165](https://github.com/kirodotdev/KiroCrew/pull/17165) (the two routes,
  merged 2026-11-07 by maintainer bolichen97) and
  [#34274](https://github.com/kirodotdev/KiroCrew/pull/34274) (a merged fork's
  reopen pinned to its transcript, merged 2026-11-10). The dashboard half is the
  open [#15957](https://github.com/kirodotdev/KiroCrew/pull/15957), which waits
  on this decision. No maintainer acceptance of the feature as a whole is
  recorded, so the status stays `draft`.
- Every statement about today's code was checked against `main` at
  `3b076d02ff` and is cited by file and symbol.
- Answers [#3816](https://github.com/kirodotdev/KiroCrew/issues/3816), where
  KiroCrewCaptain asked for the design through this process (2026-10-08) and a
  maintainer triage pass (bolichen97, 2026-09-25) left one question open:
  archive the fork after a merge, or keep it writable and merge incrementally.
  §8 records the answer this design takes.

## 1. Summary

A fork lets a person try something on the side without cluttering the chat
they came from. When the side trip pays off, nothing brings its result back.
This RFC records the design built for that: a fork gets **Merge into parent…**
in its session menu. The dialog drafts a short summary of the fork's new
messages, the person edits it, and **Merge** writes exactly that text into the
parent as a card. The parent's agent reads the card on its next turn. The fork
stays open, and a later merge covers only what the parent does not have yet.

## 2. Motivation

### Current state

- Forking exists (`dashboard/chat_fork.py`). A fork records its parent in
  `forked_from` and, since #34274, the parent transcript's creation stamp in
  `forked_from_created_at`.
- On main, `dashboard/chat_merge_back.py` serves
  `POST /api/chat/slots/{slot}/merge-back/draft` (`api_chat_slot_merge_back_draft`)
  and `POST /api/chat/slots/{slot}/merge-back` (`api_chat_slot_merge_back`),
  registered in `dashboard/routes/chat.py`. No dashboard surface calls them.
- In the dashboard, the parent link is display-only. Without #15957 a person
  can only copy and paste.

### Problems

1. A fork that pays off costs a manual copy and paste: find the useful
   messages, write a summary, paste it into the parent, then explain where it
   came from.
2. A skipped copy leaves the parent's agent without context the person already
   paid for. The parent then repeats work the fork did.
3. A pasted summary is sent as the person's own message and starts a parent
   turn they did not ask for.

### What the person gets

- One menu row on the fork replaces the copy and paste. The summary is drafted
  for them and ready to edit in a few seconds.
- Nothing lands until they press Merge. They read and edit the exact text the
  parent's agent will get, so a wrong or private line never leaves the fork
  unseen.
- The parent shows a card labelled "Merged from *fork* · N messages". Its link
  opens that fork, so anyone reading the parent can trace where the summary
  came from.
- The parent's agent is not started. It reads the card on the person's next
  message, so a merge never spends a turn or speaks for the person.
- The fork keeps working. Merging again later sends only the new messages.

### Use cases

**A. An investigation that finds the cause.** A person debugging a failing
deploy forks the chat to chase one hypothesis, a stale cache. Twelve messages
later the fork has the cause and the fix command. They open the fork's session
menu and pick **Merge into parent…**. The dialog shows "Summarizes 12 messages
not merged yet" and a five-line draft naming the cache key, the command and
the one test still unverified. They cut one line about a dead end and press
Merge. The dialog moves them to the parent, where the card sits at the end of
the transcript. They type "apply that fix", and the parent's agent answers
using the fork's findings without any of the fork's twelve messages in its
context.

**B. A long fork merged twice.** A person forks a planning chat to draft a
migration plan. After an hour they merge what they have: the draft covers 30
messages and the card lands. They keep working in the fork for another hour,
then merge again. The second draft covers only the 18 messages after the first
merge, because the cursor (`plan_merge`, `_through_cards`) skips the range the
parent's first card already holds. The parent ends up with two cards in order,
each naming its own message count, and no repeated content.

**C. The parent moved on, or closed.** A person forks, works for a while, and
meanwhile keeps talking in the parent. When they merge, the drafted text ends
with one sentence: "Messages sent here after the fork was made were not seen by
the fork." That tells the parent's agent the fork's view is older than its own.
If they closed the parent before merging, the draft is refused as
`parent_not_open` and the dialog offers **Open the parent chat**, which
reopens it pinned to the transcript the fork was copied from. If a new chat now
holds the parent's key, the reopen is refused instead of merging into the
wrong conversation.

## 3. Goals

- Bring a fork's results back into the chat it was forked from, as a summary
  that chat's agent reads on its next turn.
- The person approves the exact text before anything is written.
- A merge is visible in the parent, traceable to its fork, and survives a
  gateway restart.
- Repeated merges from one fork never send the same messages twice.

## 4. Non-goals

- Merging raw messages, a three-way merge, or a parent into a fork.
- Archiving or locking the fork after a merge.
- Undoing a merge.
- Merge-back for app tokens, agent tools or internal callers: both routes
  refuse a non-dashboard caller (`_non_dashboard_refusal`).
- Merging into a parent that takes turns outside the dashboard (a channel
  thread, a direct message, a scheduled job).
- Merging into a closed parent without opening it.

## 5. Design

### 5.1 Entry point

`MergeBackMenuItem` adds **Merge into parent…** to the session menu of a fork
only. It is greyed with its reason for a fork running a turn, an incognito or
temporary fork, or a parent outside the dashboard, so the person learns why
before any request.

### 5.2 Draft

The dialog calls the draft route. The route finds the fork messages the parent
does not hold yet, asks the background model for a hand-off note (`_PROMPT`,
at most 250 words, run through `run_bg_oneliner` at the `background` role),
redacts it (`_redacted`, `redact_via_context`) and returns `summary`,
`through` (the last covered message), `digest` (`covered_digest`, a hash of the
covered rows), the covered and remaining counts, and the parent key. The model
reads at most 40,000 characters of input (`_MAX_SUMMARY_INPUT_CHARS`), so a very
long fork is drafted in parts and the dialog says how many messages are left
for the next merge.

### 5.3 Edit and merge

The dialog opens the draft plus the gap sentence (use case C) in a text box with
a character count against `MAX_MERGE_SUMMARY_CHARS` (4,000, the deferred-note
bound). Draft again and closing ask before losing edits. Merge stays off while
the text is empty or only the gap sentence. Merge sends the text, `through` and
`digest`. The merge route refuses, before writing, a draft that no longer
matches the fork (`merge_draft_stale`), a range another merge already covered
(`already_merged`), a running fork, and a parent that is closed, deleted or
cannot be proven to be the one forked from (`_parent_identity`). Merges into
one parent take turns (`_merge_lock`), so two tabs cannot land the same range.

### 5.4 Delivery

The card is written through the existing `/note` path (`deliver_note`,
`dashboard/chat_api/slot_note.py`) as a visible `inject` row whose
`meta.mergedFrom` names the fork, its title, the message count, the covered
range and the fork transcript's identity. That row is in the parent's
transcript, so the card survives a restart, which closes the durability gap the
2026-09-25 triage found in #4904. The context half reaches the parent's agent
on its next turn. A parent mid-turn holds the card until the turn ends, and the
dialog says so.

### 5.5 The card in the parent

`MergedFromLabel` shows "Merged from *fork* · N messages" above the summary.
The title is plain text. Its link resumes the fork with `expected_created_at`
set to the card's recorded identity, so a chat later created on the fork's key
is refused rather than opened.

## 6. Migration plan

1. **Backend routes.** Shipped in #17165. Exit: both routes answer a dashboard
   fork and refuse every case in §5.3; `test/test_chat_merge_back*.py` pass.
2. **Pinned reopen.** Shipped in #34274. Exit: a resume with a mismatched
   `expected_created_at` answers 409 `resume_identity_mismatch`.
3. **Dashboard.** #15957, blocked on this RFC's acceptance. Exit: the menu row,
   the dialog and the card ship with their tests; the dialog sends exactly the
   approved text; the card's link opens only its fork.

## 7. Backward compatibility

Additive. Phase 3 adds UI on routes main already serves and one slot-list field,
`forked_from_created_at`, which an older client ignores. A merge card is an
`inject` row, which every reader already handles. No route, key or record is
removed or tightened.

## 8. Decisions this records

#3816 collected four design answers on 2026-08-21. This design keeps three and
reverses one:

| Question | Answer taken | Why |
|---|---|---|
| Visible block or hidden context? | Visible card the agent also reads | The person sees what landed; the agent gets it next turn |
| Archive the fork after a merge? | No: the fork stays writable, merges are incremental | Archiving made #4904 one-shot; its review asked for incremental merges |
| Summary or raw messages? | Summary only, edited by the person | Keeps the parent's context small; the fork holds the full record |
| Where does it land? | Appended at the parent's tail, with a gap sentence | Reconciliation is the agent's job, told by one sentence |

## 9. Security considerations

- Both routes accept only the dashboard caller and answer a slot the caller
  may not see with the same not-found reply as a missing one
  (`_slot_not_found`), so an app token cannot read a fork or write a parent.
- The draft is redacted before it reaches the dialog, and the text the person
  merges goes through `deliver_note`'s own secret check, which refuses
  credential-shaped text as `context_secret_shaped`
  (`dashboard/chat_api/slot_note.py`).
- The fork's messages go to the background model as data; the prompt tells it
  never to follow instructions inside them.
- Incognito and temporary forks are refused (`merge_back_restricted`), since
  nothing may be derived from them.
- The card's fork title is rendered as plain text, and `mergedFromOf` checks
  the type and bound of every `mergedFrom` field before it renders.

## 10. Costs and downsides

- **Size.** The dashboard half adds about 3,900 lines of text across 51 files,
  plus committed screenshots. About 1,100 of those lines are product code; the
  rest are tests and the locale catalogs. It is one more dialog, card and error
  mapping to keep in step with the routes.
- **A merge cannot be undone.** The card can only be ignored, or answered in the
  parent. A wrong summary needs a correction message.
- **The summary loses detail.** The parent's agent sees only what the person
  kept. It can be pointed at the fork, but it does not read it by itself.
- **Model cost per draft.** Each draft and each Draft again is one background
  model call.
- **The fork stays open.** A person can merge, keep going, and forget the parent
  never got the later part. The menu row stays available, but nothing reminds
  them.
- **Busy parent.** A parent whose context queue is full refuses the merge
  (`merge_queue_full`) until the person sends it a message.

## 11. Alternatives considered

- **Archive the fork on merge** (#4904). Simpler cursor, but one-shot: the fork
  cannot be continued. Rejected in that PR's review.
- **Paste the summary as a user message.** Speaks in the person's name and
  starts a turn they did not ask for.
- **Start a parent turn with the summary.** Spends a turn unasked and can act
  before the person returns to the parent.
- **Copy raw fork messages into the parent.** Floods the parent's context and
  repeats what the fork already stores.

## 12. Open questions

- Should a fork show a hint when it has new messages since its last merge? Not
  built; left for a follow-up if people forget to merge again.
