# Foreign-Agent Import Module

## Overview

`onboarding_import.py` migrates a user's setup from another AI agent into
Kiro Crew. It runs from the first-run onboarding flow (after the Kiro CLI
prerequisite gate, before the theme tour) and from Settings on demand.

The module is a **projection**, not a mirror: it reads a foreign layout and
writes only into Kiro Crew's own containers through Kiro Crew's own APIs. It never
invents a storage format, never writes a file Kiro Crew does not otherwise read,
and never copies a foreign store verbatim.

Three phases, always in this order:

| Phase | Entry point | Writes? |
|-------|-------------|---------|
| Detect | `detect_sources()` | no |
| Dry run (preview) | `preview_import()` | **no — never touches disk** |
| Apply | `apply_import()` | yes, merge-only |

**Sources:** `codex`, `claude_code`, `gemini`, `openclaw`, `hermes`, plus any an
edition registers through the `ImportSourceProvider` CPP seam (see
[Registering a source](#registering-a-source)).

`gemini` covers Google's whole terminal-agent lineage under one id, because
Antigravity CLI reuses `~/.gemini` rather than claiming a directory of its own.
Gemini CLI stopped serving Pro/Ultra/free individual accounts on 2026-06-18 and
Antigravity replaced it, so that one root holds both a displaced Gemini CLI
user's config and a current Antigravity install.

## Structure and ownership

The engine is four owners behind one facade. Each module imports only the
modules listed above it in this table, so there is no cycle and any module can be
the first one a process imports.

| Module | Owns |
|--------|------|
| `onboarding_scan.py` | The scan accumulator (`_Scan`), the candidate item (`_Item`, whose `fingerprint` is the identity the ledger and outcomes key on) and `CATEGORY_IDS`. Also: the never-raising probes (`_exists_safe`, `_is_link_like`, `_stat_kind`); bounded, symlink-safe reads (`_safe_regular_file`, `_walk_files`, `_read_bytes`); the parsers (JSON, JSON5, TOML through the `_toml` tomllib/tomli ladder, and YAML through `_load_no_alias_yaml`); the SQLite snapshot helpers, including the hardlink refusal in `_sqlite_database_is_safe`; and the shared content screens: `_sanitize_text`, `_count_secret_fields`, `_decoded_value_is_unsafe`, and the skill activation screen (`_frontmatter`, `_column0_activation_declared`, `_skill_package`, which refuses `triggers`). It is the only module that calls the credential redactor, so it is the "Onboarding import" row of `security_posture._REDACTION_SINKS`. Category-specific refusals (the MCP field allowlist and URL/argument secret checks, the persona identity guard, schedule semantics) sit with their projection in `onboarding_plan.py`. |
| `onboarding_plan.py` | The normalized plan. It holds the category projections that turn parsed foreign data into `_Item` payloads through those screens (instructions and the identity guard, memories, database directives, MCP specs, skills, schedules, workspaces, settings). It also holds in-scan dedup (`_deduplicate_items`), the per-source summary, the plan document (`_plan_from_scans`), and the readers `apply_import` uses (`_selected_pairs`, `_plan_roots`, …). |
| `onboarding_sources/` | The package's `__init__.py` is the registry: the builtin descriptors, `_normalize_source`, the per-context cache behind `_sources()`, root and context resolution (`_source_roots`, `_source_context`), failure-isolated dispatch (`_scan_source`), and the managed / superseded MCP name sets. Each adapter module (`codex`, `claude_code`, `gemini`, `openclaw`, `hermes`, `lineage`) knows one layout. It reads file and database content only through the bounded readers in `onboarding_scan.py` and projects it through `onboarding_plan.py`; path probes (`exists`, `is_file`, `is_dir`, `iterdir`, `glob`) and the `is_sensitive_path` / `contains_injection` screens are called directly. OpenClaw's bespoke root and context rules and the Hermes `%LOCALAPPDATA%` root live in their adapters. |
| `onboarding_apply.py` | The file-backed half of apply: the ledger (`_load_ledger`, `_record_ledger`, `_write_json`), the conflict strategies and restore copies, and the writers whose destination is a Kiro Crew file (`_write_workspace`, `_write_settings`, `_write_mcp`, `_write_skill`). |
| `onboarding_import.py` | The facade and the run-level orchestration: `detect_sources`, `preview_import` / `_preview`, `apply_import` (the rescan, the per-item dispatch loop and the ledger flush discipline), `_source_exists`, and the writers into Kiro Crew's own stores: `_write_instruction`, `_write_memory`, `_write_schedule`. |

Placement rules that are load-bearing:

- **The store writers stay in the facade.** The persistence-switch inventory
  (`test_persistence_writer_inventory.py`) and the cron probe inventory
  (`test_cron_store_unreadable_boundaries.py`) name them as
  `onboarding_import.py::<function>`. The dispatch loop stays beside them rather
  than behind a writer table.
- **The Claude Code presence probe stays in the facade.** `_source_exists`
  accepts a `claude_code` root that is absent when `~/.claude.json` sits beside
  it; the adapter reads that same file for its configs.
  `test_agent_sdk_provider_identity.py` pins the `"claude_code"` source-id literal
  to `onboarding_import.py`, and this probe is where the facade uses it.
- **Code outside the engine reaches names through the facade.** The dashboard
  handler, `mcp_cleanup`, the managed-MCP registration tests and the frontmatter
  tests import them; specs and comments cite others by their
  `onboarding_import.<name>` path. The facade re-exports each as the owner's own
  object. `test_onboarding_import_refactor_contract.py` pins the list, pins that
  each re-export is the owner's object, and pins that the entry points and store
  writers are defined in the facade itself. The facade also keeps every public
  project name the module bound before it was split into owners (`platform_compat`,
  `atomic_write`, `ConfigReadError`, `update_config_locked`, `config_dir`,
  `make_sync_embed_fn`, `ONBOARDING_IMPORT`, `parse_block_scalar_header`,
  `split_frontmatter`, `FileTooLargeError`, `safe_read_file_bytes_nolink`,
  `Lesson`, `LessonStore`, `contains_volatile_lesson_fact`, `mcp_server_alias`,
  `current_context`, `safe_context_call`, `contains_injection`,
  `is_sensitive_path`, `redact_with_findings`, `VectorMemoryStore`), each bound to its defining module's own object, so code
  written against the single module still resolves. Standard-library, typing and
  third-party imports (`yaml`, `croniter`) and private helpers are not part of
  that surface. The contract test pins the list.
- **A patch on the facade reaches every call site.** The caps
  (`_MAX_FILES`, `_MAX_WALK_ENTRIES`, `_MAX_DB_ROWS`, `_MAX_DB_BYTES`,
  `_MAX_SKILL_BYTES`, `_MAX_SKILL_PACKAGE_BYTES`, `_MAX_MCP_SERVERS`,
  `_MAX_IMPORTED_LESSONS`) and the helpers `_has_symlink_component`,
  `_install_skill_tree`, `_sqlite_columns`, `_scan_lineage_install`,
  `_preserve_replaced_tree`, `_preserve_replaced_json`, `_toml`, `url2pathname`,
  `_is_link_like` and `_write_json` are mirrored the same way as the
  `kiro_crew.security` facade: `onboarding_import._EXPORTS` maps each name to the
  dotted name of the one module that holds it (the Gemini adapter for the
  standard library's `url2pathname`, since that is where its call site looks it
  up), and that module's namespace is the only place the value lives. The facade
  binds none of the 18 names. A read resolves the owner through `sys.modules` on
  each access (PEP 562 `__getattr__`, defined under `if not TYPE_CHECKING:` so a
  type checker resolves a name read through the facade from its real bindings and
  its `TYPE_CHECKING` imports, and reports one that is neither), and the facade's
  module class forwards a set or a delete to the owner, so `monkeypatch.setattr`, its undo,
  `mock.patch` and `mock.patch.object` on `onboarding_import.<name>` land in the
  owner and restore the owner's own original. Every reader outside the owner --
  the facade's `_source_exists`, `_preview` and ledger flush, and the apply
  owner, the registry and the adapters -- reads the name off the owner module at
  call time instead of holding its own `from ... import` copy, so the patch
  reaches it too. `mock.patch` with a truthy `create` through the facade is the
  one spelling this cannot serve: its exit deletes the name, which reaches the
  owner, and then skips the restore. An AST guard in
  `test_onboarding_import_refactor_contract.py` reads every file under `test/`
  and every `tests/` package under `src/` whose text contains
  `onboarding_import`. It fails on a `patch`, `patch.object`, `patch.dict` or
  `patch.multiple` call or decorator whose target resolves to a mirrored name on
  the facade and whose `create` -- keyword, positional or a splat -- is anything
  but the literal `False`. `patch` is recognised when reached through
  `unittest.mock` or `mock` bound by an import (aliased or not), an assignment,
  an `import_module` result or a `sys.modules` entry, and when `patch` or one of
  its three methods is itself bound by a `from ... import` or an assignment. A
  target resolves to the facade through a dotted string (literals, module-level
  string constants and the facade's `__name__`, joined by an f-string or `+`) or
  through a name bound to the facade by an import, an assignment, an
  `import_module` result, a `sys.modules` entry, or a call to a function in the
  same file that returns one of these. Assignment chains are followed to a fixed
  point, file-wide. It does not resolve a facade or `patch` received as a fixture
  or parameter or fetched with `getattr`, nor read a file whose only spelling of
  the module name is split across string literals. Once the target is the
  facade, a name only computed at runtime is a hit too. The one exemption is an
  explicit allowlist keyed on (path, test); the scan must equal it exactly, and
  it is empty. Each rule is pinned on a synthetic source it must flag and one it
  must ignore. The same file pins that the owner is the only engine module
  binding each seam, that the facade's own code reads no mirrored name as a bare
  global, that the facade's `TYPE_CHECKING` block imports every mirrored name
  from its owner, that `__getattr__` stays hidden from type checkers while every
  name production imports from the facade still resolves, that an owner already
  in `sys.modules` is read, written and restored without a call to `importlib.import_module`, and drives a behavioral
  scenario through every cross-module reader it finds in the source. `__all__` is derived from the
  facade's bindings plus the table, so a star import carries `url2pathname` too.
  `_MAX_LESSONS_TOTAL` and `make_sync_embed_fn` are read by the facade itself
  and are not mirrored. The facade also keeps `shutil` and
  `platform_compat` as attributes, the same module objects the owners use.
- **A by-name copy on the facade is not a seam.** Besides the mirrored names, the
  facade binds by-name copies of owner names: the apply owner's strategy
  constants (`CONFLICT_STRATEGIES`, `STRATEGY_SKIP`, `STRATEGY_RENAME`,
  `STRATEGY_OVERWRITE`), `CATEGORY_IDS`, the registry's `_sources`,
  `_managed_mcp_names`, `_SOURCE_ID_RE` and `_CORE_MANAGED_MCP_NAMES`, the scan
  owner's `_frontmatter`, `_column0_activation_declared` and
  `_load_no_alias_yaml`, and the pre-split public names. No owner reads the
  facade's copy, so a facade patch of one reaches the facade's own code at most.
  A second guard in `test_onboarding_import_refactor_contract.py` reads the same
  test files and collects every name a test rebinds on the facade (`setattr` or
  `delattr` on any receiver, `patch`, `patch.object`, `patch.multiple`; not
  `patch.dict`, which mutates the shared object in place). It fails when an engine
  module other than the facade and the name's `_EXPORTS` owner also binds that
  name: mirror the name in `_EXPORTS`, or patch the module that reads it. The
  guard is pinned on a planted source, and it must see the names the engine's own
  suites rebind (`_is_link_like`, `_write_json`, `_MAX_LESSONS_TOTAL`,
  `make_sync_embed_fn`), so an empty scan cannot pass.
- **One logger.** Every owner logs through `kiro_crew.onboarding_import`, the
  name operators and tests filter import warnings on.
- **Managed MCP names are read from the live registry.** `_scan_source` wires the
  registry's own `_managed_mcp_names` into `_Scan.managed_mcp_names`. The MCP
  projection calls it exactly where and as often as it always has, so no
  projection module imports the registry. A scan built any other way refuses
  instead of guessing.
- **Deferred imports stay deferred.** The dashboard MCP sidecar lock,
  `configured_mcp_aliases` and `CronService` are imported at their one call
  site. Importing the engine never loads the dashboard, MCP discovery or cron.
  `mcp_cleanup` in turn imports the facade lazily.

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
| **Sessions / conversation transcripts** | Not industry practice — no surveyed agent migrates transcripts. A transcript is a record of a conversation with a *different* model under a *different* system prompt; replayed into Kiro Crew it is misleading context, not useful memory. Reading it also requires hard-coding each source's private JSONL/SQLite schema, which fails **silently** when upstream drifts. Removing it deletes the module's largest and most fragile surface. See [No session import](#no-session-import). |
| **Persona / `SOUL.md` as a persona** | Kiro Crew's persona surface is theme-pack persona, governed by `capabilities.theme_persona`. Importing a foreign persona document *as a persona* would inject third-party text into the agent's identity through a path that bypasses that gate. The **directive content** of such a file is still migrated — as memory (below) — but its persona role is dropped. |
| **Credentials of any kind** | `~/.claude/.credentials.json`, `~/.codex/auth.json`, `.env`, `auth-profiles.json`, gateway tokens, provider API keys. Never read. MCP `env`/`headers` keys matching the secret patterns are stripped and counted into `secret_count`. |
| **Runtime state** | Subagent records, tool results, checkpoints, hook state, in-flight task state. Not user data. |
| **Architecture-specific config** | Plugin/hook/binding/agent-list configs, memory-backend selection, provider and model mappings. Kiro Crew is KiroACP-only, so provider/model translation has no destination. |
| **Opaque binary stores** | Foreign SQLite memory stores are reported as `unsupported_memory_database`, never parsed. |
| **Allow-lists (as opposed to deny-lists)** | A foreign `permissions.allow` *widens* the security boundary. Importing it would let a foreign config grant tool access inside Kiro Crew's own gate. Deny rules only. |

## Destination mapping: the memory hierarchy

Imported instruction/knowledge content is rewritten into Kiro Crew's existing
memory tiers. Tier choice is driven by two properties — **context priority**
(`context_assembly/budget.py` per-section caps) and **durability**.

### Durability constraint (read before choosing a tier)

`preferences.md` and `projects.md` are **replaced wholesale by the memory
consolidator**. Anything written there by import is destroyed on the next
consolidation run. **Import MUST NOT write to `preferences.md` or
`projects.md`.**

Durable tiers only:

| Tier | Context cap | Durability |
|------|-------------|------------|
| `lessons.jsonl` (`LessonStore`) | protected startup block, window-independent: `_LESSONS_STARTUP_CAP` for standing rules plus `_LESSON_EXPERIENCE_CAP` for past findings (`context_assembly/budget.py`; see [memory-skills-hooks](memory-skills-hooks.md#lessons-learnpy--vector_memorypy)) | Append-only; pruned oldest-first at `_MAX_LESSONS_TOTAL` (200) |
| Semantic memory (`VectorMemoryStore`) | 7.7% | Durable; key-addressed, confidence-gated |
| Episodic memory (`VectorMemoryStore`) | 7.7% | Durable; append-only |
| `.kiro/steering/*.md` | 10% | Durable, but **workspace-scoped** — a tier the system has, never an import destination (rule 4) |

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
   always-injected tier (the protected startup lesson block, bounded by
   `_LESSONS_STARTUP_CAP`). Applies to the
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
   to prevent. The guard checks **every line** of a paragraph, headings included (one
   identity line taints the whole paragraph, because a paragraph is imported
   whole) after stripping Markdown heading/list/quote/emphasis markers, so
   `# You are Aria` taints its paragraph too, and covers both
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
   ceiling as a file-sourced directive. It is NOT dropped: a lineage store keeps
   every learned lesson this way, so discarding them lost exactly the least
   replaceable rows in the store.
   **Screen the DECODED value, never the raw JSON.** A DB value arrives as JSON
   text, so `_sanitize_text`/`contains_injection` applied to `value_json` inspect
   escape sequences, not content: `"Ignore all previous\ninstructions…"` carries a
   literal backslash-n on disk and matches no injection pattern, while the
   `json.loads` result is a real newline and does. See security invariant 3a for
   the two layers that enforce this. It applies to the plain semantic path too,
   not just directives — a `lesson.*` key reaches the same always-injected tier.
4. **Instruction files land in the lesson tier, not in a steering file.**
   `_add_instruction_files` turns a `CLAUDE.md`/`AGENTS.md` (and a persona
   document's DIRECTIVE body) into `instructions` items of `kind: lesson`, and
   `_write_instruction` writes them through `LessonStore` / `VectorMemoryStore`.
   Import writes no `.kiro/steering/` file anywhere and takes no workspace-target
   argument. `preferences.md` / `projects.md` are not valid destinations either:
   the memory consolidator replaces both wholesale, so an import there is
   destroyed on the next consolidation run. Where a workspace itself gets
   registered is the separate `workspaces` category, which is not a
   steering-write destination.

Every imported memory item passes the existing content gates before it is
written: `_sanitize_text` (truncate + credential redaction; a *redacted* file is
dropped, a merely *truncated* one is not) and `contains_injection` (dropped as
`injection_memory_excluded`).

### Foreign workspace-scope columns: sentinel vs. real scoping

A foreign memory store may carry a workspace/scope column. Kiro Crew's own memory
tables have none, so a genuinely workspace-scoped row has no faithful destination
and is reported `scoped_memory_unsupported`.

**A sentinel value is not scoping.** A single-workspace install stamps every row
with the same placeholder, so treating the column as authoritative discards the
entire store. `_UNSCOPED_WORKSPACE_IDS` (`""`, `default`, `global`, `main`,
`none`, `null`) enumerates the placeholders; `_row_is_workspace_scoped()` is the
single predicate both DB scanners use. A lineage install stamps `default` on every
row, so before this predicate existed **100% of a real memory store imported zero
rows** while the UI reported success.

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
fix** — measured on real import text it is ~25% *slower* than looping `embed()`.
That measured workload result, rather than the physical micro-batch size, is the
reason imports defer the work: llama.cpp now decodes each input in bounded
512-token physical batches while retaining the full 2,048-token logical batch
and context.

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

**The plan is advisory, not authoritative.** `apply_import()` re-scans the source from disk and never writes item payloads echoed by the client. `_parse_selection()` validates the request shape and category ids, `_select_fresh_plan()` admits only `(source_id, category_id)` pairs offered by a fresh engine plan, and `_selected_pairs()` retains only source ids in that plan plus `CATEGORY_IDS`. The plan remains the identity authority when a provider fails closed between preview and apply, so a client cannot select an unplanned source. Consequently a preview status may differ from the applied status when the source changed in between. `apply_import()` returns per-item outcomes so the caller can report exactly which items diverged from their prediction; it MUST NOT silently present the preview as the result.

## Conflict strategy (user-selectable)

A destination collision is a **user decision**, not a hard failure. The apply
request carries a strategy; the default is the safest one.

| Strategy | Behavior |
|----------|----------|
| `skip` (**default**) | Keep Kiro Crew's existing item untouched; report the incoming one as `conflict`. |
| `rename` | Import alongside the existing item under a derived non-colliding name. |
| `overwrite` | Replace the existing item, after writing a restore copy. |

Applicability and rename derivation per category:

| Category | Strategies | Rename form |
|----------|-----------|-------------|
| `skills` | skip · rename · overwrite | `<name>-<source>`, then `<name>-<fingerprint[:8]>` |
| `mcp_servers` | skip · rename · overwrite | `<name>-<source>`, then `<name>-<fingerprint[:8]>` |
| `workspaces` | skip · rename | `base-<source>`, then `base-<fp[:8]>`. `skip` reports a collision; suffix derivation occurs only under `rename`. |
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
- `rename` lets the user resolve a collision caused by a source edit: a changed skill or MCP server has a different content-derived fingerprint, so it does not match the ledger record for the installed item.

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

Apply also returns `skipped` entries (source unavailable, scan diagnostics)
which are **not** item outcomes — they describe things never attempted.

## Per-source assumptions

Every source parser hard-codes assumptions about a private upstream layout.
These fail **silently** when upstream drifts, so each is recorded here and each
MUST be covered by a fixture-based regression test.

| Source | Root (env override → default) | Layout assumptions |
|--------|------------------------------|--------------------|
| `codex` | `CODEX_HOME` → `~/.codex` | `config.toml`; `AGENTS.md` (instructions); `memories/*.md`; `skills/` (excl. `.system`); `memories*.sqlite*` reported unsupported |
| `claude_code` | `CLAUDE_CONFIG_DIR`/`CLAUDE_HOME` → `~/.claude`; also `~/.claude.json` | `CLAUDE.md`; `rules/*.md`; `settings.json`/`settings.local.json` (`permissions.deny`); `memory/`; `skills/`; per-workspace `.claude/` |
| `gemini` | `GEMINI_HOME`/`ANTIGRAVITY_HOME` → `~/.gemini` | Four probed config layouts, because Antigravity is closed-source and its subpath has moved between releases: `config/mcp_config.json` (**the live Antigravity path**, confirmed against a real install), `settings.json` (legacy Gemini CLI), `antigravity/mcp_config.json` and `antigravity-cli/settings.json` (older/absent layouts — kept as cheap hedges; `antigravity/` is runtime state, not config). `GEMINI.md` (instructions, hierarchical: root + per workspace); `skills/` only if it is a `SKILL.md` package; per-workspace `.gemini/settings.json` and `.agents/mcp_config.json`. Workspaces come from `config/projects/*.json` — one file per project, folder held as a percent-encoded `file://` URI under `projectResources.resources[].folderUri` — NOT from a `projects` map. That URI is decoded with `urllib.request.url2pathname`, not a bare `unquote`: on Windows the URI is `file:///C:/Users/...` and stripping the scheme alone leaves `/C:/Users/...`, which carries no drive and so is not absolute, which would refuse every workspace as `workspace_not_absolute`; `url2pathname` is the platform-correct inverse (plain unquoting on POSIX, drive reconstruction on Windows). **MCP shape normalization (all three verified against a real install, and required: without them a real Antigravity install imports ZERO of its servers):** `httpUrl`/`serverUrl` → canonical `url`; drop the inert `$typeName` protobuf discriminator (`exa.cascade_plugins_pb.CascadePluginCommandTemplate`) that Antigravity stamps on every stdio entry; drop `env` **only when empty** (the key name matches `_SECRET_KEY_RE`, so an empty map would otherwise score as a credential and refuse the server). A populated `env` is deliberately left untouched and still refused — stripping it would both change how the server runs and hide that secrets were present. `userSettings.themeMode` is not mapped |
| `openclaw` | `OPENCLAW_STATE_DIR` → `OPENCLAW_HOME`/`<state>` → `~/.openclaw-<profile>` → `~/.openclaw` → `~/.clawdbot` | `openclaw.json` (+ legacy `clawdbot.json`); `SOUL.md`, `MEMORY.md`, `USER.md`, `memory/*.md` under `workspace/` \| `workspace-main/` \| `workspace-<agentId>/`; `skills/`, `.agents/skills/`; `exec-approvals.json` |
| `hermes` | `HERMES_HOME`/`HERMES_AGENT_HOME`/`HERMES_CONFIG_DIR` → `%LOCALAPPDATA%/hermes` (Windows) → `~/.hermes` | `config.yaml`/`.yml`; `memories/MEMORY.md`, `memories/USER.md`; `SOUL.md`; `skills/` (excl. managed + re-import dirs); `cron/jobs.json`; `memory_store.db` reported unsupported |
| a registered lineage source | whatever the descriptor declares | Kiro Crew's OWN layout, read by `_scan_lineage_install`: `config.json`, `mcp.json`, `recent_projects.json`, `workspace_dir`/`project_dir` pointers, `workspace/skills`, `workspace/memory`, `crons.json`, and `memory.db` (`semantic_memory` + `episodic_memories`; `workspace_id` holds the sentinel `default`, and `kind='directive'` marks a rule — see the two sections above). Only the canonical `workspace/AGENTS.md` + `workspace/CLAUDE.md` filenames are read, and the same two per configured workspace — that tree holds arbitrary user documents, so a blind `*.md` sweep there is wrong |

## Registering a source

The five builtins above are the foreign agents any user may plausibly have
installed. An edition that supersedes a predecessor of its own registers it
instead of the core naming it, which is what keeps an edition-specific product
name out of this tree.

`ImportSourceProvider.import_sources()` (CPP seam, public default `[]`) returns `ImportSource` descriptors. A descriptor says WHERE an install is: `id` (normalized by the engine registry), `display_name` (carried by the engine plan to the dashboard), `env_vars` + `home_dir` (where it lives), `managed_mcp_names` (that agent's own MCP servers, never imported), and `superseded` + `stale_mcp_binaries` (see below).

**A descriptor does not supply reader code, and does not choose a reader.** The
engine does all reading with its own helpers, which is what keeps credential
redaction, prompt-injection screening, sensitive-path refusal, size caps and
symlink rejection applying to a registered source exactly as they do to a built-in
one — those gates live inside ten separate read helpers, so a seam that accepted a
scanner callable would hand an edition the engine's internal accumulator and
depend on it to re-implement every one of them. A registered source is read as an
install of **Kiro Crew's own on-disk layout** by `_scan_lineage_install` — a
predecessor, a rename, or a fork, which is the case an edition actually has. A
genuinely novel foreign format needs a reader added to the core, because only the
core can read it through the gates; when a second layout exists, naming one
becomes an additive default-valued field on this same seam.

`_sources()` unions contributions over the builtins for every scan and apply registry snapshot, read fail-closed through `safe_context_call` — a broken adapter costs the edition's sources, not the page. `_source_summary()` carries the resolved id and display name in that snapshot into the plan, so consumers do not perform a second registry lookup.

**Everything questionable about a descriptor is settled at one boundary.**
`_normalize_source` is the only place that validates and canonicalizes, and BOTH
groups pass through it, so a builtin cannot travel a laxer path than the rule it
models and no consumer re-derives a field. It emits an internal `_Source` record:
ids are shape-checked, names are casefolded, and a launcher name is version-
stripped before the shared-runtime refusal. A descriptor is DROPPED with a reason
when it:

- has no id, an id that does not match `^[a-z0-9][a-z0-9_]*$`, or an id already
  registered (the id becomes a **path segment** — imported skills land in
  `skills/imported/<source_id>/` — so a separator or parent reference would place
  content outside the tree it is namespaced into, and shadowing would silently
  change what an existing id imports);
- declares neither `env_vars` nor `home_dir`;
- declares a `home_dir` that is not a single directory name (absolute, nested,
  `..`, over 255 characters, or containing a NUL);
- claims a shared runtime in `stale_mcp_binaries` — `node`, `python3.12`,
  `node-22.1` and any other versioned spelling all reduce to the same refusal, as
  do letter-suffixed aliases version stripping cannot collapse (`nodejs`), shebang
  wrappers (`env`) and multi-call binaries (`busybox`). That list drives deletion
  from the user's global config, so claiming an interpreter would reclaim every
  MCP server that happens to run on it. An agent's launcher is its own name,
  never the interpreter it starts.

  That list is **mistake-mitigation, not a security boundary**, and an edition
  authoring a descriptor should read it that way. It cannot be complete — every
  language ships an interpreter, and `lua`, `julia` and `rscript` were only added
  after a review pointed out their absence — so a descriptor naming one that is
  still missing is refused by review, not by this guard. What makes that
  acceptable is the trust level of the input: descriptors are edition code,
  shipped and reviewed like the core, never user- or network-supplied.
  `stale_mcp_binaries` is the one field that causes **deletion** from the user's
  config, so it is the field to scrutinise hardest when reviewing a descriptor.

A source declaring only `env_vars`, with none of them set, is left **unresolved**
rather than defaulting to the user's home root — `base_home / ""` is the entire
home, and scanning it would walk every file the user owns. Callers treat an absent
root as "not installed".

A scanner that raises is reported as a `source_unreadable` diagnostic (an
unsupported one) and its partial
findings are discarded: one unreadable source must not deny the user the others,
and half of a source we now know we cannot read correctly must not be offered as
importable data.

A **lineage source** — a predecessor, a rename, or a fork that writes this
product's own on-disk layout — needs no reader of its own and names none: the
engine reads every registered source with `_scan_lineage_install`. That is the
shape an edition migrating its users off a predecessor of its own registers.

**`superseded` is not implied by registration.** Only an agent this product
REPLACES has its leftovers reclaimed from the user's global provider config
(`mcp_cleanup`, by server name and by launcher basename). A live foreign agent's
managed servers are skipped on import but never purged — OpenClaw is the shipped
instance of that distinction, and purging its entries would delete servers the
user is still running.

### Transitive re-import (Hermes)

Hermes's own import tooling writes foreign skills into
`skills/claude-code-imports/`, `skills/codex-imports/`, and
`skills/openclaw-imports/`, and merges foreign `MEMORY.md`/`USER.md` into its
own. A user who migrated Claude Code → Hermes → Kiro Crew would otherwise import
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

### Filesystem probes on foreign-supplied paths MUST NOT raise

A scanner builds candidate config paths from workspace paths the foreign config
named, so those paths are attacker-influenced. `Path.exists()` / `is_file()` /
`is_dir()` re-raise any errno pathlib does not read as "absent" — its ignored set
is ENOENT/ENOTDIR/EBADF/ELOOP and does **not** include **ENAMETOOLONG**. An
escaping `OSError` reaches `_scan_response` and returns **HTTP 500**, breaking the
wizard for every source rather than skipping one path.

The trigger is **per-component**, not total length: `NAME_MAX` is 255 on both
macOS and Linux, while `PATH_MAX` is 1024 / 4096, so a 301-character path is far
below any total ceiling and still fails to stat. A total-length guard alone
cannot close this.

Two layers, both required:

1. `_exists_safe` / `_is_file_safe` / `_is_dir_safe` are the only probes used on a
   foreign-supplied path (`_walk_files`, `_named_descendant_dirs`,
   `_add_memory_files`, `_add_instruction_files`, `_parse_configs`). They answer
   "absent" on `OSError`, because an unprobeable candidate is exactly a candidate
   to skip. This is what protects every source, not just the newest one.
2. `_bounded_workspaces` rejects a path whose total exceeds
   `_MAX_WORKSPACE_PATH_CHARS` or whose any component exceeds
   `_MAX_WORKSPACE_COMPONENT_CHARS`, with a `workspace_path_too_long`
   diagnostic — so the user sees a reason in "Not imported" instead of a silent
   skip.

### Source identity has one authority

The engine owns source identity. `_sources()` resolves and normalizes the registry once per preview or apply operation, and `_source_summary()` carries its id and display name into the plan. The handler's `_parse_selection()` accepts bounded request shapes and known categories without maintaining a second source-id table; `_select_fresh_plan()` intersects client choices with the freshly generated plan before calling `apply_import()`. `AgentImportFlow.tsx` keeps a denylist only for the reserved `quick` setup mode, so registered sources remain visible. This prevents a registered source from being hidden or a transient registry read from turning an accepted plan into an HTTP error; `test_handler_category_tables_match_the_backend` and `test_selection_survives_a_registry_that_no_longer_lists_the_source` pin the seams.

| Endpoint | Phase | Body |
|----------|-------|------|
| `GET /api/onboarding/import/scan` | detect + dry run | — |
| `POST /api/onboarding/import/apply` | apply | `{sources: [{id, categories: [...]}], conflict_strategy?}` — `conflict_strategy` is one of `skip`/`rename`/`overwrite`; absent = `skip`, unrecognized = 400 |
| `PUT /api/onboarding/import/state` | onboarding bookkeeping | `{completed: bool}`. Failures: `invalid_request` (400), `config_unreadable` (500 — `config.json` cannot be parsed, so no retry clears it), `state_failed` (500). On a completion error the first-run Import step offers a local "leave without saving" exit |

Each endpoint is owner-only: the handlers call `require_owner_dashboard_request`
after authentication. With no `owner_id` configured the gate accepts the signed
local bootstrap subjects (`local-app`, `local-startup`), which is the identity
the onboarding flow runs as; once an owner exists, a stale pre-owner session
gets the 401 re-auth answer and every other non-owner subject gets a 403
`owner_only` denial. `test_agent_config_owner_gate_invariant.py` walks the two
mutating routes as part of its gated-route invariant.

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

## No session import

**Session and transcript import does not exist, and must not be added back.**
`sessions` is not a member of `CATEGORY_IDS`; there is no session scanner, no
session writer, no session provenance classifier, no per-session read inside
`_scan_hermes_projects_db` or `_scan_lineage_memory_db`, no transcript-hash branch in
`_deduplicate_items`, and no `conversation_log` plumbing through `apply_import`
or the handler. A reader looking for the deleted symbol names will find them in
git history, not here.

**Consequence for workspace discovery.** Workspace discovery comes only from
explicit configuration — `_collect_project_paths` plus each source's
config-declared workspace values. Reading workspace paths out of session records
would widen coverage, which is exactly why it is not done: importing another
agent's transcripts is not something a user consented to by importing its
config. The narrower coverage is the deliberate trade.

**Ledger compatibility.** Existing ledgers may contain `category_id:
"sessions"` records. They are inert: no scanner produces a `sessions` item, so
the records are never consulted. The ledger version is NOT bumped — a bump
would discard every other category's records and cause a full re-import.
Already-imported sessions are left in place; import does not delete data it
wrote under a previous version.

## Security invariants

These are load-bearing. Changing any of them requires a security review.

1. **Apply re-scans from disk against a fresh engine plan.** No item payload from the HTTP client is written. `_parse_selection()` validates bounded request shape and category ids; `_select_fresh_plan()` admits only `(source_id, category_id)` pairs offered by fresh `preview_import()`, and `_selected_pairs()` keeps only source ids in that plan plus `CATEGORY_IDS`. The plan remains the identity authority even if a provider fails closed between preview and apply; a client cannot select an unplanned source.
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
