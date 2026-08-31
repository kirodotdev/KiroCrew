---
title: Transcript Section Markers — chapter breaks for one-at-a-time work
status: in-progress
author: rnoack
created: 2026-08-29
last-audited: 2026-08-31
audited-at: 079a5de22
doc-pr: 7033
implementation-prs: [7163]
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Transcript Section Markers — chapter breaks for one-at-a-time work

- Status: in-progress — Phase 1 (the event, the directive, the applier, and a
  renderer in both paths) is implemented in
  [#7163](https://github.com/kirodotdev/KiroCrew/pull/7163). Phase 1 carries no
  collapse behaviour, deliberately. Phases 2–4 remain proposals; phases 3 and 4
  are blocked on open questions.
- Author: rnoack
- Created: 2026-08-29
- Verified on `main` at `202770d13`. Every `file:line` below was resolved against
  that commit. Phase 1's implementation was written against `079a5de22`, by which
  point main had moved and several of those line numbers had shifted — resolve by
  symbol rather than by line.
- Related: `rfc-append-only-session-transcript.md` (proposes revision records for
  the same transcript this RFC adds a row type to; the two are independent —
  this one adds a row, that one changes how rows are written),
  `rfc-tool-derived-diff-cards.md` (the nearest precedent for promoting a
  transcript-derived affordance into the dashboard),
  [#6853](https://github.com/kirodotdev/KiroCrew/pull/6853) (merged — exposes
  `reset_conversation` as a boundary-deferred session directive; the context-side
  counterpart to this RFC's view-side marker, see §2.3).

## 1. Summary

Add a **section marker**: a transcript row a caller writes at the seam between
two units of work, which the dashboard renders as a labelled rule and above which
earlier rows are collapsed by default, with one click to expand. The record stays
whole on disk; only the default viewport changes.

Exposed as an MCP tool via the existing session-directive mechanism, so an agent
walking a list can mark the boundary itself as it advances.

## 2. Motivation

### 2.1 Current state

A long-lived chat session is sometimes not one conversation but a *sequence of
independent units of work* handled one at a time: review this item, finish it,
move to the next. Each unit has a beginning and an end, and once a unit is done
its transcript has no further value to the reader — they are working on the next
one.

The transcript is a single unbroken scroll. When unit *N+1* starts, the reader is
looking at the tail of unit *N*, and the only way to get a clean working surface
is to scroll past everything or open a new session.

### 2.2 What exists today, precisely

- There is **no divider or rule row** in the transcript for any purpose. Every
  `border-t`/`border-b` in the chat surface belongs to a card or panel
  (`pages/chat/ChatNavPanel.tsx:76`, `SidePanel`, `SubagentCompletionCard`), not
  to a message row.
- There **are** collapse affordances, but all are *intra-turn*:
  `pages/chat/CollapsibleToolGroup.tsx`, `pages/chat/TurnBlock.tsx` (used at
  `pages/ChatPage.tsx:7554`), `pages/chat/ToolCallLine.tsx`. None can express
  "collapse everything before this point".
- `pages/chat/EarlierMessagesBar.tsx` (rendered at `pages/ChatPage.tsx:7528`,
  gated on `slotHasMore`) looks like the wanted control but is a **server-side
  pager** for history not yet in the client, not a fold of loaded rows.

### 2.3 Why `reset-conversation` is not this feature

`POST /api/chat/slots/{slot}/reset-conversation` (route
`dashboard/routes/chat.py:75`, handler `dashboard/chat_handlers.py:3498`) gives
the slot a fresh *model* conversation while keeping the slot open. With
`{"replay": false}` it also suppresses the `[CONVERSATION HISTORY]` re-injection
that would otherwise rebuild what was just discarded
(`session_lifecycle.py:653`). It does not
solve this, for two independent reasons.

**It deliberately does not touch the view**, per its own docstring
(`chat_handlers.py:3538-3542`):

> The transcript is deliberately left in place, which means the tab still shows
> the earlier messages while the model no longer remembers them. That is the
> honest rendering of what happened — the record is the user's, the context was
> the conversation's.

**It cannot be called from inside a turn.** It is a full provider teardown, so it
answers 409 on four guards: `provider.has_active_turn()`
(`chat_handlers.py:3589`), `slot.running` (`:3595`), `slot._in_stage_execution`
(`:3604`), and attached sub-agents (`:3616`). A caller that has just finished item
*N* is, by construction, mid-turn.

The two compose rather than compete: **mark the section for the view, reset the
conversation for the context.**

That pairing is not hypothetical. [#6853](https://github.com/kirodotdev/KiroCrew/pull/6853)
has landed on `main`, adding `reset_conversation` as a session directive and MCP
tool, and it reaches the effect the four guards above block by **queuing the
discard for a later turn boundary** rather than applying it inline — the same
shape this RFC uses for a marker (§5.4, §5.5). Its own motivation is this
document's use case: *"use when a session walks a list of independent items one
at a time … and carrying item N's context into item N+1 buys nothing but
tokens."*

So the context half is already self-service from inside the turn that wants it;
adopting this RFC makes the view half self-service too, through one
boundary-deferred directive each. Two differences worth keeping straight:
`reset_conversation` is user-surface gated
(`_USER_SURFACE_DIRECTIVES`) and so also works on messaging channels, whereas a
section marker is inherently dashboard-only because it is a rendering; and the
two effects are independent — a caller may want a clean view without a clean
context, or the reverse.

This RFC does not depend on #6853. The view half stands on its own; #6853 having
landed makes the pairing available, not required.

## 3. Goals

1. A caller can mark a boundary between units of work, with a label.
2. The dashboard collapses everything above the latest boundary by default, and
   expands on one click.
3. The record is never destroyed or rewritten; collapsing is a view decision.
4. The marker never enters a model's prompt.
5. Neither an old client reading a new transcript nor a new client reading an old
   transcript breaks.

## 4. Non-goals

- **Changing model context.** Discarding what the model remembers is
  `reset-conversation`'s job (§2.3). This RFC touches only what is rendered.
- **Fixing `exclude_last_n`.** §11 open question 1 records a discrepancy between
  a docstring and the code on a hot path. Naming it is in scope; changing it is
  not.
- **A general client-state channel.** The event schema is closed (§5.2) and
  §10 argues the boundary this depends on.
- **Automatic or inferred markers.** No heuristic insertion. The boundary is an
  assertion by the writer, never a guess by a reader (§10).

## 5. Design

### 5.1 A new transcript role, not a `cls` and not `inject`

Proposed: a new role, `section_marker`, carrying its label in `meta`.

**Why not `cls` on an existing role — two writers, two rules.** The dashboard's
serializer persists `cls` *only for system rows*:

```python
cls_val = m.get("cls", "")
if role == "system" and cls_val:
    entry["cls"] = cls_val
```

— `dashboard/chat_persistence.py:1914-1916`. Meanwhile `ConversationLog.append`
persists any truthy `cls` (`history.py`, `append`). A `cls`-carried marker on any role
other than `system` is silently dropped on the dashboard's persist path. `meta`
persists for every role (`chat_persistence.py:1917-1918`), which is where the
label belongs.

**Why not `role="system"`** (the one role whose `cls` survives) — it is in the
SDK's `undrawn` set (`app-sdk/messageRenderers.tsx:349`) and so renders nothing
there, while `ChatPage` has no `system` branch and would drop it into the
assistant-bubble fallback (`pages/ChatPage.tsx:6564`). Reusing it means
un-picking a deliberate "carries state, not something to read" classification.

**Why not `role="inject"`** (what `/note` writes) — two concrete harms. It is in
`RECALL_ROLES` (`context.py:61`), so every marker would be replayed into the model
as conversation content; a chapter break must not enter the prompt. And it is in
`_PROMPT_ROLES` (`dashboard/state.py:2069`), so a marker would rank as an inbound
prompt for the sidebar's `last_turn_ts`, making a session look freshly asked-of
every time a break was drawn.

A new role is cheap on the wire: `ChatMessage.role` is an open `string`
(`website/src/types/index.ts:950-952`), and the transcript read path has no role
allowlist — `_read_messages_locked` skips only the `_type: "metadata"` header and
appends every other JSON line verbatim (`history_projection.py`,
`TranscriptReadProjection`).

One thing to *not* do: leave the new role out of `_QUESTION_RETIRING_ROLES`
(`dashboard/state.py:2063`, currently `{user, nudge}`, mirrored by the frontend's
`QUESTION_RETIRING_ROLES`). A marker must not retire a pending question card.
Default behaviour is already correct; this is a note against a well-meaning later
edit.

### 5.2 Event shape

```jsonc
{
  "role": "section_marker",
  "content": "— End of: <label> —",   // human-readable fallback, see §8
  "ts": "2026-08-29T20:41:12.184301+00:00",
  "meta": {
    "mid": "…",                        // minted by Slot.append as for any row
    "label": "<label>",                // optional, ≤120 chars
    "source": "review-walk"            // optional, provenance for display
  }
}
```

**This schema is closed on purpose.** `label` and `source` are the only
caller-supplied fields, and the scope argument in §10 depends on it staying that
way — no styling fields, no arbitrary payload, no post-write mutation. Adding a
field later should require re-arguing that section.

### 5.3 The marker carries a label

An anonymous rule tells the reader a boundary exists; a labelled one tells them
*which*, which is the difference between a usable table of contents and a set of
unmarked page edges. The label is also the natural anchor text for the collapsed
summary ("3 earlier sections — …"). Optional, so a caller with nothing meaningful
to say can still draw a plain break; capped, so it cannot be used to smuggle a
paragraph of prose into a structural row.

### 5.4 Mid-turn writes must be deferred

A marker needs no teardown, so none of `reset-conversation`'s four guards apply.
That does **not** make a mid-turn transcript append safe, and the reason is worth
stating precisely because it is not obvious.

When a turn builds its prompt, the runner passes `exclude_last_n=1` so the current
turn's user message is not fed back as history. Two call sites:
`build_session_replay` on a cold start (`dashboard/chat_runner.py:5588`, reached
only under `if is_new and not _provider_has_history …` at `:5558`), and
`build_message` on **every** turn (`:5731`), which forwards it to `_recall_rows`
(`context.py:2314`) and `recent_with_provenance` (`:2526`). So the window this
concerns is not cold-start-only. In both cases the exclusion is a **raw positional
slice applied before role filtering**:

```python
messages = conversation_log.read_messages_chained(session_key)
if exclude_last_n > 0:
    messages = messages[:-exclude_last_n]
```

— `context.py:1524-1525` (`_replay_rows`), identically at `:1571-1572`
(`_recall_rows`), and `history.py` (`recent`, whose docstring at
`:2950` says "drops that many trailing raw entries BEFORE role filtering").

So any row appended after the current-turn user row becomes the physical tail,
absorbs the exclusion, and the user message survives the slice and is replayed —
sending the request twice. **Making the row recall-ineligible does not help:**
`RECALL_ROLES` membership (`context.py:61`) governs only whether the row itself is
replayed, not which row the positional slice removes.

This is why `/note` defers its visible line, and the machinery already exists:
`Slot.flush_deferred_notes` (`dashboard/state.py:3953`), held when
`slot.running or slot._in_stage_execution` (`chat_handlers.py:7082`), capped at
`_MAX_DEFERRED_NOTES = 10` (`chat_handlers.py:6534`), flushed at the seams that
already call it (`chat_runner.py:3988`, `:4406`; `chat_orchestrator.py:244`,
`:839`). **Reuse it; do not add a second notion of "held".**

**With one carve-out, added after review: the reuse holds for an in-flight turn
but NOT inside multi-stage plan execution, which is refused rather than
deferred.** `/note`'s gate defers on `slot.running or slot._in_stage_execution`,
but the per-cycle flush is itself gated `not slot._in_stage_execution`, and that
flag is set once around the WHOLE stage loop rather than per stage turn. So
inside a plan the shared hold stops being a turn-boundary hold and becomes an
end-of-PLAN hold: a marker emitted during stage 2 of 5 would land with every
other marker the plan accumulated, after it exited — the opposite of the turn
separation this proposal is for, while quietly consuming the shared per-turn cap
across stages. Admitting markers there needs a per-stage flush seam that does not
exist; that is a change to the plan lifecycle, out of scope here.

That is worth stating precisely, because a second one already exists:
[#6853](https://github.com/kirodotdev/KiroCrew/pull/6853) defers its discard
through `slot._pending_discard_conversation_key`, consumed at a boundary in
`chat_runner.py`, not through `_deferred_notes`. The two are not obviously
mergeable — one holds a row to append, the other a teardown to perform, and they
wait on different conditions (`#6853` also waits on sub-agents running, queued,
or delivering). So a marker should reuse the note path rather than invent a
third, and whether the two boundary mechanisms should converge is left as an open
question (§11) rather than answered here.

Two qualifications. It is a **race**, not a certainty — the appended row must reach
disk via the periodic flush before the read; the comment at
`chat_runner.py:5574-5578` sizes that window for the cold-start path. And the
`flush_deferred_notes` docstring explains the hazard through recall-eligibility
rather than position (§11 open question 1), which is why this section derives it
from the slice instead.

**Deferral is acceptable here — arguably correct.** A marker held to the turn's
end lands after the closing message for item *N* and before anything for *N+1*,
which is where a chapter break belongs. The honest limitation: if one turn walks
several items, every marker it emits clumps at the turn's end. **A section marker
separates turns, not intra-turn regions**, and the tool description must say so.

### 5.5 Wiring: a session directive, not a loopback route

The right precedent is not `/note`. It is `suggest_followup` / `ask_question`,
which are **session directives**: the MCP tool returns an encoded marker in its
own result string, and the turn loop decodes and applies it in-process.

- Registry: `DIRECTIVE_TOOLS` (`session_directive.py:55`) — add `section_marker`.
- Tool side: return `session_directive.encode(kind, validated_args, human)`
  (`session_directive.py:107`); see `mcp_tools/control.py:733` for the pattern.
- Consumer side: `chat_runner.py` decodes at the tool-result event (`:6440`) and
  calls `apply_session_directive` (`:6491`).
- Applier: a new branch in `dashboard/session_directive_apply.py` alongside
  `_suggest_followup` (`:453`), dashboard-gated via `_DASHBOARD_ONLY_DIRECTIVES`
  (`:62`).

Why this over an HTTP route, in the module's own words
(`session_directive_apply.py:9-13`):

> Effects run IN-PROCESS via the same cores the HTTP endpoints call (no loopback
> HTTP, no user-token dance): the consumer is the authoritative session, so
> cross-session misattribution is unrepresentable.

**Explicitly rejecting `/note` reuse.** `/note` always performs *two* writes: a
visible `inject` row and a `_pending_context` entry drained into the next user
message (`chat_handlers.py:6982-6993`). The context half is the one thing a marker
must never do. There is no visible-only mode, and the docstring says why — "there
is no visible-only mode, because no caller wanted one"
(`chat_handlers.py:6988-6989`). Adding one would graft a second, quieter feature
onto an endpoint whose contract is "both writes always happen".

### 5.6 MCP tool signature

```
section_marker(label?: string) -> string
```

- `label` — optional, ≤120 chars. Line breaks and tabs (`\n`, `\r`, `\t`) are
  rejected by the schema: the label is drawn as a one-line rule caption, so a
  newline in it splits the caption. This is a separate guard from the shared
  hidden-category sweep, which strips zero-width and similar characters but passes
  ASCII whitespace controls through.
- Phase 1 ships no collapse field. A `collapse_earlier?: boolean = true`
  parameter was specified here originally, but Phase 1 is "the event and the rule
  (no collapse)" and nothing in it reads such a flag — so recording it on every
  row would ship Phase 2's surface with zero consumers. Phase 2 adds it, treating
  an absent key as true, which is byte-compatible with every row Phase 1 wrote.
- Returns a human-readable confirmation. Per the directive contract it must not
  over-claim: the effect is applied by the consumer *after* the model has already
  received this string, and may be refused
  (`session_directive_apply.py:24-29`). So: "Section break queued; it will appear
  at the end of this turn."

### 5.7 Rendering

**Both renderer paths must be taught the role.** There are two transcript surfaces
and **they disagree on the unknown-role fallback**, which makes this a hard
requirement:

- `pages/ChatPage.tsx` dispatches on role through `renderMessage` (`:6432`); an
  unrecognised role falls through to the final `else` and is drawn as an assistant
  markdown bubble (`:6564`).
- The SDK path (`components/ChatPane.tsx`, SideChat, embed) resolves via
  `resolveRenderer` (`app-sdk/messageRenderers.tsx:382`) and
  `if (!entry) return null` (`app-sdk/ChatMessageList.tsx:181`) — an unclaimed
  role draws nothing.

So register in both: a default renderer in `app-sdk/messageRenderers.tsx`
(alongside the `notice` entry at `:338`) and a branch in `renderMessage`. Host
overrides, if any, in `pages/chat/transcriptRenderers.tsx` (existing entries
`:106-250`).

**Collapsed (default)**, at the top of the scroll region:

```
┌────────────────────────────────────────────────┐
│  ⌃  3 earlier sections hidden                  │
│     first-item · second-item · third-item      │
│                                  [show earlier]│
└────────────────────────────────────────────────┘
```

**Expanded**, each marker inline:

```
──────────────  second-item  ──────────────
```

Rows above the last marker with `collapseEarlier` are hidden. One summary bar,
not one per marker — a walk of thirty items must not produce thirty stacked bars.

### 5.8 Collapse state: derive, do not store

Every existing disclosure affordance is ephemeral React state held above the row
so it survives virtualizer remount: `turnDisclosure` (`pages/ChatPage.tsx:996`)
and `toolDisclosure` (`:1003`), both reset on slot switch (`:1009`). Nothing per-row
is persisted. The one persisted knob nearby is `collapseAllSteps`, a global
setting in `pages/chat/ChatSettings.tsx:30` (default at `:61`).

Proposal: compute the collapse *default* from the transcript itself — the last
qualifying marker — so it is correct on first paint with nothing to persist and
nothing to migrate. Only the user's *override* need be remembered, and only if it
should survive reload. If it should, the precedent is
`website/src/hooks/usePersistedBool.ts` (localStorage via `safeSetItem`, same-tab
`mc:persisted-bool` broadcast plus cross-tab `storage` sync), keyed with the
slot-scoped convention from `hooks/useScrollMemory.ts:32`
(`scrollMemoryKeyFor(slot, tabId)`, separator `\u001F` at `:29`).

Recommendation: do not persist in the first cut. On reload the reader is almost
always returning to the current item, which is what the derived default shows.
See §11 open question 2.

**On reload**, markers arrive with the transcript through both existing doors —
the HTTP slot-detail rebuild (`store/chatSlice.ts:1448 fetchSlotDetail`, reducers
`hydrateSlotMessages` `:3426` / `replaceMessages` `:3411`) and the live
`chat_message` websocket frame (`hooks/useWebSocket.ts:1229`) — and the default
viewport is recomputed. No new transport.

**One interaction to resolve.** `EarlierMessagesBar` already occupies the top of
the transcript when there is unloaded server history (`pages/ChatPage.tsx:7528`).
A "show earlier sections" bar would sit in the same place with a similar label and
a different meaning. Both can be true at once. Merge them into one progressive
control or differentiate the wording sharply; do not ship two similar bars
stacked.

### 5.9 Persistence

No new storage. `Slot.append(role=…, content=…, meta=…)`
(`dashboard/state.py:3564`) → in-memory window → `_build_message_entry_uncached`
(`dashboard/chat_persistence.py:1868`) → the session jsonl. `meta` persists for
all roles (`:1917-1918`), carrying the label. The role is not in the
never-persisted transient set `{chunk, done, streaming, queued, permission}`
(`dashboard/state.py:2018`).

## 6. Phases

What changes, at a glance:

| Layer | Change |
| ------------------ | ---------------------------------------------------------------------- |
| MCP tool | new `section_marker(label?)` in `mcp_tools/` |
| Directive registry | add to `DIRECTIVE_TOOLS` (`session_directive.py:55`) |
| Applier | new branch in `dashboard/session_directive_apply.py` (near `:482`) |
| Append path | reuse `/note`'s deferral (`dashboard/state.py:3953` flush, existing seams) |
| Persistence | none — `meta` already persists for all roles |
| Recall | none — the role stays out of `RECALL_ROLES` (`context.py:61`) |
| Frontend | renderer in `app-sdk/messageRenderers.tsx` **and** `pages/ChatPage.tsx:6432` |
| Frontend | collapse default derived in the turn grouper (`createTurnGrouper`, `pages/chat/groupDisplayItems.ts:244`) or in `displayItems` (`pages/ChatPage.tsx:5740`) |
| Frontend | summary bar component, sibling to `pages/chat/EarlierMessagesBar.tsx` |

Each phase is independently shippable and independently abandonable. Exit criteria
are assertions, not intentions.

### Phase 1 — the event and the rule (no collapse)

Add the role, the directive, the applier, and a renderer in both paths that draws
an inline labelled rule. No collapse behaviour.

Ships alone: a labelled divider is useful by itself in a long session.

Exit criteria:
1. A `section_marker` written through the tool appears as a labelled rule in
   `ChatPage` **and** in `ChatPane`.
2. The row is present in the session jsonl with its label under `meta`, after a
   flush and a gateway restart.
3. A marker requested while `slot.running` is true is written after the turn's
   final assistant row, not before it.
4. `build_session_replay` and `_recall_rows` return no `section_marker` content
   for a session containing markers.
5. Requesting 11 markers in one turn returns `429 deferred_notes_full` on the
   eleventh (inherited from the existing cap).

### Phase 2 — default collapse and the summary bar

Derive the default viewport from the last marker with `collapseEarlier`; add the
summary bar and the expand toggle.

Abandonable: Phase 1 remains a coherent shipped feature without it.

Exit criteria:
1. With N≥1 markers, the initial render shows only rows after the last
   qualifying marker, plus one summary bar naming the hidden sections.
2. Expanding shows every hidden row and every marker as an inline rule.
3. Reload returns to the collapsed default.
4. A session with zero markers renders byte-identically to today.
5. The summary bar and `EarlierMessagesBar` never render as two similar stacked
   bars (§5.8).

### Phase 3 — persisted expand override *(blocked on open question 2)*

Remember that a reader expanded a section, across reload.

Do not start until a maintainer has answered whether this is wanted and where the
state belongs.

Exit criteria:
1. Expanding, reloading, and returning to the same slot shows the expanded view.
2. Clearing browser storage returns to the derived default with no error.
3. No stored key survives a slot's deletion.

### Phase 4 — HTTP route for non-agent callers *(deferred; no demand yet)*

`POST /api/chat/slots/{slot}/section`, mirroring `/note`'s auth and
re-authorization shape (`_check_slot_app_ownership` at `chat_handlers.py:7040`,
`_reauthorize_after_await` at `:7070`), body `{label?, collapseEarlier?}`,
returning `{"ok", "appended", "visibleDeferred"}`.

Listed so the directive design does not foreclose it. Not proposed for the first
cut — no caller wants it today.

Exit criteria:
1. The route produces a row indistinguishable from the directive path's.
2. A foreign app token receives the same indistinguishable 404 `/note` gives.

## 7. Success criteria

The feature has succeeded when a reader working a sequence of items in one session
sees only the current item on arrival, can reach any earlier item in one click,
and nothing was deleted to achieve it.

## 8. Backward compatibility

**Old frontend, new transcript.** Neither path throws. The SDK path draws nothing
(`app-sdk/ChatMessageList.tsx:181`); `ChatPage` draws the row's `content` as an
assistant bubble (`pages/ChatPage.tsx:6564`). The second is cosmetically wrong but
not broken — and it is why §5.2 puts a human-readable string in `content` rather
than leaving it empty or stuffing JSON there. An old client shows
`— End of: second-item —` as a stray line, which is a legible degradation.
**`content` is the compatibility surface; `meta` is the machine surface.**

**New frontend, old transcript.** No markers, so nothing collapses and the view is
byte-for-byte today's. The derive-only approach in §5.8 has no stored state to be
absent.

**Model side, either direction.** The role stays out of `RECALL_ROLES`
(`context.py:61`), so markers are never replayed by `_replay_rows` (`:1512`) or
`_recall_rows` (`:1552`) — an old session resumed by a new gateway, or the
reverse, sees no prompt change.

**Read path.** No role allowlist (`history_projection.py`,
`TranscriptReadProjection`), so an older gateway
reading a transcript containing markers parses them as ordinary rows.

## 9. Security considerations

**The label is caller-supplied text that persists and renders.** It inherits the
existing write-boundary redaction rather than needing its own: because the role is
not `user`, `_build_message_entry_uncached` runs `redact_exfiltration_urls` and
`redact_credentials` over `content` (`dashboard/chat_persistence.py:1884-1886`),
and `meta` goes through `_redact_meta_for_role`
(`dashboard/chat_utils.py:1367`, called at `chat_persistence.py:1918`). Validation
is still required at the boundary — length cap and control-character rejection
(§5.6) — and the rendered label must go through the same markdown/escaping path as
other row content rather than being injected as markup.

**Authorization comes from the directive path, not from arguments.** The applier
acts on the session the turn belongs to, never a key supplied by the caller
(`session_directive_apply.py:9-13`), so a marker cannot be written into a foreign
session. If Phase 4 adds the HTTP route, it must carry `/note`'s ownership check
and its post-body re-authorization (`chat_handlers.py:7040`, `:7070`) — the second
exists because the body read is an await long enough for a slot rebind.

**Audit.** Directive application emits a SEL tool-invocation event through
`_audit` (`session_directive_apply.py:79`), which a new branch inherits;
`apply_session_directive` (`:100`) derives the outcome from the applier's return
so a refusal is not audited as success.

**Resource bounds.** The deferral cap `_MAX_DEFERRED_NOTES = 10`
(`chat_handlers.py:6534`) already bounds held rows per turn; markers share it, so
a caller cannot flood a turn. No new unbounded structure is introduced.

## 10. Alternatives considered and rejected

**Keep the marker out of the record entirely — view-only client state.** This is
the objection to expect first. *A chapter break is a view concern, so why is it
being written to the persisted transcript?* Four reasons, in decreasing order of
how much they should settle it.

*1. The seam is information only the writer holds.* Nothing in the transcript
marks where one unit of work ended and the next began. The process doing the walk
knows; a client reading the rows afterwards does not. Any purely client-side fold
would have to **infer** the boundary — from a timestamp gap, a content pattern, a
turn count — and every such heuristic is wrong on the cases that matter (one item
that took four turns, two items handled back to back in one). This is not a
rendering preference a client can compute; it is an assertion about the work.

*2. The record already carries far more non-conversational rows than
conversational ones.* A view-only event is not a new category. `RECALL_ROLES` is
`{user, assistant, inject}` (`context.py:61`), and the only roles excluded from
the transcript are the transient/streaming set
`{chunk, done, streaming, queued, permission}` (`dashboard/state.py:2018`).
Everything else persists *and* is never replayed to a model — `tool`, `error`,
`subagent`, `system`, `file`. Measured across a sample of 60 dashboard transcripts
(5,664 rows): **3,152 rows — 55.6% — are persisted and non-recall-eligible**, of
which 3,063 are `tool`. By row count the transcript is already majority a record
of *what happened in the tab* rather than *what the model was told*. A
`section_marker` row joins the larger group, not a new one.

*3. Client-only state cannot reach the surfaces that need it.* There are two
render paths (§5.7) and two delivery doors (§5.8). Ephemeral client state is
invisible to a second window, to the other surface, and to a reload — the
disclosure state that already works this way is reset on slot switch
(`pages/ChatPage.tsx:1009`) and lost on refresh. For a walk that runs for hours the
boundary must survive all three.

*4. A parallel store is strictly worse.* A sidecar keyed to transcript positions
drifts the moment the transcript is trimmed, rotated, or forked, and no existing
reader knows to consult it. The transcript already has durable per-row identity
(`meta.mid`, minted in `Slot.append`) and ordering guarantees
(`monotonic_transcript_ts`) that such a store would have to reinvent.

**The legitimate half of that objection is scope**, and the answer is a boundary
stated up front rather than a promise. A `section_marker` row carries **only** an
optional capped label and an optional source string (§5.2). It must never accrue
styling or presentation fields, arbitrary caller payloads, an uncapped label, or
any field a client mutates after the write — those would turn one structural event
into a general client-state channel smuggled through the transcript. Reviewers
should hold the schema to that, and a later field addition should have to re-argue
this section.

**Have the agent print `---` in its message.** This already draws a rule today:
`MarkdownRenderer` maps a markdown thematic break to `<hr>`
(`website/src/components/MarkdownRenderer.tsx:984`), so an assistant message
containing `---` renders a horizontal line with no new code at all. It is the
cheapest thing that produces the *pixels*, and it is worth saying why it is not
the feature. The rule is inside one message's content, so there is no row to
anchor a collapse against, no label with any structure to it, and nothing any
other reader can recognise — an export, a summary pass, or a second surface sees
prose containing three hyphens, indistinguishable from a rule the user typed or a
horizontal line in quoted output. It also cannot express `collapseEarlier`. A
marker is wanted because the boundary needs to be *addressable*, not merely
visible.

**Hard-clear or truncate the transcript.** Simplest and the wrong shape. The
codebase is consistent that the record is not the agent's to destroy:
`reset-conversation` refuses to touch it on purpose
(`chat_handlers.py:3538-3542`), and `SessionManager.discard_conversation`
(a façade at `session.py:2059` over `session_lifecycle.py:653`) is *discard*
rather than *destroy* — `clear_sid` keeps the
session-map entry so channel linkage survives and stashes the dropped sid as
`discarded_sid` "so the operation is diagnosable and manually reversible"
(`session_map.py:964-977`). A destructive design fights that grain, and the
information is genuinely wanted sometimes: a reader who notices a mistake three
items later needs the earlier section.

**A fresh session per item.** Gives a clean view for free and loses everything
else: the sequence stops being one thing, the session list fills with one entry
per item, and any view over sessions is flooded. It also pays session-start cost
per item.

**Expose `reset-conversation` to agents instead.** Does not do the job (§2.3): it
leaves the view untouched by design, and its four 409 guards
(`chat_handlers.py:3589`-`:3616`) make self-service mid-walk impossible.

**Reuse `/note`.** Rejected in §5.5: it always writes a `_pending_context` entry,
its row is recall-eligible and prompt-ranked, and it has no visible-only mode by
deliberate choice.

**`cls` on an existing role.** Rejected in §5.1: `cls` survives the dashboard's
serializer only for `role == "system"` (`chat_persistence.py:1914-1916`), so the
marker would vanish on one of the two write paths.

## 11. Open questions

1. **`exclude_last_n`: positional or role-aware?** `flush_deferred_notes`'s
   docstring (`dashboard/state.py:3956-3961`) explains the hazard in terms of
   recall-eligible rows; the code slices raw-positionally (`context.py:1525`,
   `:1572`; `history.py`, `recent`). Making the exclusion skip the last
   *recall-eligible* row would match the stated intent and make mid-turn appends
   safe outright, retiring the deferral machinery for both `/note` and this
   feature. **Not proposed here** — it changes a hot path on behalf of a view
   feature, and per CONTRIBUTING a fix should not wait on a design document.
   Someone should decide whether the docstring or the code is the intended
   contract; it is a separable issue.
2. **Persist the expand override, or derive only?** §5.8 recommends derive-only
   for the first cut. If it should persist, is localStorage per slot+marker the
   right home, or does this belong in a per-slot server-side UI state that does
   not exist yet? **Phase 3 is blocked on this.**
3. **One summary bar or per-marker folds?** §5.7 assumes one bar plus inline
   rules. A per-marker accordion is more discoverable and much noisier.
4. **Do markers belong in derived surfaces?** Session summaries, exports, and the
   `/summary` path all read the transcript, and a chapter break is arguably a
   useful structural hint for a summarizer. Not determined from the code whether
   those readers need an explicit skip or would benefit from seeing them.
5. **Virtualizer measurement.** The turn grouper → `displayItems` →
   `useVirtualChat` (`pages/ChatPage.tsx:5738`, `:5740`, `:5832`) measures rows
   for the scroll window.
   Hiding a large prefix changes the measured set substantially. Not traced
   whether the virtualizer needs more than a shorter `items` array, or whether
   scroll anchoring needs a hint at the collapse boundary.
6. **Fork and transfer.** ~~Should a fork inherit markers, and should the fork point
   itself become one?~~ **ANSWERED, and shipped in this phase.** A fork and a
   transfer both inherit markers: dropping them silently relocated every section
   boundary while keeping every turn, which is a worse artifact than not copying at
   all. `chat_fork` copies `_FORKED_ROLES` over a span tracked separately from the
   fork-point index space, so `at_message_index` still counts user/assistant turns
   only. `session_transfer` carries them in an ADDITIVE top-level `section_markers`
   field rather than inside `messages`, because the importer validates message roles
   strictly and an older peer would reject a whole bundle carrying an unknown role;
   the field is omitted when a session has no markers. Both label and content take
   the redactors at each boundary. The fork point does NOT become a marker: that
   would author a boundary the user never asked for.
7. **Interaction with `collapseAllSteps`.** Should the global setting
   (`pages/chat/ChatSettings.tsx:30`) gain a sibling for section collapse, or is
   derive-from-transcript sufficient without a user-facing switch?
8. **Should the two turn-boundary mechanisms converge?** A marker would ride the
   `/note` deferral (`_deferred_notes`, flushed by `flush_deferred_notes`), while
   [#6853](https://github.com/kirodotdev/KiroCrew/pull/6853) introduces a second
   boundary path (`slot._pending_discard_conversation_key`, consumed in
   `chat_runner.py`) that additionally waits on sub-agents. If a marker lands
   there are two answers to "what happens at a turn boundary", which is the kind
   of pair that drifts. Whether they should be one seam is a maintainer call, and
   it is not a prerequisite for either change.
