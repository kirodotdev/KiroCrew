# Conversation History Module

## Overview

Persistent conversation history with provenance tracking and LLM-driven consolidation. Conversations survive session expiry and gateway restarts.

Private essential-context receipts are not transcript metadata and are never
restored as authority. A resumed provider gets a current complete snapshot even
when native history contains the previous copy. User/history replay identity and
current-request exclusion remain independent of essential-envelope deduplication.

Consolidation resolves its destination through the same strict recorded memory
binding as interactive turns, before starting an extraction provider. A named
member store must be declared, readable and prepared; malformed or unavailable
identity aborts the pass without writing to Global Memory V1. Sessions with no
memory binding retain the V1 consolidation path.
The async consolidation pass reads that execution record in a worker thread,
before applying privacy policy or allocating the extraction provider.

Owned V2 consolidation never publishes or refines shared auto-skills and does
not run the global skill lifecycle. Member experience remains in that member's
store; the existing V1 auto-skill behavior is unchanged.

The extraction pass freezes its original transcript and rechecks it after the
model returns, before writing memory. A generation change, edit/deletion or new
user turn leaves that pass pending; an appended assistant acknowledgment can
remain for the next pass. Revision checks additionally prevent a stale proposal
from overwriting a newer fact. V2 preference/project Markdown is read-only to
the consolidator even when the global legacy migration flag is false; new facts
and corrections use structured records. The full policy is owned by
[memory-skills-hooks](memory-skills-hooks.md#consolidation-historypy-historyconsolidator).

Metadata readability is part of this contract: invalid JSON or invalid text
encoding in an existing transcript returns an unreadable status. Identity-aware
consumers refuse the operation; the legacy `get_metadata()` projection still
returns an empty dictionary for callers that only display history.

Template-versus-member selection survives recent-session restore, explicit
resume and dormant-slot rehydration through the canonical execution context in
the owning session metadata. `session_agent_selection.py` preserves that record
instead of inferring selection from display fields. Restoring a template
conversation after discovery imports a member with the same name keeps its
original namespace. The same record carries member/store identity; ordinary
authorization remains independent. Old V1 conversations retain legacy resolution,
while missing member identity refuses routing.
See [session](session.md#agent-selection-provenance).

Bulk clear excludes transcripts whose metadata cannot be read, including Global
V1 transcripts. Their owner and pinned state cannot safely be inferred. An exact
sidebar delete (`DELETE /api/sessions/{key}`) still bypasses bulk identity and
pin selection, but it now reads `linked_session_key` while holding the transcript
lock because that field may be the only exact cron owner key. Unreadable metadata
therefore returns `409 cron_ownership_unknown` with the row intact; the operator
must release any candidate jobs, repair the metadata, and retry. A readable exact
delete leaves every other session untouched.

### Composition and source ownership

`kiro_crew.history` remains the compatibility facade and defines the real
`ConversationLog` type. It owns transcript paths and sidecars, the shared
in-process and cross-process lock registries, append/atomic-turn persistence,
cache generation registries, and consolidation-progress writes. The facade
re-exports the established module API and keeps thin, explicit delegates for
the extracted behavior:

- `history_cache.py` owns the bounded cache containers and the invalidation /
  guarded-publish coordinator. Cache objects and generation state remain on
  the facade owner.
- `history_search.py` owns query parsing plus the list/search/snippet catalog
  projection.
- `history_projection.py` owns bounded transcript reads, tab-chain/index
  projection, metadata reads and updates, permanent deletion, and previews.
- `history_rewrite.py` owns locked compaction rewrites and size-based rotation.
- `history_consolidation.py` owns `HistoryConsolidator` and auto-skill
  eligibility/extraction helpers; `history.py` re-exports the class unchanged.

Every `ConversationLog` component is constructed with only the same owner; there
is no helper-callback dependency bundle and no duplicate mutable history state.
Calls that are established instance patch/diagnostic seams route back through
the owner. The few module bindings with demonstrated post-construction facade
rebinds are read through narrow call-time lookups (the search scan window, read
lock/preview settings, and rewrite rotation/archive settings). Stable clocks,
parsers, formatters, logging, and atomic I/O remain ordinary module dependencies;
stable helpers still owned by the core facade are resolved lazily rather than
injected into every component.

### Bounded transcript pages

`ConversationLog.read_messages_chained_page` serves dashboard pagination without
materializing the complete parsed transcript. `TranscriptReadProjection` keeps a
bounded in-memory sparse index per transcript revision, mapping every row
stride to a byte offset. The stride starts at 128 rows and doubles whenever the
entry would exceed 1024 checkpoints, so an entry's size is capped regardless of
transcript length. Any file-stamp or invalidation-generation change rebuilds
the index from byte zero; only an exact revision reuses checkpoints. A key with
no transcript file is a stable empty revision (`total=0`); any other stat error
propagates rather than reading as empty.
The index is built lazily by the first paginated read of a revision (one O(rows)
byte scan off the event loop); authoritative full reads (restore, search, export,
consolidation) never build or touch it, so callers that never paginate pay
nothing for the feature. A page seeks to
the nearest checkpoint and decodes only the intersecting range plus at most one
stride. Tab-chain membership and ordering come from the same `_tab_id_index` as
`read_messages_chained`, including its terminal fallback (a chain none of whose
members yields a row is served from the key's own file); there is no second
lineage model, and a chain whose membership changes during a page read is treated
as a revision change.

The index stores counts and offsets only, never message content, and never crosses
a process boundary or trusts agent-writable derived state. In-memory validity
requires the full file stamp (`mtime_ns`, `ctime_ns`, size, inode, device) and the process-wide
history invalidation generation. An unpinned page read whose revision changes
mid-read is retried once; a read pinned to an `expected_revision` is not, since
the stamp that broke the pin cannot recur, and the caller's retry policy decides.
Repeated churn falls back to the complete-reader oracle so the endpoint
never returns a mixed revision. Indexed reads use the strict framing posture
(`strict_raw_records_with_offsets`): a record over `RECORD_CAP` raises instead of
being skipped, because a skipped row would shift every cursor above it; the
endpoint then serves the request from the full reader, which has no per-record cap.

Paginated slot detail composes an indexed durable prefix with the resident disk
suffix and the existing unflushed/transient window reconciliation. The first
range read returns an ordered chain revision (`key`, file stamp, invalidation
generation); every later range in that response must match it, otherwise the
whole composition retries and ultimately falls back to the full-reader oracle.
It preserves the legacy exact `total`, `before`, `next_before`, and `has_more`
fields. The no-limit route and callers requiring complete history remain on
`read_messages_chained`. Display redaction remains at `_prepare_messages`; neither
the sparse index nor page projection stores a redacted or alternate transcript.

### The dashboard transcript window (frontend)

The dashboard renders the rows these pages return through one windowing hook,
`website/src/hooks/virtualizer/useVirtualChat.ts`. The chat page, every
`VirtualTranscript` host and the artifacts gallery use it. It is a projection
only. It mounts the rows around the viewport, prices the rest from measured
heights (estimating rows not yet measured), and asks the host for the next older
page when the reader climbs near the top. It never filters, reorders, hides or
reclassifies a row. An incognito or temporary transcript is therefore windowed
exactly like a persistent one; its rows stay visible and ordered (see
[A restricted transcript is kept](#a-restricted-transcript-is-kept-what-is-derived-from-it-is-not)).

The hook is a facade over composed owners, each holding one responsibility:

| Owner (`website/src/hooks/virtualizer/`) | Owns |
|---|---|
| `useVirtualChat.ts` | option wiring, row identity (display key, stable id, alt id), and the hook-call order the owners depend on |
| `windowRange.ts` | the mounted window as the reader scrolls: the scroll recompute and its merge, the near/far jump rule behind `mountIndex`, sentinel expansion, the coverage watchdog, the older-history index trigger. A placement that moves the scroller (follow's tail and jump, a prepend rebase, the reading position's entry, visibility and restore) mounts its own window from its owner, through the same window math |
| `measurement.ts` | the per-scope `HeightIndex`, the spacer geometry read from it, and every measurement writer (resize observer, row ref seed, measure farm), each gated on the caller's `canMeasure` so a width-transition measurement never lands in the old scope, plus the mounted-row reseed a scope change runs after the reading-position restore. Only the cache WRITE stands down for the gate: the observer classifies every fire (first mount vs resize, the above-fold delta the compensation adds to scrollTop) against the height the DOM last showed for that node, tracked per mounted element apart from any scope, so a re-wrap during the transition is compensated once and a repeated fire adds nothing. The write a fire makes is the RESIDUAL for the reader's row — the row seen highest among those reaching below the fold at the last frame the reader saw — between where it is after layout and where it was last seen (moved by its own credited change: a re-wrapped straddler keeps its bottom, an appending or in-place one its top), never the batch's summed growth: Chromium's native scroll anchoring adjusts scrollTop during the reprice layout, before any observer callback, so the summed growth was paid twice there and a row native pushed under the fold read as straddling. Row positions are re-read at scroll events and seeds (the last painted frame), carried arithmetically by a fire's own write, and never re-read at the end of a fire (a layout is delivered over several callbacks). The record cannot be stale against the reader's own input: a user scroll reaches scrollTop at the start of a rendering update and dispatches its scroll event in that update's scroll steps, before layout and the observer, so the record is refreshed before any rect the fire reads; only a row that is no longer mounted stands the write down (a click or key that scrolls nothing leaves the record valid). The scope reseed announces immediately rather than through the debounce, so the cold owner's estimate spacer is replaced in the swap commit's layout phase |
| `geometryScheduling.ts` | when a measurement becomes geometry: the debounced sync, its deferral while the reader moves, the streaming row's immediate path, the rail-collapse window |
| `shiftCompensation.ts` | holding a scrolled-up reader still across prepends, splices, window shifts in either direction (rows mounting above the reader are re-priced from the estimate; rows unmounting above them are replaced by a spacer the tree prices, which is short by every re-wrap the tree has not been allowed to learn during a width transition), appends, height syncs and the width-scope swap, and planning which row measurements a commit retires. The swap: the settled bucket's cold owner re-prices the before-spacer from estimates in the render that constructs it, so a same-session swap over an unchanged list captures the reader's row render-phase into the height-sync slot; the consumer is keyed on the owner's identity as well as its announced version (two owners can report equal versions) and stands down on the swap commit itself, leaving the capture for the reseed's announcement in that commit's layout phase, which is where the committed spacer it must be paid against exists. That stand-down also reads the engine's RANGE CLAMP: the cold document prices every unmounted row at the flat estimate, so it can be shorter than the reader's scrollTop, and the engine then drags scrollTop to its ceiling (`scrollHeight - clientHeight`) with no application write anywhere and leaves it there when the reseed grows the document back (Firefox and Chromium alike at a matched depth; a shallower reader never reaches the ceiling). When the pending capture's scrollTop is above that ceiling and the live scrollTop is exactly the ceiling (within `SELF_SCROLL_EPSILON`), the capture is re-based to the clamped value with its candidates' painted geometry kept, so the reseed pays the whole move; a drop that is not the ceiling (native anchoring's spacer-delta move) is left alone, and any movement after the clamp still differs from the re-based value, so the scrollTop freshness guard still drops the capture. A followed reader keeps the tail pin; a true session switch captures nothing |
| `readingPosition.ts` | the persisted reading position: entry latch, debounced save, leave flush, visibility re-placement, restore and settle |
| `followPolicy.ts` | follow, pin and reader intent, plus `writeScrollTop`, the one path for the hook's programmatic scroll writes |
| `observers.ts` | the scroller element, the mounted-row registry, the scroll listener and the resize observer |

Beneath them sit the helpers they share. `FollowController.ts` (follow and
position-owner decisions), `WindowCalculator.ts` (window math) and
`anchorGeometry.ts` (reader-row geometry) are pure. `HeightIndex.ts` over
`HeightCache.ts` holds height truth, persisted per height scope under
`vc_heights_` (the transcript hosts scope it to the session plus a width
bucket), and `ScrollAnchorCache.ts` persists reading anchors under
`vc_anchor3_`. `MeasureFarm.tsx` is the off-screen measuring component and
`inPlaceResize.ts` the in-place resize notes. Browser storage holds measurements
and reading positions only, never message content.

Follow (`followPolicy.ts` over the pure `FollowController.evaluateAutoPin`) keeps
a reader at the end of the transcript, and decides "is the reader still at the
end" by position alone. A reader resting on follow's own last write — the pixel a
pin, a layout clamp, or the reader's own return to the bottom left them on — is
carried to the new bottom whenever content opens a gap under them, whether or not
a turn is running: a complete message landing in an idle chat (a crewmate's or
worker's report arriving in a DM, a notice, a cron row) follows without the
reader touching anything. Hardware input on its own never counts as leaving; a
wheel at the end that moves nothing is not a move. The one exception is scroll
intent whose scroll event has not dispatched yet — an UPWARD input, or a pointer
that grabbed the scrollbar (`scrollIntentPending`): the automatic pin is held
until that scroll event decides, and retried once when the intent expires
without one (a click on the thumb, a wheel-up on an unscrollable transcript). A
reader whose scroll took them off that write has left: follow releases, an idle
append leaves them where
they are, and only their return to the bottom, the jump pill, or sending a message
(every chat host force-pins on send) re-arms it. Growth is followed wherever it
lands at the end: a tail row streaming, and the host's chrome below the rows
(`TranscriptScrollShell` wraps `belowRows` in one block the resize observer
watches, so the working footer mounting under a reply that went quiet carries a
followed reader down to it instead of leaving them its height short of the end).

The rows themselves come from the chat store, not the hook.
`website/src/store/chatSlice.ts` is the facade of the one `chat` slice (the
slot-lifecycle and UI-state owners are in
[session](session.md#dashboard-chat-state)); the transcript and history-cache
owners behind it are:

| Owner (`website/src/store/chat/`) | Owns |
|---|---|
| `transcript.ts` | message identity (the client id, the server `meta.mid`, the one-shot `sendId`), redelivery and duplicate detection, echo reconciliation, the chunk-seq floor a snapshot vouches for, and the bounded-page identity rules every slot-detail merge cuts by. Pure: callers pass the arrays in |
| `paging.ts` | page sizes and limits, the switch and count-matched fetch limits, the coverage shortfall, the paging-cursor shift after a kept head, and the abort handle of the one older page in flight |
| `slotCache.ts` | what a slot-detail page writes besides its rows: the active paging cursor (`has_more` and `next_before` become `slotHasMore` and `slotOldestIndex`), a background pane's page with its has-more and bounded markers, the retained server `total` baseline, and the context meter |
| `messages.ts` | edits to a cached transcript outside the live frame: the optimistic send and its confirmation, streaming and final text, patches by tool-call id / `mid` / `ts`, and a background pane's one-time hydrate |
| `thinking.ts` | client-only reasoning rows, re-seated after every server replace or parked until their anchor pages in |
| `queue.ts` | queued rows, hydrated from a slot-detail `queue` field |
| `lifecycle.ts` | the Older-sessions list (`fetchHistory`) and its paging, and resume and delete of a history row |

`loadOlderMessages` stays in the facade. The chat host answers the hook's
older-page request with it; it reads the page before `slotOldestIndex` and lands
it only while the slot it was read for is still active. None of these owners filters rows by memory
mode, so a restricted transcript is cached and paged like any other.

### The Sessions sidebar (frontend)

The dashboard's session list, `website/src/pages/ChatSidebar.tsx`, draws two
projections of this history. The live list shows the open slots, plus peer rows
from connected crews when the instance-sessions preview is on. The Older Sessions
pane shows the `fetchHistory` pages owned by `store/chat/lifecycle.ts`. Once its
search box holds `SEARCH_MIN_CHARS` characters it shows `search_sessions` results in
the server's order, federated across connected crews while one is connected. Both lists order, group
and narrow rows only by what the person chose (sort, lane, filters, folders).
Neither list's ordering, grouping or filtering reads memory mode, so an incognito or
temporary session is listed, searched and filtered like any other (see
[A restricted transcript is kept](#a-restricted-transcript-is-kept-what-is-derived-from-it-is-not)).
The row reads memory mode only to draw its incognito or temporary glyph, and a
restricted session cannot be dropped into the composer as a reference.

`ChatSidebar.tsx` is a facade over owners that each hold one responsibility. It
calls each owner hook where that block used to sit, so React runs the effects in
the same order as before. `ChatSidebar.ownerComposition.test.ts` pins that call
order, and pins that no owner imports the facade:

| Owner (`website/src/pages/chat-sidebar/`) | Owns |
|---|---|
| `sessionSources.ts` | the rendered row set (local tabs plus live peer rows, deduplicated by row identity, local wins), the peer-list error, and the federated Older Sessions search |
| `search.ts` | the debounced backend session search, and the folder-name matches the search box adds |
| `rowIdentity.ts` | origin-qualified identity for live and history rows, and the peer guards on local pin and folder state |
| `persistence.ts` | the browser-stored view preferences (lane, width, filters, fold sets, pane height): every key except the four status-chip keys, which ride on `SESSION_FILTERS` in `filters.tsx`; and the readers, defaults, validation and migrations of every key except the width and the pre-board width (`resize.ts`), the pane height (`history.ts`), and the status chips and the folders-shelved flag (`filters.tsx`) |
| `filters.tsx` | the status chips (`SESSION_FILTERS`), the folder and tag filter state, the Recent window, the running, recent and unread sets and chip counts, and the unread auto-drain |
| `lanes.ts`, `conductor.ts` | the lane preference, the flat-lane projection and the lane cycle; the conductor lane's lineage seed poll, population, lineage tree and open conductors |
| `folders.ts` | folder sort mode, visibility, the subtree index and ancestor expansion, the filter-menu rows, and folder writes |
| `board.ts` | the tag-column board: columns, the column popover, column writes, lane seeding (it widens the sidebar through `resize.ts`), per-column collapse and membership |
| `stale.ts`, `pinnedOrder.ts`, `hoverHold.ts` | the dormant-session collapse, the manual pinned order, and the hover hold |
| `reveal.ts` | reveal-in-sidebar for a session or a folder |
| `rename.ts`, `history.ts`, `resize.ts`, `tags.ts`, `shortcuts.ts`, `create.ts` | row and folder rename, the Older Sessions pane state, the sidebar width (including the width saved while the board is open), the tag vocabulary, the chat-jump order, and session creation |
| `dnd/` | collision geometry (`collision.ts`), drop targets and drag previews (`targets.tsx`), and the drag lifecycle with its folder writes and undo offers (`useSidebarDrag.ts`) |

Some code stays in `ChatSidebar.tsx`: `SessionRow` and its source-link chips, the
row and folder render closures, the filter-dimension registry, the peer-session
adopt, the idle-session cleanup, the bulk model switch and the JSX. Source pins
read them in that file:

- `switchSlotCallsiteClassification.test.ts` counts the four `switchSlot`
  dispatches there, the row's three and the adopted session's activation.
- `listShellParity.test.ts` reads the list-shell recipes the row and the card use.
- `useInteractiveModels.test.ts` reads the bulk model switch.
- `ChatSidebar.filterDimensions.test.tsx` reads the filter-dimension registry.
- The restyle ratchet counts this file's flagged sites in the header, the filter
  and folder menus and the board column.

The idle-session cleanup is state that only the header menu's dialog in this file
reads. The render closures also stamp rows in paint order, and the row memo depends
on that order.

## ConversationLog (`history.py` facade)

Per-thread JSONL files at `~/.kiro/crew/sessions/{safe_key}.jsonl`. First line is metadata, subsequent lines are messages with `role`, `content`, `ts`, `tools`, `source_thread`, `source_user`. A writer can also supply `cls` (presentation class) and `mid` — persisted as `meta.mid`, the same field shape the dashboard slot save writes, so a dual-write injector's durable copy carries the SAME delivery identity as its in-memory window copy and a bounded slot-detail read reconciles the two as one message instead of re-appending the injection. A row appended without an id carries no `meta` at all (the pre-id shape readers keep an id-less fallback for; existing transcripts are never migrated).

- Append-only for LLM cache efficiency
- Rotation at 10MB (keeps metadata + last 200 messages, atomic write), enforced
  by `ConversationLog.append`. The dashboard whole-file save
  (`_save_slot_to_history`) does NOT rotate: a transcript written only through
  that path is bounded by `_MAX_SLOT_MESSAGES`, not by this byte cap. Rotation
  moves the dropped lines to `archive/`, and no TRANSCRIPT read stitches them
  back — `read_messages_chained` globs the sessions dir non-recursively, so an
  archived row leaves the rendered conversation even though it stays retrievable
  through the `/api/session/archive` endpoints. Rotating a session the dashboard
  still displays therefore removes the head of that conversation from the chat,
  which is why the save path does not do it.
- **Cache-fill staleness guard** — transcript memos use a cache identity of `(st_mtime_ns, st_ctime_ns, st_ino)`, rather than mtime alone. That makes the normal atomic rewrite visible even when housekeeping restores its pre-write mtime via `_restore_mtime`; the replacement inode or changed ctime forces a miss. A per-key invalidation **generation** still closes in-process fill races: `_invalidate_cache` bumps it BEFORE dropping entries, and each fill snapshots it before `stat` then re-checks it around publish via `_publish_if_current`, discarding a fill if it moved. `_meta_cache`, `_recent_cache`, `_folded_cache`, `_snippet_cache`, and `_msg_cache` record both identity and generation; `_folded_cache`/`_snippet_cache` serialize stat → read → store under `_file_lock`, while `_msg_cache`'s unlocked on-loop fallback additionally needs a cross-process flock-hold witness. The generation table is process-wide (class-level, keyed by transcript directory + sanitized stem), and invalidation covers each spelling of one session — logical key, sanitized `path.stem`, and canonical/legacy Slack aliases in both directions (`_cache_key_identities`).
- `recent(key)` — last 20 messages for context injection
- `recent_with_provenance(key)` — entries with source citations. Never a display-only row (`DISPLAY_ONLY_ROLES`, the `notice` role): notices are drawn for the reader, not conversation, and the consolidator's memory and skill-detection prompts skip them the same way while its offset still passes them (the Slack thread-parent row reaches a model only through its fenced block)
- `list_sessions()` — lists all sessions with title (first user message or LLM-generated). Sort key uses ISO `created` string consistently (defaults to ISO from `st_mtime` if no metadata `created` field, ensuring string-only comparisons). Each returned session's meta dict also carries `folder_id` when present in the persisted metadata line, so sessions can be grouped by the folder they were filed in.
- `agent_usage()` — returns `{agent_name: (session_count, last_used_mtime)}`; built on `list_sessions()` so it inherits canonical-session dedup + symlink-skip (counts per logical conversation). Used by `GET /api/agents` to order the roster most-used-first, degrading to config order on failure.
- `history_index.py` stores file freshness identities without truncation: signed-64-bit
  `st_dev`/`st_ino` values remain SQLite INTEGERs; wider values use prefixed decimal
  TEXT (`i:<value>`) to avoid INTEGER-affinity conversion to floating point. Sync,
  freshness checks, shortlist validation and snippet reads use the same encoding.
  Existing integer rows and schema version 2 remain compatible; unsigned Windows
  device IDs and 128-bit inode IDs do not require an index migration.
- `search_sessions(query, limit=50)` — case-insensitive substring content search over the newest `_SEARCH_SCAN_WINDOW` session JSONL files; the ONE ranking shared by the dashboard history filter, the `search_chat_history` MCP tool, and Discord session resume. The query is parsed by `parse_search_query` into needles: non-CJK terms are required substrings (AND over the document); a spaceless-script run (Han ideographs + kana; NOT Hangul, since modern Korean is space-separated) gates on its individual characters (required, down-weighted) plus an adjacency floor — at least one of the run's character bigrams must hit somewhere, so a spaceless multi-word CJK query matches documents containing the words apart (each word is a bigram hit) while scatter-only character noise is excluded, and adjacency dominates the ranking; the floor is waived when the query's bigram set exceeds its cap (a partial set cannot prove no-adjacency-anywhere, so truncation only ever loosens). Occurrence counts are weighted per needle, length-normalized, title-boosted, phrase-bonused, then multiplied by a bounded recency boost (×2.5 for a session modified now, decaying toward ×1 with a 30-day half-weight — never a penalty; sized so a year-old double mention loses to today's single mention while a decisively better old match still wins), and capped to `limit` results. A short ASCII term (one or two characters: `5`, `s3`; never CJK or other non-ASCII, and never a run of three or more characters, digits included — incidental substring frequency falls roughly 10x per extra character, so `4411` keeps raw frequency) carries `SearchNeedle.saturate_body`; a forge-reference needle is saturated when any of its spellings is such a term (`issue 5` gates on `#5`, `issues/5` and the bare `5`) and keeps raw frequency otherwise (`#4411`), and its CONTENT contribution is `log1p` of its length-normalized hit count instead of the raw count over the length norm: such a substring matches timestamps, account ids and commit hashes far more often than prose about the thing does, so unsaturated a long transcript's thousands of incidental digit hits out-score the session whose title IS the query (`"case 5"`). Saturating the normalized count keeps body-only matches in frequency order (a long substantive discussion still beats one stray mention in a short session). Its title hits and its place in the AND gate are unchanged, and longer terms and CJK needles keep raw frequency, so only queries carrying a short token re-rank. Exposed via `GET /api/sessions/search?q=<q>&limit=<n>` (min 2 chars); used by the dashboard history filter to find sessions by content (CR ids, error messages, file paths) rather than title alone. Returns the same meta dicts as `list_sessions()`, so each search hit likewise carries `folder_id` (when present), letting the sidebar group results by folder. Snippet builders (`_content_snippet`, mcp_core's `_extract_history_snippet`) derive their needles from the same parse via `snippet_needles` (phrase first, then whole terms/bigrams, lone CJK characters last) so match and excerpt cannot drift apart. The fold/snippet memos backing the search are keyed by the sanitized `path.stem` (from `list_sessions`' meta dicts) while writers invalidate under the logical session key; `_invalidate_cache`'s identity-wide pops are what connect the two spellings, so a housekeeping rewrite that restores the file's mtime still drops the memo and search stops matching text the transcript no longer contains.
- `needles_match_text(needles, folded_text)` — the single-string form of `search_sessions`' match gate (required needles as substrings + the CJK adjacency floor), for callers filtering one text field; Discord session resume's zero-hit title fallback uses it so title matching cannot grow a second spelling of tokenization.
- `read_file_change_messages(key)` — a lightweight Artifacts projection that streams one transcript as bytes, skips lines without the serialized `"file_changes"` key before JSON parsing, and retains only `ts` plus `meta.file_changes` in its own bounded, file-stamped cache. It never warms `_msg_cache`, so scanning the session-document firehose cannot retain the full parsed transcript corpus.
- Forge references (pull requests, merge requests, issues) are a query dimension of their own, because one item has several written spellings and a transcript carries whichever one its author used. A term naming an item — `#4411`, `PR #4411`, `pr 4411`, `pull request 4411`, `pr4411`, `pull/4411`, a full PR/MR URL, `owner/repo#4411` — becomes ONE required needle carrying every spelling of that item (`SearchNeedle.alts`, counted by the shared `count_needle`), so any spelling finds every spelling. The words that introduce the number are dropped from the gate: they are not part of the reference, and requiring the literal "pr" would disqualify a transcript that names the item only by URL. Spellings are `digit_bounded` on both sides, so `#4411` matches neither `#44110` nor the run id `1544110293`. The TYPED sigil decides the family, never a word before it: `mr#12` is read as `#12`, because letting the word win produced a reference none of whose spellings was the string the user typed. Coverage of every accepted shape is pinned by a property test that drives each one against a transcript quoting it verbatim, rather than by inspection of the spelling list. GitHub's pull/issue sequence is shared (`#4411` ≡ `/pull/4411` ≡ `/issues/4411`) while GitLab numbers merge requests separately, so `!12` and `#12` stay distinct families and never match each other; bare `merge` is not a GitLab word (GitLab is `MR 12` / `merge request 12` / `!12`). Plain digits remain one of the spellings exactly when the QUERY typed no sigil (`issue 42`, `PR 4411`, `pr4411`): such a query previously gated on the digits, so dropping them would HIDE the transcript that says "we hit issue 42 in prod", and keeping them makes the recall of the literal AND it replaces hold with ONE intended exception — a session whose only claim to the old match was the digits sitting inside a longer number, which is what the boundary exists to exclude. The LEFT edge of that boundary applies only to a spelling that starts with a digit: for a delimited spelling the character before it says nothing about the number's length, and demanding a non-digit there would refuse `#4411` inside `owner/repo2#4411` — a repo whose name ends in a digit, matched against the very reference the query named. Only a lead-in run that actually NAMES a type turns a following number into a reference: `pr 4411`, `issue 42`, `pull request 4411` and `merge request 12` (the two-word GitLab form) do; `requests 12` and `merge 1234` do NOT and stay literal terms, since dropping such a word from the gate would trade a real term for every session mentioning that number. A query that DID type a sigil never gated on bare digits, so it keeps them out and stays precise (a standalone "12" is ordinary prose). A BARE number with no naming word is not a reference at all: it keeps its plain substring needle — numeric content search (ports, error codes, run ids) is unchanged — and gains the spellings as scoring-only needles at `_FORGE_REF_WEIGHT`, so the session that references the pull request outranks one that merely contains those digits. Those ranking needles are NOT adjacency evidence (`SearchNeedle.adjacency`, which only CJK bigrams set), or they would arm the adjacency floor and turn a ranking hint into a hidden gate. Two limitations are accepted rather than special-cased, both needing a query nobody writes and both only widening the result set: a chain-only word wedged between the type word and the number (`issue merge 42`) is swallowed, and because the gate is keyed by term text a query repeating a suffix word as its own term (`pull the pull request 12`) loses that term. Closing either means keying the gate by token position instead of by text. Expansions per query are capped at `_SEARCH_MAX_FORGE_REFS`, each costing one scan per spelling per scanned session (up to eight for a named reference, up to thirteen for a bare number's both-families ranking needle, plus up to eight more for a registered provider's own prefixed id — see below); a token past the cap degrades to a plain needle.
- A REGISTERED source provider contributes its OWN id spellings through the same machinery, so an edition whose reviews are written `REV-987654321` is searchable without any provider vocabulary in core. The seam is one optional plugin hook, `search_ref(token) -> (canonical, alts) | None` on `SourceProviderPlugin`, discovered with `getattr` exactly like `path_markers()`; `source_search_ref()` fans out across the registered plugins (asking each registered plugin until one answers, then handing that answer through unjudged — shape is the normalizer's job, and skipping a malformed answer would only serve the same two-registrant case the merge below is declined for), and `register_source_provider()` publishes that collector DOWNWARD into `history_search.register_search_ref_resolver` at registration time — never from a route handler, because `parse_search_query` is also reached from paths that serve no HTTP (the Discord title-only resume gate, the `kirocrew memory search` CLI) and a process that never ran a route would otherwise answer the same query differently. One slot holds the resolver, not a list: the per-plugin fan-out already lives in the collector. The FIRST plugin to recognize a token WINS, for every token shape: a prefixed id names ONE item, so merging would conflate distinct items, and a cross-plugin merge for a bare number would exist only to serve two registrants holding a real item at the same number — which this repo, registering no provider at all, cannot produce. Cost stays bounded in ONE place: `_MAX_SEARCH_REF_SPELLINGS` (8, sized to the sibling per-plugin `_MAX_PLUGIN_PATH_MARKERS` because it bounds the same kind of thing — what ONE plugin hands core for one lookup) bounds the single answer that arrives, with no collector-side ceiling to drift from it. A bare number is NOT a provider token at all: a provider's ids are prefixed, so a run of digits names nothing it owns and the resolver is not consulted for one. `_provider_search_ref` is the single normalizer — it casefolds every spelling (the query is casefolded before parsing and `count_needle` requires already-folded needles, so a capitalized spelling produces a needle that matches NOTHING, silently), de-duplicates, drops empties, applies the fan-in ceiling, and DROPS an answer none of whose spellings carries the typed token, since such an answer describes some other item and would otherwise rank a query on text it never named. Every way a resolver can fail is contained and costs no more than a debug log — carrying a traceback where an exception was raised, and the offending value where a shape was merely wrong, so none of them is silent: raising when called, returning a malformed answer, and raising while its `alts` are READ — the hook promises a `Sequence`, which cannot do that, but a resolver ignoring the contract can, so the read sits inside the same boundary and the answer is dropped WHOLE rather than half-read, since an exception there would otherwise escape the parse as a 500 on every search. A provider is consulted only for a token no built-in shape recognized (built-ins always win), contributes SPELLINGS ONLY and never lead-in vocabulary (the words a provider would want — "review", "cr" — are common English, so admitting them would trade a real search term for every session mentioning that number), and can never gate a BARE all-digit token, which it is never even asked about — so no number of registered providers can spend the `_SEARCH_MAX_FORGE_REFS` budget on one numeric token. The purity contract — pure, allocation-cheap, no I/O — is documented and not enforced: a resolver is consulted for every term of every query and the parse runs at least twice per search, so a blocking resolver becomes per-keystroke latency in the search box.
-- `_read_messages` — identity-guarded message cache with the same double-checked, miss-only locking `_folded_content` uses for this identical race. A warm hit is served lock-free; only a MISS takes the session's in-process writer lock (`_file_lock`) and re-checks identity + cache under it. The identity `(st_mtime_ns, st_ctime_ns, st_ino)` changes on the normal atomic rewrite even when housekeeping restores the previous mtime, so a stale cache entry cannot remain current. ON the event loop the lock is acquired non-blockingly and a busy lock falls back to an unlocked fill, so an on-loop read never stalls behind a writer holding the RLock across its cross-process flock wait (`_FLOCK_ACQUIRE_TIMEOUT_S`). An unlocked fill publishes through two witnesses: a per-key invalidation **generation** covering local writers and a cross-process **flock-hold witness** covering external processes (`_flock_hold_witness`: publish only while this process provably held the sidecar flock for the whole fill window). A fill that cannot prove its window clean is discarded. Every transcript memo (`_msg_cache`, `_meta_cache`, `_recent_cache`, `_folded_cache`, and `_snippet_cache`) records identity and generation; a warm hit requires both, so a write through another `ConversationLog` instance also invalidates it through the process-wide generation table keyed by `(transcript dir, sanitized filename stem)`, with canonical and legacy Slack spellings closed over bidirectionally (`_cache_key_identities`).
- `delete_session(key)` — permanently removes a session JSONL file. Dashboard
  deletion may tear down the exact live slot and idle SessionManager generation
  captured before unlink, but it preserves chat pins, work ledgers, and
  autocompact overrides. Those stores can be claimed by a transcript created or
  restored in another process after any catalog scan; stale sidecars are
  reversible, while deleting a successor's state is not. The teardown contract
  is specified in [session.md](session.md) under **Permanent history deletion
  keeps ownership exact**.

### MCP chat-history tools (`mcp_core.py`)

These read-only tools expose the session store to the agent and are all
workspace-scoped by default (fail-closed via `_caller_workspace`/`_ws_bucket`,
`all_workspaces` opts out), exclude incognito/temporary sessions (canonical
`INCOGNITO_MEMORY_MODES` in `history.py`), and redact their output:

- `search_chat_history` — keyword lookup over past transcripts (ranked snippets).
- `get_chat_session` — read one full transcript by `session_key`.
- `list_sessions` — browse/overview counterpart to search: returns recent
  sessions newest-first (title, owning agent, message count, timestamps) built
  on `ConversationLog.list_sessions()`, with `limit` (default 20, max 100).
  Opt-in `summarize=true` calls `POST /api/sessions/summarize` to attach a fresh
  one-line LLM summary per session — MCP core has no LLM access, so the LLM leg
  runs gateway-side on an ephemeral background session (cheap Haiku model),
  bounded to 8 sessions and best-effort (falls back to the title on any failure).
  The reply is shape-checked before anything is stored: the taught `SKIP`
  verdict, alone or with a reason, and a refusal (`label_guard.looks_like_prose`
  with the summary's own ceilings, without the conversation-referring openers
  and without the sentence-shape signals, since a summary is a sentence by
  contract) both return `""` — never a cached value — so one model refusal is
  not served on every later list until the transcript changes.
  A generated summary is cached in a **sidecar file** (`sessions/.summaries/`),
  never in the session JSONL, keyed by the session file mtime — so summarizing an
  active session never rewrites (and cannot clobber a concurrently-appended
  message in) its log, and a repeat call for an unchanged session pays zero LLM
  cost. A new message advances the mtime and invalidates the cache. Because the
  session log is untouched, `list_sessions(summarize=true)` remains a true read of
  conversation history (`get_cached_summary` / `set_cached_summary` in
  `ConversationLog`). The intent-level session summary shown in the chat panel
  uses the same mtime-signature contract but a **separate** sidecar
  (`sessions/.intents/`), because the two artifacts have independent writers and
  sharing one file would reintroduce the read-modify-write race the sidecar design
  avoids — see [session-summary.md](session-summary.md). The gateway-side
  one-liner
  generation uses the shared `llm_helpers.run_bg_oneliner` helper (the same
  acquire→drive→destroy skeleton as title / link-label / folder-icon generation).

### Foreign-agent session import

The first-run importer accepts session history from Codex, Claude Code, OpenClaw,
and Hermes, plus any edition-registered source declaring the `lineage` layout —
that reader covers the `workspace/` tree the predecessor entry used to, so the
capability moved behind registration rather than being removed. It projects each
selected conversation to
**visible user and assistant text only**. Hidden reasoning, tool calls and tool
results, system messages, raw instructions, provider session identifiers,
approval state, and other runtime metadata are not copied.
Known non-text record/content envelopes are excluded as whole units even when a
foreign store labels them with a user/assistant role or places visible-looking
text in their content field.

Claude transcript records marked as metadata, sidechain activity, tool-use
results, or a non-external user type are excluded as whole records even when
they contain visible-looking text. Workspace discovery collects every valid
scalar cwd/project field from a record and every current Codex
`payload.workspace_roots[]` entry; one record is not reduced to its first path.

OpenClaw JSONL is considered only under `agents/<agentId>/sessions` and only
when the sibling `sessions.json` has one unambiguous entry resolving to that
file. The entry must have `createdVia` operator/channel/talk, a human
`createdActor`, no parent/spawn/runtime/plugin/fork ownership, and a key outside
the cron, subagent, ACP/bridge, hook, node, heartbeat, and internal-effects
namespaces. Trajectory/checkpoint artifacts and deleted/reset archives are
diagnosed and excluded. Canonical `agents/<agentId>/agent/openclaw-agent.sqlite`
stores are safety-checked and diagnosed as unsupported; their sessions are not
partially projected.

Hermes SQLite import requires both `sessions` and `messages`, joining
`messages.session_id` to `sessions.id`. Accepted sessions have a nonempty source
other than subagent/tool/cron and a null `parent_session_id`; parented/runtime
lineage is diagnosed, and only accepted sessions contribute workspaces. Message
projection remains visible user/assistant text only and honors the current
`active`/compacted marker. A legacy messages-only database has no sufficient
provenance and is diagnosed rather than guessed.

Imported conversations are persisted through `ConversationLog` under generated,
closed destination keys. They enter the normal History list but do not create
live dashboard slots, resume a foreign runtime, or reuse a foreign identifier as
an executable KiroCrew session key. The normal ConversationLog metadata/message
schema, rotation, path sanitization, and retention behavior therefore remain
authoritative.

Import is merge-only and idempotent. A durable provenance ledger binds the
foreign source and stable source-item identity to the generated destination key;
re-applying the same item is reported as already imported instead of appending a
duplicate conversation. The foreign session tree is read-only throughout scan
and apply and is never rewritten, moved, or deleted.
The existence check, interrupted-prefix repair, append, and rollback for one
destination session run under the same `ConversationLog._locked` critical
section, so concurrent imports cannot interleave transcripts or record a
partial session as complete.

Bounded JSONL parsing never emits a partial conversation: reaching a file line
or line-byte limit excludes every conversation projected from that file, and
reaching a per-session visible-message limit excludes that session while allowing
other complete sessions in the file. A malformed JSONL record likewise excludes
the whole file, including workspace paths observed in its otherwise valid prefix.
Each exclusion is reported by its limit reason. Within one source, mirrored
identical normalized visible transcripts collapse to one import candidate, but
the retained candidate keeps its stable source-item identity rather than deriving
identity from its transcript. A growing source session therefore remains tied to
the same provenance ledger entry.

## Dashboard History Persistence — Frozen Prefix + Live Window (`dashboard/chat_persistence.py`)

Dashboard restoration reads an existing canonical execution context before applying
the transcript's agent field. A provisional history write left by an interrupted
switch therefore cannot replace the committed choice, even if its rollback could
not acquire the history lock. Missing selection records retain legacy resolution;
unreadable records remain execution refusals. Member/store integrity and ordinary
authorization are still checked. Async restore prefetches this record off-loop alongside
the transcript and applies the resulting name on the event loop.

`_save_slot_to_history` persists dashboard chat slots. It models the session
file as a **frozen prefix + live window** so on-disk history is never
overwritten or truncated — a slot that restored only the last ~500 messages can
no longer destroy older turns.

- **Frozen prefix**: the first `slot._disk_older_count` on-disk message lines —
  the turns OLDER than the in-memory window (set at restore/resume/rehydrate
  from `len(disk) - window`). These bytes are read verbatim and NEVER rewritten.
  They are cached on the slot keyed by `(file-mtime, _disk_older_count)` so a
  steady 5s flush is O(window), not O(file size).
- **Live window**: all of `slot.messages` (small, bounded by the 10000-message
  cap). It is **re-serialized in full on every save**. Re-serializing the whole
  window is what makes in-place edits (stop-event resolution `stopping→stopped`,
  file-change chips, mcp_oauth banner completion) and any reordering done by
  `_flush_segment` (which moves a trailing `stop_event` to land AFTER the
  finalized assistant reply) persist correctly — there is no fragile position
  counter to drift.
- **Default save** (flush loop, close, folder/tag/title changes) writes
  `metadata + frozen_prefix + serialize(window)`. It is always a superset of
  what is on disk, so it archives nothing and skips the O(file) diff read.
- **`slot._disk_window_len`**: count of window messages the last save wrote to
  disk. Memory trimming (`_MAX_SLOT_MESSAGES`) may fold a leading window message
  into the frozen prefix (`_disk_older_count += …`) only for messages actually
  persisted (`min(excess, _disk_window_len)`); an unpersisted overflow is logged
  rather than silently counted as on-disk.
- **`slot._disk_older_durable_count`**: the durable-only position base — how
  many non-transient rows (`state._TRANSIENT_ROLES`) have left the window off
  the front. Maintained at every site that sets or advances
  `_disk_older_count` (restore/resume/rehydrate/channel rebuild recompute it
  from disk; the trim path advances it by the durable rows in the WHOLE
  evicted slice, unpersisted overflow included — it is a position base with no
  disk contract, so an uncounted lost row would silently shift every later
  position). It exists for absolute message positions
  (`session_control.read_messages`), never for save-model arithmetic — the
  save's frozen-prefix contract stays on `_disk_older_count`.
- **Single-file only**: the save touches `_path(history_key)` and never reads or
  writes sibling files. `tab_id` is 1:1 with a file (fork creates a fresh slot
  with its own file), so chaining is untouched and legacy no-tab_id sessions are
  never merged with unrelated sessions.
- **Fork point identity**: response-level forks prefer the selected row's stable
  `meta.mid` (`at_message_id`) and resolve its visible-message position against the
  complete chained transcript inside `chat_fork.py`. The id takes precedence when a
  request also carries the loaded window's `at_message_index`; a missing id is treated
  as stale and a duplicate id as ambiguous, so the handler never guesses a cutoff.
  Modern sessions therefore fork without loading earlier pages into the browser.
  Pre-id transcript rows retain the index path, which the frontend enables only after
  loading the full visible history. Restore paths preserve a missing legacy `mid`
  rather than minting an in-memory-only identity that full-history operations cannot
  resolve.
- **Tail-only fork** (`direction="tail"`): copies only `visible[at_index+1:]`
  into the new slot instead of the head `visible[:at_index+1]`. The head is
  always dropped -- there is no summarize option. Gated server-side by
  `dashboard.tail_fork_enabled`; if the gate is off, a `direction="tail"`
  request falls back to a normal head-fork instead of erroring. The source
  slot's history file is untouched, so the head stays archived in the parent.
- **Fork inherits `memory_mode`, and never loosens it**: an incognito or
  temporary session forks like a persistent one, and the child is born with the
  parent's mode -- passed to `get_or_create_slot` at creation so the child's
  `dashboard:` key is registered restricted in the same step, never stamped on
  afterwards. There is no `slot_not_persistent` refusal: one would buy no
  privacy, for the reason the titling section below gives -- the parent's full
  transcript is already in its session JSONL, and a fork copies transcript
  while engaging neither guarantee the modes make (`is_restricted`,
  `blocks_reads`). What a fork must not do is
  produce a *persistent* child from a restricted parent -- that would hand
  no-write content to consolidation -- so the request body carries no
  `memory_mode` and the parent's value is the only source. A temporary child
  still receives its copied turns: `build_session_context` assembles the
  thread-history block before any `blocks_reads` gate. The response and the
  `chat.slot_fork` audit event both report the inherited mode. The inherited
  value is validated against `VALID_MEMORY_MODES` before the child is
  allocated: rehydration copies the transcript header's `memory_mode` onto the
  slot as written, so a hand-edited or partially written header can leave a
  value outside the allowlist on a live parent, and passing it through would
  raise out of the slot constructor as a 500. The fork instead answers 409
  `fork_source_memory_mode_invalid` (SEL `denied`), and no child exists.
  This refusal precedes execution-identity and database lookup, preserving the
  named mode error even when the source's other metadata is unavailable.
- **Member fork identity**: a V2 fork also inherits the parent's canonical
  execution context before the child receives copied history. The captured
  member/store identity must be valid; missing, damaged or mismatched identity
  refuses routing. Ordinary owner/app authorization is checked independently.
  Persistent, incognito and temporary forks keep
  their existing mode guarantees, and Global or named V1 history is never
  relabeled as private V2 by forking it.
  Cancellation waits for an in-flight binding publication before removing the
  empty child. Any published assignment remains attached to that unique key,
  including after a later save failure, so partial history cannot lose its
  recorded owner. This can leave an unused session identity record.
- **Concurrency**: `_flush_dirty_slots` runs the save in an executor thread while
  `_run_chat` mutates `slot.messages` on the event loop. `slot._lock` is an
  asyncio lock (unusable from the thread), so the save instead takes a
  consistent snapshot: it reads `_disk_older_count`, snapshots
  `list(slot.messages)`, and re-checks `_disk_older_count` (bounded retry) so a
  concurrent trim cannot interleave with the read-serialize-write.
- **Explicit-snapshot pairing (`expected_disk_older_count`)**: a caller that
  freezes its own `messages` snapshot on the loop and then awaits the save cannot
  use that retry — the snapshot is already frozen, and the counter the worker
  reads belongs to a later moment. A trim at the window cap in that gap credits
  the trimmed rows to `_disk_older_count`, so the write emits them twice: once in
  the frozen prefix it now claims, once at the head of the still-frozen snapshot.
  Such a caller passes the counter it observed in the SAME synchronous stretch as
  the snapshot; the save refuses on drift (returns `False`, writes nothing) and
  the caller answers its retryable refusal. The rewind boundary transaction does
  this and re-adopts the same boundary at its commit, since the commit puts the
  pre-trim window prefix back, together with `_disk_older_durable_count`, which
  the trim advances beside the boundary — leaving either advanced counts a row as
  having left the window front while it is back inside it. A trim landing after
  the worker read the boundary cannot be refused (the correct file is already
  written), so both are corrected at the commit instead. Neither is stamped by
  the save, so the pre-await values are the file's truth in every interleaving.
  Any other caller that freezes a snapshot across an await owes the same pairing;
  `save_slot_off_loop` does not forward the parameter yet, so a boundary
  transaction routed through it still reads the live counter in the worker.
- **`_disk_window_len` is deliberately left possibly SHORT after such a trim, and
  the direction is the whole argument.** The save stamps it *absolutely*, so a
  trim landing BEFORE the stamp has its decrement erased while one landing after
  it does not — and the commit cannot distinguish the two without the count the
  save actually wrote, which is not `len(snapshot)` either (a note row authorized
  elsewhere is filtered out of the write, so the snapshot can be longer than the
  file's window region). Over-claiming is the harmful direction: a later trim then
  credits rows to the frozen prefix that the file does not hold, and the next save
  re-emits window rows. Under-claiming costs no rows — it under-credits the prefix,
  warns about rows that are in fact on disk, and drops the following save onto a
  whole-file re-read, while the foreign-append merge below preserves the on-disk
  window line the memory window has dropped. Making it exact wants the save to
  publish its whole witness set as ONE routing-keyed record, which is also what
  the stamping race above wants. `_frozen_prefix_cache`, the trim's last casualty,
  needs nothing: the trim sets it to `None`, which only costs the next save a
  re-read.
- **Witness stamping is routing-gated**: the post-write bookkeeping
  (`_pending_rewrite`, `_disk_window_len`, `_disk_meta_*`, `_frozen_prefix_cache`)
  describes the file this save wrote, but it lives on the live slot, which the
  event loop can rebind mid-write. The write stays correct (it lands on the
  transcript authorized before it), so the save re-confirms
  `slot_history_key(slot)` against the key it wrote and SKIPS the stamping when
  they differ — stamping would clear a `_pending_rewrite` the new transcript still
  owes and claim its unsaved rows as persisted. Every witness left at its pre-save
  value is the conservative reading, so the next save re-reads the prefix,
  re-takes the archive-safe path, and re-observes the file. The
  `ConversationLog` cache invalidation is keyed on the file that WAS written and
  stays unconditional. Everything the stamp needs (the post-write `stat`, the
  carried-forward `created_at`) is computed BEFORE the re-check so the stamped
  region is assignments only — a save runs in a worker thread, and a syscall
  inside that region is the realistic point at which the loop gets to rebind
  under a half-applied stamp. Full atomicity against the loop is not reachable
  from the thread (`slot._lock` is an asyncio lock, and once the rebind path has
  recomputed these for its own transcript no undo is right); it wants the five
  fields collapsed into one assignable record carrying the key it describes.
- **Cross-process lock (`_locked`)**: `_save_slot_to_history` holds the session's
  cross-process `_locked` (the SAME lock `append` / `append_off_loop` / rotate /
  rewrite / metadata edits take) across its metadata read, frozen-prefix read,
  archive diff, and `atomic_write`. `_locked` expands every Slack spelling through
  `transcript_lock_stems` and delegates to `ConversationLog.locked_stems`, which
  acquires the exact physical stems in sorted order; canonical `slack_<ts>` and
  pre-migration bare `<ts>` writers therefore cannot synchronize on different
  sidecars. Writers resolve `_path` only after that complete set is held, so a
  waiter cannot publish a filename choice made before restore created the other
  alias. Without the lock a concurrent `append_off_loop` (e.g. a workflow/cron
  result appended to the originating dashboard session) could land between the
  save's file snapshot and its file-replacing `atomic_write`, silently deleting
  the acknowledged append. On the event loop `_locked` makes ONE non-blocking
  acquire per physical stem and raises `HistoryLockTimeout` under contention
  rather than blocking the loop — so **on-loop callers MUST offload**:
  `save_slot_off_loop(state, slot, …)` dispatches the save to a worker thread so
  it takes the patient off-loop acquire path. It is `best_effort=True` by default
  (a lock timeout / I/O error is logged, not raised — the in-memory slot is the
  source of truth and the periodic flush retries); archival paths that must
  confirm the durable write before removing the session (session close/cleanup)
  pass `best_effort=False` so the exception propagates and the caller rolls back.
  Off-loop callers (`_flush_dirty_slots`, `save_all_slots_to_history` at
  shutdown) call `_save_slot_to_history` inline — off the loop `_locked` polls
  patiently to a bounded deadline. The same discipline applies to every other
  session-JSONL writer: `clear_closed` (resume un-flags `closed` under `_locked`,
  offloaded via `asyncio.to_thread`) and all `history.py` mutators hold `_locked`.
- **Delete-won guard**: `delete_session` unlinks the session file under the
  same `_locked` and leaves no tombstone, and the patient off-loop acquire
  means a save can legitimately sit waiting while a permanent delete runs to
  completion ahead of it. Inside the lock, before any `mkdir`/`atomic_write`,
  the save therefore aborts cleanly (no write, no error — the flush loop
  clears `_dirty`) when the file is gone AND the slot has OBSERVED its session
  on disk. The observation witness is `_disk_meta_created_at` — recorded
  exactly at the hydrate sites and at each committed save, nowhere else — and
  it is the SOLE gate: the window counters take no part in either direction,
  because fork/transfer set `_resumed_count` optimistically after a
  best-effort first save (a transient first-write failure must not read as a
  deletion and eat the retry), and a restored zero-message session has
  all-zero counters while its delete must still win against the save of its
  first message. A delete that already
  reported success is not silently undone. Only `FileNotFoundError` from
  `stat` counts as the delete witness; any other failure (permissions, device
  not ready) propagates and leaves the retry armed. A file that EXISTS can
  also be delete-won: `delete_session` leaves no tombstone, so a foreign
  append landing after the delete creates a fresh file — the save tells the
  incarnations apart by the metadata `created_at` (the file's identity, which
  a save always carries forward and which therefore never changes for a
  continuously-existing file) against `_disk_meta_created_at`, the identity
  the slot last observed at restore or at its own save; a known-vs-known
  mismatch aborts rather than merging the deleted window into the new
  transcript, while a readable-but-absent `created_at` (legacy meta) fails
  open. The metadata is read through `get_metadata_status`, and an UNREADABLE
  line fails CLOSED: the save raises (leaving `_dirty` armed for the flush
  retry) and `session_was_deleted` returns True (the copy is refused,
  retryably) — a transient read failure must not blank the identity
  comparison and let deleted content overwrite a replacement session. A brand-new slot's first
  save has none of that evidence and creates the file normally. The abort
  returns `False` (every other completion returns `True`), and
  `save_slot_off_loop` forwards it — for BOTH `best_effort` modes the skip
  raises nothing, so a clean return no longer proves a committed write.
  Callers that republish the slot's content elsewhere check it: the fork
  aborts with 409 and the transfer export refuses the bundle, because a copy
  made from the surviving in-memory window would resurrect the destroyed
  conversation under a fresh key whose own save carries no delete evidence.
  Because the periodic 5s flush can hit the guard FIRST and clear `_dirty` —
  after which fork/transfer skip their dirty-gated flush arms and never see
  the `False` — both also call `session_was_deleted(state, slot)` directly at
  their copy choke points: the same evidence + stat-ENOENT witness, answered
  independently of flush ordering (lock-free, safe because a permanent delete
  never un-happens). Being lock-free also means the delete can land INSIDE the
  probe, between its stat and its metadata read, and `get_metadata_status`
  reports a vanished file as a genuine `({}, True)` -- so an empty `created_at`
  is re-stated before it is trusted, which is what tells "legacy metadata"
  (fails open) from "deleted a moment ago" (refuses). The save's guard needs no
  equivalent: it reads the metadata and stats the path inside `_locked`, the
  lock `delete_session` unlinks under, so no delete can interleave between its
  two reads.
  A single pre-copy probe is not enough, because writing the copy is itself an
  await that does not serialise against the source's delete: the transfer
  re-probes after bundle assembly, and the fork re-probes after its DESTINATION
  save, both before the copy is acknowledged. The boundary a handler owns is
  ACKNOWLEDGMENT — a delete committing before it wins, and the fork therefore
  removes the destination transcript it had already written (`delete_session`
  on the destination key, off-loop) and pops the never-broadcast slot before
  answering 409; a delete committing after the copy is acknowledged is out of
  scope and the copy survives its source, the way a repo fork outlives what it
  came from. Rolling the destination back cannot harm the source (different
  key, different lock), so the fail-closed probe costs at worst a retryable
  409. If that removal itself fails the copy stays on disk and is logged at
  ERROR — the one case that still needs a human.
  Archival callers (close/cleanup) ignore it — the delete already disposed of
  what they were archiving. Residuals: a slot that never observed its session
  on disk (fresh slot adopting an existing key whose file is deleted while it
  waits) still recreates — the writer-recreates case `delete_session`'s
  docstring already accepts; and for a slot the delete's cleanup cannot pop
  (e.g. a cron-linked tab whose slot key matches none of the spellings the
  cleanup probes), the abort latches — every later save of new activity is
  skipped, which is why the skip logs at WARNING with the slot key.
- **Turn persistence is offloaded through ONE choke point**
  (`save_conversation_turn_off_loop`, `llm_helpers.py`): `save_conversation_turn`
  makes TWO `append` calls, so an on-loop caller pays ~24 ms of loop time per turn
  AND takes `_locked`'s single non-blocking acquire — dropping the durable copy
  exactly when another writer is active. Every async caller (the Slack handler,
  gateway, and transport dispatch) awaits the choke point rather than restating
  the offload, and `test_persist_off_loop.py` is an AST build gate that fails if
  any `async def` body calls `save_conversation_turn` directly. Unlike
  `append_off_loop`, the choke point **awaits** the write: its callers go on to
  refresh a dashboard tab or hand the session to consolidation, both of which read
  the transcript back.
- **A turn is an atomic PAIR, and offloading is what makes that need saying.**
  `append` locks per ROW, so two concurrent turn-writes for one session can land
  as `user_A, user_B, assistant_A, assistant_B` — turns that no longer pair up,
  and which no ordering pass can repair because every row's `ts` is individually
  correct. On the event loop this was impossible: a synchronous
  `save_conversation_turn` never yields between its two appends, so the
  single-threaded loop made the pair atomic *by accident*. Moving the write to a
  worker thread removes exactly that accidental guarantee. So
  `ConversationLog.atomic_appends(key)` is the required companion to the offload,
  not an optional extra: **any caller that offloads MULTIPLE appends for one
  session must hold it around the whole group.** `_locked` is reentrant for the
  same key on the same thread, so the per-row locks inside `append` reuse the
  held lock. Enter it off the loop only — it takes the same fail-fast-on-loop
  acquire path as `append`.
- **Row ordering has two writers with different floor sources.** Both
  `ConversationLog.append` and `_ChatSlot.append` stamp each row strictly after
  its predecessor via `monotonic_transcript_ts`, so a `ts` sort reproduces write
  order even on a host whose clock cannot separate two writes (Windows ticks in
  ~15.6 ms steps). They learn about that predecessor differently, and the
  asymmetry is deliberate:
  - `ConversationLog.append` reads the authoritative on-disk tail (`_last_row_ts`)
    under the cross-process flock, so it sees every committed row.
  - `_ChatSlot.append` runs on the event loop, where a `stat` plus a tail read per
    append would violate the no-blocking-call-on-event-loop rule. It floors on
    `latest_transcript_ts(window_tail, slot._disk_tail_ts)` — both in-process
    reads. `_disk_tail_ts` is refreshed at the save boundary, inside the `_locked`
    section where the foreign lines are already parsed, so it costs nothing.

  The window is NOT a superset of the file: a genuinely foreign on-disk row is
  preserved without being folded into `slot.messages`, so without the cached tail
  the slot's next row could TIE it. A foreign row arriving *between* two saves is
  still invisible until the next one — the reachable shape (a subagent/cron append
  observed at the following flush) is closed, the general case is not, and that
  bound is intentional rather than an oversight. The floor is monotone by
  construction: `latest_transcript_ts` only ever selects a *later* candidate, so it
  can move a row forward but never backward. It **skips** candidates it cannot
  parse, because `transcript_sort_key` deliberately buckets unparseable values
  AFTER every real instant (right for display order, backwards for a floor) — one
  corrupt row would otherwise win the comparison, be discarded by the stamper as
  unparseable, and switch the ordering guarantee off for that session.
- **On-loop offload discipline is enforced, not convention-only**: the offload
  invariant above was previously guaranteed only by convention — a future
  contributor calling a raw mutator (`append` / `update_metadata` / `set_title`
  / `delete_session` / `_save_slot_to_history`) from an async handler would get
  a write that works in every uncontended test yet silently drops under real
  contention (the on-loop `HistoryLockTimeout` swallowed by a best-effort
  `try/except`), invisible in CI. `_locked` now calls
  `_check_on_loop_persist_discipline(key)` on entry: if a running event loop is
  detected it either **raises `OnLoopPersistError`** (strict mode — on under
  `KIROCREW_STRICT_ON_LOOP_PERSIST=1` or `KIROCREW_DEV_MODE`) so an un-offloaded
  call-site fails tests rather than losing data, or emits a **loud throttled
  warning** and proceeds via the single non-blocking safety-net acquire
  (default / production gateway, strict off — never a new hard failure in the
  field). Strict is deliberately NOT auto-on under bare pytest (the suite's own
  async harness calls several mutators directly on the loop as a convenience, so
  auto-strict would flag harness code, not drift); the enforcement tests flip
  the env flag explicitly. Off the loop the check is a no-op (the sanctioned
  path). Tests that deliberately drive the low-level on-loop primitive wrap the
  call in `history.allow_on_loop_persist()` (a `ContextVar`-scoped bypass);
  production code must NEVER use it. **Considered-and-deferred alternative — a single-writer
  queue:** funnel every session-file mutation through one dedicated writer thread
  (or per-key `asyncio.Queue` drained off-loop) so the loop never touches
  `_locked` at all and no caller can bypass the discipline structurally. It was
  deferred because it reshapes every mutator into an async enqueue (touching the
  same ~15 call-sites plus the synchronous CLI/subagent/cron writers that must
  stay inline), serializes unrelated keys unless sharded, and complicates the
  close/cleanup paths that need a confirmed durable write (`best_effort=False`).
  The refcounted `_flock_state` + the strict on-loop guard give most of the
  safety at a fraction of the churn; the single-writer queue is the intended
  escape hatch if the guard's warn-and-proceed production fallback ever proves
  insufficient (e.g. a hot on-loop path that must not be lost).
- **Rewrite path** (`rewrite=True`, an explicit `messages` snapshot, or a slot
  left in `_pending_rewrite` — rewind/regenerate/fork): writes
  `metadata + frozen_prefix + serialize(snapshot)`. These INTENTIONALLY drop the
  post-edit window tail, so the dropped lines are archived first via
  `_archive_dropped_lines` → `_archive_lines` (the frozen prefix appears
  unchanged in both old and new, so it is never archived). `_pending_rewrite` is
  set by rewind/regenerate after they truncate the window and cleared only on a
  successful rewrite save, so a failed inline rewrite still gets retried as an
  archive-safe rewrite by the next flush (never silently overwritten).
- **Foreign-append merge & id-first dedup** (`_frozen_prefix_and_foreign_appends`):
  a default save captures its `window` snapshot BEFORE taking `_locked`, so a
  cross-process writer (subagent / cron / CLI) can fully append + release the
  lock in that gap. A bare `meta + frozen + window` replace would then delete
  that acknowledged append, so the save first scans the on-disk WINDOW region
  (the bytes after the frozen prefix) for lines the in-memory window does not
  represent and carries them into the payload as `foreign_lines`. Matching is
  **count-bounded** (deques of window-entry indices; each disk line matches at
  most one window entry and each window entry absorbs at most one disk line) and
  runs in ordered passes so the outcome is independent of disk-line order:
  - **Pass 0 — `meta.mid`** across all disk lines, resolved before every
    heuristic tier: every window append mints a stable per-message id
    (`meta.mid`, read via `row_mid`), a save persists it, and the durable-copy
    writers carry the window row's id onto their copy. An id match folds only
    when **corroborated** by body or `ts` (same `(role, content)` — a durable
    copy — or same `ts` — an in-place edit): `meta.mid` is caller-suppliable
    (`_ChatSlot.append` preserves a pre-existing id), so bare id equality
    could pair two genuinely distinct messages. A corroborated match IS the
    same message — the line is dropped (the window re-serializes it) and,
    being exact, it is **not** a dedup drop and never churns the
    `foreign-dedup` archive. An id match with **no** corroborating entry
    falls through to the legacy ladder as if id-less (typically preserved).
    An id-carrying line whose id matches **no** available window entry is
    **foreign regardless of body equality** — two genuinely distinct
    identical-content messages carry distinct ids, which is exactly the case
    the body tiebreak below could never tell apart — and bypasses the
    heuristic tiers; it still **counts in the ts-ambiguity accounting**, so
    its `ts` group stays contested and an id-less line sharing that `ts` is
    preserved (a rare stale duplicate) rather than silently ts-folded — the
    same favour-duplication-over-loss direction as the ambiguity gate itself.
    Id-less lines (pre-id transcripts, writers that pass no id) fall through
    to the legacy ladder below, unchanged.
  - **Pass 1 — exact `(ts, role, content)`** across the id-less disk lines: an
    unchanged re-serialization, unambiguously **ours** (dropped — the window
    re-writes it). Resolving these before the ts/rc passes is what makes a
    burst of messages sharing ONE `ts` (coarse clocks — notably Windows'
    ~15 ms tick — stamp rapid appends with an identical
    `datetime.now().isoformat()`) match one-for-one instead of being
    mis-classified and duplicated on disk.
  - **Pass 2**, for each still-unmatched disk line, in order: (a) a **ts-only**
    match — an in-place edit keeps `ts` but changes content, so the window's
    version wins and the disk line is dropped — but applied ONLY when the `ts`
    group is an unambiguous 1:1 (exactly one unmatched window entry AND exactly
    one unmatched disk line share it); OR (b) a bounded `(role, content)`
    tiebreak against an as-yet-unconsumed window entry — covers an id-less
    `append_if_absent` durable copy persisted with a fresh `ts` (the workflow/
    cron-result injectors reflect the message in the slot AND write it via
    `append_if_absent_off_loop`, so the same message legitimately exists twice
    with different timestamps and must NOT be double-persisted; both copies
    carry one `meta.mid` — the injectors pass the window row's minted id
    through the append path — so those copies fold in pass 0 and reach this
    tiebreak only when the id is missing). A line matching
    NEITHER is foreign and preserved.
  - **Count-bounded, exact-first identity (the fix for GPT 5.6's HIGH data-loss
    findings).** `(role, content)` is only a bounded tiebreak in which **each
    window entry absorbs at most ONE disk copy**. So if the on-disk window region
    holds two id-less lines with identical `(role, content)` but distinct
    timestamps — the window's own persisted copy PLUS a *genuinely distinct*
    event from another process (e.g. a cron that reports the same status text
    twice) — the first is folded and the **second is preserved as a foreign
    append** (an earlier plain-`(role, content)`-set match collapsed both real
    events into one). Symmetrically, because colliding timestamps make a
    ts-only match AMBIGUOUS (a foreign append that happens to share the `ts` is
    indistinguishable from an edited window entry), ts-only matching is applied
    ONLY to unambiguous 1:1 `ts` groups; an ambiguous group preserves its disk
    lines as foreign — favouring a rare stale duplicate over irreversibly
    dropping an acknowledged cross-process append.
  - **Archive of ambiguous drops (no permanent loss).** A fresh-`ts` id-less
    copy folded by tiebreak (b) is the genuinely ambiguous case
    (indistinguishable from a distinct same-content message without a stable
    id), so those drops are returned as `dedup_dropped` and routed through
    `_archive_lines` (`reason="foreign-dedup"`) by `_save_slot_to_history`
    before the atomic replace — the trade-off loses no data permanently. (A
    ts-less / ts-matched plain re-serialization is a normal window copy and is
    dropped silently to avoid archive spam; a corroborated id-matched pass-0
    fold is exact, not ambiguous, and is likewise silent.)
  - **Successor identity, landed on the save side.** The **creation-time
    per-message uuid** (`meta.mid`, minted by `_ChatSlot.append`, persisted by
    the save, carried onto durable copies — the successor identity tracked by
    [issue #381](https://github.com/kirodotdev/KiroCrew/issues/381)) is now the
    fold's pass-0 identity, so for stamped lines identity is *exact* rather
    than inferred. The bounded timestamp-first heuristic above is thereby
    **demoted to a legacy fallback** for un-stamped lines: pre-id transcripts
    are never migrated, and writers that persist id-less copies (e.g. the
    Discord/Slack dashboard mirrors) still resolve through it until they thread
    the id through. The
    `test_foreign_append_content_identity_dedup_semantics` contract test pins
    that fallback; the `TestForeignFoldMidIdentity` cases pin pass 0.
  - **Residual window (rewrite saves).** The scan runs only for default saves
    (`collect_foreign = not rewrite`). Rewrite saves (rewind / regenerate / fork)
    intentionally truncate the window and are same-session/same-process, so they
    **skip** the foreign scan and can still clobber a concurrent cross-process
    append that lands between the pre-lock window snapshot and the lock — a known,
    narrow residual window (the dropped tail is handled by the rewrite's
    archive-diff, not the foreign scan).
- **Consolidation offset & rotation generation**: `last_consolidated` is an
  absolute message index the consolidator snapshots (as `total`) BEFORE its slow
  LLM call and writes back via `mark_consolidated`. A rotation firing during that
  await truncates the file and shifts every surviving index, so the stale offset
  can no longer be applied. Detection uses a monotonically-increasing
  `rotation_generation` counter in the metadata line (bumped by `_maybe_rotate`
  on every rotation, carried forward by compaction, absent field == 0 for legacy
  files): the consolidator snapshots it alongside the offset
  (`rotation_generation()`) and `mark_consolidated(key, total, generation=…)`
  resets `last_consolidated` to 0 whenever the generation changed — **regardless
  of how many messages the rotation retained**. This closes the gap a pure
  `offset > msg_count` heuristic misses (a rotation retaining ≥ the offset leaves
  `offset ≤ msg_count` true yet still shifted every index, silently marking
  never-consolidated retained messages as done); the `offset > msg_count` check
  remains as a defense-in-depth fallback for legacy callers that pass no
  generation. Reconsolidating a few already-processed messages is harmless and
  idempotent; dropping unprocessed ones is a persisted data-integrity failure.
- **Client-supplied row `meta` survives the whole path (the client-meta
  survival contract).** The `meta` a dashboard send carries (`POST /api/chat`
  body) rides onto the user row it becomes and reaches every
  reader unchanged: ingress drops only `RESERVED_ROW_META_KEYS` (today
  `decisions_strip`, the gateway's own receipt carrier -- `chat_handlers.py`),
  `_redact_meta` (`chat_utils.py`) redacts credential- and exfiltration-shaped
  STRING values recursively and is not a key allowlist, `slot.append` broadcasts
  the row's `meta` on the WebSocket `chat_message` echo
  (`include_metadata=True`), `_save_slot_to_history` writes it verbatim on the
  JSONL line, and `read_messages_chained` / `GET /api/chat/slots/{slot}` return
  it on rehydrate. A client may therefore stamp a semantic key on a send and
  read it back from the row wherever the row is drawn -- the optimistic bubble,
  the echo, a reloaded transcript, a second tab -- with no per-slot client state:
  `sendId` (delivery identity), `origin: 'widget'`, and the "Request a Feature"
  flow's `featureRequest: true` (the stamp `isFeatureRequestRefusal` in
  `transcriptRenderers.tsx` keys the usage-limit form fallback on, #13429) all
  rest on it. A later allowlist on client-supplied meta would break them
  silently, so the contract is pinned end to end -- ingress, live row, WS echo,
  JSONL line, chained read, slot fetch -- by
  `test/test_chat_send_client_meta_survival.py`, which uses a non-`sendId` key
  (`featureRequest`) with `decisions_strip` as the reserved-key control, beside
  `test/test_chat_send_echo_scope.py`, which pins `sendId` alone.

## Session Archive (`history.py`, `history_rewrite.py`)

Lines that ARE intentionally dropped (rotation, compaction, history edits) are
archived instead of being permanently deleted:

- **Archive location**: `~/.kiro/crew/sessions/archive/{key}__{YYYYMMDD-HHMMSS}.jsonl`,
  where the separator is `ARCHIVE_SEGMENT_DELIMITER`. It is `__` rather than a dot
  because session keys legitimately contain dots (a Slack `thread_ts`), which a
  right-most-dot parse would attribute to the wrong session.
- **Triggers**: `_rotate()` (>10MB), `rewrite_session()` (compact), and the
  dashboard rewrite path (`_save_slot_to_history` with a snapshot /
  `rewrite=True` / `_pending_rewrite` → `_archive_dropped_lines`). The default
  frozen-prefix dashboard save drops nothing, so it does not archive.
- **Atomic writes**: exclusive-create (`open mode 'x'`) avoids TOCTOU clobber
- **Retention**: configurable via `session.archive_retention_days` (default 30
  days; `-1` or `null` disables cleanup so the user manages deletion manually).
  `_cleanup_old_archives()` reads the value from config when called with no
  explicit `retention_days`, and is rate-limited to once per hour.
- **The same pass expires closed SESSION LEDGERS**, on that same setting and
  inside that same throttle: `_cleanup_expired_crew_logs()` hands the resolved
  window to `ledger.store.sweep_expired()`. One switch governs both halves
  because a session's message bodies live in its ledger — expiring the transcript
  archive while the ledger it points into grew forever would keep the larger half
  of the same history indefinitely, and a second setting for it would be a second
  thing to find and turn off. The ledger half is imported lazily and contained: it
  runs on the ARCHIVE path, where raising would turn "a ledger tree that could not
  be swept" into "a transcript that could not be archived", trading a disk-space
  problem for a loss of history. An absent `archive/` directory no longer returns
  early, since a session holds a ledger long before anything of its transcript is
  archived.
- **API**: `GET /api/session/archive` (list), `GET /api/session/archive/{name}` (read with path traversal protection)
- **Rotated history stays pageable.** `read_messages_chained_full(key)` returns,
  per chain key, that key's `reason="rotate"` archive segments (filename-stamp
  order, numeric `-N` collision suffixes sorted numerically) followed by the
  surviving file — the pre-rotation timeline. This is the **pagination corpus**:
  the slot-detail handler's `before`/`next_before` cursors address its collapsed
  rows, and the fork path prepends the same rotated rows so a clicked index
  resolves to the same message (`read_rotated_messages_chained(key)` returns just
  the rotated head). Only `rotate` segments participate — `compact`,
  `foreign-dedup`, and rewrite drops are content the product DISCARDED and never
  resurface. The no-limit slot-detail branch advertises an archived head via
  `has_more=true` / `next_before=<collapsed rotated-row count>` instead of
  retiring the affordance. Plain `read_messages_chained` keeps its
  archive-blind semantics: `_disk_older_count` and `last_consolidated` offsets
  are counted against the un-archived files and must not shift when a rotation
  lands. Rotated rows parse with a per-key cache invalidated on the segments'
  stat signature. Retention still applies: archives past
  `session.archive_retention_days` are deleted, and the history behind them
  becomes unreachable again — pageable-rotated-history is best-effort by design.

### Pairing a session key with its files

`transcript_stem(key)` returns the filename stem a key's transcript and archive
segments share — the sanitized key (`dashboard:chat-1` → `dashboard_chat-1`). It is
public so callers that account for or reclaim a session's disk usage
([session-storage](session-storage.md)) resolve the pairing here instead of
re-deriving the sanitization. A second copy of that rule would drift the moment
this one changed, and the failure is silent and destructive: the pairing misses,
and a caller deleting "the session" removes one half and leaves the other behind.

- `set_title(key, title)` — persists a title into the session's metadata line (first line of JSONL)

### Session titling is independent of `memory_mode`

Auto-titling (`dashboard/chat_title.py:_maybe_auto_title`) runs for **every**
`memory_mode` — `persistent`, `incognito`, and `temporary` alike — and the
resulting title is persisted for all three. This is deliberate, not an
oversight:

- Titling reads only the slot's **own** messages and prompts the shared `_bg`
  session. It neither reads stored memory nor writes any, so neither of the two
  guarantees a non-persistent mode actually makes (`is_restricted` → no
  consolidation/lessons; `blocks_reads` → no memory-context injection) is
  engaged by it.
- Persisting the title discloses nothing new. `_save_slot_to_history` has no
  `memory_mode` gate, so an incognito/temporary slot already writes its **full
  transcript** to its session JSONL for tab recovery, gateway-restart restore and
  the History browser. The title is a summary of content that is already on disk
  in the same file, and `restore_recent_sessions` skips only on `closed`, never on
  `memory_mode`.

Gating titling on `blocks_reads` (as an earlier revision did) therefore bought
no privacy while leaving temporary tabs permanently labelled "New Session…".
The manual `POST /api/chat/slots/{slot}/generate-title` endpoint never had such
a gate, so a temporary session could already be titled and persisted on demand.
Do not reintroduce a `memory_mode` condition here without first changing what
`_save_slot_to_history` writes.

### A restricted transcript is kept; what is derived from it is not

Incognito and temporary sessions persist their transcript exactly like a
persistent one. This was briefly not so: the store simplification that
introduced the execution carrier (#11780) made the slot save return early for a
non-persistent slot, so a restart lost every incognito conversation, while the
mode picker still promised "Keeps the transcript for tab recovery". The mode's
guarantee is about LEARNING from the conversation, not about the conversation
existing: consolidation (`HistoryConsolidator`), the MCP chat-history tools,
memory injection, lesson writes, the session summary and the workflow/task
snapshots all gate on the `memory_mode` the metadata line carries, and the
transcript is what the user reopens from History. Two rules follow for the
writer:

- **The line records the strictest mode known.** The slot's own `memory_mode`
  and the live execution carrier can disagree for a moment (a mode switch
  publishes the carrier first; a queued-prompt flush can outlive a close that
  tightened the slot). The save reads both -- inside the transcript lock, like
  every other field on the line -- and writes the stricter, so a restart never
  re-reads a looser mode than the session ran under. A carrier that raises
  `MissingExecutionIdentity` (a pre-carrier member record) falls back to the
  slot's own mode; every other unreadable carrier still fails the save.
- **The on-disk `memory_mode` is a ratchet.** Both writers of the line -- the
  full save and the empty-window metadata merge -- fold the mode the line
  already carries into that stricter-wins read, so a later writer on the same
  key can only tighten the field, never loosen it. The rows a restricted slot
  committed outlive the slot: its close pops it, `get_or_create_slot` hands the
  freed key to a persistent slot, and that slot's saves rebuild the line from
  their own state. Without the fold the first such save would relabel the
  committed private rows persistent and hand them to every learning reader. A
  persistent slot recreated on a restricted key therefore writes under the
  restricted mode, with no store name (next bullet). An absent or unrecognised
  on-disk value reads as persistent and tightens nothing.
- **An unreadable line is deferred; a corrupt line is rewritten strictest.**
  `get_metadata_status` answers `readable=False` for two different facts, and
  the writers tell them apart through `metadata_line_state` (`readable`,
  `transient`, `corrupt`; `METADATA_LINE_*`). A TRANSIENT failure -- the file
  could not be opened or decoded after the bounded retries -- clears on its
  own, so the full save raises and `_dirty` stays armed, the empty-window merge
  and `update_metadata_if` return without writing, and the next attempt
  re-decides. A CORRUPT first line -- bytes on disk that are not JSON, which
  every atomic line writer makes permanent rather than a write in flight --
  never clears, and a writer that kept deferring on it would never persist a
  row again: `closed` could never land and the tab would resurrect on every
  restart. So the WRITERS rewrite it: the full save and `_update_metadata_locked`
  (reached by `update_metadata_if`, which runs its guard against the line as it
  will be rebuilt) replace the first line, keep the rows after it, and stamp
  `memory_mode` at the STRICTEST mode (`STRICTEST_MEMORY_MODE`, the last of
  `MEMORY_MODES`) with no `memory_store`, whatever the slot or the fields say.
  The line's real contract is unknowable and the ratchet forbids relabelling it
  looser, so the strictest mode is the only value the rewrite may carry; the
  live slot then follows the tightened line like any other fold. A corrupt line
  therefore becomes a restricted transcript, never a persistent one. The healed
  line carries no `created_at` when the title or merge writer heals it (the
  identity is unknowable, and a minted one would read to the owning slot's next
  full save as a fresh incarnation after a delete); the full save's own heal
  stamps the slot's recorded identity. READERS are unchanged: the derivation seam
  and every identity-sensitive reader refuse both states, a corrupt line before
  the heal because it cannot be read and after it because it is restricted.
- **A restricted line names no `memory_store`.** See
  [session.md](session.md): with no carrier written for a restricted session,
  the store name is what the restart would read as a legacy owner claim and
  refuse. The member is re-selected from `agent` on the next turn. The store is
  gated on the FOLDED mode above, so a persistent slot writing under a
  ratcheted restricted line names none either.
- **A title-born header carries the mode too.** `_persist_title` can be the
  FIRST writer of a session's line (the on-send titling attempt runs before the
  turn-end save and the periodic flush), and a header with no `memory_mode`
  reads back as persistent after a restart — restored with memory writes
  allowed and listed in History as an empty persistent session (the ~190-byte
  ghost files of the 0.7.0.8 report). So the title upsert of an
  incognito/temporary slot includes `memory_mode`; a persistent slot's does
  not, because the transcript save owns the field. The title upsert folds the
  on-disk mode under the line lock like every other writer, and the full save
  canonicalises a rehydrated slot mode before applying the stricter-mode fold. If
  the titler outlives a closed restricted slot and a persistent replacement now
  holds the same transcript, it tightens that live replacement and its carrier
  before the metadata write. A failed write re-reads the line off-loop and restores
  the replacement only when the restricted mode did not become durable, matching
  the rows-only hand-over's commit-witness rollback.
- **A rows-only hand-over never files restricted rows under a looser line.**
  The close/cleanup drain (`_persist_handover_tail`) writes a popped original's
  unsaved tail with `rows_only=True`, which defers every slot-owned field —
  `memory_mode` included — to the line a same-key replacement published. A
  restricted original draining onto a PERSISTENT replacement's line would
  therefore put private rows under a line that says persistent. The line is a
  ratchet any writer may tighten, so when the retained mode is stricter than
  the line's the drain folds it in and TIGHTENS the line — `memory_mode`
  becomes the stricter value and a carried `memory_store` is dropped, since a
  restricted line names no store — and the rows land under it; the
  replacement's title, folder, tags and pin are not the drain's and stay. The
  LIVE replacement is tightened with it, in process and before the write
  (`_tighten_replacement_to_restricted_original`): the session summary and the
  export gate on `slot.memory_mode` and then read the whole transcript from
  disk, so a persistent replacement would hand the original's rows to a model
  or a file. The carrier compare-and-set runs before the live slot and restricted
  marker mutate, and retries once from a fresh carrier read, so a failed compare-and-set
  leaves no slot state to roll back. Its live carrier is tightened in place with its
  identity intact;
  the durable `execution_context` record carried on the line is folded to the
  line's mode by the same save (`_tighten_carried_execution`, also on the
  full-save carry), and the turn-start binding folds the line's canonical
  `memory_mode` into a live-first carrier and republishes the stricter carrier
  before memory context is built. A durable-only `read_session_execution`
  already folds the line itself. This is the same file the
  other race order reaches: a line the original had committed ratchets the
  replacement's own save down to the restricted mode.
  Refusing instead would lose the reply the user was watching with no retry
  path (the slot is popped), which is why the drain tightens rather than
  refuses. The reverse (a persistent tail onto a restricted line) commits and
  keeps the line's stricter mode untouched, as stricter-wins requires. The
  tightening is reachable only when the original committed nothing before the
  close; the replacement's next full save folds the tightened line back in, so
  the ratchet holds. If the hand-over save then fails, the line is re-read off
  the event loop while holding the transcript derivation lock: a mode at least
  as strict as the attempted tightening proves the atomic rows-and-line
  replacement landed and keeps the live tightening; a persistent or absent line
  permits restoring the replacement's prior mode, marker and same-generation
  live carrier only while that exact witness is still current. The carrier
  rollback runs inside the transcript hold and is safe off-loop because the live
  execution registry serializes it with its own lock; loop-owned mode and marker
  changes wait until the worker reports that rollback. Every line writer records
  the live holder's monotonic pending mode after its atomic rewrite and before
  releasing the transcript lock. Thus a concurrent tightener ordered before the
  read is visible in the line, while one ordered after it is visible in the
  pending witness re-checked on the event loop before rollback. An unreadable or
  busy line keeps the tightening, as does any restricted rollback floor, failing
  closed until a later read or save settles it. Carrier rollback never restores
  vouched authority; the next binding re-establishes that from independent identity.
- **A live slot follows a tightened line.** The hand-over tightens its replacement
  in process (above); the other writers reach the live holder from the event
  loop instead. A save thread that folds the line stricter than the slot's own
  mode records it as pending state. Each off-loop save registers adoption on
  its loop-bound executor future rather than after the await, and guarded saves
  shield that future so worker completion applies the pending mode and re-derives
  the restricted-key marker even when the awaiting task is cancelled; adoption
  targets the slot on which the committed mode was recorded, including a live
  same-key replacement, while the periodic flush remains a redundant convergence
  path. The turn-start binding
  reads the metadata
  line beside the live-first carrier, republishes the carrier at the folded mode,
  then tightens the slot from that same result. So a persistent slot
  recreated on a restricted key is restricted in memory too, and export, the
  summary and the memory gates never keep reading a persistent slot over a
  restricted line.
- **One derivation seam gates every reader that learns from a transcript.**
  A reader that DERIVES from a transcript -- hands rows to a model, a peer, a
  downloadable file or a memory store -- can be handed rows a restricted line
  already governs if it checks a mode and then reads rows in separate steps: a
  live slot can lag its file (a same-key persistent recreation of a closed
  restricted tab; another writer -- a second gateway on the same data home, a
  hand-over drain, a subagent or cron -- tightening the line while the slot
  still reads persistent), and a line read once is a snapshot a writer can
  tighten before the rows are read. Guarding each consumer separately does not
  end that class, so it is removed at the read seam instead:
  `ConversationLog.derive_messages` / `derive_messages_chained` /
  `derive_recent`, and `snapshot_for_consolidation(key, withhold_restricted=True)`,
  validate the line (`transcript_withholds_derivation`: `memory_mode` through
  `is_incognito_transcript`, failing CLOSED on an unreadable line, an absent
  file being no refusal) and read the rows under ONE `_locked` hold -- the same
  lock every tightening writer takes -- and raise `TranscriptWithheld` instead
  of yielding rows. `derive_messages_chained` locks and validates EVERY
  transcript in the tab-id chain (`chained_keys`) before reading. It resolves
  the chain once more inside the hold, validates that settled set, and reads
  only those settled keys directly through `_read_messages`; it never performs
  a third resolution that could pull in an unlocked, unvalidated member, and it
  preserves the plain reader's shared-list identity when the index knows no
  chain. A member that joined between the resolve and the hold is answered as
  `TranscriptBusy`, the same answer `publication_hold` gives a changed chain: a
  membership change is "cannot vouch right now", not a privacy verdict, so the
  export maps it to its retryable 503 rather than to the privacy 400. A
  restricted sibling of a legacy tab therefore governs the whole
  chained result. The public `derivation_hold` context is the single
  timeout-mapping seam for these reads. A lock the seam cannot take
  within the acquire ceiling is answered as `TranscriptBusy` (a
  `TranscriptWithheld`): the reader cannot
  vouch for the contract, so it gets no rows and every best-effort skip holds;
  the export and the tunnel map it to their retryable 503, the MCP
  `get_chat_session` says retry rather than private. A full save that meets a
  transiently unreadable EXISTING line refuses (raises, `_dirty` stays armed)
  before the ratchet can fold an empty dict as `persistent` over a restricted
  line; a corrupt line it rewrites under the strictest mode (the ratchet bullet
  above). Every deriving consumer is on the seam: the session summary
  (`chat_summary`, skip with reason `memory_mode`), every transfer bundle
  (`session_transfer._read_chained_history`, shared by the file export -- 400
  `export_slot_not_persistent` -- and the tunnel send -- 400
  `transfer_slot_not_persistent`, each auditing `denied`), the History
  browser's `list_sessions(summarize=true)` leg, the suggestions prompt
  (`suggestions._build_context`, session dropped), the MCP history tools
  (`search_chat_history` row dropped; `get_chat_session` refused as
  `refused_incognito`), and the consolidator (both snapshots refuse as
  `_CONSOLIDATION_REFUSED` with no failure charge; skill detection reads through
  `derive_messages`). The plain reads (`read_messages`, `read_messages_chained`,
  `recent`, ...) stay for transcript PLUMBING -- resume, save, rewind, fork,
  mirror, the History browser, migrations, injections -- which must see a
  restricted transcript. `test/test_transcript_derivation_seam.py` enumerates
  every plain-read reference in the source tree against a named plumbing list,
  so a new consumer written against a plain read fails the suite and must
  either move to the seam or declare itself plumbing in the diff.
- **One publication seam gates every transcript-derived durable or egress
  publication.** `ConversationLog.publication_hold(key, expected_keys=...)`
  takes the chained lock set used by `derive_messages_chained`, re-resolves and
  validates every settled member's live metadata line, and holds those locks for
  one durable write or synchronous response commit. Any chain membership change
  raises `TranscriptBusy`; egress callers pass the exact settled keys returned
  with the rows used to assemble their bundle. A restricted line raises
  `TranscriptWithheld`; lock timeout or unreadable metadata also raises
  `TranscriptBusy`. Intent and one-line summary sidecars, consolidation's
  preference/project/lesson/episodic writes, and auto-skill stage/create/refine
  writes enter this seam after their model calls. Export constructs its response
  under the hold; transfer revalidates immediately before the tunnel POST. A
  threading lock is never held across an await, so network transmission is the
  accepted residual window. `test/test_transcript_derivation_seam.py` enumerates
  the publish consumers, reasons the two await-bearing builder exceptions, and
  pins revalidation between assembly and commit. History search's query-time
  restricted-session filter reads the live metadata line (`list_sessions` plus
  `get_metadata`) on every query; its text index only shortlists content and is
  not the privacy snapshot.
- **The suggestions builder skips restricted transcripts.**
  `suggestions._build_context` walks `list_sessions()` and pulls each
  session's last user messages into a prompt shipped to the model and cached
  for the dashboard; it skips any session whose `memory_mode` is restricted,
  mirroring `chat_folder_suggest`, so a restricted transcript is never read
  there at all.

## HistoryConsolidator (`history_consolidation.py`, re-exported by `history.py`)

Background task that fires once a session's message count reaches
`_CONSOLIDATION_THRESHOLD` (30) messages past its last consolidation offset. Uses the
persistent background ACP session (kiro-cli long-running session, same as
cron/heartbeat/lesson extraction) to extract:
- `history_entry` → appended to today's daily history file
- `preferences_update` → overwrites `preferences.md` if changed
- `projects_update` → overwrites `projects.md` if changed

The two `*_update` values replace the whole file, so each is gated by
`_is_plausible_memory_file()` before writing: a value that does not start with
the file's mandated markdown header (`# User Preferences` / `# Active
Projects`) is discarded with a warning instead of written. This rejects
protocol-word answers (the literal string `unchanged` and similar), which would
otherwise destroy the file AND — because the next consolidation prompt embeds
the file's current content — prime every later pass to echo the placeholder
into the other memory file, keeping both destroyed until a human rebuilds them.
The prompt sanctions omitting the key entirely when nothing changed (the write
path treats a missing key as no-change), so a compliant model never needs to
echo the file back — removing the temptation that produces placeholder answers
and saving output tokens each pass; the header gate remains the backstop.
The gate requires the exact mandated header as the first line AND a body that
does not normalize into a known placeholder ("unchanged", "no changes needed",
"N/A", …); markdown emphasis wrapping is stripped first so a decorated
placeholder cannot bypass the set. An empty body after the exact header is
accepted (deleting the last entry is a legitimate complete file), and there is
deliberately no size floor — a legitimate memory file can be a single tiny
bullet, and a legitimate consolidation can shrink a bloated file by half or
more. The discard warning logs only the rejected value's length, never its
content, because raw model output can contain anything and the log ring feeds
the dashboard.

Non-blocking via `asyncio.create_task`. Requires `SessionManager` to be passed
at construction time; consolidation is silently skipped if no session manager
is available.

**Loop safety:** the task body runs on the event loop thread, so any blocking
work inside it must be offloaded. `_write_structured_memory` and `_save_lessons`
both embed items via blocking in-process llama.cpp inference calls
(`write_lesson` performs a rule embed plus up to `_MAX_BACKFILLS_PER_CALL` lazy
backfill embeds per lesson), so they are invoked through `asyncio.to_thread()` —
running them inline would freeze the gateway loop (heartbeats, Slack, dashboard)
for the duration of each embed, and can trip the faulthandler hard-kill. A
transcript-derived pass resolves its lesson or episode embedding before entering
`ConversationLog.publication_hold`; `rule_emb_resolved` / `embedding_resolved`
prevent a second inference inside that short hold, and lazy lesson backfills
remain for the standing repair sweep. (The
model load itself never blocks the embed call — it runs on a background daemon
thread; embed returns `None` until the model is resident.) The same
applies to `TaskRunner._extract_lesson`, which calls `write_lesson` after a task
failure. Dashboard memory handlers that write semantic entries or embed a query
(`set_semantic`, `_try_embed`) offload the same way. Because these writes now run
on worker threads concurrently with loop-thread reads (`search_episodic` during
context assembly), `VectorMemoryStore` serializes the semantic UPSERT
read-modify-write and the FAISS add + id-map append with `_db_lock` (a `RLock`);
`write_lesson`'s dedup scan and backfill UPDATEs rely on sqlite's serialized-mode
statement atomicity (WAL + `busy_timeout`) rather than application-level locking
— the lock is never held across a blocking embed.

**Embed budget:** the offload bounds the loop, not the cost. One pass writes up
to `_MAX_SEMANTIC_PER_CONSOLIDATION` + `_MAX_EPISODIC_PER_CONSOLIDATION` rows and
each embeds inline, so a degraded embedder made the pass cost N times one call's
latency on an embed-pool worker every other embed consumer shares.
`_write_structured_memory` therefore charges both tiers' store writes against
`_EMBED_BUDGET_SECS_PER_PASS`. The first overrun latches for the rest of that
pass: every remaining row is written with `defer_embedding=True`, which stores the
same NULL-vectored row a failed embed already produces and leaves the vector to
`backfill_missing_embeddings`. On the semantic side the same flag also takes the
stale-episodic retirement down its text-only arm, the arm it already takes when an
embed returns nothing. The deferral is logged once per pass, never once per row.

## Stop Events

Stop events are persisted to JSONL as `system` messages. The structured
stop-event data lives in the `cls` field as a JSON-encoded object (which
`parse_cls_meta` lifts into `meta` for frontend consumers via
`StopEventCard`). The `content` field mirrors the same JSON for
backward-compatible consumers that only read `content`.

```json
{
  "role": "system",
  "content": "{\"kind\":\"stop_event\",\"id\":\"stop-<uuid>\",\"state\":\"stopped\",\"outcome\":\"soft\",\"ts_start\":\"2026-04-27T00:07:40Z\",\"ts_end\":\"2026-04-27T00:07:40Z\"}",
  "cls": "{\"kind\":\"stop_event\",\"id\":\"stop-<uuid>\",\"state\":\"stopped\",\"outcome\":\"soft\",\"ts_start\":\"2026-04-27T00:07:40Z\",\"ts_end\":\"2026-04-27T00:07:40Z\"}",
  "ts": "2026-04-27T00:07:40Z",
  "source_thread": "dashboard",
  "source_user": "dashboard"
}
```

Possible `state` values:

| State | Meaning |
|-------|---------|
| `stopping` | Cooperative cancel in flight; waiting for agent ack |
| `stopped` | Agent acknowledged cancel; session preserved |
| `stop_failed_reset` | Agent did not ack within budget; session was hard-killed and reset |

The stop event is inserted at soft-start time with `state: "stopping"` and
updated in place (same `id`) when the outcome resolves. The updated message
is re-broadcast via `_on_message` so the frontend `StopEventCard` transitions
from `stopping` → `stopped`/`stop_failed_reset`. A press that finds an
orphaned card from a prior attempt **in the same turn** (no turn-opening row —
`user`/`nudge`/`subagent`, mirroring `TURN_OPENER_ROLES` in
`groupDisplayItems.ts` — after it) RE-ARMS that row in place (same `id`, back to `stopping`) instead of
resolving it and appending a fresh row — the pane upserts stop cards by
`meta.id`, so a resolve-plus-append put two chips on screen for one press
(`_open_stop_event_card` in `chat_handlers.py`, shared by `/stop` and
`/interrupt`). A cross-turn orphan is settled where it lies and the press's
card is appended fresh, so the chip lands in the turn the user stopped.
Because reuse makes card ids non-unique across presses, per-attempt identity
for the resolver callbacks is carried by the monotonic
`slot._stop_generation`, not by the card id.

Stop rows are presentation, not conversation: the tail-preview reader
(`TranscriptReadProjection.last_message_info`, which feeds the Crew Members
roster subtitle and the session-list preview) skips rows matched by
`is_stop_event_row` so a transcript ending on a stop never previews the raw
JSON payload. The skip moves only the preview TEXT: the returned epoch reads
the newest skipped STOP row (a stop is activity), falling back to the
previewed row's own timestamp — every other non-previewable row (a quiet
zero-width-space reply, an empty content row) leaves the timestamp travelling
with the previewed row, so roster recency ordering is unaffected.

After a cancelled turn, `context.build_cancelled_turn_preamble` reads the
cancelled user prompt and partial assistant output from this log and
prepends them to the next prompt as a bracketed preamble, because kiro-cli
discards cancelled turns from its own ACP conversation log. The flag
`_Session.prev_turn_cancelled` (set by `SessionManager.stop_turn` on
soft-cancel success) gates the one-shot re-injection.

## Session Lifecycle

Cold-start prompt replay merges the on-disk chained transcript with a frozen
live-window snapshot before applying role quotas, a tail-first model-window
budget and redaction. Message identity is `meta.mid`, falling back to a delivery
`sendId` or an exact legacy timestamp/role/content tuple. Only object-valued
metadata supplies delivery IDs; scalar and list metadata use the legacy identity
without changing the persisted row. Cross-source matching
is one-to-one, so repeated text with distinct IDs and repeated id-less rows are
retained. The triggering request's captured identity is excluded whether or not
that row was flushed. Queue drain passes its appended row directly to the runner,
including `inject` rows with `cron`, `recovery` and `user_replay` kinds. Other
entry points capture the latest user, nudge, subagent or inject row before any
await. Same-text older deliveries remain history because exclusion uses the
captured row's identity. There is no additional whole-slot prefix after this replay.
An explicit replay, including an empty replay, suppresses `ContextBuilder`'s
inner JSONL fallback; only an absent replay requests fallback construction.

1. New session → full context injected (memory + skills + lessons + last 20 messages)
2. Messages saved to JSONL with provenance after each response
3. Context ≥ configured threshold (`session.autocompact_pct`, default 70%) → compaction via kiro-cli `/compact` (fire-and-forget)
4. Session expires (30min idle) → provider killed
5. User returns → new session with history re-injected
6. After the message count crosses `_CONSOLIDATION_THRESHOLD` (30) past the last offset → background consolidation → structured memory updated

## Threads: an Anchored Session (`dashboard/chat_threads.py`)

A **thread** is an ordinary chat session plus an **anchor** to one message of
another conversation. It is a relation, not a container:

```
Anchor = (surface, conversation, message_id)
```

Dashboard anchors are `(dashboard, <parent slot key>, <mid>)`; a channel's are
its own (`(slack, <channel_id>, <thread_ts>)`). Any message of any chat surface
can carry one -- the user's or the assistant's -- addressed by that message's
durable `meta.mid`.

The thread's SESSION is minted through the same core `session_create` uses
(`session_control.create_session`), so it has everything a chat surface has by
construction: tools with real approval cards, a model, memory, a steer channel, a
queue, a stop record, a transcript, compaction, and a place in the sidebar. It is
deliberately NOT a `session_fork`: fork copies the whole parent transcript and
refuses an `agent` override, while a thread wants neither the parent's rows nor
that refusal -- it may take an agent of its own.

**On the dashboard the anchor is stored ONCE**, in the parent's **anchor index**,
which is what lets a conversation list its threads in one read. A failure to record
it retracts the minted session, so there is never an index the thread outlives or a
thread the index does not know.

A second copy on the thread's own metadata line was written here for durability --
so a thread could still name what it hangs off if the index were lost -- and nothing
ever read it, which makes it one more shape to keep consistent for no reader. Slack
keeps its `_thread_anchor` metadata because a Slack thread has NO parent transcript
to hang an index off, and Slack's own recorder reads that copy as its idempotency
guard: one surface's only record, not a duplicate of another's. That recorder is
not in this change -- it lands with the Slack anchor recorder, which ships
separately.

`ThreadAnchor` itself lives in `messaging/link.py`, beside the `ChannelLink` it is
a message id away from, and is ONE type across every surface -- the dashboard's
`chat_threads.Anchor` is an alias of it. It holds each field to a bounded opaque
shape and no further, because a type that pattern-matched one surface's spelling
for a message id would refuse the others; the dashboard's own extra rule, that its
`mid` is a minted row id, is checked at the route and in `open_thread`.

**On a channel, only the first half is written.** The index's admission rules are
what makes it dashboard-shaped: it refuses unless the parent's transcript exists
and holds the anchor's row. A Slack channel has no such row -- Slack's transcripts
are per thread (`slack:<ts>`), not per channel -- so a channel's anchor is recorded
on the thread session's metadata and in the crew log, and the index is not asked to
hold it. See `slack-gateway.md` § Thread anchors. Each surface decides that for
itself at its own dispatch site; there is no shared adapter layer, because one
registrant and no lookup is not a seam yet.

### The anchor index (version 2 of the thread sidecar)

Same file as version 1 -- `ConversationLog.threads_sidecar_path(key)` =
`<sessions dir>/.threads/<safe key>.json`, keyed by the parent's transcript key
(`chat_utils.slot_history_key`) -- with a second half:

```json
{"version": 2,
 "anchors": {"<mid>": {"thread_slot": "chat-…", "title": "…",
                       "opened_by": "user|agent:<key>", "opened_at": "…",
                       "closed_at": null, "summary_mid": null}},
 "threads":  {"<mid>": [ … version-1 replies, read-only … ]}}
```

`anchors` is what this writer owns. `threads` is version 1's transcript of
replies -- from when a thread had no session of its own -- and it is **read in
place and never rewritten**: the anchor writer carries it over verbatim, and a
conversation that only ever had v2 threads grows no empty legacy map. Both halves
come out of ONE hardened open (`_read_thread_sidecar`), so they cannot be read
under two different sets of rules; a document carrying NEITHER half, and a half
that is PRESENT but not a map, both raise `ThreadStoreUnreadable` rather than
reading as absent -- treating `{"threads": []}` as empty is exactly what would
let a write erase a damaged file.

It is still a third sidecar next to `.summaries` and `.intents`, and still its
own file for the reason each of those is: its own writer and no mtime contract
with the transcript. Anchors never enter `slot.messages` or the JSONL, so the
transcript read paths, the frozen-prefix save model and consolidation are
untouched.

**Admission is version 1's, unchanged, because the reasoning is unchanged.**
`read_thread_anchors` / `write_thread_anchor` / `update_thread_anchor` run the
read-modify-write under the transcript's own `_locked(key)` -- the lock
`delete_session` unlinks the sidecar under -- and:

- REFUSE (`"missing"`) when no transcript exists, for the reason
  `set_cached_intent_summary` gives: a caller holds no lock while it suspends,
  and an unconditional write landing after a delete would recreate the sidecar
  and resurrect a chat the user was told is gone;
- admit an anchor against ONE transcript: the opener captures the metadata line's
  `created_at` (`thread_transcript_identity`) BEFORE the parent lookup -- so the
  identity is never younger than the rows the parent was found in -- and passes
  it back as `expected_created_at`; a chat deleted and recreated under its
  deterministic key after that capture answers `"replaced"` instead of receiving
  the old chat's thread;
- refuse unless a row on disk carries the parent's `meta.mid` (`"unflushed"`): an
  anchor is durable only through the row it hangs off, so a parent that exists
  only in the slot's memory window is not admitted until the slot has flushed it,
  or a crash before the flush would leave the thread unreachable. For a pre-field
  transcript with no `created_at`, that same check is what tells a replacement
  apart, since a replacement never carries the old chat's message ids;
- answer `"duplicate"` for a mid that already carries an OPEN anchor -- one
  thread per message, because a second would split the discussion with no way to
  tell which is live. A CLOSED anchor does not refuse: closing is what makes the
  message available again.

`thread_anchor_admissible(key, mid)` is a read-only probe answering what the
write would, so an opener refuses BEFORE minting a session it would have to
retract. The write re-checks every rule under the lock, so the probe is an
optimisation and never the authority.

**Retention bounds hold whatever the file says.** The file is opened once without
following a link, sized on that descriptor and read to the ceiling
(`THREADS_SIDECAR_MAX_BYTES`, 64 MiB; a link, a non-regular file or more bytes is
`ThreadStoreUnreadable`, and the writer answers `sidecar_full` at the same
ceiling). An anchor row is reduced to `THREAD_ANCHOR_FIELDS` and nothing else --
`thread_slot` a bounded opaque key, `opened_by` one of `user` / `agent:<key>`,
`opened_at` and `closed_at` ISO-8601 instants, `summary_mid` a minted row id --
so `title` is the ONLY field that can carry prose, and it is cut at
`THREAD_ANCHOR_TITLE_MAX_CHARS` (200) and redacted at every output boundary. A
row breaking any of those is dropped whole rather than half-read; a key that is
not a minted row id (`m-` + 16 hex, `mint_row_mid`) is not an anchor; the map
stops at `THREADS_MAX_ANCHORS_PER_SIDECAR` (5 000) entries in file order. The
write side holds the same line as version 1: the `.threads` directory must be a
real directory (a link there is refused before anything is created under it) and
on POSIX the leaf is replaced relative to its pinned descriptor
(`atomic_write_at`), so no write of this store lands outside the session
directory. A sidecar written by another hand under the data home cannot grow the
gateway's memory past a legitimate one, nor reach the dashboard through a
metadata field or a key, nor redirect a write.

`ConversationLog.append_thread_reply` is **removed**. It wrote a `version: 1`
document, so it would clobber the anchors half, and nothing writes replies once a
thread is a session of its own.

Session Storage (`session_storage.py`) is unchanged: it counts the sidecar in the
session's size and moves, restores and purges it with the transcript
(`_unit_paths`, `_canonical_origin` accept `crew/.threads/<stem>.json`; the
location is the one spelling `history.threads_sidecar_for_stem` gives), publishing
and rolling it back under the transcript's lock, relative to the pinned `.threads`
descriptor. `delete_session` still takes the sidecar with the transcript
all-or-nothing. **Anchors do not travel**: a fork, a transfer and an export carry
a transcript's rows, not its sidecars, so a copied chat starts with no threads and
the original keeps its own.

### Opening: one coroutine, two entry points

`open_thread(state, anchor, *, title, agent=None, opened_by, note="")` is the
whole implementation. In order: resolve the anchored conversation and probe
admission; mint the slot through `create_session`; write the parent's index entry,
which is the dashboard's ONE record of the relation; emit `thread/opened` and the
`chat.thread_anchor` frame; deliver the opener's note when there is one.

**Nothing about the parent is injected at open.** A thread is a perpetual session,
so a quote placed in its transcript at open would be carried for the thread's whole
life and re-read on every turn -- and it would describe the parent as it stood at
the click, which is the wrong parent for a thread first written to a week later.
The parent reaches the thread at its FIRST TURN instead, as a summary built then.
See [The parent-context projection](#the-parent-context-projection).

**The note is the only thing anybody said, so it is the only thing delivered.** The
route passes whatever the opener typed. A note travels `send_to_target` -- the same
verb `session_send` uses, with the parent as the caller, because the parent CREATED
this slot and the ownership fence therefore admits it without a new authorization
path -- and because it travels that ordinary path it gets the queue receipt every
chat message gets, not a bespoke first row that looks unlike every later one. A
bare click delivers nothing: the thread starts no turn, and the drawer's empty hint
("Type a message to start it") is the truth the person sees. A full-tool turn on
boilerplate nobody typed would spend a model call unasked. The result reports
`seeded`, which says whether a note was delivered.

The dashboard writes no `_thread_anchor` on the thread session's own metadata. It
has somewhere durable to put the relation -- the parent's index, guarded by the
parent row the anchor hangs off -- so a second copy would be a second thing to
keep true. The thread still reaches that one record without a copy: its own
`session/opened.parent.slot` names the parent slot, which is enough to read the
parent's index and find the anchor that names this thread. Slack is the asymmetric
case and keeps its metadata copy for a reason of its own, set out in
[slack-gateway](slack-gateway.md).

| opener | entry point |
|---|---|
| the user clicks a message (streaming or not) | `POST /api/chat/threads/{mid}/open` |
| an agent opens one on the message it is answering | MCP `thread_open(title?, anchor_mid?, agent?, note?)` on `@kirocrew-dashboard` |

Both reach the same route. The user's call names the conversation in the body
(`slot_key`); the agent's does not, and the conversation is then taken from the
VERIFIED `X-Session-Key` -- never from the body -- so the tool cannot open a
thread on another chat. `thread_open` is in `SESSION_CONTROL_TOOLS` (its dispatch
needs the verified caller key) and in `channel.CHANNEL_AGENT_BLOCKED_TOOLS` (a
channel-bound session's conversation is a thread other people are in; channel
surfaces reach threads through their own adapter).

The CALLER passed to `create_session` is the anchored conversation itself, which
is what makes the child inherit the right things without a second policy: the
parent's workspace (the memory boundary), its trust posture, its project and its
folder, plus a `session/opened` entry whose `parent` link already records the
thread's lineage -- so `thread/opened` adds the anchor and nothing else. It also
means thread-opening is gated by `agent.session_control` (default true), whose own
description is "let one chat session open a new session": a thread is one session
opening another, so an operator who turned that switch off gets a plain refusal
rather than a bypass.

**Retraction.** An anchorless thread is worse than no thread -- a session in the
sidebar that the conversation it belongs to cannot find, and whose own projector
has no anchor to build a summary from -- so a failure to record either half closes
the minted slot. A failure to deliver the NOTE does not: the thread exists once its
anchor is recorded, and throwing away a real thread over one message would be the
larger loss. The result reports `seeded` either way.

Closing archives the conversation; the slot is DELETED from history on top of that
only when it is empty, and emptiness is checked rather than assumed. The minted
slot is an ordinary sidebar session from the moment `create_session` returns -- the
person can open it and type into it -- and the anchor write that decides this
retraction can wait out the patient off-loop lock acquire, seconds rather than an
instant. A message sent inside that window is somebody's, `delete_session` has no
recovery path, and the archive is the right home for a chat that was used. Any
conversation row counts (`_RETRACT_KEEPING_ROLES`), a streamed chunk included: a
turn is already answering, and its text is the answer before the row persists.

### Anchoring to a message that is still streaming

A streaming assistant row has **no `mid`**: ids are minted when the row persists,
post-turn. So anchor resolution is:

| target | anchor |
|---|---|
| any persisted message (the user's, or a finished reply) | that message's `mid` |
| the assistant row still streaming | the **user message that started that turn** |

The client asks for the second case with the literal path segment `inflight`,
which `turn_anchor_mid` resolves to the newest user row carrying a mid. This is
not a workaround for the storage constraint: the thread belongs under the sentence
that started the work, so the paradigm and the constraint agree.

The partial reply is not lost -- the thread's projector reads it at the thread's
first turn. `in_flight_snapshot(slot)` reads it from the slot's own `role="chunk"`
rows, which is where the dashboard's streaming text lives (`chat_runner` appends
one per delta, plus one for the redactor's withheld tail at each segment flush; the
turn's own `assistant_text` local is unreachable from here and is reset at every
tool boundary). The read is **non-consuming and never blocks**, by three
properties: the list reference is copied first and `purge_chunks` REBINDS
`slot.messages` rather than mutating it, so a segment finalizing under the read
cannot empty the copy; `chat_utils._collapse_wire_rows` returns a fresh merged row
and never mutates its input dicts, which are shared with the live window; and
nothing touches `slot._pending`, the queue a live SSE or OpenAI-compat reader owns
(`release_pending_chunks` and `purge_chunks` are the consuming reads and are not
called). An empty snapshot from a parent that is not running means nothing is
streaming; an empty snapshot from a parent that IS running marks the projection
`partial` anyway, rather than letting it claim the parent had finished.

**Opening never touches the running turn.** It holds no semaphore, does not gate
on `provider.has_active_turn()`, writes nothing into the turn, and is therefore
not a fourth `_handle_busy` branch, not a `messaging.queue_mode` value and needs
no capability gate -- which is what leaves [messaging](messaging.md)'s one-turn-
per-session, exact-FIFO and single-drain-turn rules untouched. Open never fails
because the parent is busy.

### The parent-context projection

A thread is a full session, so the parent's history is not its history. Nothing is
injected when the thread opens, and nothing is ever injected verbatim -- both
follow from sessions being perpetual. `dashboard/thread_projection.py` builds what
the thread is told, and `_run_chat` calls it one step before
`drain_pending_context` reads the queue.

**The first turn gets one summary block, from three bands.** The parent's own
stored intent summary, REUSED rather than re-derived (`read_intent_summary`; a
stale payload is used and labelled stale, because the window and the delta cover
what moved since). The anchor window: the rows around the message the thread hangs
off, with the anchor itself weighted, its text read from the parent's TRANSCRIPT so
the block and the drawer's quoted parent cannot disagree. And everything after the
anchor, chunk-folded -- summarized in groups of `FOLD_CHUNK_ROWS`, so a parent that
ran for a thousand rows since the anchor still fits `BLOCK_BUDGET_CHARS`. There is
no compaction-summary band: the crew log's `on_compaction_applied` records
percentages, and nothing writes a compaction summary anywhere, so the intent
summary is the parent's own account to reuse.

**Every later turn gets only the delta** -- the rows the parent appended since the
recorded cursor. An empty delta injects NOTHING, rather than a block saying nothing
happened, so a thread beside an idle parent pays no block per turn.

**Addressing is by the parent's crew-log `seq`, joined by TIME.** The log's
`message/received` carries no `mid` (its emitter does not run where a slot appends,
there being no session id yet), so an anchor's id cannot be turned into a log
position at all. `open_thread` resolves it once -- the last parent entry at or
before the anchor row's timestamp -- into `parent_log_seq` on the anchor row. Time
rather than correlating the Nth transcript row with the Nth `message/*` entry,
because compaction rewrites the transcript and rewind drops rows from it while the
append-only log keeps both. Once at open rather than per turn, because a thread
first written to after the parent compacted its anchor row away has no row left
whose timestamp could be resolved. An unresolved position reads as
`THREAD_ANCHOR_PARENT_LOG_SEQ_UNKNOWN` and takes a small window off the parent's
TAIL -- never a window from seq 1, which would project a whole parent for exactly
the threads whose position is least trustworthy.

**Naming the parent's log takes more than its live handle** (`slot_log_sid`). An ACP
handle carries a session id only once `session/new` has succeeded on THAT client, and
a thread reads its parent BETWEEN the parent's turns -- so a slot whose client is
fresh (a cold start, a provider switch, a turn torn down) has a conversation with a
crew log and no live id for it. Asking the handle alone reads that as "this chat keeps
no log", which leaves a correctly anchored thread with no window and answers
`thread_context_read` with `no_parent_log`. The order is: the live handle, then
`_crew_log_opened_sid` (this process's statement of the store the slot writes), then
`_crew_log_previous_sid` (the store it was on before a switch -- same conversation),
then the thread's own lineage edge, and for the read path only, a store scan by slot
key. All but the last are in memory, so the scan is what answers after a restart; it
is not on the per-turn projection path. No copy of the id is kept on the anchor: the
slot and the log both state it, and a third copy would be a shape to keep in step for
no reader.

**Each projection records itself.** `thread/context_projected` on the THREAD's own
log carries the cursor, the window start, the summary version, the block size, the
row count and `partial`. It is written even for a projection that injected nothing,
which is what makes a quiet turn distinguishable from a turn the projector never
ran on, and it is where the next turn reads its cursor from -- so a gateway restart
between opening a thread and writing to it neither replays the first projection nor
skips it. Two consecutive entries bracket exactly the parent rows summarized
between them.

**Exact rows stay reachable.** A thread carries a handle (parent slot key, anchor
mid, parent turn id) and `thread_context_read` reads the real rows on demand. The
projection is the cheap always-on account; the tool is the precise one, and a model
that needs the exact bytes asks for them.

**Degrading is the rule, not the exception.** A parent that is gone, a crew log
that is off, an unreadable window, a summarizer that cannot be reached: each costs
the turn its block, never the turn. One fold group whose model call fails falls
back to that group's own digest lines, so a rougher account of eight rows beats no
account of two hundred.

### API

All routes answer 404 `slot_not_found` for a missing slot or a foreign app caller
(anti-enumeration, App Kit §5.2). There is no `not_crewmate_chat` refusal: threads
work on every chat surface, because the thread runs as its OWN session rather than
as the crewmate whose slot it hangs off.

- `GET /api/chat/threads?slot=<key>` -- `{"threads": {<mid>: summary}}`, one entry
  per message that has a thread. Two shapes fold into one map, because the footer
  renders one badge per message and does not care which era the thread came from:
  `{"kind": "session", "thread_slot", "title", "opened_by", "opened_at",
  "closed_at", "summary_mid"}` for an anchor, and version 1's own
  `{"kind": "legacy", "count", "last_reply_ts", "participants"}` for a legacy
  thread. An anchor wins when a message has both -- the live thread is the one to
  open, and the legacy replies stay reachable through the fold. Deliberately NOT
  folded into `GET /api/chat/slots/{slot}`: the transcript read path stays
  unchanged.
- `GET /api/chat/threads/context?from=<seq>&to=<seq>` -- the parent's EXACT rows,
  for the calling thread only. Registered BEFORE `{mid}` (aiohttp matches in
  registration order, so the literal would otherwise be swallowed by the pattern
  and refused as an invalid mid). Names no conversation: the thread is resolved
  from the verified `X-Session-Key`, so the one parent it can reach is the
  caller's own. Answers `{anchor, from, to, last_seq, rows}` with one digest line
  per message, the span capped at 40 rows; 404 `not_a_thread` for a session with
  no anchor, which is deliberately not an empty page. Backs MCP
  `thread_context_read`, which is registered on `kirocrew-core` rather than on
  `kirocrew-dashboard` even though `thread_open` is a dashboard tool. The reason is
  which sessions mount which server: the dashboard set is OPT-IN, so a session
  whose agent spec references neither the server nor its tools has none of them --
  and the first-turn block injected into EVERY thread names `thread_context_read`
  and tells the model to call it. A thread run by the default agent therefore read
  an instruction to use a tool it did not have, and said so when asked. Core is
  always mounted, so the promise is keepable. Containment is unchanged by the
  address: the tool takes no target, resolves the parent through the same strict
  identity gate the dashboard verbs use, and stays on the channel-agent block list.
- `GET /api/chat/threads/{mid}?slot=<key>` -- `{"parent": {mid, role, content,
  ts}, "anchor": summary|null, "legacy_replies": [...]}`. The thread's own
  MESSAGES are not here: they are the thread slot's transcript, read through the
  ordinary chat endpoints, which is the point of it being a real session. 404
  `parent_not_found` when the mid is no longer in the chat (the frozen disk prefix
  plus the memory window, after `chat_handlers._reconcile_slot_window` -- the same
  reconciliation the detail and resume handlers run), 400 `invalid_mid`.
- `POST /api/chat/threads/{mid}/open` `{slot_key?, title?, agent?, note?}` --
  answers **201** `{thread_slot, anchor, title, seeded}`. `{mid}` may
  be the literal `inflight`. Refusals: 400 `invalid_mid`, 400
  `missing_required_fields` (no `slot_key` and no caller session naming one), 404
  `slot_not_found`, 404 `parent_not_found`, 409 `already_open` (carrying
  `thread_slot`, so the caller opens the existing thread instead of hunting for
  it), 409 `transcript_missing` (no transcript yet, or the parent row not flushed
  yet), 409 `transcript_replaced`, 409 `threads_full`, 400 `surface_unsupported`,
  503 `threads_unavailable` (no conversation log, an unreadable sidecar, a lock
  timeout, an `OSError` out of the sidecar write). A `create_session` refusal is
  surfaced verbatim with its own code, since translating it would hide which one
  fired.

**What version 1's reply route left behind.** `POST .../reply` is gone, and with
it `_run_thread_turn`, the `_in_flight` set and `409 thread_turn_in_flight` (the
one-reply lock), the 32 KiB per-reply body cap and the 64 000-character stored
clip with its `[reply clipped]` marker, the 500-per-thread and
5 000-per-sidecar reply caps, the `thread:<slot>:<mid>` session key, the
`build_thread_message` envelope, and the read-only tool posture
(`publish_readonly_spec` under `READ_ONLY`, or `REJECT_ALL` elsewhere) with its
"actions go through the main chat" boundary prompt. A thread's messages are
transcript rows under the transcript's own limits, answered by ordinary turns
with ordinary approval cards, so none of that machinery has anything left to do.

### Close

`close_thread(state, anchor)` stamps `closed_at` on the anchor and posts a closing
card in the PARENT conversation. The card states the close and names the thread; it
carries no written summary, because no surface composes one -- a body line would be
one fixed sentence presented as a report. The card is a row under the display-only
`thread_closed` role whose `meta` carries `thread_summary: {thread_slot, title}`,
and it persists, rewinds, exports and re-reads like any other row. The role is what
keeps it out of the parent's model-visible history: `context.RECALL_ROLES` admits
`user` / `assistant` / `inject`, and replay rebuilds an admitted row as `role` plus
`content` alone, so an `assistant` card would reach the next cold turn as the
crewmate's own earlier words -- "Thread ended." plus the thread's title, a sentence
the crewmate never said -- with the `meta` that made it a card already dropped and
nothing able to re-attach it. A role outside that set is dropped from replay whole,
which is the accurate account: nothing was said.
Its own `mid` is recorded as the anchor's `summary_mid`, which is the back-link
target. Both link forms resolve because the thread is a real slot:
`/chat/<thread_slot>` opens it as a full page, `/chat/<parent_slot>?thread=<mid>`
opens the same slot in the drawer. Closing does not delete the session. A close on
a mid with no anchor answers `thread_not_found`; on an already-closed one,
`already_closed`.

The dashboard draws that row as a CARD (`pages/chat/ThreadClosedCard.tsx`, claimed
by the `thread_closed_card` renderer entry on a `thread_closed` or `assistant` row
carrying `meta.thread_summary` -- the `assistant` spelling is claimed so a transcript
holding the card under that role keeps drawing it), with the recorded `thread_slot`
as a pressable way back in.
The drawer takes it when an anchor still claims that slot; when none does -- closing
frees the message, so a later thread on it holds the anchor and every older card's
slot matches nothing -- `openThreadSlot` answers false and the host opens the
session as a full page through its ordinary `onSessionOpen`. The thread is a real
session either way, so the two outcomes are "in the drawer" and "as a page", never
"nothing happens".
Two reasons it is not left as the row's own text: read as prose it is the crewmate
saying "Thread ended.", which the crewmate never said, and a back-link no surface
renders is a pointer the reader does not have. The stored text is that prose rather
than a `[Thread closed]` marker because the sidebar's session-card preview has no
renderer and shows the row raw, where a bracketed marker read as a leaked token. The card is keyed by SLOT and the
drawer by the anchored `mid`, so the controller finds the mid by the slot its
anchor holds (`openThreadSlot`); one anchor holds any one slot, so the lookup is
exact, and a card whose anchor is gone offers no control rather than guessing.

**Reading an ended thread and starting a new one are different requests on the
same message.** A footer, the close card's back-link and a `?thread=<mid>` ADDRESS
all name a thread the reader can see, so each asks with `read` and the ended thread
opens, readable, its composer saying that a message there continues it. The row's
own **Reply in thread** asks without `read`: closing released the message, so that
starts a fresh thread. One rule for both would either hide the conversation the
reader pointed at or refuse to start the next one -- and on the address in
particular, minting would answer a reload with a new conversation and leave the
ended thread unreachable from its own URL.

**The transition is persisted before the card is published**, and the card's mid
is stored by a second write. The card is a durable row in the parent AND it is
broadcast, so posting it first would leave a "thread closed" card standing over a
thread the index still reads as open -- and `sidecar_full` refuses the same way
every time, so each retry would append another card and none would converge.
Ordered this way the only surviving failure is the benign one: a closed anchor
whose card did not post, or one whose `summary_mid` was lost, which the row shape
already admits and which costs the back-link and nothing else. The second write is
best-effort for that reason: raising there would report a close that happened as a
failure.

`POST /api/chat/threads/{mid}/close` `{slot_key, thread_slot}` is the entry point,
and **End thread** in the drawer header is its caller. Those two fields are the whole
body: the one caller sends no free text, so a `reason` or `summary` field would be a
parameter the route accepts and no surface fills. There is no agent branch either,
for the same reason -- no `thread_close` tool exists, so every close names its slot
outright.

`thread_slot` is the thread the CALLER believes it is ending, and an anchor naming a
different one is refused `409 already_closed`. The mid identifies the MESSAGE, and a
message carries a succession of threads, so a drawer left open while this message was
ended and reopened elsewhere would otherwise end the replacement. It is required
rather than optional for that reason: an omitted field restores the hole exactly.
This is a distinct guard from `update_thread_anchor`'s `expect_thread_slot`, which
catches the row changing between `close_thread`'s own read and its write.

**A thread route refuses an app caller on a LINKED slot**, even one the app owns.
These routes address the slot's transcript, and a linked slot's transcript belongs to
whatever bound it. An app may claim a name a later binding links -- only `member-` is
reserved -- and the binding does not ask who created the slot, so ownership survives
while the transcript key becomes the binder's. The check is re-run after the awaits a
handler makes, because the link can be set inside one.
It is deliberately a different control from the `X`
that dismisses the panel: dismissing is a view action and reaches no thread, while
ending closes the anchor, releases the message to carry a later thread, and returns
the result to the conversation the thread came from. End is offered only for a live
session thread -- a version 1 fold has nothing to end, a closed one nothing left to
close -- and a refused end leaves the thread open and on screen rather than hiding
an outcome that did not happen.

End is also the release valve for the second copy of the relation. The anchor is
recorded in the parent's index under retraction at open, but the thread SESSION is a
second copy of the relation and can be deleted through the ordinary session
controls. That leaves an open anchor whose
`thread_slot` resolves to nothing, and the anchor is what refuses a second thread
on the same message. Closing needs only the parent's index, so End releases such an
anchor from the drawer without the thread's session existing. The read path does
NOT additionally reconcile: presenting an anchor as closed because its slot is
absent would mark live threads closed whenever their tab is merely not open, since
a session out of memory is not a session deleted.

### Ledger

The session-kind crew log, no second store ([crew-log-core](crew-log-core.md)).
Three kinds join the existing grammar. Two are written on the PARENT
conversation's log -- where a reader asks "what hangs off this chat":

```
thread/opened  {anchor: {surface, conversation, mid}, thread_slot, title, opened_by, in_flight?}
thread/closed  {anchor: {…}, thread_slot, summary_mid?}
```

The third is written on the THREAD's own log, because it describes what that
session was TOLD and a reader asking "what did this thread know" is reading the
thread:

```
thread/context_projected  {anchor: {…}, cursor_seq, window_start_seq?, summary_version?,
                           fold_generation?, block_chars?, partial?, rows?}
```

`cursor_seq` is required: it is the last parent seq the projection summarized, and
the state the NEXT projection computes its delta from, so an entry without it can
only be redone from the window start rather than continued. `rows: 0` is a real
record -- it says that turn found nothing new and injected nothing, which is what
separates a quiet turn from a turn the projector never ran on. Two consecutive
entries bracket exactly the parent rows summarized between them.

`session/opened.parent`, which the create core writes on the THREAD's own log,
already records the lineage, so `thread/opened` carries the anchor and nothing
else: two statements of one edge would be two things to keep consistent. Both
emitters refuse an incomplete anchor rather than writing a partial one, because
the log cannot be rewritten and an entry whose anchor names no message records a
thread nobody can find. Both are best-effort at the call site: a crew log that
cannot be written must not cost the person their thread. The MEMBER event log's
closed vocabulary is deliberately NOT extended -- a thread is not a roster
projection, and widening a vocabulary four projections read costs more than it
buys; a thread count in the Crewmates drawer reads the anchor index, which is
already per-slot.

### Wire

`ws.broadcast_thread_anchor` emits owner-only `chat.thread_anchor` frames
`{slot, mid, event: "opened"|"closed", thread_slot, title, ts, opened_by?,
summary_mid?}`. A frame with a `slot` field is a tier-1 slot-scoped WS event.

This replaces `chat.thread_reply`, and the rename IS the shape of the change: a
thread's turns now stream on its OWN slot's `chat_chunk` and message frames like
any other chat, so what a parent conversation still needs told is only that a
thread appeared or closed under one of its rows. No alias is kept -- the payload
has no `run_id`, `role` or `content`, so a client written against the old frame
would read every field as absent, and a renamed event it does not subscribe to is
a frame it ignores, which is the honest failure.

The client's store (`state/threadLiveStore.ts`) is keyed by `(slot, mid)`, and a
`closed` frame is applied ONLY when the row it holds names the same
`thread_slot`. Closing releases the message, so a replacement thread can be opened
on that mid before a close for the old one arrives -- two tabs, or an agent ending
a thread while the reader starts the next -- and applied blind that stale close
would overwrite the live replacement with the ended thread's slot, leaving a footer
that reads `Ended` and points at a conversation nobody is in. An `opened` frame is
the newest word on its mid by definition and always replaces. This is the same
identity check the close ROUTE makes with `expect_thread_slot`, on the other side
of the wire, because either side alone leaves the other's race open.

### Surfaces

Threads are offered on **every** chat surface, not only a crewmate's: ordinary
chat, a member or crewmate DM, `td-*` resident sessions, and Slack. They are on
by default and carry no switch of their own, which
[rfc-crewmates-launch §07](../../request-for-change/rfc-crewmates-launch.md#07-reply-threads-p1)
records as an amendment to its P1 scope. The reason a thread needs no switch is
that it IS a session: closing it, stopping its turn and `session_control` already
govern it, so a thread toggle would be a second spelling of a control that
exists, and on Slack it would gate threading the platform provides itself. One
controller owns the surface half — `pages/chat/useThreads` — so the ordinary chat
page and the Crewmates page wire it in one line each instead of keeping two copies
of four pieces of state. It owns the per-conversation anchor read, which thread is
open, and opening one; it deliberately does NOT own a thread's messages, which are
its own slot's transcript.

Opening is decided from the anchor index alone, so the common case costs no
request: a message whose anchor is `kind: "session"` opens straight away, and one
whose anchor is `kind: "legacy"` shows the read-only fold. Only a message with no
anchor reaches `POST .../open`.

**`ThreadPanel` frames the ordinary chat pane rather than drawing a transcript of
its own.** Header, the anchored message quoted once, then `ChatPane` on the
thread's own slot — so a real composer, many turns, tool rows, approval cards,
steer, queue, stop, model and agent pickers and history all arrive by
construction, and none is reimplemented. Version 1 threads have no session to
render: they draw as a read-only fold under the anchor with one action, start a
real thread here, and a message carrying both shows the fold above the live
thread. Nothing rewrites a version 1 document.

A thread answers its own tool approvals, and needs no wiring to do so: the pane's
COMPOSER owns that decision. `ChatInput` selects the pending approval for its own
`slotId` (not for whichever slot is active) and resolves it in place, standing its
text area down for the approval box while one waits — the same behaviour the main
chat's composer has. So a thread, whose only surface is a pane, is a place a tool
can actually be granted. `ChatPane` deliberately passes no `onApprove` to
`ChatMessageList`: that would make the permission group a SECOND approval surface
above the composer's own, offering two sets of Approve / Trust / Reject for one
decision.

**Two addresses, one slot.** `?thread=<mid>` on the parent's chat URL IS the open
drawer, so a reload, a Back and a copied link all land on the same thread; the
header's pop-out leaves for `?sid=<thread slot>`, the same session as an ordinary
tab. The parameter is dropped when the session switches, because a mid names a
message in one conversation and names nothing in the next.

**The footer draws on an ANCHOR, not on a reply count.** A live thread has no
count to show — how many turns another session holds is not the parent row's
business — so it reads as the thread and its title, plus `Closed` once it has been
wrapped up. A version 1 thread keeps what it always showed: the faces of who took
part, `N replies`, and when the last one landed. A message with no thread draws
nothing here; its `Reply in thread` action lives in the hover row.

The footer is a SIBLING of its bubble, not a child, and states its own side as
`align-self` (`self-end` under the user's right-aligned bubble, `self-start` under
the crewmate's), which beats the row wrapper's `align-items`. An appearance that
re-aligns or indents the ROW must therefore re-state the footer too: CLI UI mode
moves the user's bubble full-width to the left and indents both bubbles with a bar
and padding on the message root, so it carries its own footer rules in
`styles/cli-mode.css` -- without them the user's footer sits pinned to the far right
of a left-aligned bubble and the crewmate's 16px left of its own. Those rules are
measured in a real engine by `scripts/capture-thread-footer-cli-align.mjs`, which
asserts each footer's first mark against its bubble's edge on both appearances and
requires the unfixed state to reproduce; `src/test/cliModeThreadFooter.test.ts` pins
the rules' source text, since happy-dom resolves neither `:has()` nor the
`align-self`/`align-items` contest. The assistant-side `self-start` is load-bearing
on every appearance: that column's `align-items` is the default `stretch`, so
without it the footer renders as a full-width button. The side is declared as
`align-self` plus the matching negative margin, which is what pulls the button's own
`px-1.5` back off the bubble's text edge; both halves per side, because keeping one
and losing the other lines the footer up 6px inside the bubble it belongs to.

**A reply carries the action in a header row at its TOP**, streaming or finished,
and that row is `sticky` within the transcript viewport so a reply taller than the
view keeps the control on screen. The footer row is withheld while a reply streams,
which withheld the action at exactly the moment a long answer going the wrong way
is worth branching off; an action in the body's row has the opposite fault, because
a streaming body grows under the pointer and walks the target away between the
decision to click and the click. The top of the reply does not move as text is
appended below it. The header lives OUTSIDE `.message-bubble`: that element is
`overflow-hidden`, and a clipping ancestor between a sticky element and the scroll
container turns sticky back into static. The overflow menu keeps the same action as
the secondary path on a finished reply. A streaming row has no `mid` (ids are
minted post-turn), so the action carries none and the route's `inflight` sentinel
stands in.

Two refusals of an open mean *not yet* rather than *not ever*, and get a sentence
that says to try again: `transcript_missing`/`unflushed` (the anchored row exists
on screen but not yet on disk) and `caller_memory_changed` (the parent slot moved
under a creation already in flight). Measured against a live gateway, opening on
the reply being written during a fresh chat's FIRST turn hits these; the same call
on any message already flushed succeeds, and the parent's turn streams on to
completion either way.

## Inline Image Attachments (`chat_attachments.py`)

A message's inline images are session-scoped content and are stored with its
transcript. `![alt](/abs/path.png)` is resolved off disk by the dashboard at VIEW
time (`/api/file-raw`), and the path an agent writes normally points into its own
per-process scratch directory (`agent_scratch.py`), which is reclaimed when the
agent process dies — so the reference outlives the bytes and the transcript
renders a missing-file chip.

At each write boundary the referenced image is copied into
`<sessions dir>/<transcript stem>.attachments/<sha256[:16]>-<basename>` and the
**persisted** destination is rewritten to point there. Two boundaries share the
one helper, `persist_inline_images`:

| Boundary | Covers |
|---|---|
| `ConversationLog.append` / `append_if_absent` | agent, channel, cron and workflow rows |
| `chat_persistence._build_message_entry` | the dashboard slot save's window re-serialization |

Contract:

- **Copy, never move.** The original file stays where the agent put it. The
  dashboard slot save COMMITS the rewritten destination back into its in-memory
  row: the save re-serializes the whole window on every flush, so a row still
  naming the scratch file would be re-resolved each time and, once scratch is
  reclaimed, overwrite the good persisted path with the dead one. The live UI
  reads the image from disk at view time either way.
- **Content-addressed**, so one image referenced by many messages is stored once.
- **Idempotent**: a destination already inside the attachments directory is left
  alone, which lets the two boundaries compose and lets the slot save
  re-serialize its window on every flush without re-copying.
- **A preserved image corroborates an id match.** The two boundaries can meet
  one message at different times: the slot save (or an injector's
  `append_if_absent`) lands it with the image rewritten to its stored copy, and
  by the time the other writer runs the agent's scratch file can be gone, so
  that writer's rewrite fails open to the original path and the bodies disagree.
  Both id-aware dedup sites — `append_if_absent`'s same-`meta.mid` check and the
  slot save's pass-0 fold in `_frozen_prefix_and_foreign_appends` — therefore
  accept `same_text_modulo_images` (equal text, image destinations compared by
  the stored copy's own naming, at least one already inside this transcript's
  attachments directory) as corroboration alongside equal body or equal `ts`.
  Corroboration stays required, because `meta.mid` is caller-suppliable; body
  equality stays the rule for id-less callers.
- **`role != "user"`**, the same gate the redaction boundary uses: an inline image
  is agent output, and a path the user typed names a file of their own.
- **Bounded scan.** A row with more than `MAX_IMAGE_OPENERS_PER_MESSAGE` (256)
  `![` openers is left as written without scanning: the reference scanner is
  quadratic in the opener count and this runs under the session lock on
  LLM-authored text. The Storage page's empty-shell `rmdir` of a drained
  attachments directory re-takes the transcript lock, because a resuming writer
  creates that directory and lands its first image under the same lock.
- **Fail-open per image**, at debug level. Skipped: remote and `data:`
  destinations, relative paths, non-image extensions, anything over 25 MiB,
  sensitive paths, and non-regular files — **symlinks are refused, never
  followed**, because the copy lands where the dashboard serves it.
- `delete_session` takes the attachments with the transcript in three
  all-or-nothing steps: rename the directory aside (one atomic rename — a failure
  aborts with transcript and images intact), unlink the transcript (a failure
  renames the directory back, so the retained rows still resolve), then purge the
  staged copy. A purge residue (Windows: a file still open in a viewer) is an
  orphan under a `.attachments.trash-*` name that nothing serves, logged at
  WARNING for the operator; it never fails the delete and never leaves a
  transcript pointing at missing pictures.
- The Storage page's reclaim (`session_storage.py`) treats the directory as the
  session's third half: `_unit_paths` lists its files, so they are measured with
  the session, moved to the trash batch under `crew/<stem>.attachments/`, restored
  with it, and emptied with it; an image written recently keeps the session
  fresh. The drained directory is removed after the batch is durable and
  recreated by restore. Only regular files are taken -- a foreign entry stays,
  and so does the directory holding it.

Reads go through `hooks.safe_read_file_bytes_nolink`, the house chokepoint: it
opens the final component as itself on every platform and validates the
descriptor it opened (regular, not hardlinked, not sensitive), so no
check-to-use window remains.

**Reclamation is delete-only, by decision.** Rotation moves old rows to
`archive/`, and those rows still name their attachments — so rotation orphans
nothing and must not sweep; sweeping against the live transcript alone would
break the references the archive keeps. An attachment becomes genuinely
unreferenced only when archive retention expires its last row. The ceilings are
**per message** (12 images, 64 MiB, 25 MiB each); across messages a session's
attachments grow with every distinct image it posts until the session is
deleted — content-addressing dedups repeats, not a stream of unique pictures. A
per-session byte ceiling, or a sweep coupled to archive-retention expiry, is a
follow-up ([issue #10437](https://github.com/kirodotdev/KiroCrew/issues/10437)), not part of the write
boundary.

**Known limitation:** the rewritten destination is the absolute path of the
attachments directory. Relocating or restoring the data home under a different
path breaks every persisted image reference the same way the original scratch
path did; a home-relative encoding belongs with the next renderer change. For
the same reason attachments do not travel with a session transfer or export
(`session_transfer.py` carries the transcript text and drops host-local
references by design), exactly as the scratch path they replace never did.

`sessions/` is write-protected but deliberately not read-sensitive
(`security/paths.py`), so `/api/file-raw` serves an attachment under the existing
sensitive-path policy.

## Source Provenance

Messages include `source_thread` and `source_user` fields:
- **Slack**: `source_thread` = Slack thread_ts, `source_user` = Slack user ID
- **Dashboard**: `source_thread` = "dashboard", `source_user` = "dashboard"
- Session keys prefixed `dashboard:` for dashboard chat slots

Dashboard history list shows source icons: 🖥 (dashboard) / 💬 (Slack).
