export interface StatusData {
  uptime: string
  start_time?: number
  sessions: number
  messages: number
  cron_jobs: number
  subagents: number
  lessons: number
  update_available?: boolean
  /**
   * Can this install replace its own code? Only a git checkout can — a wheel
   * install (the `cli.sh` managed venv) upgrades by re-running the installer, so
   * `POST /api/update` would 409. Shipped with the availability flag so the UI
   * can pick the right affordance without first running a check.
   */
  update_self_updatable?: boolean
  /** Did a check ever reach a verdict? Distinguishes "current" from "never checked". */
  update_checked?: boolean
  /** Upgrade command for an install that cannot replace itself ("" when it can). */
  update_command?: string
  update_progress?: { step: string; detail: string } | null
  version?: string
  /**
   * Which release lane these bytes came from. The gateway resolves it (see
   * `src/kiro_crew/release_channel.py`) rather than leaving the dashboard to
   * parse `version`: the same release is stamped as SemVer for the desktop app
   * and PEP 440 for wheels, and neither PEP 440 prerelease spelling
   * (`1.2.3rc4`, `1.2.3.dev<stamp>`) contains a `-`, so a mirror of the rule
   * here would drift and quietly call a prerelease build stable.
   *
   * Optional because an older gateway does not send it — treat a missing value
   * as "unknown", never as "stable".
   */
  release_channel?: 'nightly' | 'insider' | 'stable'
  branch?: string
  commit?: string
  platform?: string
  yolo?: boolean
  /** ISO timestamp when the current timed auto-approve grant expires ("" when none). */
  yolo_expires_at?: string
  /** Seconds left on the grant; -1 when it has no timed expiry. */
  yolo_remaining_secs?: number
  /** True when the active grant has no timed expiry (declared in config, or until_shutdown). */
  yolo_until_shutdown?: boolean
  /** Configured ad-hoc duration: a timed label, or 'until_shutdown'. */
  yolo_duration?: '30m' | '1h' | '6h' | '12h' | '24h' | 'until_shutdown'
  /** Whether enterprise governance currently allows the until_shutdown option. */
  yolo_until_shutdown_permitted?: boolean
  no_crons?: boolean
  /** True when the gateway has a live Slack (Socket Mode) connection. */
  slack_connected?: boolean
  /** Governance enforcement health. */
  governance?: 'active' | 'degraded' | 'disabled' | 'unknown'
}

export interface SystemData {
  hostname: string; os: string; arch: string; cpu_count: number
  load_1m: number; load_5m: number; load_15m: number; cpu_pct: number
  mem_total_gb: number; mem_used_gb: number; mem_free_gb: number
  ip: string; net_rx_mb: number; net_tx_mb: number
  net_rx_kbs: number; net_tx_kbs: number
  disk_total_gb: number; disk_free_gb: number
  python: string; pid: number; cwd: string
  proc_mem_mb: number; proc_cpu_pct: number
  child_processes: number; thread_count: number
  mcp_processes?: { sandbox: number; kiro_cli: number; builder_mcp: number }
  mcp_total?: number
  ollama_running?: boolean; ollama_pid?: number; ollama_mem_mb?: number; ollama_remote?: boolean
}

/** One age band of the storage report. The labels come from the server so the
 *  buckets the UI offers can never disagree with the ones it measures. */
export interface SessionStorageBucket {
  label: string; sessions: number; bytes: number
}

export interface SessionStorageBatch {
  batch_id: string; created_at: number; reason: string
  sessions: number; bytes: number
}

/**
 * What sessions cost on disk, and what may be reclaimed.
 *
 * Deliberately carries NO per-store breakdown: a session is one unit to the
 * person reading this, and the fact that it is written in two places is an
 * implementation detail the product does not surface.
 */
export interface SessionStorageReport {
  total_bytes: number; total_sessions: number
  active_sessions: number; active_bytes: number
  reclaimable_sessions: number; reclaimable_bytes: number
  /** Non-empty when this instance must not reclaim — show it instead of the action. */
  reclaim_blocked_reason: string
  buckets: SessionStorageBucket[]
  trash: {
    bytes: number
    /** Staged bytes are still occupying the disk until the trash is emptied. */
    still_on_disk: boolean
    /** True when the trash shares a filesystem with the stores, so moves are renames. */
    instant: boolean
    batches: SessionStorageBatch[]
  }
}

export interface SessionStorageCleanup {
  sessions: number; bytes: number; remaining: number
  /** Empty on a dry run — nothing was staged, so there is no batch to undo. */
  batch_id?: string
  dry_run?: boolean
}

export interface CronJob {
  id: string; name: string; message: string
  enabled: boolean; schedule: string; last_status: string
  cron_expr?: string | null; every?: number | null; every_secs?: number | null
  at?: number | null; created_ts?: number | null
  agent?: string; model?: string; channel?: string; approval_mode?: string; silent?: boolean
  strict_schedule?: boolean
  /** When true, this cron's runs do not appear as a chat session in the active
   * session list (results still go to Slack/notifications + History). Default false. */
  hide_in_chat?: boolean
  last_run_ts?: number; next_run_ts?: number | null; has_result?: boolean; has_slot?: boolean
  /** IANA timezone the cron expression's hour/minute fields are stored in.
   * Absent / null for legacy jobs created without an explicit TZ — treat as UTC. */
  timezone?: string | null
  skip_dates?: string[] | null
  script?: string | null; command?: string | null; last_result?: string | null; last_error?: string | null
  is_running?: boolean; running_since?: number | null
  folder_id?: string
}

export interface Lesson {
  rule: string; category: string; ts: string
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
   *  native ``skill://`` loader (it may still load via KiroCrew text-injection). */
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
  /** First markdown heading, used as a one-line summary. */
  description: string
}

/** Response shape of ``GET /api/steering``. */
export interface SteeringList {
  files: SteeringFile[]
  roots: Array<{ source: string; path: string; exists: boolean }>
  /** Active project directory (display path), empty when none is set. */
  project: string
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

/** Response from GET /api/skills/-/discover */
export interface DiscoverSkillsResponse {
  results: DiscoveredSkill[]
  providers: string[]
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
  /** Cross-referenced against KiroCrew's configured servers. */
  installed: boolean
  /** Install methods derivable from the entry (capability: ["capability"]). */
  methods: string[]
  deprecated: boolean
}

/** Response from GET /api/mcp/discover */
export interface McpDiscoverResponse {
  results: DiscoveredMcpServer[]
  providers: string[]
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
}

/** A raw mcp.json server spec (stdio command/args/env OR remote url). */
export interface McpCustomSpec {
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
}

/** GET /api/mcp/custom/{name} — full editable spec (env included). */
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

export interface McpServer {
  name: string; command: string; args?: string[]
  url?: string
  status: string; error?: string; tools?: string[]
  source: string; enabled: boolean; disabledTools?: string[]
  presence?: McpScopePresence
  /** Optional status-enrichment fields supplied by newer runtimes. */
  accountLabel?: string
  connectedSince?: string
  /** True when the entry lives in KiroCrew's own mcp.json — the scope the
   *  Edit JSON action reads and writes (consent-disabled rows included). */
  kirocrewManaged?: boolean
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

export interface TodoTask {
  id: string
  text: string
  /** kiro-cli's todo model is a plain boolean — there is no in-progress state. */
  completed: boolean
}

/**
 * The agent's own TODO list for a slot, mirrored from the `todo_list` tool.
 *
 * `completed`/`total`/`current` are computed server-side so the pill's "N of M"
 * label can never drift from the list it summarises. `current` is the first
 * not-completed task and is a DERIVATION — the agent does not report a current
 * task. Absent (`null`/`undefined`) means the agent never used its todo tool,
 * which renders as no pill; a present list with zero tasks means it cleared the
 * list, which is a different thing.
 */
export interface TodoList {
  description: string
  tasks: TodoTask[]
  completed: number
  total: number
  current: string
}

export interface SessionLink {
  channel: string
  label: string
  target: string
  /**
   * `origin` — the conversation started on that channel (read-only).
   * `out`    — dashboard replies are mirrored there (one-way, from `!link`).
   * `both`   — a session-RESUME binding from an in-channel `!sessions` pick:
   *            replies go there AND messages from there land in this session.
   */
  direction: 'origin' | 'out' | 'both'
  live: boolean
}

export interface ConfiguredChannelTarget {
  channel_type: string
  target_id: string
  label: string
  available: boolean
  unavailable_reason: string
}

export interface ChatSlot {
  key: string; title?: string; messages: number; running: boolean; stopping?: boolean; pending_approval?: boolean; created?: string; last_ts?: string; last_message?: string; agent?: string; model?: string; reasoning_effort?: string; mode?: string; surface?: string; workspace?: string; trust?: boolean; trust_reads?: boolean; folder_id?: string; pinned?: boolean; tags?: string[]; links?: SessionLink[]; slack_linked?: boolean; slack_channel?: string; slack_thread_ts?: string; color_index?: number | null; memory_mode?: 'persistent' | 'incognito' | 'temporary'; clean_mode?: boolean; project?: string; forked_from?: string | null; source_links?: { provider: 'github' | 'gitlab'; number: number; url: string; ci?: 'running' | 'passed' | 'failed' | null; state?: 'open' | 'draft' | 'merged' | 'closed'; mergeable?: string; mergeStateStatus?: string; kind?: 'change' | 'issue' }[]; source_links_total?: number
  /** Artifact companion binding: slug of the artifact this slot is a companion
   * chat for. Set at slot create and persisted in the history meta line, so the
   * binding survives a gateway restart and a History-page resume. */
  artifact?: string
  /** Metadata for kind="webapp" artifacts (deploy state, architecture, costs). */
  webapp_metadata?: WebAppMetadata
  // Board fields
  has_options?: boolean; options?: string[]; pending_approval_info?: PendingApproval | null; last_activity_ts?: string; waiting_for_input?: boolean; prompt_preview?: string; subagents_running?: boolean; orchestrating?: boolean
  // Soft-stop state machine
  stop_state?: 'idle' | 'soft_pending' | 'killing'
  /** Agent TODO list. Null/absent = the todo tool was never used in this slot. */
  todo?: TodoList | null
}

export interface PullRequestCommit {
  sha: string; title: string; body: string; author: string; date: string; url: string
}

export interface PullRequestCheck {
  name: string; workflow: string; status: string; conclusion: string
  bucket: 'passed' | 'skipped' | 'failed' | 'pending'; url: string
  startedAt: string; completedAt: string
}

/** Lightweight per-URL status used by wayfinding chips (sidebar + Changes tab
 *  strip). Every field is present only when known: the backend serves them
 *  from a short-TTL cache and refreshes in the background, so a freshly seen
 *  pull request has no status until a later poll. The merge fields are omitted
 *  while the provider is still computing mergeability, so their absence means
 *  "no news" — never "nothing blocks the merge". */
export interface PullRequestStatus {
  state?: 'open' | 'draft' | 'merged' | 'closed'
  ci?: 'running' | 'passed' | 'failed'
  /** Normalized merge ability, same vocabulary as `PullRequestSource.mergeable`. */
  mergeable?: string
  /** Normalized merge-state detail, same vocabulary as `PullRequestSource.mergeStateStatus`. */
  mergeStateStatus?: string
}

/** Response of the batched status endpoint. `refreshing` names the URLs whose
 *  cached value is expected to change shortly (a background provider refresh is
 *  in flight), so the client can re-poll soon instead of waiting a full cache
 *  interval; `ttlSecs` is the server's own cache TTL, which paces the steady
 *  state so the client never hardcodes a copy of it. */
export interface PullRequestStatusBatch {
  statuses: Record<string, PullRequestStatus>
  refreshing?: string[]
  ttlSecs?: number
}

export interface PullRequestComment {
  id: string; kind: 'comment' | 'review' | 'inline'; author: string; body: string
  state: string; createdAt: string; url: string; path: string; line?: number | null
  threadId?: string; resolvable?: boolean; resolved?: boolean
}

export interface PullRequestFile {
  path: string; status: string; additions: number; deletions: number; patch: string
}

/* ── Issue sources (GitHub issues / GitLab issues) ─────────────────────────
 * A session that MENTIONS an issue url gets an Issues side-panel tab, the same
 * inferred association pull requests already have. Served by
 * POST /api/source/issue. */

/** `color` is a bare 6-hex-digit string with NO leading '#' (GitHub's format;
 *  GitLab's `#rrggbb` is normalized to it server-side). The UI adds the '#'. */
export interface IssueLabel {
  name: string; color: string; description: string
}

export interface IssueMilestone {
  title: string; state: string; dueOn: string
}

export interface IssueComment {
  id: string; author: string; body: string; createdAt: string; url: string
}

/** A pull request / merge request the provider reports as linked to the issue. */
export interface IssueLinkedChange {
  provider: 'github' | 'gitlab'; url: string; number: number; title: string; state: string
}

/** Reaction tallies. Null on providers (or issues) that report none. */
export interface IssueReactions {
  total: number; plus1: number; minus1: number; laugh: number; hooray: number
  confused: number; heart: number; rocket: number; eyes: number
}

export interface IssueSource {
  provider: 'github' | 'gitlab'
  /** Always the validated request url, never the provider's echo of it. */
  url: string
  number: number
  title: string
  /** Issue body, markdown. */
  description: string
  state: 'open' | 'closed'
  /** 'completed' | 'not_planned' | 'reopened' | '' (always '' on GitLab). */
  stateReason: string
  author: string
  /** ISO8601, or '' when the provider omitted it. */
  createdAt: string
  updatedAt: string
  closedAt: string
  closedBy: string
  labels: IssueLabel[]
  assignees: string[]
  milestone: IssueMilestone | null
  commentCount: number
  locked: boolean
  reactions: IssueReactions | null
  comments: IssueComment[]
  linkedChanges: IssueLinkedChange[]
  /** Sections potentially incomplete because a provider request failed or hit a limit. */
  partialSections?: string[]
}

export interface PullRequestSource {
  provider: 'github' | 'gitlab'; url: string; number: number; title: string
  description: string; state: string; draft: boolean; mergedAt: string; updatedAt: string
  headBranch: string; baseBranch: string; headSha: string; author: string
  additions: number; deletions: number; changedFiles: number
  /** Normalized merge ability: 'mergeable' | 'conflicting' | 'unknown' ('' when the provider omitted it). */
  mergeable?: string
  /** Normalized merge-state detail: 'clean' | 'dirty' | 'behind' | 'blocked' | 'unstable' | 'draft' | 'need_rebase' (GitLab) | 'unknown' | ''. */
  mergeStateStatus?: string
  /** Auto-merge is armed: GitHub auto-merge, or GitLab merge-when-pipeline-succeeds. */
  autoMerge?: boolean
  commits: PullRequestCommit[]; checks: PullRequestCheck[]
  comments: PullRequestComment[]; files: PullRequestFile[]
  /** Sections potentially incomplete because a provider request failed or hit a page/output limit. */
  partialSections?: string[]
}

export interface ChatFolder {
  id: string; name: string; collapsed?: boolean; order: number; parent_id?: string; color?: string; default_agent?: string; project_dir?: string; hidden?: boolean; history_count?: number
}

export interface ChatTag {
  id: string; name: string; color: string; order: number; status?: boolean
}

export type TagColumnMode = 'any' | 'all' | 'none'

export interface TagColumn {
  id: string; name: string; tag_ids: string[]; mode: TagColumnMode; order: number; include_untagged?: boolean
}

export interface ChatMessage {
  role: string; content: string; cls: string; ts?: string
  /** Original unprocessed text — source of truth for reparse on stream completion. */
  rawText?: string
  /** Structured metadata for role-specific data (e.g. tool_input for permission messages). */
  meta?: Record<string, unknown>
  /** Regenerated variants of an assistant message (most recent last). */
  variants?: { content: string; ts?: string }[]
  /** Which variant index is currently active. */
  variant_idx?: number
  /** Counter for consecutive identical tool message deduplication. */
  _toolCount?: number
  /** Message kind discriminator for special message types (e.g. 'stop_event'). */
  kind?: string
}

export interface SubagentActivity {
  id: string; task: string; agent: string
  status: 'pending' | 'running' | 'tool' | 'done' | 'error' | 'stopped'
  streaming: string; lastTool: string
  startedAt: number; elapsed: number; error?: string
  toolCount?: number      // observed tool calls (incl. auto-approved) — running-card progress
  stalled?: boolean       // reaper flagged this subagent as idle/stalled
  retrying?: boolean      // transient-backend retry (or cancel auto-continue) in flight
  approval_id?: string
  approving?: boolean
  /** Inline terminal output for native (`native:*`) cards only. Native cards
   *  cannot lazy-load from disk (no SubagentManager record), so the bounded
   *  done-event result is stored here. Managed cards leave this unset and use
   *  the on-demand DiskLoader to keep Redux memory bounded. */
  result?: string
}

export interface ToolActivity {
  type: string
  text: string          // reasoning text or tool name
  purpose?: string      // tool purpose
  input?: string        // tool input (commands, file content, etc.)
  output?: string       // tool output (stdout, results, etc.)
  ts: number
  auto?: boolean        // auto-approved tool call
  approval_id?: string  // pending approval ID
  approval_type?: string // 'chat' or 'spawn'
  tool_call_id?: string  // for matching tool results
  rejected?: boolean     // true when approval was rejected
}

/** Parsed content block produced by the block assembler. */
export type BlockType = 'markdown' | 'code' | 'diff' | 'mermaid' | 'excalidraw' | 'widget'
export interface ContentBlock {
  type: BlockType
  content: string
  language?: string
  complete: boolean
  /** 1-based line in the original raw source where this block starts. */
  startLine?: number
  /** Artifact slug (widget blocks only) — when present, the widget is
   * already saved as an artifact in the user's library. The dashboard uses
   * this to render the bookmark filled, link the title to /artifacts/<slug>,
   * and treat clicks as un-save rather than save. */
  slug?: string
}

export interface Notification {
  kind: string; title: string; body: string; ts: string
  acked?: boolean; job_id?: string; task_id?: string; approval_id?: string
  slot?: string; session_key?: string; slack_link?: string
  // RFC Phase 3: schema-v2 routing + per-channel settings stamps
  source?: string; channel?: string; priority?: string; silenced?: boolean
  // RFC Phase 4: inline actions, stacking, dashboard-internal deep link
  group_key?: string; url?: string
  actions?: { id: string; label: string; url?: string }[]
}

/** One row from GET /api/notifications/channels. */
export interface NotificationChannel {
  channel: string
  source: string
  registered: boolean
  default_priority: string | null
  protected: boolean
  settings: { muted?: boolean; priority?: string }
}

export interface SecretaryItem {
  id: string; channel: string; channel_name: string
  thread_ts: string | null; message: string
  sender_id: string; sender_name: string
  thread_context: { sender: string; text: string }[]
  classification: string; draft: string; confidence: string
  status: string; created_at: number; context_summary?: string
}

export interface PendingApproval {
  tool: string
  tool_input: string
  tool_kind: string
  request_id: string
}

export interface SubagentInfo {
  id: string; task: string; done: boolean; error?: string; result?: string
}

export interface SessionInfo {
  key: string; title?: string; messages: number; created?: string; modified?: number; agent?: string; memory_mode?: 'persistent' | 'incognito' | 'temporary'
}

export interface TaskDetail {
  index: number; title: string; description: string; status: string; error: string; result: string; attempts: number
  depends_on: number[]; requires_approval: boolean; force_approval?: boolean; task_type?: string
  created_at?: number; started_at?: number; finished_at?: number
}
export type RunStatus = 'planning' | 'planned' | 'running' | 'completed' | 'failed' | 'cancelled' | 'paused' | 'pausing';
export interface ProjectRun {
  task_id: string; name?: string; running: boolean; status: RunStatus
  steps: number; completed: number; failed: number; skipped: number
  current_step: number; spec: string; spec_name: string; error: string
  tokens_used: number; replan_count: number; task_details: TaskDetail[]
  started_at: number; finished_at: number
  work_dir: string; branch_name: string
  spec_content: string; lessons_learned: string[]; commits: number
  original_input: string; source: string; groups: number[][]
  /** Whether this run should auto-approve tool calls (per-run trust toggle).
   * Reflects the last chosen value; deny-lists and force_approval gates still block. */
  auto_approve?: boolean
  auto_approve_remaining_secs?: number
}
export interface TaskRunnerStatus {
  running: boolean; available: boolean; runs: ProjectRun[]
  /** Pre-fill value for the per-run workspace-folder selector: configured
   *  taskrunner.workspace_dir if set, else the default per-run base directory. */
  default_workspace_dir?: string
}



export interface ArtifactPublication {
  /** Publishing-provider artifact UUID — stable across versions. */
  artifact_id: string
  /** Stable view URL: https://.../artifact/<id>. */
  view_url: string
  /** Publishing provider name (registry key of the destination). */
  provider?: string
  /** Sync authority: 'mirror' (KiroCrew-authoritative) | 'live' (remote CRDT). */
  collab_mode?: 'mirror' | 'live'
  visibility: 'PRIVATE' | 'SHARED' | 'PUBLIC'
  shared_with: string[]
  auto_sync: boolean
  last_synced_kirocrew_version: number
  /** Maps KiroCrew version (as string) -> provider version number. */
  version_map: Record<string, number>
  published_at: string
  published_by: string
  /** Conflict / sync-failure message surfaced to the UI; empty when healthy. */
  last_error: string
}

/** A publishing provider's self-described capabilities for a given artifact kind,
 *  returned by GET /api/artifacts/publish-providers?kind=<kind>. */
export interface PublishProviderDescriptor {
  name: string
  display_name: string
  capabilities: string[]
  kind_support: 'native' | 'converted' | 'degraded' | 'unsupported'
  capable: boolean
  /** False => tooling not installed yet; installs automatically on first publish.
   *  Optional: older gateways omit it (treat as available). */
  available?: boolean
  sharing_model: {
    supports_private: boolean
    supports_shared: boolean
    supports_public: boolean
    principal_kind: string
    supports_roles: boolean
    supports_expiration: boolean
    programmable: boolean
    out_of_band_url: string
  }
  sync_model: { authority: string; concurrency: string; collab_mode: 'mirror' | 'live' }
  discovery_model: {
    list_mine: boolean
    list_shared_with_me: boolean
    list_public: boolean
    full_text_search: boolean
    pull_by_id: boolean
  }
}

export interface ForkMetadata {
  upstream_artifact_id: string
  upstream_url: string
  upstream_owner: string
  upstream_version: number
  forked_at: string
}

/** One provider-neutral remote listing row, as returned by
 * GET /api/remote-artifacts/{provider}/browse. Inert in the public edition —
 * the endpoint 404s until a companion registers a provider.
 *
 * Fields fall into two groups:
 *  - The documented backend RemoteListing core (external_id, title, owner,
 *    view_url, updated_at, snippet) plus the handler's local_slug annotation —
 *    always present on a contract-faithful provider.
 *  - Optional companion extensions (tags, visibility, current_version,
 *    editable) beyond the base RemoteListing contract. A provider that only
 *    serializes the core dataclass omits these, so any UI gated on them (e.g.
 *    the Clone shortcut, which requires `editable`) simply doesn't render. */
export interface RemoteArtifact {
  /** The provider's stable id (clone/fork routes key on it). */
  external_id: string
  title: string
  owner?: string
  /** The provider's human link for the remote copy. */
  view_url?: string
  /** Best-effort ISO/epoch string for sort/display. */
  updated_at?: string
  snippet?: string
  /** Companion extension (not in the base RemoteListing contract). */
  tags?: string[]
  /** Companion extension (not in the base RemoteListing contract). */
  visibility?: string
  /** Companion extension (not in the base RemoteListing contract). */
  current_version?: number
  /** Set by the browse handler: the local slug if this remote artifact is
   * already cloned/forked onto this device (else null). */
  local_slug?: string | null
  /** Companion extension (not in the base RemoteListing contract).
   * Positive-only hint: the caller can edit the remote copy, so a Clone
   * shortcut is offered. NOT an enforcement gate — the provider remains
   * sole authority at push time. A core-only provider omits it, so Clone is
   * not shown (only Fork). */
  editable?: boolean
}

export interface Artifact {
  slug: string
  name: string
  kind: 'widget' | 'html' | 'markdown' | 'svg' | 'json' | 'text' | 'webapp'
  /** Provenance/origin bucket. Carries either a legacy bucket
   * (chat|cron|subagent|manual|import) or the actual session origin
   * (dashboard|slack|cli|task-runner|unknown), so treated as an open string. */
  source: string
  /** Originating chat session key (for the Source column's title resolution). */
  session_key?: string
  /** Live-resolved title of the originating chat session (or "(deleted session)"
   * when that session is gone). Absent for non-chat origins — fall back to `source`. */
  session_title?: string
  description: string
  tags: string[]
  version: number
  created_at: string
  updated_at: string
  content?: string
  /** Original source path for file-backed artifacts (live pointer). */
  source_path?: string
  /** True when the live state differs from the latest numbered snapshot.
   * Computed at GET time — accounts for both silent saves and external
   * file edits to source_path. Drives the "Snapshot Live" button. */
  live_dirty?: boolean
  /** Publication state. Absent/null until the artifact has been
   * published to a sharing provider. */
  publication?: ArtifactPublication | null
  /** Fork provenance (P1). Absent/null if not a fork. */
  fork_metadata?: ForkMetadata | null
  /** Short, markdown-stripped, redacted content preview — only present when the
   * list was requested with ?snippet=1 (used by the command palette's
   * Artifacts provider). For a ?content=1 query it is match-centered. */
  snippet?: string
  /** Library folder this artifact is filed in ("" / absent = unfiled/root).
   * Opaque folder id — resolve names via the artifact-folders list. */
  folder_id?: string
  /** User pin/favorite mark. Metadata-only (no version bump). Drives the
   * All | Pinned filter on the Artifacts page. */
  pinned?: boolean
  /** True when the store created this record itself from a chat-emitted
   * `<mcwidget>` rather than from an explicit save. Serialized straight off the
   * backend dataclass field of the same name (`Artifact.to_dict` is an
   * `asdict`, so every list/detail response already carried it — only this type
   * was missing it). Load-bearing for the chat Artifacts panel: the store
   * sweeps auto-registered records oldest-first past
   * `MAX_AUTO_WIDGET_ARTIFACTS` (200) unless `pinned`, so an
   * auto-registered-and-unpinned artifact is the ONLY one whose survival a
   * "save permanently" action changes. Absent on older payloads — treat
   * undefined as false. */
  auto_registered?: boolean
  /** Metadata for kind="webapp" artifacts (deploy state, architecture, costs). */
  webapp_metadata?: WebAppMetadata
}

/** A non-code document produced during a chat session — the virtual entries
 * shown in the Artifacts "All" tab. Not a persisted artifact until saved
 * (materialized) via api.materializeArtifact(path). */
export interface SessionDoc {
  path: string
  name: string
  updated_at: string
  session_key: string
  /** Human-readable session title (falls back to the session key). */
  session_title: string
  message_ts: string
  /** True when this path already backs a saved (pinned) artifact. */
  saved: boolean
  /** Slug of the backing artifact when saved; empty otherwise. */
  slug: string
}

/**
 * A folder in the local artifact library. Nested via `parent_id`
 * (`""`/absent = root). Structurally compatible with `ChatFolder` so the
 * shared folder utilities (`orderFoldersWithPaths`, `computeReorderedFolders`,
 * `FolderMoveSubmenu`) work on both without adaptation.
 */
export interface ArtifactFolder {
  id: string
  name: string
  order: number
  parent_id?: string
  icon?: string
  /** Optional #rrggbb display color chosen by the user. */
  color?: string
  /** Direct artifact count (excludes subfolders) — computed server-side per GET. */
  item_count?: number
  /** Full ancestry path root→leaf ("Parent › Child") — computed server-side. */
  path?: string
}

export interface ArtifactEvent {
  ts: string
  type: 'created' | 'edited' | 'iterated' | 'referenced' | 'reverted' | 'comment'
  by?: string
  session_id?: string
  version?: number
  /** For ``reverted`` events: the historical version whose content was
   * copied into the new current version. */
  from_version?: number
  /** Event-type-specific extras. For ``comment`` events:
   * ``action`` (deleted | reviewed | resolved), ``comment_snippet``
   * (≤100-char excerpt of the affected comment), and ``reason``
   * (agent's justification on deletes). */
  metadata?: Record<string, string | number | boolean | null>
}

export interface CommentAnchor {
  quote?: string
  prefix?: string
  suffix?: string
  start_offset?: number
  end_offset?: number
  version_number?: number
}

export interface ArtifactComment {
  id: string
  origin: string
  provider?: string | null
  scope: 'private' | 'shared'
  author: string
  is_agent: boolean
  body: string
  anchor?: CommentAnchor | null
  thread_id: string
  parent_id?: string | null
  status: 'open' | 'review' | 'resolved'
  sync_state: string
  /** True when the anchored text no longer exists in the artifact content
   * (backend rescans anchors on every content write). */
  anchor_orphaned?: boolean
  created_at: string
  updated_at: string
}

// ── WebApp Artifact types (kind="webapp") ────────────────────────────────────

export interface WebAppDeployTarget {
  provider: string;
  account: string;
  region: string;
  public_url: string;
  profile: string;
}

export interface WebAppArchitecture {
  tier: string;
  frontend: string;
  backend: string;
  state: string;
  resources: Array<{ type: string; id: string }>;
}

export interface WebAppLifecycle {
  created_at: string;
  expires_at: string | null;
  persistent: boolean;
  ttl_hours: number;
  status: string;
}

export interface WebAppCost {
  model: string;
  window_hours: number;
  estimates: Array<{ views: number; usd: number }>;
  idle_usd: number;
  note: string;
}

export interface WebAppTeardown {
  method: string;
  handle: string;
  reversible: boolean;
}

export interface WebAppMetadata {
  slug: string;
  origin_session: string;
  /** Local copy of the app tree — powers the gateway's local preview channel. */
  app_dir?: string;
  deploy_target: WebAppDeployTarget;
  architecture: WebAppArchitecture;
  lifecycle: WebAppLifecycle;
  cost: WebAppCost;
  teardown: WebAppTeardown;
}
