---
title: A per-chat SQLite FTS5 search index for "Search this chat", built only when someone searches
status: implemented
author: buluoray
created: 2026-11-25
last-audited: 2026-11-26
audited-at: 05202bda8d
doc-pr: 42880
implementation-prs: [43118]
tracking-issues: [42872]
supersedes: []
superseded-by: []
---

# RFC: A per-chat search index for "Search this chat"

**Status:** implemented. Accepted at team review, 2026-11-26 (maintainer Ray Xu). The document merged in
[#42880](https://github.com/kirodotdev/KiroCrew/pull/42880). It is an RFC because it adds a file format that stays on users'
machines (CONTRIBUTING.md, "a one-way door"). The repository owner (buluoray) ruled on
2026-11-25 that the per-chat index is a SQLite FTS5 table with the `trigram`
tokenizer rather than a format of our own; this revision records that. The implementation for
[#42872](https://github.com/kirodotdev/KiroCrew/issues/42872) is on main from
[#43118](https://github.com/kirodotdev/KiroCrew/pull/43118) (`05202bda8d`):
`src/kiro_crew/chat_search_cache.py` builds and keeps the per-chat cache, and
`dashboard/chat_history_cached.py` answers the search and counts routes from it. The
window already shows the server's whole-chat counts, so phase 2 needs no separate change.
As built, each chat's file is `<sessions>/.search-cache/<stem>.db`
(`chat_search_cache.CACHE_DIR`); §5.6 and §8 name that path. The other main-side
references in §2 were read at `867ca8eab20` and describe main before the build.

**Where this belongs.** Neither existing search RFC is the right home.
[rfc-session-semantic-search.md](rfc-session-semantic-search.md) ranks whole
chats by meaning, and
[rfc-search-all-chats-later-stages.md](rfc-search-all-chats-later-stages.md)
extends the cross-chat dialog. This document covers the find window inside one
chat, which neither touches, so it stands on its own.

## 1. Summary

When a person searches a chat for the first time, Crew reads that chat once,
redacts each message's text, and writes a small SQLite file beside the
transcript: an FTS5 table with the `trigram` tokenizer over the redacted text,
plus each message's role, time and attachment kinds. Later searches query that
file instead of reading the transcript. New messages are appended to it. A
rewrite of the transcript rebuilds it. All these files together have a size
cap, and the least recently searched go first. Nothing is built for a chat
nobody searches.

## 2. The problem

### 2.1 What main does today

| Step | Where |
|---|---|
| The window sends `GET /api/chat/slots/{slot}/history/search` for each page | `website/src/components/chatHistorySearch/ChatHistorySearch.tsx`; the route is registered in `src/kiro_crew/dashboard/routes/chat.py` |
| The handler runs `search_history` in a thread | `api_chat_slot_history_search` in `src/kiro_crew/dashboard/chat_history_search.py` |
| `search_history` reads the transcript from the newest row back, 200 rows a page, at most 4000 rows a reply, and stops at the page's hit limit | same file, `SCAN_PAGE_ROWS`, `SCAN_BUDGET_ROWS` |
| Each row is decoded and classified, and `redact` runs on every message, whether it matches or not | `_read_row` (it sets `row.shown` from `redact`) |
| The tab counts are the rows this reply read, summed across loaded pages in the browser | the `counts` loop in `search_history`; the `counts` memo in `ChatHistorySearch.tsx`, whose comment says "a whole-chat total needs an index" |

The only index is the page index in `sessions/.page-index/`
(`PAGE_INDEX_DIR` in `src/kiro_crew/history_page_index.py`). It stores byte offsets, so Crew can
jump to page N. It holds no text, so every search still decodes, classifies and
redacts each row it reads.

### 2.2 What that costs

Measured at `c1612bce457` on copies of three large chats in a scratch data
home (live-scan ranges cover cold and warm runs):

| Chat | File | Rows | Exact counts, live scan | In-memory prototype, any query |
|---|---|---|---|---|
| A | 113 MB | 27,110 | 1.1-2.1 s | 2-4 ms |
| B | 60 MB | 9,938 | 3.1-6.3 s | 3-5 ms |
| C | 61 MB | 40,413 | 2.8-5.2 s | 10-14 ms |

- **Counts are wrong.** In chat A a search for `the` shows All = 30; the chat
  has 1,377 matching messages. In chat C the Links tab shows 107 of 3,320.
  Each tab click starts a new scan that stops at a different row, so the
  numbers change on every click ([#42848](https://github.com/kirodotdev/KiroCrew/issues/42848)).
- **`redact` is the cost.** In chat B a full scan took 7.5 s under cProfile, and
  `redact` took 5.7 s of it (76%). Decoding JSON took 9%.
- **The searchable text is small.** It is 1.5-6% of the file (1.6-3.8 MB for
  these chats). Most of a transcript is tool input and output in `meta`, which
  search never reads.
- **Building once** takes 1.2-1.7 s for these chats, about one live scan; nearly
  all of it is reading and redacting the transcript (§5.6 has the FTS5 cost).
- The last column is a pure-Python prototype over the same rows. An FTS5
  lookup is faster than that, so it is the upper bound, not the target.

## 3. Goals

- Every tab shows exact counts for the whole chat, the same on every click.
- A second search of the same chat answers in milliseconds.
- The index stores only redacted text, and matching never reads raw text.
- No work and no disk for a chat nobody searches.

## 4. Non-goals

- Search all chats (the `session_index.db` FTS5 index). It is not changed.
- Searching tool output or attachment contents.
- A background job that pre-builds indexes. Most chats are worker sessions that
  nobody ever searches; building theirs would cost disk and CPU for nothing.
- Changing the query syntax (`history_search.needles_match_text` stays the
  matcher).

## 5. Design

### 5.1 When an index is built

Only on a chat's first search, in the request's worker thread (never on the
event loop). One build runs at a time per process, so ten tabs searched at
once do not start ten full reads. While a build runs, the window shows
"Searching…" as it does today.

### 5.2 What it stores

One row per message row that search can show, keyed by the row index:

- the text, **already redacted**, as `_read_row` would show it, then casefolded
  (the FTS5 column);
- the role and the time;
- the kinds it counts under (images, links, videos, files, html, widgets), as
  one bit per kind;
- the artifact slugs its prose links to.

What depends on the request is NOT stored, and is computed per search: pinned
state (pins are read each time), whether a linked artifact is html and visible
to this caller, the `since`/`until` filter, and a cron chat's display floor. So
a permission or pin change never goes stale in the index.

### 5.3 Redaction order, and why

This is a requirement of the index: it stores redacted text, and matching
never reads raw text. Each row is redacted once, at build time, and every
later search matches against that stored text.

1. It removes the 76% cost: `redact` runs once per row per build, not once per
   row per keystroke.
2. Raw text is never written to the index file, so the new file holds nothing
   the transcript's own redaction would hide.
3. The query matches the same text the person sees in the excerpt.

### 5.4 Keeping it current

Each search first reads the chain's revision (cheap, no row decode).

| What changed | What the index does |
|---|---|
| Nothing (same revision) | Answers from the file. |
| New rows at the end | Reads only the new rows and inserts them. |
| The dashboard rewrote the file with the same rows (it does on every turn) | Checks row 0 and the newest 256 rows by digest; they match, so it keeps the index and appends. |
| A row inside those 256 changed | Deletes and re-inserts just that row. |
| Row 0 changed, the chain of files changed (a size rotation), or there are fewer rows (rewind, compaction) | Deletes the file and builds a new one. |

The check is by content, not inode or mtime: the per-turn rewrite replaces the
file every turn, so an inode test would rebuild every chat after every turn.

Known limit: an edit deeper than 256 rows back, with row 0, the row count and
the checked rows all unchanged, is not seen until the next rebuild. Nothing in
Crew writes such an edit today. The code carries a `shortcut:` comment for it.

### 5.5 Size cap and eviction

- **Disk:** all index files together stay under 200 MB. After each build, the
  least recently searched files (by mtime, touched at most once a minute on a
  search) are removed until the total fits. The file just built always stays.
- **Memory:** nothing is held beyond SQLite's own page cache for an open
  connection; connections close when a search ends.
- **Deleting a chat** removes its index file in the same step that removes its
  transcript.

### 5.6 The file format

- **Where:** `<sessions>/.search-cache/<stem>.db`, beside the existing
  `.page-index/` folder. `<stem>` is the same name the transcript uses.
- **Permissions:** owner-only (`0600`, folder created owner-only), like the
  transcript. A full build writes a temp file and renames it into place.
- **Tables:**

```sql
-- the text a query matches; rowid = the transcript row index
CREATE VIRTUAL TABLE msg_fts USING fts5(folded, tokenize='trigram');
-- what each message counts under; same rowid
CREATE TABLE msg(
  row   INTEGER PRIMARY KEY,
  role  TEXT NOT NULL,
  ts    REAL,
  kinds INTEGER NOT NULL,   -- one bit per kind
  slugs TEXT                -- linked artifact slugs, JSON list
);
-- what the index was checked against
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
  -- key, chain, total, head digest, tail digests, redaction fingerprint
```

- **A query:** `msg_fts MATCH` for each needle of 3+ characters, joined to `msg`
  for the kind filter. One `GROUP BY` over the same join gives every tab's
  count for the whole chat. Only the 30 hits of a page read their excerpt.
- **Versioning:** two checks on open. `PRAGMA user_version` is the schema
  version, bumped whenever the tables change. `meta.redaction` is a
  fingerprint of the redaction rules the text was redacted with: a digest of
  the rule table (`security/redaction_structured/`) and of the owner's
  redaction on/off setting. If either differs from the running gateway, the
  file is deleted and rebuilt, so a stricter rule in a release, or redaction
  turned back on, never leaves older text in an index. There is no migration:
  the file is an index, and the transcript is always the source.
- **Size, measured:** FTS5 `trigram` with the text stored came to about 3.9×
  the text (4.0 MB of text, 15.6 MB file, built in 0.9 s; SQLite 3.53.1, using
  the repository's own docs as stand-in text). For the chats in §2.2 that is
  about 6-15 MB each, so 200 MB holds 13-30 large chats, and far more small
  ones. A 3+ character lookup took under 1 ms.

### 5.7 Queries of 1 or 2 characters

`trigram` cannot answer a needle shorter than 3 characters: it has no entry,
and MATCH returns nothing. That covers most CJK queries, which split into
1-character needles, and short English terms. For those needles the query
filters the same chat's `msg_fts.folded` column with a substring test
(`instr`), a full scan of one chat's text: 4.7 ms over 4 MB in the measurement
above. Needles of 3+ characters in the same query still use MATCH first, and
the substring test runs only on those rows. The result is the same set the
Python matcher (`history_search.needles_match_text`) gives today, which stays
the final check on each candidate.

## 6. Failure modes

| Failure | Result |
|---|---|
| The index file is missing, corrupt, of another schema version or of another redaction fingerprint | Deleted and rebuilt from the transcript on this search. |
| The folder cannot be written (disk full, permissions) | The search falls back to today's live scan; a warning is logged. |
| The transcript keeps changing during a build | The build retries from a fresh revision up to 6 times, then the request fails as a live scan's would today. |
| The transcript cannot be read | The same error the live scan returns today. |
| The gateway restarts mid-write | A full build is renamed into place only when done; an append is one SQLite transaction. Either way the file is whole. |
| The SQLite build has no FTS5 or no `trigram` (older than 3.34) | The search falls back to today's live scan. The Search all chats index already needs the same. |

## 7. Rollout

| Phase | PR-sized change | Exit criteria |
|---|---|---|
| 1 | The index module, the search handler reading it, the delete hook. No setting. | Every tab shows the same whole-chat count on every click; a second search of a 60 MB chat answers in under 50 ms; matching reads only the index's redacted text; a 1-character CJK query and a 2-letter query return the same hits as the live scan; a changed redaction fingerprint rebuilds the file; the folder never exceeds the cap in a test that writes past it; deleting a chat removes its index file. |
| 2 | The window drops the per-page count summing (the `counts` memo in `ChatHistorySearch.tsx`) and its `shortcut:` comment, because the server's counts are now whole-chat. | The tabs show the server's counts unchanged. |

Phase 2 can be dropped without phase 1; the summed counts are then simply
redundant.

## 8. How to undo it

- **For a user:** delete `<sessions>/.search-cache/`. It is safe at any time,
  even with the gateway running: the next search rebuilds what it needs. No
  transcript, pin or setting is stored there.
- **For the project:** revert the implementation PR. The leftover folder is
  then unused and can be deleted the same way. Nothing else reads it.

## 9. Backward compatibility

- The route's reply keeps its fields. `counts` changes meaning from "the rows
  this reply read" to "the whole chat". A client that summed pages would
  over-count, which is why phase 2 removes that summing in the same release.
- No setting, no config key, no migration.

## 10. Security and data

- The index holds only redacted text, owner-only, beside the transcript it
  came from. It adds no route, no token path and no network reach.
- It is removed with its chat, and the size cap bounds it.
- The index is read only from the transcript, so a chat whose transcript is
  not kept gets no index.

## 11. Alternatives considered

- **Keep the live scan and only redact the hits.** It removes most of the cost
  (1.5-3.8× faster in our test), but matching would then read raw text, which
  §5.3 rules out. The counts stay wrong too.
- **One FTS5 index over every message of every chat, kept current on write.**
  Still rejected. It would be about 0.5 GB more on a host whose disk was 97%
  full (the Search all chats index, `session_index.db`, is already 540 MB
  there), and it is built for chats nobody searches. It would also have to
  follow every path that rewrites a transcript; the existing index lists at
  least eight (the "Freshness is not a write hook" note in `history_index.py`).
  This design uses the same engine but per chat, built only on a first search,
  checked by content on each search (§5.4), and capped (§5.5).
- **A cache format of our own (compressed JSON of redacted text, matched in
  Python).** This was the first draft of this document. It is smaller (about
  the text size) and fast enough (2-14 ms), but it is a format nobody else
  knows, its matching would be ours to keep fast, and it has no CJK story
  beyond a full scan. FTS5 `trigram` is SQLite's own substring index, already
  used by Search all chats, so a short query is the only case left to a scan
  (§5.7).
- **Pre-build indexes in the background for recent chats.** Rejected for now:
  most chats are never searched, and a first search costs about one live scan.
  It can be added later without changing the format.

## 12. Open questions

1. Is 200 MB on disk the right cap? It holds 13-30 large chats (§5.6). A setting
   is possible but not proposed.
2. Should closing a chat (not deleting it) also remove its index? This
   document proposes no: a closed chat can be reopened and searched, and the
   cap already bounds the folder.
3. What exactly the redaction fingerprint covers. This document proposes the
   rule table plus the owner's on/off setting. Any change to either rebuilds
   every index on its next search, which costs one build per searched chat.

## 13. Decision

No decision recorded yet.
