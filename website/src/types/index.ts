export interface StatusData {
  uptime: string
  start_time?: number
  sessions: number
  messages: number
  cron_jobs: number
  subagents: number
  lessons: number
  update_available?: boolean
  update_progress?: { step: string; detail: string } | null
  version?: string
  branch?: string
  commit?: string
  platform?: string
  yolo?: boolean
  no_crons?: boolean
  /** True when the gateway has a live Slack (Socket Mode) connection. */
  slack_connected?: boolean
  /** Governance enforcement health (AVP-23427). */
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

export interface CronJob {
  id: string; name: string; message: string
  enabled: boolean; schedule: string; last_status: string
  cron_expr?: string | null; every?: number | null; every_secs?: number | null
  at?: number | null; created_ts?: number | null
  agent?: string; model?: string; project_path?: string; channel?: string; approval_mode?: string; silent?: boolean
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
}

export interface Lesson {
  rule: string; category: string; ts: string
}

export interface Skill {
  key: string; name: string; description: string; always?: boolean; source?: string; package?: string
  /** Absolute path to SKILL.md on disk, when known. */
  path?: string
  /** Absolute path to the skill folder. */
  dir?: string
  /** Names of installed agents whose ``resources`` glob matches this skill's
   *  SKILL.md path.  Empty list means no agent loads it via kiro-cli's
   *  native ``skill://`` loader (it may still load via KiroCrew text-injection). */
  loaded_by_agents?: string[]
}

/** A single entry in a skill folder's tree listing. */
export interface SkillTreeEntry {
  path: string  // relative to the skill root, posix-style (e.g. "references/doc.md")
  type: 'file' | 'dir'
  size: number
}

export interface McpScopePresence {
  kirocrew: boolean
  kiroGlobal: boolean
  ccGlobal: boolean
}

export interface McpServer {
  name: string; command: string; args?: string[]
  status: string; error?: string; tools?: string[]
  source: string; enabled: boolean; disabledTools?: string[]
  presence?: McpScopePresence
}

export interface McpApplyChange {
  name: string
  kirocrew?: boolean
  kiroGlobal?: boolean
  ccGlobal?: boolean
  uninstall?: boolean
  toolOverrides?: Record<string, boolean>
}

export interface ChatSlot {
  key: string; title?: string; messages: number; running: boolean; stopping?: boolean; pending_approval?: boolean; created?: string; last_ts?: string; last_message?: string; agent?: string; model?: string; reasoning_effort?: string; mode?: string; surface?: string; workspace?: string; trust?: boolean; trust_reads?: boolean; folder_id?: string; pinned?: boolean; tags?: string[]; slack_linked?: boolean; slack_channel?: string; slack_thread_ts?: string; color_index?: number | null; memory_mode?: 'persistent' | 'incognito' | 'temporary'; clean_mode?: boolean; project?: string; forked_from?: string | null
  // Board fields
  has_options?: boolean; options?: string[]; pending_approval_info?: PendingApproval | null; last_activity_ts?: string; waiting_for_input?: boolean; prompt_preview?: string; subagents_running?: boolean
  // Soft-stop state machine
  stop_state?: 'idle' | 'soft_pending' | 'killing'
}

export interface ChatFolder {
  id: string; name: string; collapsed?: boolean; order: number; parent_id?: string; icon?: string; default_agent?: string; hidden?: boolean; history_count?: number
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
  status: 'pending' | 'running' | 'tool' | 'done' | 'error'
  streaming: string; lastTool: string
  startedAt: number; elapsed: number; error?: string
  approval_id?: string
  approving?: boolean
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
export type BlockType = 'markdown' | 'code' | 'diff' | 'mermaid' | 'widget'
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
}
export interface TaskRunnerStatus {
  running: boolean; available: boolean; runs: ProjectRun[]
}



export interface ArtifactPublication {
  /** Publishing-provider artifact UUID — stable across versions. */
  artifact_id: string
  /** Stable view URL: https://.../artifact/<id>. */
  view_url: string
  /** Publishing provider name ("artifactory" | "chorus" | …). */
  provider?: string
  /** Sync authority: 'mirror' (KiroCrew-authoritative) | 'live' (remote CRDT, e.g. Chorus). */
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
 *  returned by GET /api/artifacts/publish-providers?kind=<kind> (Mesh-2445). */
export interface PublishProviderDescriptor {
  name: string
  display_name: string
  capabilities: string[]
  kind_support: 'native' | 'converted' | 'degraded' | 'unsupported'
  capable: boolean
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

export interface Artifact {
  slug: string
  name: string
  kind: 'widget' | 'html' | 'markdown' | 'svg' | 'json' | 'text'
  source: 'chat' | 'cron' | 'subagent' | 'manual' | 'import'
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
   * file edits to source_path. Drives the "Snapshot Live" button
   * (Mesh-1654 round 6). */
  live_dirty?: boolean
  /** Publication state (Mesh-1880). Absent/null until the artifact has been
   * published to a sharing provider. */
  publication?: ArtifactPublication | null
  /** Fork provenance (Mesh-1880 P1). Absent/null if not a fork. */
  fork_metadata?: ForkMetadata | null
  /** Short, markdown-stripped, redacted content preview — only present when the
   * list was requested with ?snippet=1 (used by the command palette's
   * Artifacts provider). For a ?content=1 query it is match-centered. */
  snippet?: string
  /** Library folder this artifact is filed in ("" / absent = unfiled/root).
   * Opaque folder id — resolve names via the artifact-folders list (Mesh-2720). */
  folder_id?: string
}

/**
 * A folder in the local artifact library (Mesh-2720). Nested via `parent_id`
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
