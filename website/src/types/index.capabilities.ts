// Shared types for the agent's memory, skills, steering and MCP servers:
// lessons and memory stores, skills and their budget, steering files, skill
// and MCP discovery, and configured MCP servers with their scopes. Consumers
// import them from `types/index.ts`.

export interface Lesson {
  rule: string; category: string; ts: string
  /** The `DELETE /api/lessons` selector that names exactly this row. A lesson's
   *  identity is `(rule, repo_scope)`, so two same-rule rows in two scopes are
   *  two lessons: `""` is the global row, a fragment is that scope's row, and
   *  `null` is a row whose stored scope is unusable -- send NO selector for it,
   *  the unselective delete is the only path that reaches such a row. */
  repo_scope?: string | null
  /** Which JSONL file the row was read from, when the list is the JSONL union of
   *  the global file and the active workspace's. `DELETE /api/lessons` defaults to
   *  the global file, so a workspace row's delete must carry these back or it
   *  removes a same-text global row and leaves this one. Absent on vector rows,
   *  where the delete reaches the store whatever `scope` says. */
  scope?: 'global' | 'workspace'
  workspace?: string
  /** Where the lesson came from (`user_explicit`, `consolidation`, ...), as the
   *  store recorded it. Absent or empty when the row carries no source. */
  source?: string
}

/** One row of `GET /api/memory/stores` — a declared memory store.
 *
 *  Every count is BEST-EFFORT. A store whose file is missing or unreadable
 *  answers `exists: false` with null counts instead of failing the listing, so
 *  one damaged silo cannot hide every healthy one from the picker. `null`
 *  therefore means "not known" and must never be rendered as zero.
 */
export interface MemoryStoreSummary {
  /** Declared name. `'default'` addresses the global store. */
  name: string
  owner_member?: string
  /** The owner's validated avatar override; use owner_member as its exact seed. */
  owner_avatar?: unknown
  memory_version?: number | null
  is_default: boolean
  /** `'v1'` is the shared schema every install starts on; `'crew'` is the
   *  faceted per-silo schema. Typed open so a lineage added later still
   *  renders instead of falling through a closed union. */
  lineage: 'v1' | 'crew' | string
  exists: boolean
  semantic_count: number | null
  episodic_count: number | null
  lessons_count: number | null
  /** Whether carve facets apply. False on the v1 lineage, whose rows carry no
   *  facet columns at all — which is a different answer from "no rows". */
  facets_supported: boolean
  backup_count: number | null
  /** ISO-8601 UTC, or null when the store has never been backed up. */
  newest_backup: string | null
}

/** One row of `GET /api/memory/retired` — an episode a semantic write superseded.
 *
 *  Nothing hard-deletes an episode, so the text survives and the row can be put
 *  back. `retired_times` counts retirements rather than rows, because an episode
 *  can be retired, restored and retired again.
 */
export interface RetiredMemory {
  id: string
  text: string
  /** Key of the semantic entry that superseded it; empty when unrecorded. */
  superseded_by?: string
  retired_times?: number
  /** ISO-8601 stamp of the MOST RECENT retirement. */
  ts?: string
}

/** One row of `GET /api/memory/backups`.
 *
 *  `name` is the only handle a restore takes. The route returns no filesystem
 *  path on purpose: that would hand the browser the data-home layout.
 */
export interface MemoryBackup {
  name: string
  size_bytes: number
  /** ISO-8601 UTC, read from the stamped file name rather than the file's mtime,
   *  which a copy or a restore rewrites while the name still says when the
   *  contents were taken. */
  taken_at: string
}

/** One row of a carve page (`GET /api/memory/carve` with no `count_by`).
 *
 *  The embedding and `value_json` are absent from the wire by design, so a
 *  carve hands back a partition rather than a vector.
 */
export interface MemoryCarveEntry {
  id: string
  kind: string
  key?: string
  text?: string
  tags?: string
  importance?: number
  confidence?: number
  source?: string
  created_at?: string
  updated_at?: string
  scope?: string
  surface?: string
  crew?: string
  session_key?: string
  derived_from?: string
}

export interface Skill {
  key: string; name: string; description: string; always?: boolean; source?: string; package?: string
  /** False when the skill set `inject_on_trigger: false` — a trigger match then
   *  contributes a one-line pointer instead of the whole SKILL.md. */
  inject_on_trigger?: boolean
  /** Byte length of SKILL.md — half of the injection cost (the other half is
   *  how many times that body was delivered). */
  size_bytes?: number
  /** Times this skill's body was DELIVERED into a prompt. Not trigger matches:
   *  the ledger records only on delivery, so a false positive and a pointer-only
   *  skill both count zero. An opted-out skill therefore stops accruing, making
   *  its figure historical. `null`/absent means no ledger entry, which is NOT
   *  the same as zero (an entry can also age out of the window). */
  deliveries?: number | null
  /** Unix time (seconds) of the latest delivery; `null`/absent exactly when
   *  `deliveries` is. Drives the Skills list's "last used" text and its
   *  Recently used sort. */
  last_used_at?: number | null
  /** False when the SKILL.md lives outside the directory Kiro Crew owns (e.g. a
   *  `skills.extra_paths` entry). Such a skill is listed but not ours to rewrite,
   *  so the injection toggle must not be offered — the endpoint refuses it. */
  owned?: boolean
  /** Absolute path to SKILL.md on disk, when known. */
  path?: string
  /** Absolute path to the skill folder. */
  dir?: string
  /** Names of installed agents whose ``resources`` glob matches this skill's
   *  SKILL.md path.  Empty list means no agent loads it via kiro-cli's
   *  native ``skill://`` loader (it may still load via Kiro Crew text-injection). */
  loaded_by_agents?: string[]
}

/** Response shape for GET /api/skills/budget — the control-plane cost data. */
export interface SkillBudgetRow {
  key: string
  name: string
  size_bytes: number
  deliveries: number | null
  /** null when the cost is not measurable: an `always: true` skill is injected
   *  every turn but that injection is never recorded in the usage ledger. */
  chars: number | null
  inject_on_trigger: boolean
  always: boolean
  owned: boolean
  source: string
  folded_from?: string[]
  idle_days: number | null
}
export interface SkillBudgetResponse {
  window_days: number
  total_chars: number
  rows: SkillBudgetRow[]
}

/** A single entry in a skill folder's tree listing. */
export interface SkillTreeEntry {
  path: string  // relative to the skill root, posix-style (e.g. "references/doc.md")
  type: 'file' | 'dir'
  size: number
}

/** A Kiro steering file — always-on markdown injected into every session. */
export interface SteeringFile {
  /** ``"<source>/<rel>"`` — the API handle for read/update/delete. */
  key: string
  /** File name only (e.g. ``api-standards.md``). */
  name: string
  /** Path relative to the steering root, posix-style. */
  rel: string
  /** ``user`` → ~/.kiro/steering (global), ``workspace`` → <project>/.kiro/steering. */
  source: string
  /** Display path with the home prefix collapsed to ``~``. */
  path: string
  size: number
  /** First markdown heading of the document BODY, used as a one-line summary.
   *  Front matter is excluded, so a document opening with `inclusion:` is
   *  summarised by its title rather than by its first declaration. */
  description: string
  /** Declared `inclusion` mode, canonicalised: always one of `always`,
   *  `fileMatch`, `manual`, `auto`. An absent or unrecognised declaration
   *  reports `always`, which is both Kiro's documented default and what
   *  kiro-cli does with a value it does not recognise. */
  inclusion: string
  /** The `inclusion` value exactly as written, `''` when the field is absent.
   *  Differs from `inclusion` only when the author's spelling is not a mode —
   *  which is the one case worth telling them about. */
  inclusion_declared: string
  /** `fileMatchPattern` verbatim, `''` when absent. Only meaningful alongside
   *  `inclusion: fileMatch`. */
  file_match_pattern: string
  /** True for a leaf symlink admitted read-only: its resolved target passes the
   *  session loader's gate against the source's trust base, so the document
   *  loads into sessions but the write path refuses it. Optional because the
   *  UI reads it defensively — a cached listing from an older backend simply
   *  renders no chip. */
  linked?: boolean
  /** False exactly for linked entries — the tab disables Edit/Delete on them.
   *  Optional: an absent field fails OPEN (editable), see `selectedReadOnly`. */
  editable?: boolean
  /** Resolved symlink target (display path, home collapsed to `~`); `''` when
   *  the entry is not linked. */
  target?: string
}

/** Response shape of ``GET /api/steering``. */
export interface SteeringList {
  files: SteeringFile[]
  roots: Array<{ source: string; path: string; exists: boolean }>
  /** Active project directory (display path), empty when none is set. */
  project: string
  /** Why `project` is empty when it is: `none` (no chat names a project) or
   *  `ambiguous` (open chats name different ones, so the server refuses to
   *  pick). `set` when `project` is populated.
   *
   *  Required, not optional: the backend that serves this bundle is the one that
   *  answers this call, so the only skew that can occur is an OLD tab against a
   *  NEW backend — never a new tab against a backend too old to send it. */
  project_state: 'set' | 'none' | 'ambiguous'
  /** Opaque fingerprint of the project this listing resolved to, empty when
   *  there is none. A workspace write echoes it back so the server can refuse
   *  (409) once the chat slot has been re-pointed at a different project. */
  project_key: string
}

/** A skill result from the multi-provider discover endpoint. */
export interface DiscoveredSkill {
  id: string
  name: string
  description: string
  provider: string
  display_provider: string
  repo_url?: string
  author?: string
  installed: boolean
  tags?: string[]
  /** Install/download count from the provider (0 = unknown). */
  installs?: number
}

/** Outcome of one provider leg in a skill discovery search. */
export interface SkillProviderOutcome {
  name: string
  /** Display label for the provider; absent on gateways that predate it. */
  display_name?: string
  status: 'ok' | 'timeout' | 'error'
  /** The provider's reason, scrubbed server-side. Set only when it said why it could not search. */
  message?: string
}

/** Response from GET /api/skills/-/discover */
export interface DiscoverSkillsResponse {
  results: DiscoveredSkill[]
  providers: string[]
  /** Additive for compatibility with gateways that predate outcome reporting. */
  provider_outcomes?: SkillProviderOutcome[]
}

/** Response from GET /api/skills/-/discover/preview */
export interface DiscoverSkillPreview {
  description: string
  name: string
  license?: string
  author?: string
  /** Full SKILL.md markdown (display-capped server-side). */
  content?: string
  /** Bundle file manifest (capped at 200 entries). */
  files?: string[]
  file_count?: number
  /** Why the provider could not fetch the skill, when it said (scrubbed server-side). */
  error?: string
}

/** Response from POST /api/skills/-/discover/install */
export interface DiscoverInstallResult {
  ok: boolean
  key: string
  slug: string
  provider: string
  kind: 'created' | 'updated'
  file_count: number
}

/** A server result from the multi-provider MCP discover endpoint. */
export interface DiscoveredMcpServer {
  /** Provider-specific id (official: reverse-DNS name; capability: backend-defined id). */
  id: string
  /** Short display name (last path segment for official). */
  name: string
  /** Optional prettier title ("" if none). */
  title: string
  description: string
  provider: string
  display_provider: string
  /** "" when the provider reports no version. */
  version: string
  /** "" if unknown. */
  repo_url: string
  /** Cross-referenced against Kiro Crew's configured servers. */
  installed: boolean
  /** Install methods derivable from the entry (capability: ["capability"]). */
  methods: string[]
  deprecated: boolean
}

/** Outcome of one provider leg in an MCP discovery search. */
export interface McpProviderOutcome {
  name: string
  status: 'ok' | 'timeout' | 'error'
}

/** Response from GET /api/mcp/discover */
export interface McpDiscoverResponse {
  results: DiscoveredMcpServer[]
  providers: string[]
  /** Additive for compatibility with gateways that predate outcome reporting. */
  provider_outcomes?: McpProviderOutcome[]
}

/** Install-plan preview inside the discover detail response. */
export interface McpInstallPlan {
  method: 'npx' | 'uvx' | 'docker' | 'url'
  spec: { command?: string; args?: string[]; env?: Record<string, string>; url?: string }
}

/** Response from GET /api/mcp/discover/detail */
export interface McpDiscoverDetail {
  id: string
  name: string
  title: string
  /** Full description (markdown ok, redacted server-side). */
  description: string
  provider: string
  version: string
  repo_url: string
  /** What Install will write (preview) — null for capability entries. */
  install_plan: McpInstallPlan | null
  /** Env vars the user must fill after install ([] if none). */
  required_env: string[]
}

/** Response from POST /api/mcp/discover/install */
export interface McpDiscoverInstallResult {
  ok: boolean
  name: string
  required_env: string[]
  method: string
  /** False when the entry was written disabled (required env unset). Absent for capability installs. */
  enabled?: boolean
  /** False when the install was saved but the agent spec was not rewritten. */
  agent_spec_rewritten?: boolean
}

/** A raw mcp.json server spec (stdio command/args/env OR remote url). */
export interface McpCustomSpec {
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  /** Authorable on remote (url) specs; read responses redact every stored value. */
  headers?: Record<string, string>
}

/** GET /api/mcp/custom/{name} — editable spec; header values are redacted. */
export interface McpCustomSpecResponse {
  name: string
  spec: McpCustomSpec
  enabled: boolean
}

export interface McpScopePresence {
  kirocrew: boolean
  kiroGlobal: boolean
  // Provider-specific global scopes contributed by an edition via the
  // extra_mcp_scopes() seam, keyed by `${scopeId}Global` (e.g. "ccGlobal").
  // The public build has none; a companion adds them at runtime.
  [scope: string]: boolean
}

export interface McpTempRefusal {
  key: string; path: string
  cause: 'sealed' | 'unclassifiable' | 'check-failed'
}

export interface McpServer {
  name: string; command: string; args?: string[]
  url?: string
  /** Header names are preserved, but read responses redact every value. */
  headers?: Record<string, string>
  status: string; error?: string; tools?: string[]
  source: string; enabled: boolean; disabledTools?: string[]
  /** How status/tools were established: "handshake" (real spawn + tools/list)
   *  or "declared" (managed server's static declaration — nothing verified it
   *  can start). Absent on older runtimes. */
  probeMode?: string
  /** Wall-clock seconds of the probe that produced `status`; 0/absent = never probed. */
  probedAt?: number
  presence?: McpScopePresence
  /** True when the last probe met a recognisable OAuth challenge. Absent means
   *  the probe learned nothing about authorization — NOT that none is needed,
   *  so it must not be rendered as "no sign-in required". */
  authChallenge?: boolean
  /** Whether the kiro-cli runtime already holds a grant for this url. Only sent
   *  alongside `authChallenge`; absent is "unknown", which is why the sign-in
   *  wording is gated on an explicit `false`. */
  authGrantPresent?: boolean
  /** The owner added this remote server from the dashboard, at this url, so the
   *  table may offer the in-place sign-in on it. Absent or false for a server
   *  an agent, a session or a hand edit added. */
  ownerSignIn?: boolean
  /** Spec-declared temp keys the probe refused (path already redacted); the
   *  probe ran with the managed temp instead. Absent when nothing was refused. */
  tempRefusals?: McpTempRefusal[]
  /** Optional status-enrichment fields supplied by newer runtimes. */
  accountLabel?: string
  connectedSince?: string
  /** True when the entry lives in Kiro Crew's own mcp.json — the scope the
   *  Edit JSON action reads and writes (consent-disabled rows included). */
  kirocrewManaged?: boolean
  /** Which config switched the row off, from the backend — never inferred from
   *  `enabled` + `kirocrewManaged`. `kirocrew`: a disable in Kiro Crew's own
   *  store, which the Kiro Crew scope badge + Apply lifts (the consent step).
   *  `shared`: a disable in a config this panel does not write for enable (the
   *  shared Kiro MCP config the IDE edits, or a provider global), so the row is
   *  inert here; a row disabled in both reads `shared`. `null` when enabled. */
  disabledIn?: 'shared' | 'kirocrew' | null
  /** The file to edit to re-enable a `shared` row, home collapsed to `~`, when
   *  the backend can name it; `null` when it cannot. */
  disabledInFile?: string | null
  /** WHY the row is off, when the switch is not the honest story: `invalid`
   *  means the config's `disabled` is not a boolean (`"false"`, `1`, `null`) and
   *  the backend read it fail-closed -- an invalid value never launches a
   *  server -- so the fix is to repair the value where it sits, not to flip a
   *  switch. `null` when enabled or when some config really says `true`. */
  disabledReason?: 'invalid' | null
  /** Consecutive failed probes on record. Absent means none — a healthy server
   *  carries neither this nor `quarantined`. */
  probeFailures?: number
  /** True when those failures crossed the threshold and the server is no longer
   *  mounted into new sessions. Distinct from `enabled`, which is the user's own
   *  choice and is never overwritten by this. */
  probeFailing?: boolean
}

export interface McpApplyChange {
  name: string
  kirocrew?: boolean
  kiroGlobal?: boolean
  uninstall?: boolean
  toolOverrides?: Record<string, boolean>
  // Provider-specific global scopes ("<id>Global", e.g. "ccGlobal") contributed
  // by an edition via the extra_mcp_scopes() seam. Omitting a scope means
  // "preserve current presence"; the pattern index keeps `kiroGlobal` typed too.
  [scopeGlobal: `${string}Global`]: boolean | undefined
}

/** A provider-specific global MCP scope surfaced by the extra_mcp_scopes() seam. */
export interface McpGlobalScope {
  /** Presence/apply key, e.g. "ccGlobal". */
  id: string
  /** Human display label for the scope badge, e.g. "Claude". */
  label: string
}
