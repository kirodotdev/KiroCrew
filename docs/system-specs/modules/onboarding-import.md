# Foreign-Agent Import Module

Previously: 2026-07-28 (initial spec: industry-consensus scope, session-import
removal, memory-hierarchy destination mapping, dry-run contract, and
user-selectable conflict strategies)

## Overview

`onboarding_import.py` migrates a user's setup from another AI agent into
KiroCrew. It runs from the first-run onboarding flow (after the Kiro CLI
prerequisite gate, before the theme tour) and from Settings on demand.

The module is a **projection**, not a mirror: it reads a foreign layout and
writes only into KiroCrew's own containers through KiroCrew's own APIs. It never
invents a storage format, never writes a file KiroCrew does not otherwise read,
and never copies a foreign store verbatim.

Three phases, always in this order:

| Phase | Entry point | Writes? |
|-------|-------------|---------|
| Detect | `detect_sources()` | no |
| Dry run (preview) | `preview_import()` | **no — never touches disk** |
| Apply | `apply_import()` | yes, merge-only |

**Sources:** `codex`, `claude_code`, `gemini`, `meshclaw`, `openclaw`, `hermes`.

`gemini` covers Google's whole terminal-agent lineage under one id, because
Antigravity CLI reuses `~/.gemini` rather than claiming a directory of its own.
Gemini CLI stopped serving Pro/Ultra/free individual accounts on 2026-06-18 and
Antigravity replaced it, so that one root holds both a displaced Gemini CLI
user's config and a current Antigravity install.

## Scope: what is migrated

The scope follows the de-facto industry consensus (cross-checked against
Codex CLI, Claude Code, OpenClaw, and Hermes Agent migration tooling). Only
data that is (a) user-authored, (b) portable across agents, and (c) expensive to
recreate by hand is in scope.

| Category | Rationale | Destination |
|----------|-----------|-------------|
| `instructions` | User-authored rules — the single least replaceable asset | memory hierarchy (below) |
| `memories` | Plain-Markdown knowledge; semantically stable across agents | memory hierarchy (below) |
| `skills` | Self-contained dir + `SKILL.md`; near-identical format everywhere | `skills/imported/<source>/<name>/` |
| `mcp_servers` | De-facto standard shape (`mcpServers` / `[mcp_servers.*]`) | `mcp.json` → `mcpServers` |
| `denied_commands` | Hand-tuned deny rules; recreating them is tedious and error-prone | `config.json` → `hooks.denied_commands.user_added` |
| `settings` | Only unambiguous scalars: timezone, theme mode, theme color | `config.json` |
| `workspaces` | Project dirs, but **only** from explicit config — see below | `config.json` → `workspaces` |
| `schedules` | Portable cron/interval specs | `CronService`, always `enabled=False` |

### Not migrated (explicit non-goals)

Each of these is a deliberate exclusion. Do **not** "restore" one because it
looks like a gap — reopen the decision in this spec first.

| Excluded | Why |
|----------|-----|
| **Sessions / conversation transcripts** | Not industry practice — no surveyed agent migrates transcripts. A transcript is a record of a conversation with a *different* model under a *different* system prompt; replayed into KiroCrew it is misleading context, not useful memory. Reading it also requires hard-coding each source's private JSONL/SQLite schema, which fails **silently** when upstream drifts. Removing it deletes the module's largest and most fragile surface. See "Session-import removal". |
| **Persona / `SOUL.md` as a persona** | KiroCrew's persona surface is theme-pack persona, governed by `capabilities.theme_persona`. Importing a foreign persona document *as a persona* would inject third-party text into the agent's identity through a path that bypasses that gate. The **directive content** of such a file is still migrated — as memory (below) — but its persona role is dropped. |
| **Credentials of any kind** | `~/.claude/.credentials.json`, `~/.codex/auth.json`, `.env`, `auth-profiles.json`, gateway tokens, provider API keys. Never read. MCP `env`/`headers` keys matching the secret patterns are stripped and counted into `secret_count`. |
| **Runtime state** | Subagent records, tool results, checkpoints, hook state, in-flight task state. Not user data. |
| **Architecture-specific config** | Plugin/hook/binding/agent-list configs, memory-backend selection, provider and model mappings. KiroCrew is KiroACP-only, so provider/model translation has no destination. |
| **Opaque binary stores** | Foreign SQLite memory stores are reported as `unsupported_memory_database`, never parsed. |
| **Allow-lists (as opposed to deny-lists)** | A foreign `permissions.allow` *widens* the security boundary. Importing it would let a foreign config grant tool access inside KiroCrew's own gate. Deny rules only. |

## Destination mapping: the memory hierarchy

Imported instruction/knowledge content is rewritten into KiroCrew's existing
memory tiers. Tier choice is driven by two properties — **context priority**
(`context.py` per-section caps) and **durability**.

### Durability constraint (read before choosing a tier)

`preferences.md` and `projects.md` are **replaced wholesale by the memory
consolidator**. Anything written there by import is destroyed on the next
consolidation run. **Import MUST NOT write to `preferences.md` or
`projects.md`.**

Durable tiers only:

| Tier | Context cap | Durability |
|------|-------------|------------|
| `lessons.jsonl` (`LessonStore`) | 22.6% — highest of any tier | Append-only; pruned oldest-first at `_MAX_LESSONS_TOTAL` (200) |
| Semantic memory (`VectorMemoryStore`) | 7.7% | Durable; key-addressed, confidence-gated |
| Episodic memory (`VectorMemoryStore`) | 7.7% | Durable; append-only |
| `.kiro/steering/*.md` | 10% | Durable, but **workspace-scoped** |

### Mapping rules

1. **Top-tier directives → the active lesson store.** Normally `lessons.jsonl`;
   when the **vector** store already holds `lesson.*` entries it is authoritative
   and receives the write instead — via `set_semantic_if_absent`, NOT
   `write_lesson`: that writer deletes an existing lesson on exact-substring or
   >50% topic overlap ("newer replaces older"), which for an import would let a
   foreign directive delete a correction the *user* taught the agent. Import
   recognizes the same overlap and reports `existing` instead of replacing. `ContextBuilder` reads vector lessons first and
   never falls back to the JSONL when they exist, so a JSONL-only write would be
   recorded as imported yet stay invisible to the agent.
   The destination is the highest-priority durable
   always-injected tier (22.6% of the context budget). Applies to the
   *directive* content of a source's instruction documents — `CLAUDE.md`,
   `AGENTS.md`, `~/.claude/rules/*.md`, each workspace's own `CLAUDE.md`, and
   the directive body of a persona document (`SOUL.md`). `_instruction_paragraphs`
   splits a document into individually-injectable directives: it reuses
   `_memory_chunks` for bounding, then drops anything shorter than
   `_MIN_INSTRUCTION_CHARS` (10) and any paragraph whose every line is a
   Markdown heading (a heading carries no directive alone). Each surviving
   paragraph becomes one `Lesson(category="preference", rule=<text>)`;
   `negative` stays `None`. `LessonStore.save` is itself exact-rule
   deduplicating, so a re-import reports `existing` → `deduplicated` rather than
   a false `accepted`. Because the store prunes **oldest-first at 200** — and the
   user's own corrections are the oldest rows — import is bounded **twice**: a
   per-import ceiling of `_MAX_IMPORTED_LESSONS` (50) at scan time (overflow emits
   `instruction_count_limit`), AND a hard refusal at write time once the store
   already holds `_MAX_LESSONS_TOTAL` entries. The per-import cap alone is
   insufficient — 151 existing + 50 imported still evicts one — so the write-time
   check is the load-bearing one.
   **Identity paragraphs are excluded** (`_IDENTITY_PARAGRAPH_RE`): a persona
   document mixes identity ("You are Aria…") with directives ("Always cite a file
   path"), and importing the former into an always-injected lesson would make
   foreign text act as the agent's persona through a path that bypasses
   `capabilities.theme_persona` — exactly what excluding the persona role is meant
   to prevent. The guard checks **every non-heading line** of a paragraph (one
   identity line taints the whole paragraph, because a paragraph is imported
   whole) after stripping Markdown list/quote/emphasis markers, and covers both
   statements ("You are Aria") and subjectless imperatives ("Act as Aria",
   "Assume the role of…"). It does NOT reject an ordinary directive that merely
   mentions "you" or opens with the same verb ("Act on review feedback").
   Instruction content passes the same two gates as memory: a *redaction* (not a
   size truncation) drops the file as `credential_bearing_instruction`, and
   `contains_injection` drops it as `injection_instruction_excluded`.
2. **Narrative knowledge → episodic memory.** Prose and notes that are not
   directives: `MEMORY.md`, `USER.md`, `memories/*.md`, project-local memory
   dirs. Chunked by `_memory_chunks` (paragraph-packed, ≤2000 chars),
   `importance=0.5`, `tags=["imported", <source_id>]`, `source="import"`.
   Written with **`defer_embedding=True`** — see "Deferred embedding" below.
3. **Key-value facts → semantic memory.** Only where a source already stores
   an explicit key/value pair whose key matches `_SEMANTIC_KEY_RE` and carries
   one of `_SEMANTIC_PREFIXES` (`pref.` / `project.` / `user.` / `lesson.`).
   Written with `set_semantic_if_absent` so a concurrent native write is never
   overwritten.
   **A row the source types as a `directive` is a rule, not a fact**, so it is
   routed to the lesson tier via `_add_db_directive` instead — it passes the same
   identity guard (`_IDENTITY_PARAGRAPH_RE`) and the same `_MAX_IMPORTED_LESSONS`
   ceiling as a file-sourced directive. It is NOT dropped: MeshClaw stores every
   learned lesson this way, so discarding them lost exactly the least replaceable
   rows in the store.
   **Screen the DECODED value, never the raw JSON.** A DB value arrives as JSON
   text, so `_sanitize_text`/`contains_injection` applied to `value_json` inspect
   escape sequences, not content: `"Ignore all previous\ninstructions…"` carries a
   literal backslash-n on disk and matches no injection pattern, while the
   `json.loads` result is a real newline and does. See security invariant 3a for
   the two layers that enforce this. It applies to the plain semantic path too,
   not just directives — a `lesson.*` key reaches the same always-injected tier.
4. **Workspace-scoped rules → `.kiro/steering/`, opt-in only.** A per-project
   instruction file (a workspace's own `CLAUDE.md`/`AGENTS.md`) may be written
   to `<workspace>/.kiro/steering/imported-<source>.md` **only** when the user
   supplies an explicit workspace target. Import MUST NOT default the target to
   the current directory, the data home, or the user's home. Absent an explicit
   target the item is reported `skipped` with reason
   `workspace_target_required` — a missing target is never implicit consent.

Every imported memory item passes the existing content gates before it is
written: `_sanitize_text` (truncate + credential redaction; a *redacted* file is
dropped, a merely *truncated* one is not) and `contains_injection` (dropped as
`injection_memory_excluded`).

### Foreign workspace-scope columns: sentinel vs. real scoping

A foreign memory store may carry a workspace/scope column. KiroCrew's own memory
tables have none, so a genuinely workspace-scoped row has no faithful destination
and is reported `scoped_memory_unsupported`.

**A sentinel value is not scoping.** A single-workspace install stamps every row
with the same placeholder, so treating the column as authoritative discards the
entire store. `_UNSCOPED_WORKSPACE_IDS` (`""`, `default`, `global`, `main`,
`none`, `null`) enumerates the placeholders; `_row_is_workspace_scoped()` is the
single predicate both DB scanners use. MeshClaw stamps `default` on every row, so
before this predicate existed **100% of a real MeshClaw memory store imported
zero rows** while the UI reported success.

Matching is exact after casefold + strip, never a prefix or substring: `default2`
and `maintenance` are real workspace names, not sentinels. **Accepted ambiguity:** a
user whose workspace is genuinely *named* one of the six placeholders has that
row imported as an unscoped memory instead of reported unsupported. That is the
right side to err on — the value is indistinguishable from a placeholder by
construction, and the cost is one extra general-purpose memory rather than the
silent total loss the strict reading caused. Keep the list to values that are
placeholders by convention; do not add plausible real workspace names to it.

### Deferred embedding (bulk memory writes)

Episodic import writes pass `write_episodic(defer_embedding=True)`: the row lands
with a **NULL embedding** and is FTS5 keyword-searchable immediately, but is not
yet in vector search. `apply_import` returns
**`embedding_backfill_pending`** (the episodic write count), and the caller MUST
schedule `VectorMemoryStore.backfill_missing_embeddings()` off the request —
`_schedule_embedding_backfill` in the dashboard handler runs it on the
maintenance executor. When `apply_import` created its **own** store (CLI/tests,
no caller to schedule anything) it runs the sweep itself before closing, and
reports `embedding_backfill_pending: 0`.

Why deferral and not batching: embedding cost grows steeply with text length
(~0.4s per 2000-char chunk on CPU), and import writes hundreds of chunks, so an
inline embed held the apply request for minutes. **`embed_batch()` is not the
fix** — measured on real import text it is ~25% *slower* than looping `embed()`,
because one 2000-char chunk already fills the model's micro-batch (`n_ubatch ==
n_ctx == 2048`), so grouping adds padding with no parallelism to reclaim.

`backfill_missing_embeddings()` requires numpy but **not faiss**. Faiss is an
optional accelerator and not a declared dependency, so gating the sweep on it
made it a silent no-op on a stock install — deferred rows would have stayed NULL
forever. `search_episodic` already falls back to `_sqlite_vector_search` (a
stdlib cosine scan over the stored blobs), so the vectors are useful either way;
only the index rebuild is skipped when faiss is absent.

## Dry run

`preview_import()` is a **hard** dry run: it performs the full scan and produces
the complete per-item plan without opening any destination for writing. The API
never applies as a side effect of previewing, and there is no flag that turns a
preview into an apply.

The plan is per-item, not per-category: each entry carries `source_id`,
`category_id`, `item_hash`, a human-readable label, the projected destination,
and the **predicted** status (see status vocabulary). A category-level count
alone is not a valid plan.

**The plan is advisory, not authoritative.** `apply_import()` re-scans the
source from disk and never trusts payloads echoed back by the client; only
`(source_id, category_id)` pairs are read out of the submitted plan, filtered
against `SOURCE_IDS`/`CATEGORY_IDS`. Consequently a preview status may differ
from the applied status when the source changed in between. `apply_import()`
returns per-item outcomes so the caller can report exactly which items diverged
from their prediction; it MUST NOT silently present the preview as the result.

## Conflict strategy (user-selectable)

A destination collision is a **user decision**, not a hard failure. The apply
request carries a strategy; the default is the safest one.

| Strategy | Behavior |
|----------|----------|
| `skip` (**default**) | Keep KiroCrew's existing item untouched; report the incoming one as `conflict`. |
| `rename` | Import alongside the existing item under a derived non-colliding name. |
| `overwrite` | Replace the existing item, after writing a restore copy. |

Applicability and rename derivation per category:

| Category | Strategies | Rename form |
|----------|-----------|-------------|
| `skills` | skip · rename · overwrite | `<name>-imported-<source>`, then `<name>-<fingerprint[:8]>` |
| `mcp_servers` | skip · rename · overwrite | `<name>-<source>`, then `<name>-<fingerprint[:8]>` |
| `workspaces` | skip · rename | `base-<source>`, then `base-<fp[:8]>`. **Behavior change:** the pre-existing three-step ladder silently suffixed on a name collision; that is a rename, so it now requires `rename`. Plain `skip` reports the collision. |
| `instructions`, `memories` | n/a — merge-only, never collide destructively | — |
| `denied_commands`, `settings` | n/a — merge-missing only | — |
| `schedules` | skip only — a duplicate schedule is matched by `_same_schedule` | — |

Rules:

- `overwrite` MUST write a restore copy under
  `imports/replaced/<run-stamp>/<category>/` before replacing anything, and MUST
  record the restore path in the item outcome. The stamp is taken ONCE per apply
  run (`%Y%m%dT%H%M%SZ`), so everything a single import replaced is found
  together. **If the restore copy cannot be written, the overwrite is abandoned
  and the item reports `conflict`** — an unrecoverable replace is worse than a
  reported conflict.
- The replacement itself MUST be a **move-aside → install → delete** swap, never
  a delete-in-place. A partial delete (a locked file on Windows) would leave the
  installed item mangled *and* the install failing, so the user ends up with
  neither version. A rename is atomic: it either frees the name completely or
  fails with the original still whole. A failed install restores the original, so
  a failed replace is a no-op rather than data loss. Restore paths suffix on
  collision — the run stamp is second-resolution, so two overwrites of one item
  inside a second must not clobber the first's restore copy (nor refuse, which
  would read to the user as an unresolvable conflict).
- The strategy is validated at the API boundary: absent means `skip`, but a
  present-but-unrecognized value is a **400**, never a silent downgrade.
  Quietly treating a typo'd `overwrite` as `skip` would report success while
  replacing nothing. `_normalize_strategy` in the backend is a second,
  fail-safe layer for non-HTTP callers.
- A writer returns `_WriteOutcome(status, renamed_to, restored_to)`. The two
  detail fields are populated ONLY when a strategy actually took effect, so a
  plain `skip` apply produces exactly the payload it did before strategies
  existed.
- `renamed_to` / `restored_to` are **backend-only**. They are filesystem
  details and MUST NOT cross into the browser; the HTTP response carries
  `conflict_strategy` plus `conflicts` / `resolvable_conflicts` **counts** only.
- A conflict entry carries `resolvable: bool` (true iff the category is in
  `STRATEGY_CATEGORIES`), so a client can offer a retry only when one could
  actually help.
- A strategy is chosen **per apply request**, applying to every item in it. A
  finer-grained per-item choice is a UI concern layered on top: the UI may issue
  several apply requests with different strategies.
- `rename` exists specifically to give the user a way out of an otherwise
  terminal conflict. Before it existed, an upstream edit to an
  already-imported skill or MCP server produced a permanent `conflict` with no
  resolution path, because the item fingerprint covers the content digest and
  therefore did not match the ledger.

## Idempotency and deduplication

Three independent layers. All three are required; none subsumes another.

### 1. Within one scan — `_deduplicate_items()`

Removes duplicate `_Item`s by `fingerprint` inside a single scan. Runs at the
end of every `_scan_source()`.

### 2. Across applies — the ledger

The ledger is a **fast path, not the authority**. It records "this exact item was
imported once", which equals "the destination still holds it" only for categories
that cannot be replaced afterwards. `skills` and `mcp_servers`
(`_REPLACEABLE_CATEGORIES`) CAN be replaced by a later `overwrite`, so a ledger
hit does not short-circuit them — the writer's destination check decides, and
reports `existing` when the item genuinely is still there. Without this, import
V1 → overwrite V2 → revert the source to V1 left V1's stale fingerprint
deduplicating the revert while V2 stayed installed.

For those categories a successful write also carries a stable
`destination_key` (`skills:<source>/<name>`, or the MCP server name), and
`_record_ledger` drops any other record for the same `(category, destination_key)`
so a single-occupancy destination keeps exactly one record.

`<data_home>/imports/foreign-agent-imports.json`, shape
`{"version": <int>, "records": {<fingerprint>: {...}}}`. An item whose
fingerprint is already recorded is reported `deduplicated` and skipped without
touching the destination.

`fingerprint = sha256(source_id \0 category \0 key)`. The payload is **not**
part of the fingerprint; content participates only where a category folds a
content digest into its `key`.

The ledger MUST be flushed at least once per category and on every exit path
(including exceptions), so an interrupted apply cannot re-import already-written
items. It MUST NOT be rewritten once per item — the file is rewritten whole, so
per-item flushing is O(n²) in serialization and rename cost.

### 3. At the destination — per-writer collision checks

Each writer decides its own collision semantics and returns one status.
Destination checks are authoritative over the ledger: an item absent from the
ledger but already present at the destination is `existing`, not a re-import.

| Category | Destination check |
|----------|-------------------|
| `instructions` | Exact-text match against existing lessons → `existing` |
| `memories` | episodic: `has_episodic_text()` exact match. semantic: `set_semantic_if_absent()`; same key + different value → `conflict` |
| `skills` | Per-file byte comparison. All files present and identical → `existing`; a subset present → `conflict` |
| `mcp_servers` | Same name → deep-equal spec? `existing` : `conflict`. Plus `configured_mcp_aliases()` alias-collision check across every effective MCP source |
| `denied_commands` | Rule already present (by pattern) → `existing` |
| `workspaces` | Resolved path already registered → `existing` |
| `schedules` | `_same_schedule()` (name + message + timezone + trigger) |
| `settings` | `_merge_missing()` — never overwrites an existing value |

**Known limitation.** Exact-match dedupe for episodic memory means an upstream
edit of one character produces a new chunk and therefore a second, near-identical
episodic row. This is now structural rather than merely unimplemented: import
writes with `defer_embedding=True`, and `write_episodic`'s similarity check needs
a vector, so the fuzzy layer cannot run at write time. Accepted as the lesser
evil — a false "already have it" silently drops user knowledge, which is worse
than a near-duplicate — and the layers that actually protect the user are
unaffected:

- **Exact-text dedupe still applies** (the text-prefix check runs before any
  embed), so a byte-identical re-import is still a no-op.
- **The fingerprint ledger still applies**, so re-running an import does not
  duplicate anything.
- **The native memory is still never destroyed.** `preserve_existing=True` means
  import cannot tombstone, merge away, or evict an existing row; deferral only
  changes whether a *near*-duplicate is added alongside it.

## Status vocabulary

Writers return one of four statuses; the API maps them to three item outcomes.
This vocabulary is the frontend contract — the UI MUST NOT invent a fifth state.

| Writer status | Outcome | Meaning |
|---------------|---------|---------|
| `imported` | `accepted` | Written |
| `existing` | `deduplicated` | Already present, identical; nothing written |
| `conflict` | `rejected` | Destination holds a different item; resolvable via strategy |
| `rejected` | `rejected` | Refused by a safety or validity gate; not resolvable via strategy |

Apply also returns `skipped` entries (source unavailable, scan diagnostics,
`workspace_target_required`) which are **not** item outcomes — they describe
things never attempted.

## Per-source assumptions

Every source parser hard-codes assumptions about a private upstream layout.
These fail **silently** when upstream drifts, so each is recorded here and each
MUST be covered by a fixture-based regression test.

| Source | Root (env override → default) | Layout assumptions |
|--------|------------------------------|--------------------|
| `codex` | `CODEX_HOME` → `~/.codex` | `config.toml`; `AGENTS.md` (instructions); `memories/*.md`; `skills/` (excl. `.system`); `memories*.sqlite*` reported unsupported |
| `claude_code` | `CLAUDE_CONFIG_DIR`/`CLAUDE_HOME` → `~/.claude`; also `~/.claude.json` | `CLAUDE.md`; `rules/*.md`; `settings.json`/`settings.local.json` (`permissions.deny`); `memory/`; `skills/`; per-workspace `.claude/` |
| `gemini` | `GEMINI_HOME`/`ANTIGRAVITY_HOME` → `~/.gemini` | Four probed config layouts, because Antigravity is closed-source and its subpath has moved between releases: `config/mcp_config.json` (**the live Antigravity path**, confirmed against a real install), `settings.json` (legacy Gemini CLI), `antigravity/mcp_config.json` and `antigravity-cli/settings.json` (older/absent layouts — kept as cheap hedges; `antigravity/` is runtime state, not config). `GEMINI.md` (instructions, hierarchical: root + per workspace); `skills/` only if it is a `SKILL.md` package; per-workspace `.gemini/settings.json` and `.agents/mcp_config.json`. Workspaces come from `config/projects/*.json` — one file per project, folder held as a percent-encoded `file://` URI under `projectResources.resources[].folderUri` — NOT from a `projects` map. **MCP shape normalization (all three verified against a real install, and required: without them a real Antigravity install imports ZERO of its servers):** `httpUrl`/`serverUrl` → canonical `url`; drop the inert `$typeName` protobuf discriminator (`exa.cascade_plugins_pb.CascadePluginCommandTemplate`) that Antigravity stamps on every stdio entry; drop `env` **only when empty** (the key name matches `_SECRET_KEY_RE`, so an empty map would otherwise score as a credential and refuse the server). A populated `env` is deliberately left untouched and still refused — stripping it would both change how the server runs and hide that secrets were present. `userSettings.themeMode` is not mapped |
| `meshclaw` | `MESHCLAW_HOME` → `~/.meshclaw` | SQLite memory DB (`memory.db`: `semantic_memory` + `episodic_memories`; `workspace_id` holds the sentinel `default`, and `kind='directive'` marks a rule — see the two sections above); skills; config; `workspace/AGENTS.md` + `workspace/CLAUDE.md` and the same two per configured workspace. Only those canonical filenames are read — the MeshClaw workspace holds arbitrary user documents, so a blind `*.md` sweep there is wrong |
| `openclaw` | `OPENCLAW_STATE_DIR` → `OPENCLAW_HOME`/`<state>` → `~/.openclaw-<profile>` → `~/.openclaw` → `~/.clawdbot` | `openclaw.json` (+ legacy `clawdbot.json`); `SOUL.md`, `MEMORY.md`, `USER.md`, `memory/*.md` under `workspace/` \| `workspace-main/` \| `workspace-<agentId>/`; `skills/`, `.agents/skills/`; `exec-approvals.json` |
| `hermes` | `HERMES_HOME`/`HERMES_AGENT_HOME`/`HERMES_CONFIG_DIR` → `%LOCALAPPDATA%/hermes` (Windows) → `~/.hermes` | `config.yaml`/`.yml`; `memories/MEMORY.md`, `memories/USER.md`; `SOUL.md`; `skills/` (excl. managed + re-import dirs); `cron/jobs.json`; `memory_store.db` reported unsupported |

### Transitive re-import (Hermes)

Hermes's own import tooling writes foreign skills into
`skills/claude-code-imports/`, `skills/codex-imports/`, and
`skills/openclaw-imports/`, and merges foreign `MEMORY.md`/`USER.md` into its
own. A user who migrated Claude Code → Hermes → KiroCrew would otherwise import
the same skill twice under two different `source_id`s — which **neither** the
fingerprint (source-scoped) **nor** the destination check (different target dir)
can catch.

Therefore those three directory names live in `_FOREIGN_REIMPORT_SKILL_DIRS` and
are folded into `_HERMES_SKILL_EXCLUDED_PARTS`, alongside the existing
managed-skill exclusions (`.bundled_manifest`, `.hub/lock.json`). Nothing is lost:
the originals are still on disk, so the original source imports them normally.

The same overlap exists for Hermes **memory** (its importer merges foreign
`MEMORY.md`/`USER.md` into its own using a `§` delimiter). That case is NOT
excluded: the collision is content-level rather than a directory name, and
dropping Hermes's `MEMORY.md` wholesale would lose real data for a
Hermes-native user. Near-duplicate episodic rows are accepted as the lesser
evil (see the dedupe limitation above).

## API contract

All three endpoints are dashboard-owner-only and audited. `request["app"]` MUST
be `""` — an app token is never a dashboard user.

### Adding a source touches THREE allowlists, not one

The source id is enumerated independently in three places, and missing any one
of them fails in a different way:

| Layer | Symbol | Failure mode if omitted |
|-------|--------|-------------------------|
| Backend registry | `onboarding_import.SOURCE_IDS` (+ `_SOURCE_NAMES`, `_SOURCE_ROOTS`, `scanners`) | The source is never scanned |
| Handler projection | `dashboard/handlers/onboarding_import._SOURCE_IDS` + `_SOURCE_NAMES` | **`_scan_response` raises and the endpoint 500s — breaking the wizard for EVERY source**, on any machine where the new source's home merely exists. `_SOURCE_NAMES` is indexed with `[source_id]`, so it `KeyError`s even once `_SOURCE_IDS` is fixed |
| Frontend filter | `AgentImportFlow.tsx` `SUPPORTED_SOURCE_IDS` | `eligibleSources()` silently drops the source — it never renders, even with a working backend |

The handler duplication is load-bearing (it is the content-free projection
boundary), so it is pinned by `test_handler_source_tables_match_the_backend` in
`test/test_api_onboarding_import.py`: the omission fails loudly in CI instead of
as a production 500.

| Endpoint | Phase | Body |
|----------|-------|------|
| `GET /api/onboarding/import/scan` | detect + dry run | — |
| `POST /api/onboarding/import/apply` | apply | `{sources: [{id, categories: [...]}], conflict_strategy?}` — `conflict_strategy` is one of `skip`/`rename`/`overwrite`; absent = `skip`, unrecognized = 400 |
| `POST /api/onboarding/import/state` | onboarding bookkeeping | `{completed: bool}` |

Concurrency: apply holds a module-level import lock. Config-writing categories
run under the config lock; `mcp_servers` runs in a separate phase **outside**
the config lock (the MCP handlers take the MCP file lock before the config lock,
so holding both in the other order would invert). MCP writes reuse the
dashboard's MCP sidecar lock.

Response: `{imported: {<category>: <count>}, imported_count, already_imported,
embedding_backfill_pending, item_outcomes: [...], conflicts: [...],
skipped: [...], secret_count, unsupported_count, ledger}`. `item_outcomes` is
authoritative; the aggregate counts MUST be derived from it and MUST NOT be
reported independently. `embedding_backfill_pending` is **backend-only** — it
tells the handler to schedule the embedding sweep and MUST NOT cross into the
browser (the HTTP `summary` does not carry it).

## Session-import removal

Session/transcript import is removed. The removal deletes the categories'
scanners, writers, and their supporting machinery:

- `sessions` from `CATEGORY_IDS`; `_write_session`, `_session_destination_key`
- `_jsonl_session_items`, `_message_from_record`, `_extract_visible_content`,
  `_claude_record_is_excluded`, `_without_runtime_sessions`,
  `_add_sessions_and_workspaces`, `_record_workspaces`
- the OpenClaw session-provenance set: `_OPENCLAW_RUNTIME_NAMESPACES`,
  `_OPENCLAW_SESSION_OWNERSHIP_FIELDS`, `_OPENCLAW_CHECKPOINT_RE`,
  `_OPENCLAW_CREATED_VIA`, `_openclaw_session_provenance_is_user_owned`,
  `_openclaw_session_paths`, `_openclaw_session_artifact`,
  `_openclaw_entry_matches_file`, `_openclaw_registry_map`
- session reads in `_scan_hermes_db` / `_scan_meshclaw_memory_db`, and
  `_HERMES_RUNTIME_SESSION_SOURCES`
- the `sessions` branch of `_deduplicate_items` (transcript-hash canonicalization)
- session-only limits: `_MAX_JSONL_LINES`, `_MAX_MESSAGES_PER_SESSION`,
  `_MAX_LINE_BYTES`, `_VISIBLE_ROLES`, `_VISIBLE_TEXT_TYPES`, `_NON_TEXT_TYPES`
- `conversation_log` plumbing through `apply_import` and the handler

**Consequence for workspace discovery.** Workspaces were partly discovered by
reading workspace paths out of session records. After removal, workspace
discovery comes only from explicit configuration (`_collect_project_paths` and
each source's config-declared workspace values). This narrows coverage; it does
not break it. Do not reintroduce a session read to widen it.

**Ledger compatibility.** Existing ledgers may contain `category_id:
"sessions"` records. They are inert: no scanner produces a `sessions` item, so
the records are never consulted. The ledger version is NOT bumped — a bump
would discard every other category's records and cause a full re-import.
Already-imported sessions are left in place; import does not delete data it
wrote under a previous version.

## Security invariants

These are load-bearing. Changing any of them requires a security review.

1. **Apply re-scans from disk.** No payload from the HTTP client is ever
   written. Only `(source_id, category_id)` pairs, allowlist-filtered, are read
   from the submitted plan.
2. **Credentials are never read.** Known credential files are not opened;
   secret-shaped MCP `env`/`headers` keys and URL-embedded secrets are stripped
   and counted, never stored.
3. **YAML aliases are rejected.** `_NoAliasSafeLoader` refuses anchors/aliases —
   `yaml.safe_load` alone still expands them, which is a billion-laughs vector
   on untrusted input.
3a. **Content screens run on the value that will actually be written**, after any
   decoding step. Screening a JSON/encoded form and then writing the decoded one
   is not screening: a newline is stored as backslash-n so no injection pattern
   matches, and `AKIA` hides a credential outright. Enforced in
   two layers for DB-sourced values: `_decoded_value_is_unsafe()` screens every
   string leaf (and dict key) of the `json.loads` result at the row level, and
   `_add_db_directive` re-screens its own input as defence in depth — it is the
   last gate before the always-injected lesson tier and must not trust its caller.
   The walk **fails closed** past `_MAX_DECODED_VALUE_DEPTH`
   (`unscreenable_memory_record`) rather than stopping: a bound that silently
   truncates the walk reports a partially-screened value as clean, which makes the
   bound itself the bypass — a credential nested past it would reach the lesson
   tier. An unscreenable value is refused, never partially screened.
   This is not only a `memories` concern: `_SEMANTIC_PREFIXES` includes `lesson.`
   and `get_lessons()` selects `key LIKE 'lesson.%'`, so a plain semantic row under
   that prefix reaches the same tier `get_lessons_context()` injects into every
   session as authoritative.
4. **Symlink components are re-checked immediately before every write**, not
   only at plan time (TOCTOU).
5. **Skill packages land atomically** — staged in a sibling temp dir, then
   `os.replace`; failure leaves nothing partial.
6. **`is_sensitive_path` gates workspace registration**, and the data home may
   never be registered as a workspace.
7. **Deny-only for command rules.** An allow-list is never imported.
8. **Bounded everywhere.** File count, per-file bytes, total bytes, walk
   entries, chunk size, workspace count, MCP server count, schedule count, DB
   size and row count all carry explicit ceilings.
9. **Imported schedules are always disabled** (`enabled=False`), so no imported
   job can execute without an explicit user action.
