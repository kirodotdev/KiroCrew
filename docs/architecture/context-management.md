# Context Management

What Kiro Crew puts in front of the model, in what order, and where each piece
comes from. Six questions, one section each: a fresh main chat session, the
per-turn additions, a sub-agent, the default agent versus any other agent, a
crew member's session, and which crew-member private files exist today.

This is a contributor document: it cites private symbols throughout, so it lives
here rather than in the packaged end-user tree (`src/kiro_crew/docs/`), whose
charter routes an engineering note to `docs/`. The packaged index links across to
it.

Everything here is read from the current code, and citations name a file and a
**symbol** (`src/kiro_crew/context.py` → `build_message`) rather than a line
number, which rots. Where an older doc disagreed with the code, that doc was
corrected rather than annotated.

Detail this file deliberately does not restate:

| For | Read |
|---|---|
| Agent specs, `skill://` mapping, config files | [Agents & Configuration](../../src/kiro_crew/docs/agents.md) |
| Skill authoring, frontmatter, discovery tools | [Skills](../../src/kiro_crew/docs/skills.md) |
| Spawning, limits, results | [Subagents & Parallel Work](../../src/kiro_crew/docs/subagents.md) |
| Memory types, V1/V2, modes | [Memory & Learning](../../src/kiro_crew/docs/memory-and-learning.md) |
| Section internals and budget rationale | [memory-skills-hooks spec](../system-specs/modules/memory-skills-hooks.md) |
| Crew records, binding, `select_crew` | [crew-mode spec](../system-specs/modules/crew-mode.md) |
| Sub-agent lifecycle, host/profile resolution | [subagent spec](../system-specs/modules/subagent.md), [platform-context spec](../system-specs/modules/platform-context.md) |

## 1. Main chat session start

One function assembles the whole first-turn prompt: `context.py` →
`build_message` with `is_new_session=True`. It appends ~30 separately sourced
strings and returns them joined. `context.py` → `build_session_context` builds the
middle — the part wrapped in `[SESSION CONTEXT …]`, and the only part injected
once rather than per turn.

### Block order

Each block is a bracket marker that is also a contract with the model.
`context_blocks.py` → `_MARKERS` is the authoritative list and `_CLOSERS` the ones
that close; the metering reads the final string back through them, so that table
cannot disagree with what was sent.

| # | Block | Fed by | Condition |
|--:|---|---|---|
| 1 | `[AGENT SYSTEM PROMPT]` | `config/prompt.md`, or `prompt-orchestrator.md` by slot `mode`; `_load_agent_prompt` for a custom agent | skipped on a slim resume |
| 2 | `[CRITICAL RULES]` | `_critical_rules_for` (runtime-conditional) | unless the agent sets `includeCrewContext: false` |
| 3 | `[CURRENT DATE]` | `get_local_tz` + `KiroCrewConfig.timezone` | always |
| 4 | `[CURRENT AGENT]` / `[RUNTIME]` | `_runtime_display_name`, trusted `runtime_source` from the dispatcher | when a session key exists |
| 5 | `[CREW MEMBER OPERATING MODE]` | constant prose | member DM slot, and `_member_backend_can_dispatch` |
| 6 | `[MEMBER IDENTITY]` … | `_build_member_section` (see §5) | V1 member sessions |
| 7 | `[UI LANGUAGE]` | `_build_ui_language_section`, the configured language | only when set explicitly |
| 8 | `[CONTEXT SCOPE]` | `_build_context_scope_section` | a parent withheld a group (§3) |
| 9 | `[USER PROFILE]` | onboarding answers in config | non-empty, `lessons` group |
| 10 | `[WORKSPACE IDENTITY]` | `workspace_dir_for` | `kirocrew` agent only |
| 11 | `[DOCUMENTATION]` | `_build_docs_section`, the packaged docs dir | `kirocrew` agent, `project` group |
| 12 | `[Steering resources]` | `_load_steering_resources` → `file://*.md` in `~/.kiro/agents/kirocrew.json` | **Claude Code backend**, `kirocrew` agent, `project` group |
| 13 | `[THREAD CONVERSATION HISTORY]` | `compress_thread_history`, else `_recall_rows` truncation | new, non-resumed session |
| 14 | `[PREVIOUS TURN WAS CANCELLED …]` | `_build_stop_event_notes` | recent user stop |
| 15 | `[Memory …]` + `[Memory activity index]` + `[Memory tools]` | `memory.py` → `get_context`, `activity_index` | not temporary, `memory` group |
| 16 | `[Skills:]` (pinned bodies, then discovery) | `skills.py` → `get_context` | see §4 |
| 17 | `[Learned corrections …]` | vector `get_lessons_context`, else `lessons.jsonl` | `lessons` group |
| 18 | `## Recent Session Context` | `conversation_log.recent_with_provenance` | `memory` group |
| 19 | `[RESPONSE PREFERENCES]` | `_build_response_preferences_section` | reply-style level set |
| 20 | `[CONVERSATION HISTORY …]` | outer replay, `build_session_replay` | compressed history passed |

**A Memory V2 member is the one exception to this order.** Its essentials
envelope (`_build_v2_essentials`) is not a row in the table: the last step of
`build_session_context` splices it in immediately after the critical-rules block,
ahead of every other block. Three rows are suppressed for it, each on the same
`not essentials` gate: the member row (6), steering (12) and the memory family
(15) — the envelope carries its own persona, project documents and memory
binding, so re-injecting those would duplicate them (§5). Read the table as the
V1 / non-member order.

Then the per-turn blocks of §2 follow, and the user's own text is last. This
build happens *after* the user's message arrives, so it lands directly on
time-to-first-token: `build_session_context` stamps `_mark(...)` per group
(`member`, `preamble`, `profile`, `workspace`, `docs`, `steering`,
`thread_history`, `stop_notes`, `memory`, `skills`, `lessons`, `provenance`,
`finalize`) and `_emit_context_section_timings` logs and histograms them.

### What memory contributes at session start

`build_session_context` calls `MemoryStore.get_context` with
`include_activity=False`. That is narrower than it reads:

- **Preferences** — injected **complete**, not capped, while the protected set
  stays under the model-safe ceiling.
- **Semantic memory** — only the eligible `pref.*` records
  (`get_preferences_context`), not a query-ranked search.
- **Projects, daily history, episodic fragments** — **not injected**. `activity_index()`
  lands instead: a bounded index of project headings and the last three days'
  titles (the `cap` default of `activity_index`),
  plus a `[Memory tools]` line pointing at `memory_recall` for the bodies.

Lessons (`learn_add`) are separate and injected for **every** agent, custom
included. A lesson with no `repo_scope` applies everywhere; a scoped one reaches
only sessions whose project is inside that tree. The legacy `scope="workspace"`
tier never reaches a prompt.

### Budgets and caps

Two independent allowances plus one ceiling. Every limit is in characters, not
tokens, because characters are exact and free to count.

Each per-block budget is `_budget(fraction)` — a **share of the base**, never a
number of its own. The share is what the code states and what stays true when the
base moves, so the table quotes the share and the constant to read it from rather
than a byte figure (`docs/system-specs/common/code-style.md` owns this rule).

| Name | Share of the base | Constant |
|---|---|---|
| Background admission — the base every share below divides | whole | `_CONTEXT_BUDGET_BASE` |
| Preferences | 2.6% | `_MEMORY_PREFS_CAP` |
| Projects | 3.9% | `_MEMORY_PROJECTS_CAP` |
| Daily history | 16% | `_MEMORY_HISTORY_CAP` |
| Lessons | 22.6% | `_LESSONS_CAP` |
| Semantic / episodic | 7.7% each | `_SEMANTIC_MEMORY_CAP`, `_EPISODIC_MEMORY_CAP` |
| Skills discovery block | 15% | `_SKILLS_CAP` |
| Steering | 10% | `_STEERING_CAP` |
| Preamble headroom | 3% | `_PREAMBLE_HEADROOM` |

Four more limits are fixed constants rather than shares of that base, so read each
one where it is defined:

| Name | Constant |
|---|---|
| Confined project skill bodies | `PROJECT_SKILL_BODY_CAP` in `skills.py` |
| Episodic block on a new session | `_EPISODIC_INJECT_CAP` |
| Memory activity index | the `cap` default of `activity_index` in `memory.py` |
| Per-message truncation on the fallback path | `_PER_MESSAGE_CAP` |

Thread history has its own reference base, `_HISTORY_REFERENCE_BASE`, and takes
21% of it on the fallback path (`_HISTORY_BUDGET_CHARS`) and 27% compressed
(`_COMPRESSED_HISTORY_CAP`), each then scaled by the model window. One embedding
deadline covers a whole prompt build: `_PROMPT_BUILD_EMBED_TIMEOUT_SECS`, five
seconds.

- **Admission is by whole block, never by slicing the joined prompt.** Protected
  blocks (rules, preferences, identity, steering, pinned skill bodies) go first and
  are never truncated; the rest spend what is left, and an omitted block is named
  in a `[Context budget: omitted …]` line.
- **Thread history has its own window-scaled allowance**, and keeps its framing
  plus the newest tail rather than vanishing.
- **The model-safe ceiling** on protected content is
  `max(3 × the base, window_tokens × 4.0 × 0.125)` (`_PROTECTED_CONTEXT_FLOOR` is
  the base tripled; `_PROTECTED_CONTEXT_CHARS_PER_TOKEN` and
  `_PROTECTED_CONTEXT_WINDOW_FRACTION` are the two factors). Over it, lessons
  re-render smaller first; preferences are kept from their head with an in-prompt
  notice naming the file.
- **A bigger model window does not buy more background.** `_resolve_caps` scales
  only thread/replay limits; an unknown or `auto` window resolves to the 1M
  reference (`_effective_window`).

No memory cap is a config key — they are module constants. The keys that do change
assembly are `skills.max_triggered`, `skills.lazy_load`, `agent.session_sharing`,
`agent.member_dispatch`, and each agent's `includeCrewContext`.

Memory, lessons, history, folder names, channel text and skill bodies are all
agent- or third-party-writable, so `_neutralize_structural_markers` rewrites forged
boundary markers inside them before the genuine headers are minted around them.
Slack thread text and folder paths are also screened by `contains_injection` and
**dropped** with an audit row — a span-local scrub cannot stop directive prose.

## 2. During a session

After the first turn `build_message` injects no transcript — the ACP session
carries its history natively, and a second copy is two sources of truth. Per turn
it adds:

| Block | Source | When |
|---|---|---|
| `[RUNTIME]` refresh | trusted `runtime_source` | every follow-up; a channel turn also re-asserts the diff-block mandate |
| Channel history | `channel_history.context_for` | group-channel turns |
| `[SLACK THREAD CONTEXT]` | thread parent / metadata | Slack threads |
| `[PROJECT]` | the slot's project dir | every turn, `project` group |
| `[BOARD]` | slot board tags, sanitized ids | slot carries tags |
| `[RESOURCES]` | `resource_status.probe` | only when host memory is tight |
| `[FOLDER]` | sidebar ancestry | once per session, and after a move |
| `[THEME PERSONA]` / `$skill` bodies | `request_prefix_context` | dashboard-generated |
| `[Skill: name]` bodies, `[Relevant skills for this message]` | trigger matching | see below |
| `[Hook context:]` | `hooks.on_message` returning `HOOK_INJECT_CONTEXT` | matching hook |
| `[REPLY FORMAT RULES]` + guidance | `_interactive_guidance` | interactive sessions |
| `[CURRENT USER REQUEST …]` + the user's text | the turn | always last |

The guidance paragraphs sit **before** the request header on purpose: trailing
them displaced the request from the prompt's recency edge and the model regressed
to an older question.

### Trigger-matched skills

Off by default. `skills.max_triggered` defaults to **0**, which disables per-turn
word-overlap matching entirely; discovery then runs through the skill index,
`skill_search` and the `$skillname` token. Set it positive to re-enable matching.
The mechanism, when on:

- `skills.py` → `get_triggered_skills` scores each visible skill's comma-separated
  `triggers` through `trigger_match.py` → `trigger_score`. A phrase's score is the
  fraction of *its* words present in the message; an entry's score is its best
  phrase; a `!`-prefixed phrase whose words all appear is a veto evaluated after
  every positive, so phrase order cannot change the outcome.
- The bar is `MIN_TRIGGER_OVERLAP = 0.7`. `always: true` skills are excluded (already
  pinned) and a `repo_scope` mismatch suppresses mechanically. Matches are then
  truncated to `max_triggered`, highest score first.
- `skills.py` → `split_triggered` decides delivery. **Full body is the default**:
  a matched skill's procedure lands in the prompt as `[Skill: name]`. An
  unconfined skill opts out with `inject_on_trigger: false` and contributes one
  line to the `[Relevant skills for this message]` block from `skills.py` →
  `trigger_hint` instead. A confined project skill always takes the body path,
  because handing out a live path would bypass the descriptor-pinned reader.
- One SEL audit row records the matched set, the body/pointer split, and any
  negative-trigger deny.
- The Jev decision point (`decisions/points/skills_select.py` →
  `selected_skills`, wired through the `select` callable) may **replace** the
  matched set for a sampled session; `None` keeps the match, `[]` empties it. See
  [Jev Skill Selection](../../src/kiro_crew/docs/decisions.md).

### What comes back after compaction

kiro-cli compacts its own window and drops the session-start blocks with it.
`SessionManager.mark_needs_reinjection` arms a one-shot flag on confirmed
compaction; the next turn reads it through `messaging/dispatch.py` →
`consume_reinjection` and passes `needs_reinjection=True`. `build_message` then
re-adds, once:

1. the memory activity index and the `[Memory tools]` line;
2. `[REINJECTED AFTER COMPACTION — skills index for discovery]` — the same loader
   call and the same agent gate as session start, via `_skills_injection_plan`;
3. `[REINJECTED AFTER COMPACTION — response preferences]`, re-read from current
   config so a level changed mid-session lands;
4. the member section, re-read from disk — so the member gets its *current*
   briefing and permanent rules back, not the pre-compaction copy.

If that turn does not land (cancelled, refused, errored), `rearm_reinjection` puts
the flag back, so the context is never lost to a failed turn.

### The `[work ledger]` snapshot

Not part of `build_message`. `session_ledger.py` → `render_snapshot` renders a
compact block (`goal`, `phase`, `next`, the last three `tried`, artifacts, bounded
by `_SNAPSHOT_MAX_CHARS`) and `dashboard/handlers/autonudge.py` prepends it to a
**monitor /
auto-nudge cycle message**, so each cycle starts from durable state rather than
transcript memory. It is empty when the session has no ledger or its phase is
terminal. See [Session Ledger](../../src/kiro_crew/docs/session-ledger.md), [Monitor Loops](../../src/kiro_crew/docs/monitor-loops.md).

## 3. Sub-agent sessions (`spawn_run`)

A sub-agent's prompt is built by the **same** `build_message`, from
`subagent_manager/run.py`, with three differences.

**The task text.** A spawn naming no agent gets `_SYSTEM_PREFIX` ("You are a
focused sub-agent…") prepended; an explicit `agent=` does not, so the named
agent's own contract is not overridden. A run resumed after an unexpected
cancellation also carries `_CANCEL_RESUME_PREFIX`.

**Switchable context groups.** `spawn_run` exposes three booleans, all defaulting
to true, declared once in `mcp_tools/spawn.py` → `_context_group_props` so the two
spawn tools cannot drift:

| Flag | Drops | Advertised rule |
|---|---|---|
| `include_memory` | preferences, activity index, semantic, provenance — and, for a V2 member, its briefing layer and manual anchors (§5) | false when the task is fully specified by the text you wrote |
| `include_lessons` | learned corrections, user profile | false only when the child purely reads and reports |
| `include_project` | `[PROJECT]`, `[DOCUMENTATION]`, steering | false when the work is outside the active project tree |

`subagent.py` → `_context_groups_of` turns them into the `context_groups` set that
both `build_message` and the `state.json` record read, so a retried or continued
run sees the scope its caller asked for. Anything withheld is named to the child
in `[CONTEXT SCOPE]` — an unexplained gap makes a sub-agent invent user
preferences, and naming it converts that into an honest "not provided".

**What a sub-agent does not get.** No channel history, Slack thread context, board
tags, folder breadcrumb, dashboard tool nudges, or the parent's reply-style
preferences — and not the parent's conversation, so the brief must be
self-contained. It does inherit a member parent's captured member id and memory
store, so a crew's silo survives delegation.

A **fresh** run also gets no slim resume: it is a new session with no transcript to
restore. A **continued** run (`spawn_continue`, or `keep=True` resumed later) is the
opposite — `run.py` passes `resumed=True` into `build_message`, so it takes the
slim-resume path of §2 and gets the refreshed header over its restored history
rather than the full session-start build. A continuation that did not actually
resume fails closed rather than running without its prior context.

### Shared session vs dedicated process

`subagent_manager/run.py` → `_should_use_session_sharing_impl`: sharing needs
`agent.session_sharing` true (default **true**), a live ACP/kiro parent session, and
no per-run `model`, `allowed_tools` or `bare` flag. `keep=True`, a continuation, a
per-run model or effort override, or a member launch force the dedicated path.

| | Shared runtime | Dedicated process |
|---|---|---|
| Process | the parent's kiro-cli | its own kiro-cli |
| Start cost | ~200ms | ~3–5s |
| Context assembly | identical `build_message` call | identical |
| `$KIROCREW_SCRATCH` | **the parent's** | **its own** |

`agent_scratch.py` → `allocate_scratch` gives each spawned agent **process**
`<data home>/scratch/<label>-<token8>/`, and `scratch_env` points
`TMPDIR`/`TMP`/`TEMP`/`KIROCREW_SCRATCH` at it — per process, not per session
(`acp/client.py` for a dedicated client, `acp/runtime.py` for a runtime). It also
pins kiro-cli's chat log there, but only where `cap_kiro_cli_logs` can bound it
(`_CAN_CAP_LOGS`); on Windows the key is omitted and kiro-cli keeps its default
location, since a pinned log nothing rotates is worse than the CLI's own unlink.

So a **shared-session** child sees the parent's scratch dir while a
**dedicated-process** child gets an empty one: a brief staged under
`$KIROCREW_SCRATCH` is unreadable to a dedicated child. Reclamation is keyed on
process liveness rather than on file age — a directory is removed only once its
recorded owner's process GROUP is dead **and** the whole tree has been idle past a
grace window, and an ownerless directory is never deleted.

## 4. Default agent vs other agents

One function decides, for both session start and post-compaction re-injection:
`context.py` → `_skills_injection_plan(agent, is_cc)`. It returns
`(inject?, globs)` from two facts — whether the agent's JSON maps skills, and
whether the agent is the built-in `kirocrew`.

| Agent | `skill://` mapping | Skills it sees | Why |
|---|---|---|---|
| `kirocrew` | none | the whole catalog (bounded discovery) | the default path |
| `kirocrew` | mapped | only the mapped set, on **either** backend | bounded directory and scoped activation |
| custom | none | **nothing** | the agent is expected to bring its own |
| custom | mapped | only the mapped set, on **either** backend | bounded directory and scoped activation |

The mapping is read by `agent_discovery.py` → `agent_skill_globs`, which pulls the
`skill://` entries out of the spec's `resources` (`skill_resource_uris`) and
expands each into an fnmatch glob over real paths (`expand_skill_uri`:
`skill://~/…` against the home dir, `skill:///abs/…` verbatim, a relative URI
against the project root inferred from the spec's location). `file://` steering
entries are deliberately excluded, and an `only=` list matching nothing yields
**no** skills rather than the full catalog.

A mapping defines availability on both backends. Crew supplies a bounded directory
and an agent-scoped `skill_search` pointer; ordinary bodies load only when selected.
`skill_search(action="list", offset=...)` pages through the complete resolved set,
including skills absent from the startup directory. `action="read", key="full/key"`
loads that exact key. Qualified `$namespace/name` references match complete keys;
an unqualified leaf is accepted only when unique within the available set.

Native Kiro 2.21.2 loads skill names/descriptions at startup and bodies on demand.
It does **not** eagerly load every `skill://` body, but its metadata directory is
unbounded. Crew's native launch view therefore removes `skill://` entries while
retaining the authored mapping for Crew discovery. Managed transport aliases
preserve the original agent identity in Crew. The workspace CLI overlay disables
inherited native resources; inherited steering and AGENTS.md are carried as explicit
file resources unless inheritance was already disabled. Authored specs stay intact.
This applies to native CLI launch paths; it does not redefine ACP, which is the
transport protocol. MCP Tool Search discovers tool schemas, not skill bodies.

### What "lazy skill loading" means here

1. **Bounded discovery, always.** Default and mapped agents receive a directory
   inside the skills section allowance, with list/search/read instructions for
   the omitted tail. Ordinary trusted project skills also activate on demand,
   through the descriptor-confined reader rather than a mutable checkout path.
2. **`skills.lazy_load`** (default true) selects the ranked `## Available Skills`
   index. False selects the shorter search pointer and up to eight usage-ranked
   names. Both use the same allowance; mapping never expands it.
3. **Required instructions.** `always: true` bodies share an explicit 99,000-byte
   startup capacity, including rendered headings and framing. An unavailable or
   over-capacity required body fails context construction with an actionable error;
   required instructions are never silently truncated or deferred.
4. **Activation.** Search considers metadata and body terms together, ranking
   overall query coverage before rarity and metadata preference. Incremental body
   indexing has a short work budget; incomplete results say so and can be retried.
   Paginated listing and exact reads remain available during indexing. Trigger-time
   loading (§2), when enabled, is also constrained by an explicit mapping.

### Consequence for `kirocrew-worker`

`agent.py` → `_write_worker_spec` derives the worker spec as
`default + @kirocrew-work − cron scheduling − unassigned opt-in servers`. The
mirrored keys are `_WORKER_MIRRORED_SHAPES`: `tools`, `allowedTools`,
`excludedTools`, `mcpServers`, `model`. **`resources` is not mirrored**, so the
worker inherits only the template's `file://` steering glob and carries no
`skill://` entry.

Its name is not `kirocrew`, so `_skills_injection_plan` reads it as a custom agent
with no mapping — row 3 of the table. A dispatched worker therefore sees **no skill
catalog, no `skill://` bodies and no trigger matching** (matching is gated on
`not is_custom` for the same reason). A worker that needs a procedure must be told
to read it — `skill_search` / `skill_fetch` still work, and the usual instruction
names the SKILL.md path in the brief. Memory, lessons, critical rules and hooks are
unaffected: those are injected for every agent.

## 5. Crew mode (Crew page)

A **crew member** is an entry in `config.json` under `agents.<name>` binding a
kiro-cli template (`kiro_agent`), a workspace, a memory store, a model and
free-text `triggers`; `config/loader.py` → `resolve_agent_bindings` turns the name
into `ResolvedBindings`, and the member's own space lives in `members.py`.

### The member's DM session

`context.py` → `_build_member_section` assembles four layers, in fixed precedence
order — earlier outranks later, with one stated exception:

| Layer | Owner | Source | Writable by |
|--:|---|---|---|
| 1 `[MEMBER IDENTITY]` | product, derived | the crew's `description` + `triggers` in `config.json` | dashboard |
| 2 `[HOW YOU WORK]` | product | `_MEMBER_HOW_YOU_WORK`, identical for every member | nobody |
| 3 `[PERMANENT RULES]` | the user | `<data home>/trust/member-rules/<slug>.json` | dashboard only |
| 4 `[CURRENT ASSIGNMENT]` | the member | `<data home>/members/<slug>/briefing.md` | the member's own file tools |

The exception: layer 3's own header claims precedence over the whole section,
protocol included, so the user's safety boundary is never formally outranked by
product prose.

Two failure behaviours are deliberate and opposite. A **missing** rules file reads
as `""` (the normal unbounded-by-choice state). An **existing but unreadable** one
raises `MemberRulesUnreadable` and **aborts the turn** — a member the user bounded
must not keep running with no bounds. The briefing is total by contract: any
failure reads as "no briefing yet", and content past `MEMBER_BRIEFING_MAX_CHARS`
is cut with a visible marker so the member prunes it. On Windows that
read fails closed (`member_briefing_supported`) and the section says the layer is
unavailable. Every variable payload runs through `_scrub_member_payload` first.

`members.py` → `member_turn_context` is the single chokepoint deciding, per
session lifecycle, whether the section is delivered and whether the rules gate
runs: `FRESH` (session start), `WARM_REINJECTION` (post-compaction), `WARM` (gate
only — the section is still live in the conversation), `SLIM_RESUME` (re-inject
the *current* section over the restored, possibly stale copy), `MINIMAL` (cron;
none).

### Member-scoped memory

- An explicitly created member owns a **Memory V2** store keyed by an immutable
  `member_id` that survives renames and template changes: facts, rules,
  experiences, summaries and vectors in one SQLite database.
- A V2 member's prompt carries **no** searched memory block. It gets the bounded
  essential envelope from `_build_v2_essentials` plus a `[Memory tools]`
  instruction to call `memory_recall` with a specific question. A store that
  cannot be prepared prints `[Member memory unavailable]` and the turn continues —
  substitution with Global memory is never authorized.
- Legacy and auto-discovered members stay on **Global V1** and read §1's block.
- Isolation is routing for the built-in tools, not secrecy against arbitrary code
  running as the same OS user.

### Routing and dispatch

`select_crew` (roster / bind) is answered by `mcp_core.py` → `_do_select_crew`,
and `route_crew` by its own `_do_route_crew` beside it. Routing scores the crews'
free-text `triggers` through the same
`trigger_match.py` primitive the skill matcher uses (`rank_triggered`), so the two
cannot disagree. A bind records a routing-*intent* pointer via
`members.record_activity` (`via="select_crew"`), keyed under `decided_in` because
the decision is made in the parent session while the crew runs elsewhere.

A member dispatches real work instead of running it inline. Its DM thread mounts
the dashboard session-control server per session
(`members.member_dispatch_session_server`); the member caller is authorized
automatically for sessions it created, bounded by `agent.member_dispatch`.

**The inheritance rule that matters:** `dashboard/session_control.py` resolves a
new session's agent as `agent.strip() or caller_slot.agent` — an omitted `agent`
makes the child inherit the **caller's** agent, so a session created from a
conductor is born a conductor, with no file-writing tools and unable to do the
work. Pass `agent="kirocrew-worker"` (or the intended implementer) explicitly, and
a self-contained brief with `session_send`.

## 6. Crew-member private files: implemented vs planned

Audited against `<data home>/members/<slug>/` and the keystone-gated
`<data home>/trust/` subtree. On a populated host a member directory holds
`activity.jsonl` (plus its lock); `briefing.md` appears once the member writes one,
and `[CURRENT ASSIGNMENT]` renders `(empty — write your first briefing there …)`
until it does.

| Capability | Status | Evidence |
|---|---|---|
| Per-member markdown separate from `~/.kiro/agents/*.json` | **Not as a member-private file** | The only member-owned markdown is `briefing.md` (`members.py` → `member_briefing_path`), which is working memory, not a persona. Persona lives in the agent spec — and `agent_spec_format.py` does support a markdown spec (`<name>.md`, YAML frontmatter + body as the prompt, JSON wins when both exist) — but that file sits in the agents directory shared with every other tool, not in the member's private space. |
| Per-member working memory (briefing) | **Implemented** | `members.py` → `member_briefing_path`, `read_member_briefing`, `member_briefing_supported`; injected as layer 4 by `context.py` → `_build_member_section`. Agent-writable by design, with no dashboard endpoint — the member edits it with its own file tools. Capped at `MEMBER_BRIEFING_MAX_CHARS` on read. |
| Per-member permanent rules | **Implemented** | `members.py` → `member_rules_path`, `read_member_rules`, `write_member_rules`; stored under `trust/member-rules/` so the member's file tools cannot rewrite its own boundary. `MEMBER_RULES_MAX_CHARS` cap enforced on write (a human dashboard action), never truncated on read. |
| Private per-member memory | **Implemented for explicitly created members** | Memory V2: one SQLite database per immutable `member_id`, resolved by `config/loader.py` → `resolve_agent_bindings` and `execution_context.py` → `member_config_for_id`; bounded essentials from `member_essential_context.py`, recall through `memory_recall`. **Gap:** legacy and auto-discovered members remain on Global V1 and are not migrated, and a member cannot choose or rebind a shared store. |
| Per-member activity log | **Implemented** | `members.py` → `record_activity`, `read_activity`; append-only `activity.jsonl`, rotated at `_ACTIVITY_LOG_MAX_BYTES` keeping one generation, with a per-record cap that aborts the read rather than skipping an over-cap line. |
| Per-member permission control | **Partial** | Real and enforced, but keyed on the **template**, not the member: `agent_capabilities.py` resolves owner-reviewed intent over `agent_state.CAPABILITY_SECTIONS` (`mcpServers`, `tools`, `allowedTools`, `autoApprove`, `skills`, `prompt`, `model`, `resources`). A member gets its own permissions only through a **private copy** of its template (`private_to` lineage; `dashboard/handlers/agents.py` → `_foreign_private_copy_owner`, `_prune_private_copy_of_deleted_crew`), which is refused to a second crew. **Gaps:** two members sharing one template share its permissions; `[PERMANENT RULES]` is prompt-level guidance, not an enforced gate; and `agent.member_dispatch` is one global ceiling rather than a per-member one. |
| Per-member DM binding | **Implemented** | `members.py` → `dm_binding_path`, `read_dm_binding`, `write_dm_binding`; under `trust/member-bindings/`, because the binding is the thread's identity authority. |

The stated purpose of the private-file direction is per-member private memory plus
per-member permission control; both exist today, in the forms above.

## Where to look

| Question | Files |
|---|---|
| What is in the first-turn prompt, in what order | `src/kiro_crew/context.py` (`build_message`, `build_session_context`) |
| Which block is which, and how big it was | `src/kiro_crew/context_blocks.py` (`_MARKERS`, `_CLOSERS`, `measure_prompt`) |
| Budgets, caps, the protected ceiling | `src/kiro_crew/context.py` (`_budget`, `_resolve_caps`, `_ResolvedCaps`) |
| Memory block contents | `src/kiro_crew/memory.py` (`get_context`, `activity_index`) |
| Lessons | `src/kiro_crew/learn.py`, `src/kiro_crew/vector_memory.py` |
| Skill index, pinned bodies, discovery | `src/kiro_crew/skills.py` (`get_context`, `load_skill`) |
| Trigger matching, and its model-picked override | `src/kiro_crew/trigger_match.py`, `src/kiro_crew/skills.py` (`get_triggered_skills`, `split_triggered`, `trigger_hint`), `src/kiro_crew/decisions/points/skills_select.py` |
| Which agents get skills | `src/kiro_crew/context.py` (`_skills_injection_plan`), `src/kiro_crew/agent_discovery.py` (`agent_skill_globs`, `expand_skill_uri`) |
| Steering and hooks | `src/kiro_crew/context.py` (`_load_steering_resources`, `steering_target_admissible`), `src/kiro_crew/hooks.py` |
| Post-compaction re-injection | `src/kiro_crew/messaging/dispatch.py` (`consume_reinjection`, `rearm_reinjection`) |
| Ledger snapshot on nudge turns | `src/kiro_crew/session_ledger.py` (`render_snapshot`), `src/kiro_crew/dashboard/handlers/autonudge.py` |
| Sub-agent context scope | `src/kiro_crew/subagent.py` (`_context_groups_of`), `src/kiro_crew/mcp_tools/spawn.py` |
| Sub-agent prompt assembly, shared vs dedicated | `src/kiro_crew/subagent_manager/run.py` |
| Scratch directories | `src/kiro_crew/agent_scratch.py` (`allocate_scratch`, `scratch_env`) |
| The worker agent spec | `src/kiro_crew/agent.py` (`_write_worker_spec`) |
| Member identity, rules, briefing, activity, V2 essentials | `src/kiro_crew/members.py`, `src/kiro_crew/context.py` (`_build_member_section`), `src/kiro_crew/member_essential_context.py` |
| Crew records, routing, binding | `src/kiro_crew/config/loader.py`, `src/kiro_crew/mcp_core.py` |
| Per-member permissions | `src/kiro_crew/agent_capabilities.py`, `src/kiro_crew/agent_state.py` |
| Session creation and agent inheritance | `src/kiro_crew/dashboard/session_control.py` |
