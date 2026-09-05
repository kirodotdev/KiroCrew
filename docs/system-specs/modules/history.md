# Conversation History Module

## Overview

Persistent conversation history with provenance tracking and LLM-driven consolidation. Conversations survive session expiry and gateway restarts.

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
- **Cache-fill staleness guard** — mtime-keyed memos cannot trust "same mtime == same content": housekeeping rewrites (compaction / rotation / metadata edits / `mark_consolidated`) restore the pre-write mtime via `_restore_mtime`, so a fill spanning one would park pre-rewrite data under an mtime the file still has — undetectably, for the life of the process. Two mechanisms close the fill window, by cache: `_meta_cache` and `_recent_cache` publish through a per-key invalidation **generation** (`_invalidate_cache` bumps the counter BEFORE dropping entries; each fill snapshots it before its `stat` and re-checks it around the publish via `_publish_if_current`, discarding the fill if it moved — lock-free on read paths reachable on the event loop, a discarded fill costs one re-read), while `_folded_cache`/`_snippet_cache` serialize the whole stat → read → store under `_file_lock` (`_folded_content`), and `_msg_cache` uses that same double-checked miss-only locking whose unlocked on-loop fallback publishes under the generation plus a cross-process flock-hold witness (`_read_messages`, below). The generation table is process-wide (class-level, keyed by transcript dir + sanitized stem — see `_read_messages` below for why instance scope is not enough), and generations, invalidation, and the pops all cover every cache-key spelling of one session — logical key, sanitized `path.stem`, and the canonical/legacy Slack aliases in both directions (`_cache_key_identities`) — because `list_sessions` keys its fills by stem while most writers invalidate under the logical key. `_meta_cache`, `_folded_cache`, and `_snippet_cache` entries ALSO record the generation they were filled under and a warm hit requires both the mtime and the generation to match, with the store routed through `_publish_if_current`: the fold's `_file_lock` is process-wide and path-keyed, so it orders fills against every in-process writer regardless of instance — but `_invalidate_cache`'s pops reach only the writer's own instance's caches, so an entry already sitting warm in ANOTHER instance survives a preserved-mtime rewrite, and the generation clause on the hit (backed by the process-wide table) is what unhits it. The store-side guard is generation-stamp hygiene — the lock already covers the fill window in-process; unlike `_read_messages`' flock-hold witness, the search memos have no cross-process witness, so a preserved-mtime rewrite from a different PROCESS remains a known residual gap for them.
- `recent(key)` — last 20 messages for context injection
- `recent_with_provenance(key)` — entries with source citations
- `list_sessions()` — lists all sessions with title (first user message or LLM-generated). Sort key uses ISO `created` string consistently (defaults to ISO from `st_mtime` if no metadata `created` field, ensuring string-only comparisons). Each returned session's meta dict also carries `folder_id` when present in the persisted metadata line, so sessions can be grouped by the folder they were filed in.
- `agent_usage()` — returns `{agent_name: (session_count, last_used_mtime)}`; built on `list_sessions()` so it inherits canonical-session dedup + symlink-skip (counts per logical conversation). Used by `GET /api/agents` to order the roster most-used-first, degrading to config order on failure.
- `search_sessions(query, limit=50)` — case-insensitive substring content search over the newest `_SEARCH_SCAN_WINDOW` session JSONL files; the ONE ranking shared by the dashboard history filter, the `search_chat_history` MCP tool, and Discord session resume. The query is parsed by `parse_search_query` into needles: non-CJK terms are required substrings (AND over the document); a spaceless-script run (Han ideographs + kana; NOT Hangul, since modern Korean is space-separated) gates on its individual characters (required, down-weighted) plus an adjacency floor — at least one of the run's character bigrams must hit somewhere, so a spaceless multi-word CJK query matches documents containing the words apart (each word is a bigram hit) while scatter-only character noise is excluded, and adjacency dominates the ranking; the floor is waived when the query's bigram set exceeds its cap (a partial set cannot prove no-adjacency-anywhere, so truncation only ever loosens). Occurrence counts are weighted per needle, length-normalized, title-boosted, phrase-bonused, then multiplied by a bounded recency boost (×2.5 for a session modified now, decaying toward ×1 with a 30-day half-weight — never a penalty; sized so a year-old double mention loses to today's single mention while a decisively better old match still wins), and capped to `limit` results. Exposed via `GET /api/sessions/search?q=<q>&limit=<n>` (min 2 chars); used by the dashboard history filter to find sessions by content (CR ids, error messages, file paths) rather than title alone. Returns the same meta dicts as `list_sessions()`, so each search hit likewise carries `folder_id` (when present), letting the sidebar group results by folder. Snippet builders (`_content_snippet`, mcp_core's `_extract_history_snippet`) derive their needles from the same parse via `snippet_needles` (phrase first, then whole terms/bigrams, lone CJK characters last) so match and excerpt cannot drift apart. The fold/snippet memos backing the search are keyed by the sanitized `path.stem` (from `list_sessions`' meta dicts) while writers invalidate under the logical session key; `_invalidate_cache`'s identity-wide pops are what connect the two spellings, so a housekeeping rewrite that restores the file's mtime still drops the memo and search stops matching text the transcript no longer contains.
- `needles_match_text(needles, folded_text)` — the single-string form of `search_sessions`' match gate (required needles as substrings + the CJK adjacency floor), for callers filtering one text field; Discord session resume's zero-hit title fallback uses it so title matching cannot grow a second spelling of tokenization.
- `read_file_change_messages(key)` — a lightweight Artifacts projection that streams one transcript as bytes, skips lines without the serialized `"file_changes"` key before JSON parsing, and retains only `ts` plus `meta.file_changes` in its own bounded, file-stamped cache. It never warms `_msg_cache`, so scanning the session-document firehose cannot retain the full parsed transcript corpus.
- Forge references (pull requests, merge requests, issues) are a query dimension of their own, because one item has several written spellings and a transcript carries whichever one its author used. A term naming an item — `#4411`, `PR #4411`, `pr 4411`, `pull request 4411`, `pr4411`, `pull/4411`, a full PR/MR URL, `owner/repo#4411` — becomes ONE required needle carrying every spelling of that item (`SearchNeedle.alts`, counted by the shared `count_needle`), so any spelling finds every spelling. The words that introduce the number are dropped from the gate: they are not part of the reference, and requiring the literal "pr" would disqualify a transcript that names the item only by URL. Spellings are `digit_bounded` on both sides, so `#4411` matches neither `#44110` nor the run id `1544110293`. The TYPED sigil decides the family, never a word before it: `mr#12` is read as `#12`, because letting the word win produced a reference none of whose spellings was the string the user typed. Coverage of every accepted shape is pinned by a property test that drives each one against a transcript quoting it verbatim, rather than by inspection of the spelling list. GitHub's pull/issue sequence is shared (`#4411` ≡ `/pull/4411` ≡ `/issues/4411`) while GitLab numbers merge requests separately, so `!12` and `#12` stay distinct families and never match each other; bare `merge` is not a GitLab word (GitLab is `MR 12` / `merge request 12` / `!12`). Plain digits remain one of the spellings exactly when the QUERY typed no sigil (`issue 42`, `PR 4411`, `pr4411`): such a query previously gated on the digits, so dropping them would HIDE the transcript that says "we hit issue 42 in prod", and keeping them makes the recall of the literal AND it replaces hold with ONE intended exception — a session whose only claim to the old match was the digits sitting inside a longer number, which is what the boundary exists to exclude. The LEFT edge of that boundary applies only to a spelling that starts with a digit: for a delimited spelling the character before it says nothing about the number's length, and demanding a non-digit there would refuse `#4411` inside `owner/repo2#4411` — a repo whose name ends in a digit, matched against the very reference the query named. Only a lead-in run that actually NAMES a type turns a following number into a reference: `pr 4411`, `issue 42`, `pull request 4411` and `merge request 12` (the two-word GitLab form) do; `requests 12` and `merge 1234` do NOT and stay literal terms, since dropping such a word from the gate would trade a real term for every session mentioning that number. A query that DID type a sigil never gated on bare digits, so it keeps them out and stays precise (a standalone "12" is ordinary prose). A BARE number with no naming word is not a reference at all: it keeps its plain substring needle — numeric content search (ports, error codes, run ids) is unchanged — and gains the spellings as scoring-only needles at `_FORGE_REF_WEIGHT`, so the session that references the pull request outranks one that merely contains those digits. Those ranking needles are NOT adjacency evidence (`SearchNeedle.adjacency`, which only CJK bigrams set), or they would arm the adjacency floor and turn a ranking hint into a hidden gate. Two limitations are accepted rather than special-cased, both needing a query nobody writes and both only widening the result set: a chain-only word wedged between the type word and the number (`issue merge 42`) is swallowed, and because the gate is keyed by term text a query repeating a suffix word as its own term (`pull the pull request 12`) loses that term. Closing either means keying the gate by token position instead of by text. Expansions per query are capped at `_SEARCH_MAX_FORGE_REFS`, each costing one scan per spelling per scanned session (up to eight for a named reference, up to thirteen for a bare number's both-families ranking needle, plus up to eight more for a registered provider's own prefixed id — see below); a token past the cap degrades to a plain needle.
- A REGISTERED source provider contributes its OWN id spellings through the same machinery, so an edition whose reviews are written `REV-987654321` is searchable without any provider vocabulary in core. The seam is one optional plugin hook, `search_ref(token) -> (canonical, alts) | None` on `SourceProviderPlugin`, discovered with `getattr` exactly like `path_markers()`; `source_search_ref()` fans out across the registered plugins (asking each registered plugin until one answers, then handing that answer through unjudged — shape is the normalizer's job, and skipping a malformed answer would only serve the same two-registrant case the merge below is declined for), and `register_source_provider()` publishes that collector DOWNWARD into `history_search.register_search_ref_resolver` at registration time — never from a route handler, because `parse_search_query` is also reached from paths that serve no HTTP (the Discord title-only resume gate, the `kirocrew memory search` CLI) and a process that never ran a route would otherwise answer the same query differently. One slot holds the resolver, not a list: the per-plugin fan-out already lives in the collector. The FIRST plugin to recognize a token WINS, for every token shape: a prefixed id names ONE item, so merging would conflate distinct items, and a cross-plugin merge for a bare number would exist only to serve two registrants holding a real item at the same number — which this repo, registering no provider at all, cannot produce. Cost stays bounded in ONE place: `_MAX_SEARCH_REF_SPELLINGS` (8, sized to the sibling per-plugin `_MAX_PLUGIN_PATH_MARKERS` because it bounds the same kind of thing — what ONE plugin hands core for one lookup) bounds the single answer that arrives, with no collector-side ceiling to drift from it. A bare number is NOT a provider token at all: a provider's ids are prefixed, so a run of digits names nothing it owns and the resolver is not consulted for one. `_provider_search_ref` is the single normalizer — it casefolds every spelling (the query is casefolded before parsing and `count_needle` requires already-folded needles, so a capitalized spelling produces a needle that matches NOTHING, silently), de-duplicates, drops empties, applies the fan-in ceiling, and DROPS an answer none of whose spellings carries the typed token, since such an answer describes some other item and would otherwise rank a query on text it never named. Every way a resolver can fail is contained and costs no more than a debug log — carrying a traceback where an exception was raised, and the offending value where a shape was merely wrong, so none of them is silent: raising when called, returning a malformed answer, and raising while its `alts` are READ — the hook promises a `Sequence`, which cannot do that, but a resolver ignoring the contract can, so the read sits inside the same boundary and the answer is dropped WHOLE rather than half-read, since an exception there would otherwise escape the parse as a 500 on every search. A provider is consulted only for a token no built-in shape recognized (built-ins always win), contributes SPELLINGS ONLY and never lead-in vocabulary (the words a provider would want — "review", "cr" — are common English, so admitting them would trade a real search term for every session mentioning that number), and can never gate a BARE all-digit token, which it is never even asked about — so no number of registered providers can spend the `_SEARCH_MAX_FORGE_REFS` budget on one numeric token. The purity contract — pure, allocation-cheap, no I/O — is documented and not enforced: a resolver is consulted for every term of every query and the parse runs at least twice per search, so a blocking resolver becomes per-keystroke latency in the search box.
- `_read_messages` — mtime-guarded message cache with the same double-checked, miss-only locking `_folded_content` uses for this identical race. A warm hit is served lock-free; only a MISS takes the session's in-process writer lock (`_file_lock`) and re-checks mtime + cache under it, so a cache fill cannot publish a pre-rewrite parse after an mtime-restoring rewrite (`_restore_mtime`) invalidated the cache. ON the event loop the lock is acquired non-blockingly and a busy lock falls back to an unlocked fill, so an on-loop read never stalls behind a writer holding the RLock across its cross-process flock wait (`_FLOCK_ACQUIRE_TIMEOUT_S`). An unlocked fill publishes through two witnesses, one per writer class: a per-key invalidation **generation** covering local writers (`_invalidate_cache` bumps the counter BEFORE dropping entries; the fill snapshots it before its stat and publishes only while it is unmoved, re-checked after the store), and a cross-process **flock-hold witness** covering external processes (`_flock_hold_witness`: publish only while this process provably held the sidecar flock for the whole fill window — an external writer's invalidation bumps a table in its own process, invisible here, so the flock is what excludes it; the witness carries a release epoch so a broken-and-reacquired hold never passes as continuous). A fill that races no rewrite is kept instead of re-parsed on the next read; one that cannot prove its window clean is discarded. Every published entry also records the generation it was stored under, and a warm HIT requires both the mtime and the generation to match: `_invalidate_cache`'s pops reach only its own instance's caches, so the process-wide bump is what unhits an entry when the rewrite was performed through a different `ConversationLog` instance. The generation guards `_msg_cache` specifically — the derived `_meta_cache`/`_recent_cache`/`_folded_cache`/`_snippet_cache` memos keep mtime-plus-instance-local-pop guards, so the cross-instance preserved-mtime case is a knowingly accepted residual gap for those (they back bounded views and search memos, not the authoritative transcript) — and lives in a process-wide class-level table keyed by `(transcript dir, sanitized filename stem)` — the same scope as the per-path lock table, because the writer forcing a reader onto the unlocked fill may be a different `ConversationLog` instance — with the legacy/canonical Slack spellings closed over bidirectionally (`_cache_key_identities`), because one session is reachable under both its logical key and its sanitized `path.stem` spelling and the writer and reader do not always use the same one.
- `delete_session(key)` — permanently removes a session JSONL file

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
  archive diff, and `atomic_write`. Without it a concurrent `append_off_loop`
  (e.g. a workflow/cron result appended to the originating dashboard session)
  could land between the save's file snapshot and its file-replacing
  `atomic_write`, silently deleting the acknowledged append. On the event loop
  `_locked` makes ONE non-blocking acquire and raises `HistoryLockTimeout` under
  contention rather than blocking the loop — so **on-loop callers MUST offload**:
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

### Send identity on a row: `sendId` scalar and `sendIds` list

A dashboard send mints a client-side `sendId` so the initiating tab can retire
its own optimistic bubble against the row the server persists. The identity is
carried in **two spellings, and a reader MUST consult both**:

- `meta.sendId` — the scalar, on a row that stands for exactly one send. The two
  ENQUEUE paths (`queue_for_next_turn`, and the sub-agent hold branch in
  `chat_handlers`) persist it into the QUEUE ENTRY's metadata, not only onto the
  live `queue_push` broadcast — a tab that missed the broadcast learns the send
  landed only from the drained row, which reads entry meta. The accepted-steer
  path instead stamps the ROW it persists, and its `steer_push` carries the id
  purely as a live echo (`chat_delivery`).
- `meta.sendIds` — the list, written by `chat_runner`'s drain (`_merged_send_ids`)
  and ONLY when a single drained row folds together more than one queued send.
  The scalar remains present, naming just one of them.

The list is not a rename of the scalar: it exists because one row can genuinely
represent N sends. So a consumer keyed on the scalar alone silently under-matches
a merged row — it retires one send and reinserts the others as phantom bubbles.
Each side funnels the pair through one accessor for that reason: `rowIdentities`
and `reconcileOptimisticEcho` in `chatSlice.ts` on the client,
`_merged_send_ids` in `chat_runner.py` on the server. New readers should use
those rather than reading either field directly.

### Delivery markers on a row

Alongside the identity, a dashboard row carries client-only delivery markers. They
are written by the sending tab and never by the server, so a row read back from
history carries none of them — their absence means "nothing claimed", not "not
delivered":

- `meta.optimistic` / `meta.pendingServerRow` — this row is the client's own bubble
  standing in for a send the server has not echoed back yet. `pendingServerRow` is
  what makes a refetch retain it instead of rebuilding it away.
- `meta.deliveryUnknown` — the POST did not return a readable receipt, so whether
  the turn was accepted is genuinely unknown. Live doubt.
- `meta.deliveryUnresolved` — doubt a LATER send's confirmation demoted. The row
  is kept, in past tense, rather than erased.
- `meta.deliveryConfirmed` / `meta.confirmedSendId` — positive proof the send
  landed, plus which send id the proof named.

**These are independent booleans, so their combinations are not all legal.**
Nothing clears the two doubt markers when a send later confirms, so
`deliveryConfirmed` alongside a stale `deliveryUnknown` is representable and does
occur. Resolve them through `rowDeliveryState` (`utils/sendDelivery.ts`), which
owns the precedence — confirmation first, then live doubt, then spent doubt — and
do not re-derive it per reader: two readers resolving it independently is how a
bubble comes to render muted while the composer beside it reads "delivered".

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
  transcript** to its session JSONL for tab recovery and gateway-restart
  restore. The title is a summary of content that is already on disk in the same
  file, and `restore_recent_sessions` skips only on `closed`, never on
  `memory_mode`.

Gating titling on `blocks_reads` (as an earlier revision did) therefore bought
no privacy while leaving temporary tabs permanently labelled "New Session…".
The manual `POST /api/chat/slots/{slot}/generate-title` endpoint never had such
a gate, so a temporary session could already be titled and persisted on demand.
Do not reintroduce a `memory_mode` condition here without first changing what
`_save_slot_to_history` writes.

## HistoryConsolidator (`history_consolidation.py`, re-exported by `history.py`)

Background task that fires when unconsolidated count ≥ 10 messages. Uses the
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
for the duration of each embed, and can trip the faulthandler hard-kill. (The
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
from `stopping` → `stopped`/`stop_failed_reset`.

After a cancelled turn, `context.build_cancelled_turn_preamble` reads the
cancelled user prompt and partial assistant output from this log and
prepends them to the next prompt as a bracketed preamble, because kiro-cli
discards cancelled turns from its own ACP conversation log. The flag
`_Session.prev_turn_cancelled` (set by `SessionManager.stop_turn` on
soft-cancel success) gates the one-shot re-injection.

## Session Lifecycle

1. New session → full context injected (memory + skills + lessons + last 20 messages)
2. Messages saved to JSONL with provenance after each response
3. Context ≥ configured threshold (`session.autocompact_pct`, default 70%) → compaction via kiro-cli `/compact` (fire-and-forget)
4. Session expires (30min idle) → provider killed
5. User returns → new session with history re-injected
6. After 10+ messages → background consolidation → structured memory updated

## Source Provenance

Messages include `source_thread` and `source_user` fields:
- **Slack**: `source_thread` = Slack thread_ts, `source_user` = Slack user ID
- **Dashboard**: `source_thread` = "dashboard", `source_user` = "dashboard"
- Session keys prefixed `dashboard:` for dashboard chat slots

Dashboard history list shows source icons: 🖥 (dashboard) / 💬 (Slack).
