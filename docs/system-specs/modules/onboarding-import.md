# Foreign-Agent Import Module

Last Updated: 2026-07-28 (initial spec: industry-consensus scope, session-import
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

**Sources:** `codex`, `claude_code`, `meshclaw`, `openclaw`, `hermes`.

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

1. **Top-tier directives → `lessons.jsonl`.** The highest-priority durable
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
   a false `accepted`. Because the store prunes **oldest-first at 200**, import
   caps its own contribution at `_MAX_IMPORTED_LESSONS` (50) and emits an
   `instruction_count_limit` diagnostic on overflow — otherwise a large foreign
   instruction file would silently evict the user's own accumulated corrections.
   Instruction content passes the same two gates as memory: a *redaction* (not a
   size truncation) drops the file as `credential_bearing_instruction`, and
   `contains_injection` drops it as `injection_instruction_excluded`.
2. **Narrative knowledge → episodic memory.** Prose and notes that are not
   directives: `MEMORY.md`, `USER.md`, `memories/*.md`, project-local memory
   dirs. Chunked by `_memory_chunks` (paragraph-packed, ≤2000 chars),
   `importance=0.5`, `tags=["imported", <source_id>]`, `source="import"`.
3. **Key-value facts → semantic memory.** Only where a source already stores
   an explicit key/value pair whose key matches `_SEMANTIC_KEY_RE` and carries
   one of `_SEMANTIC_PREFIXES` (`pref.` / `project.` / `user.` / `lesson.`).
   Written with `set_semantic_if_absent` so a concurrent native write is never
   overwritten.
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
episodic row. Similarity-based dedupe is possible (the vector store is already
present) and is a candidate improvement; it is deliberately not in this version
because a false "already have it" silently drops user knowledge, which is worse
than a near-duplicate.

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
| `meshclaw` | `MESHCLAW_HOME` → `~/.meshclaw` | SQLite memory DB; skills; config; `workspace/AGENTS.md` + `workspace/CLAUDE.md` and the same two per configured workspace. Only those canonical filenames are read — the MeshClaw workspace holds arbitrary user documents, so a blind `*.md` sweep there is wrong |
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
item_outcomes: [...], conflicts: [...], skipped: [...], secret_count,
unsupported_count, ledger}`. `item_outcomes` is authoritative; the aggregate
counts MUST be derived from it and MUST NOT be reported independently.

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
