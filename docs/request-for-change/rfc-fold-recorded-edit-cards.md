---
title: Edit cards fold once the turn's changed-files card records them
status: accepted
author: iamwhatever
created: 2026-10-16
last-audited: 2026-10-16
audited-at: 5c790ca239
doc-pr:
implementation-prs: []
tracking-issues: [21336]
supersedes: []
superseded-by: []
---

# RFC: Edit cards fold once the turn's changed-files card records them

- Status: accepted. This is the product owner's decision, given 2026-10-16 on
  [#21336](https://github.com/kirodotdev/KiroCrew/issues/21336). It amends the
  fold rule of [rfc-tool-derived-diff-cards.md](rfc-tool-derived-diff-cards.md)
  § 3.1 (a diff card stays out of both folds), in its own document so that one
  is not rewritten. Claims about today's code were checked at `5c790ca239`
  (main).
- Author: iamwhatever.

## 1. Problem

A turn shows every file edit as its own card, and the card that should say what
the turn changed does not appear on kiro-cli's v3 engine.

- **Every edit stays on screen.** `isDiffCardItem` in
  `website/src/pages/chat/TurnBlock.tsx` keeps an edit row out of the running
  window (the newest five tool rows) and out of the settled fold. A worker turn
  that folds a hundred other steps still shows 10 to 25 edit cards in a row,
  and many are scratch scripts the agent wrote for itself, not results.
- **The changed-files card is missing on v3.** At turn end `_flush_file_changes`
  writes `meta.file_changes` onto the turn's reply, and `FileChangeChips` draws
  it under the answer. Its entries come only from `_snapshot_write_target` in
  `src/kiro_crew/dashboard/chat_turn/file_changes.py`, which reads the older
  kiro-cli `fs_write` shape: a `command` of `create`, `strReplace` or `insert`.
  v3 (KAS) writes carry no `command`. `str_replace` sends
  `{path, oldStr, newStr, replace_all}`, and `fs_write` and `fs_append` send
  `{path, text}`. Each returns `None`, so a v3 turn records no file changes. On
  one maintainer install, no session carried the card after 2026-08-24, across
  hundreds of v3 edits.

So on v3 the per-edit cards are the only record of what a turn changed. That is
why they cannot simply fold.

## 2. Decision

The turn's changed-files card is the record of what the turn changed. An edit
whose change that card records is a working step like any other.

| Edit row | While the turn runs | After it ends |
|---|---|---|
| change recorded by the card | folds with the other tool rows; the newest five stay open | folds; the card lists the file |
| change not recorded | stays out of the fold, as today | stays out of the fold, as today |

- **Recorded is decided when the write is snapshotted.** When
  `_snapshot_write_target` returns a snapshot for the tool call, the runner
  stamps that tool row `meta.file_change_recorded: true`. The dashboard folds a
  diff card only when its row carries the stamp. The answer is known on the
  row's first render, live and after a reload, so turn completion never opens a
  row the running window folded (TurnBlock's one fold state).
- **Not recorded** covers a write shape the snapshot does not read (other
  harnesses), a refused sensitive path (`validate_file_path` returns `None`),
  and every row written before this change. Folding those would hide the only
  record of the edit, so they keep today's rule.
- **The snapshot reads the v3 shapes.** A v3 write that carries a diff content
  block takes the `create` rule. KAS's block carries the whole file before and
  after the call, which is the property the older `create` block has: on the
  same install, 917 of 934 `str_replace` diffs with a block start past line 1
  and carry context lines. A `str_replace` without a block takes the
  `strReplace` rule and is rebuilt from disk; `replace_all` declines the
  rebuild exactly as `replaceAll` does. An `fs_write` or `fs_append` without a
  block reads the file, as `create` does.
- A recorded write that fails stays in the card as unverified, or drops out
  when its file provably did not change (`_settle_failed_write`). Its row still
  folds: the row's own status says the call failed.

## 3. What this amends

- [rfc-tool-derived-diff-cards.md](rfc-tool-derived-diff-cards.md) § 3.1, the
  `isDiffCardItem` bullet: "keeps the row out of BOTH folds" becomes "keeps the
  row out of both folds unless the turn's changed-files card records its
  change". Per-card density relief (the fold chip, the over-cap summary chip)
  is unchanged.
- Nothing else. A folded card still renders when its fold opens, so the
  dashboard's rule not to repeat tool edits as prose diffs still holds.
  Messaging surfaces are unaffected.

## 4. Alternatives considered

- **Fold an edit once the card records it. Chosen.** The user sees one list of
  changed files at the end, and no edit loses its only visible trace.
- **Fold every edit card.** Rejected. Other harnesses and older turns have no
  changed-files card, so the folded rows would be the only record of the edit.
- **Decide at turn end from `meta.file_changes`.** Rejected. The card exists
  only after the turn, so a running turn could not fold its edits, and opening
  rows at completion breaks the rule that completion only ever folds more.
- **Build the list in the frontend from the edit rows' diffs.** Rejected. It is
  a second changed-files source beside `FileChangeChips`, and the two would
  have to agree.

## 5. Tests

The implementation pins:

1. `_snapshot_write_target` returns a snapshot for v3 `str_replace`, `fs_write`
   and `fs_append`, with and without a content block. `replace_all: true`
   declines the rebuild. A shell call whose `command` is a command line still
   returns `None`.
2. The runner stamps `file_change_recorded` on a write row whose snapshot was
   recorded, and not on a refused sensitive path or a non-write row.
3. A v3 turn that edits a file ends with `meta.file_changes` on its reply.
4. TurnBlock folds a stamped edit row in the running window and in the settled
   fold, in both collapse modes, and keeps an unstamped one out of both.

## 6. Rollout

One implementation pull request, after this document lands, closing #21336.
