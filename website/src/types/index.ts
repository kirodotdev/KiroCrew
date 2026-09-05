import type { ReviewFixMetadata } from './reviewFix'

export type {
  ReviewFixActionRequest,
  ReviewFixAuditEvent,
  ReviewFixCreateInput,
  ReviewFixFindingSnapshot,
  ReviewFixGit,
  ReviewFixGroup,
  ReviewFixGroupState,
  ReviewFixMetadata,
  ReviewFixModel,
  ReviewFixState,
  ReviewFixTarget,
  ReviewFixTargetMode,
  ReviewFixTaskResponse,
  ReviewFixValidation,
} from './reviewFix'

export interface StatusData {
  uptime: string
  start_time?: number
  sessions: number
  messages: number
  /**
   * `null` means UNKNOWN — the WS pusher's count refresh has not succeeded
   * yet (e.g. the lesson store is failing). StatCard renders null as a
   * loading skeleton; publishing 0 instead would assert an authoritative
   * false zero (issue #7204). HTTP/SSE paths always send numbers.
   */
  cron_jobs: number | null
  subagents: number
  /** See cron_jobs — null = unknown, rendered as a skeleton, never a fake 0. */
  lessons: number | null
  /**
   * Is a newer build available? `null`/absent means NO VERDICT — a check that
   * never ran, or one that failed. Only `true` may light an update affordance,
   * and only `false` alongside `update_check_status === 'succeeded'` may render
   * "up to date". Treating a missing verdict as `false` is the bug this pair
   * exists to prevent.
   */
  update_available?: boolean | null
  /**
   * Can the gateway apply an update in-process? Only a git checkout can — a wheel
   * install (the `cli.sh` managed venv) upgrades by re-running the installer, and
   * a desktop bundle is updated by its own updater, so `POST /api/update` would
   * 400/409 on both. Shipped with the availability flag so the UI can pick the
   * right affordance without first running a check.
   */
  update_can_apply?: boolean
  /**
   * How far the check itself got: `unchecked`, `checking`, `succeeded`, `failed`
   * or `deferred` (another surface owns this install's updates).
   */
  update_check_status?: 'unchecked' | 'checking' | 'succeeded' | 'failed' | 'deferred'
  /** Copyable upgrade command for an install that cannot apply in-process ("" when none). */
  update_command?: string
  /**
   * The candidate release's version string ("" until a check has found a newer
   * build). Carried on the hot-path subset so the proactive update popup can
   * key its per-version snooze/skip without calling the check endpoint; the
   * changelog text deliberately is not.
   */
  update_latest_version?: string
  /**
   * DISPLAY-ONLY fold of `update_latest_version` (clean base on the stable
   * channel). The popup's snooze/skip keys and every arm path keep reading
   * the raw field. Optional: an older gateway does not send it.
   */
  update_latest_version_display?: string
  /**
   * The release channel this INSTALL follows (the `channel` file `cli.sh` wrote).
   * "" when the layout has no channel at all — a git checkout tracks a remote, a
   * desktop bundle and a container are updated by something else — which is what
   * gates the gateway channel switcher. Distinct from `release_channel` below,
   * which says which lane the RUNNING BYTES were built on; the two legitimately
   * diverge between a channel switch and the new lane's build landing.
   */
  update_channel?: string
  /**
   * Is the running build ahead of everything `update_channel` publishes? True
   * means that lane has never shipped these bytes, so the install is not on it
   * and only re-running the installer moves it — the state a channel switch
   * leaves on an install whose bytes the gateway cannot replace.
   *
   * Backend-derived from the feed comparison, deliberately NOT from comparing
   * `update_channel` against `release_channel`: promotion re-points the soaked
   * candidate's bytes without re-stamping them, so a promoted stable build
   * reports `release_channel: 'insider'` while being a stable install with
   * nothing pending. False on every layout with no feed answer (git checkout,
   * desktop bundle, container) and on older gateways (absent reads as false).
   */
  update_channel_move_pending?: boolean
  update_managed_by?: string
  update_can_arm?: boolean
  /**
   * Commit distance from a git checkout's upstream, both directions. Diverged
   * (both > 0) reports `update_available: false` exactly like a current
   * checkout — the destructive apply paths must never be offered local
   * commits — so this pair is what lets the About badge tell the two apart
   * without waiting for a manual check. 0/0 on non-git layouts, before any
   * check, and on older gateways (absent reads as 0).
   */
  update_commits_ahead?: number
  update_commits_behind?: number
  update_last_checked_at?: number | null
  update_check_interval_secs?: number
  /**
   * Mandatory-update verdict: true when this install sits below either the
   * enterprise governance pin or the release feed's breaking-change floor.
   * The proactive update modal drops its snooze/skip affordances while true.
   */
  update_required?: boolean
  /** The floor that made the update mandatory (bare release, '' when none). */
  update_min_version?: string
  update_progress?: { step: string; detail: string } | null
  version?: string
  /**
   * The RUNNING build's version folded for display — the clean base on the
   * stable channel (`0.4.0` for bytes stamped `0.4.0rc14`), the raw version
   * on every other channel. DISPLAY-ONLY sibling of `version` above, which
   * stays raw because the SPA compares it across pushes to force a reload
   * over a gateway upgrade. Optional: an older gateway does not send it —
   * fall back to `version`.
   */
  version_display?: string
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
  /**
   * Short content hash of the SERVED frontend bundle's entry point. The SPA
   * compares it across status pushes and reloads when it moves — the reload
   * signal `version` cannot give for a same-version rebuild (a git checkout's
   * in-app update), and one that reaches every open tab. `""`/absent means no
   * built bundle / older gateway: unknown, never a change.
   */
  bundle_id?: string
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
  /**
   * Live health of every messaging channel, keyed by channel type (`slack`,
   * `wecom`, `telegram`, `discord`, `webex`, `teams`, `weixin`, `imessage`). The
   * gateway derives it by looping its own channel roster, so a channel added
   * there arrives here without a payload change — which is why this is a map and
   * not eight named fields.
   *
   * Optional because an older gateway sends no `channels` at all. Treat that as
   * "no answer" and fall back to `slack_connected`; reading an absent map as a
   * set of disconnected channels would invent an outage.
   *
   * `error` is the last connect failure, already capped at 120 chars by the
   * gateway, and `''` when there is none. So `{ connected: false, error: '' }` is
   * AMBIGUOUS by construction: it is what an unconfigured channel and a
   * configured one that never started both look like. Nothing in this payload
   * separates them — each channel's own config endpoint reports `configured`,
   * which is what Settings > Channels reads.
   */
  channels?: Record<string, { connected: boolean; error: string }>
  /** Governance enforcement health. */
  governance?: 'active' | 'degraded' | 'disabled' | 'unknown'
}

/**
 * GET /api/update/check — the update capability contract for this install
 * (`_update_info` in `dashboard/handlers/updates.py` plus the request-scoped
 * extras). Every field is optional so an older gateway that predates one still
 * type-checks; consumers treat absence as "unknown", never as a verdict.
 */
export interface UpdateCheckResult {
  supported?: boolean
  managed_by?: string
  mode?: string
  can_download?: boolean
  can_apply?: boolean
  requires_restart?: boolean
  channel?: string
  latest_version?: string
  /**
   * DISPLAY-ONLY sibling of `latest_version`, folded to the clean release
   * version on the stable channel (a promoted candidate keeps its insider/rc
   * stamp in the bytes, e.g. "0.4.0rc14" for the "0.4.0" release). Never pass
   * this to `InAppUpdateFlow`'s `version` prop, `/api/update/arm`, or a
   * snooze/skip key — those must use the raw `latest_version`, which is
   * compared byte-for-byte against the installed build's own never-folded
   * `__version__` during apply.
   */
  latest_version_display?: string
  /**
   * Copyable upgrade command ("" when none). Carried by the channel-switch
   * response (POST /api/update/channel, which answers with this same contract
   * re-run against the new lane); the check endpoint itself carries the
   * command inside `remediation` instead.
   */
  update_command?: string
  changes?: string
  check_status?: 'unchecked' | 'checking' | 'succeeded' | 'failed' | 'deferred'
  update_available?: boolean | null
  version_newer?: boolean
  /**
   * Commit distance from the tracked git upstream, both directions. A diverged
   * checkout (both counts > 0) reports `update_available: false` exactly like a
   * current one — the destructive apply path must never be offered its local
   * commits — so this pair is the only wire signal that "no update" means
   * "rebase or merge" rather than "up to date". The diverged condition is
   * derived at the render site (`commits_ahead > 0 && commits_behind > 0`), not
   * shipped as a redundant server boolean. Both 0 outside a successful
   * git-checkout check.
   */
  commits_ahead?: number
  commits_behind?: number
  error_code?: string | null
  unavailable_reason?: string | null
  remediation?: { kind?: string; message?: string; command?: string } | null
  current_version?: string
  auto_update?: boolean
  minimum_version_enforced?: string
  update_required?: boolean
  /** Legacy alias some older payloads carried; `latest_version` is authoritative. */
  version?: string
}

export interface SystemData {
  hostname: string; os: string; arch: string; cpu_count: number
  load_1m: number; load_5m: number; load_15m: number
  /**
   * Probe-derived metrics are OPTIONAL by construction. The server assembles
   * `/api/system` key-by-key under a per-probe `try/except: pass`, seeded from
   * cached static info — so a frame carrying `mem_total_gb` (static cache) with
   * no `mem_used_gb` (live probe failed or returned nothing) is an ordinary
   * outcome, not an error. Narrow through `utils/metrics.ts` before any
   * arithmetic or formatting; a bare `.toFixed()` on one of these is a crash.
   */
  cpu_pct?: number
  mem_total_gb?: number; mem_used_gb?: number; mem_free_gb?: number
  ip: string; net_rx_mb: number; net_tx_mb: number
  net_rx_kbs: number; net_tx_kbs: number
  disk_total_gb?: number; disk_free_gb?: number
  python: string; pid: number; cwd: string
  /** Live resident set size. `proc_mem_peak_mb` is the high-water mark since
   *  the gateway started, so it never falls; do not render it as live memory. */
  proc_mem_mb: number; proc_mem_peak_mb?: number; proc_cpu_pct: number
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

/**
 * One empty of the trash, running or recently finished.
 *
 * POST /api/system/session-storage/empty answers 202 with this; GET on the same
 * path returns the current one, and stops returning a finished one once it has gone
 * stale — so an outcome is never presented as current days later. The gateway keeps
 * a single slot, so a second empty is refused with 409 and this same shape rather
 * than queued.
 *
 * `total_bytes` comes from the staged manifests — the same figure the trash row
 * showed — so it is the denominator for `freed_bytes` and never a remeasurement.
 */
export interface SessionStorageEmptyJob {
  job_id: string
  running: boolean
  total_bytes: number
  freed_bytes: number
  /** Empty unless the delete was refused or stopped on an error. */
  error: string
  /** Reason codes for batches deliberately KEPT. A kept batch is a refusal: the
   *  user asked for it to be destroyed and it is still there, so an empty `error`
   *  with a non-empty `skipped` is not a success. */
  skipped: string[]
}

/* ── Session inventory (contract §1–§3) ── */

/** One session row in the inventory list. */
export interface SessionInventoryItem {
  uid: string
  title: string
  origin: string
  bytes: number
  mtime: number
  active: boolean
  /** A turn is in flight. Narrower than `active`: everything live is active,
   *  but an idle session that the product could still resume is not live. */
  live: boolean
  background: boolean
}

/** GET /api/system/session-storage/sessions */
export interface SessionInventoryList {
  total_bytes: number
  total_sessions: number
  reclaimable_bytes: number
  reclaim_blocked_reason: string
  /** Every conversation, plus only the LARGEST replay-only sessions — see `background`. */
  sessions: SessionInventoryItem[]
  /** The replay-only group as a whole. `listed` is how many of `sessions` it
   *  contributed, so the difference is what the list does not name. Never derive
   *  the group's size or total by filtering `sessions`: on a long-lived install
   *  the group holds six figures of rows and the list carries a capped sample. */
  background: { sessions: number; bytes: number; listed: number }
  /** What an age sweep would reclaim at each offered threshold, cumulative
   *  ("older than `days`") and already excluding anything in use — so an option
   *  can be labelled with real numbers before any dry run. */
  age_options: { days: number; sessions: number; bytes: number }[]
  trash: {
    bytes: number
    still_on_disk: boolean
    instant: boolean
    batches: SessionStorageBatch[]
  }
}

/** GET /api/system/session-storage/sessions/{uid} — lazy detail */
export interface SessionInventoryDetail {
  uid: string
  first_message: string
  turns: number
  images: number
  bytes: number
  mtime: number
}

/** One uid the server refused in POST .../trash */
export interface SessionTrashRefusal {
  uid: string
  /** `resumable` is the common one: idle, but the product could still resume it. */
  reason: 'in_use' | 'resumable' | 'too_fresh' | 'unknown'
}

/** POST /api/system/session-storage/trash response */
export interface SessionTrashResult {
  sessions: number
  bytes: number
  batch_id: string
  refused: SessionTrashRefusal[]
}

export interface CronJob {
  id: string; name: string; message: string
  enabled: boolean; schedule: string; last_status: string
  cron_expr?: string | null; every?: number | null; every_secs?: number | null
  at?: number | null; created_ts?: number | null
  agent?: string; model?: string; channel?: string; approval_mode?: string; silent?: boolean
  /** Crews a sequence job runs, in order. Takes PRECEDENCE over `agent` at run
   *  time, so any consumer attributing a job to a crew must read this first. */
  agent_sequence?: string[]
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
  /** Operator-granted vault secrets injected into a script/command job's env at
   * fire time: env-var name -> vault secret NAME (values never leave the vault).
   * Absent/null when the job holds no grant. */
  secret_env?: Record<string, string> | null
  /** Agent-requested grant awaiting the operator's approve/deny. Approving
   * re-verifies the request's code pin server-side, so a job whose script or
   * command changed after the request refuses with `code_changed`. */
  secret_env_pending?: Record<string, string> | null
  secret_env_pending_ts?: number | null
  folder_id?: string
  /** Chat session that owns this job — ownership decides chat-side reachability
   * (cron_list only lists a session its own jobs). Null for an ownerless job,
   * which is invisible to every chat session and manageable only from the
   * Schedule page or the CLI. */
  session_key?: string | null
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
  /** Optional status-enrichment fields supplied by newer runtimes. */
  accountLabel?: string
  connectedSince?: string
  /** True when the entry lives in KiroCrew's own mcp.json — the scope the
   *  Edit JSON action reads and writes (consent-disabled rows included). */
  kirocrewManaged?: boolean
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

/**
 * What ONE agent session's MCP servers reported while starting.
 *
 * Distinct from every other MCP payload in the dashboard: `/api/mcp/active`
 * reads an agent spec off disk and `/api/mcp/probe` records whether the gateway
 * itself can start a server. Both answer a question about the host. This is the
 * only one that answers "what did THIS session actually mount".
 *
 * Two properties callers must respect:
 * - A name absent from every bucket means *no report yet*, never *not mounted*:
 *   the backend's init drain is time bounded and a late frame still arrives.
 * - The buckets are a SUPERSET of `configured`, because the backend also starts
 *   the agent spec's own servers, not just the ones Kiro Crew injects.
 */
export interface McpSessionReport {
  /** Server names Kiro Crew put on the wire for this session. */
  configured: string[]
  /** Reported initialized. */
  ready: string[]
  /** Reported a startup failure. */
  failed: string[]
  /** Asked for authorization and has not reported since. */
  awaiting_auth: string[]
  /** Server name -> its redacted failure reason, when one was reported. */
  failures: Record<string, string>
}

export interface SessionLink {
  channel: string
  label: string
  target: string
  /**
   * `origin` — the conversation the session started on.
   * `out`    — dashboard replies are mirrored there (one-way, from `!link`).
   * `both`   — a session-RESUME binding from an in-channel `!sessions` pick:
   *            replies go there AND messages from there land in this session.
   *
   * Provenance only. It does NOT decide whether a row is operable — an `origin`
   * row carries a disconnect control like any other (see `paused`). One channel
   * can also carry TWO rows at once (born there AND mirrored there), so
   * `channel` alone does not identify a row; pair it with origin-ness.
   */
  direction: 'origin' | 'out' | 'both'
  live: boolean
  /**
   * The user disconnected this channel: turn output stops flowing there, but the
   * binding is retained so a reply in that conversation resumes the same session.
   * Distinct from `live`, which reports whether the transport *can* send at all —
   * a disconnected channel on a healthy transport is still `live`.
   *
   * Set on every row including `origin`, because the conversation a session was
   * born in can be disconnected too. `direction` records provenance only; it does
   * not decide whether a row has a control.
   *
   * Optional because a browser holding a `slots` payload cached from before this
   * field shipped has links without it; absent reads as connected.
   */
  paused?: boolean
}

export interface ConfiguredChannelTarget {
  channel_type: string
  target_id: string
  label: string
  available: boolean
  unavailable_reason: string
}

/**
 * What a connected crew offers a session bound to it for execution.
 *
 * Every field mirrors a gateway-wide read the chat shelf normally makes
 * same-origin against THIS machine (`/api/agents`, `/api/models`,
 * `/api/effort-levels`, `/api/workspaces`). A peer-bound session must offer the
 * PEER's options instead: a model or crew that exists only here would be accepted
 * by the picker and then fail on the first send, which is worse than not offering
 * it at all.
 */
export interface RemoteCrewCapabilities {
  instance_id: string
  /** The peer's gateway version, or "" when it could not be read. */
  version: string
  local_version: string
  /** The equality gate the backend enforces on every dispatch. False also covers
   *  "could not be read": an unknown version cannot be proven equal. */
  version_match: boolean
  agents: { name: string; description: string; scope: string; model: string }[]
  /** The agent the PEER falls back to when the session has picked none. "" when
   *  the roster read failed — never substitute this machine's default, which
   *  names a crew from a roster the peer does not share. */
  default_agent: string
  models: { model_name: string; display_name: string; description: string; context_window: number }[]
  effort_levels: string[]
  workspaces: { name: string; path: string }[]
  default_workspace: string
  /** Per-field failure codes for the reads that did not land, so one unreachable
   *  roster disables its own control rather than blanking the whole shelf. */
  unavailable: Record<string, string>
}

export interface ChatSlot {
  /** The agent that will actually answer, when it is NOT the requested `agent`;
   *  "" / absent means nothing to report. The backend stores `agent` verbatim
   *  (the user's intent) and reports the divergence here instead of rewriting it,
   *  and it reports "" rather than guessing whenever resolution is unsettled — so
   *  a consumer must treat absent as "no news", never as a mismatch. */
  effective_agent?: string
  /** The backend's verdict on whether the live session can run `model`:
   *  `true` it cannot (the spawn withheld the pin and the session is on the
   *  backend default), `false` it can, `null`/absent NOT KNOWN YET — no session
   *  has advertised a comparable list for this pin.
   *
   *  Consumers must fail open on the unknown state (`displayModel` does): it is
   *  the absence of an answer, never a denial. DISPLAY only — the pin is
   *  deliberately kept when withheld, so this must not drive a write. */
  model_withheld?: boolean | null
  /** Remote-execution binding. `executor` is "local" for an ordinary session and
   *  "remote" for one whose turns run on a connected crew; `instance_id` names
   *  that crew. The backend ships BOTH on every slot so "runs locally" is a
   *  positive value rather than an absent key — otherwise an older gateway's
   *  payload would read as local for a session that is not. The binding's third
   *  field, the peer's own slot key, stays server-side: it is meaningful only
   *  inside a request routed back through that instance. */
  executor?: 'local' | 'remote'
  instance_id?: string
  key: string; title?: string; messages: number; running: boolean; stopping?: boolean; pending_approval?: boolean; created?: string; last_ts?: string; last_turn_ts?: string; last_message?: string; agent?: string; model?: string; reasoning_effort?: string; mode?: string; surface?: string; workspace?: string; trust?: boolean; trust_reads?: boolean; folder_id?: string; pinned?: boolean; tags?: string[]; links?: SessionLink[]; slack_linked?: boolean; slack_channel?: string; slack_thread_ts?: string; color_index?: number | null; color_hex?: string | null; memory_mode?: 'persistent' | 'incognito' | 'temporary'; clean_mode?: boolean; project?: string; forked_from?: string | null; source_links?: { provider: SourceProviderId; number: number; url: string; label?: string; repo?: string; ci?: 'running' | 'passed' | 'failed' | null; state?: 'open' | 'draft' | 'merged' | 'closed'; mergeable?: string; mergeStateStatus?: string; kind?: 'change' | 'issue' }[]; source_links_total?: number
  /** Provenance bucket from the backend `SlotOrigin` ("user" | "app" | "cron"
   * | "system"; absent/"" for untagged background slots). The session-pulse
   * survey shows only on a "user" slot, so an imported Slack thread, a
   * task-runner slot, or an app/cron-minted session (which can share the
   * `chat-<n>-<ts>` key shape) never triggers it. */
  origin?: string
  /** Slot key of the session that asked for this one via the session-control
   * create verb; "" / absent for a person's own tab, a fork, a restore. Durable
   * (written at birth, rehydrated), so it is the one link from a crew member's
   * DM thread to the worker sessions it drives — the Crew Members drawer
   * filters the live slots on it. */
  created_by?: string
  /** Artifact companion binding: slug of the artifact this slot is a companion
   * chat for. Set at slot create and persisted in the history meta line, so the
   * binding survives a gateway restart and a History-page resume. */
  artifact?: string
  /** Metadata for kind="webapp" artifacts (deploy state, architecture, costs). */
  webapp_metadata?: WebAppMetadata
  // Board fields
  has_options?: boolean; options?: string[]; pending_approval_info?: PendingApproval | null; last_activity_ts?: string; waiting_for_input?: boolean; prompt_preview?: string; subagents_running?: boolean; orchestrating?: boolean
  /** An unanswered question card the turn is parked on, so the row would
   * otherwise read "Thinking…" with nothing able to advance it. Narrower than
   * `waiting_for_input` (true of every finished turn, and therefore no signal)
   * and separate from `pending_approval` (a tool gate). */
  needs_input?: boolean
  /** The transcript shows the last turn ending without a reply (trailing error
   * row or unanswered user row) — the state behind the composer's Resume
   * button. Always false while `running`. Lets the sidebar stop rendering a
   * goal-loop session as actively working when it is actually stalled. */
  interrupted?: boolean
  // Soft-stop state machine
  stop_state?: 'idle' | 'soft_pending' | 'killing'
  /** In-flight `wait` tool sleep, absent when nothing is sleeping. `deadline_ts`
   * is absolute seconds on the BACKEND clock (Date.now() / 1000 territory), so
   * the transcript can count down against it and survive a page reload;
   * `wait_id` is the handle the End-wait button must quote. */
  wait_state?: { wait_id: string; seconds: number; deadline_ts: number } | null
  /** Agent TODO list. Null/absent = the todo tool was never used in this slot. */
  todo?: TodoList | null
  /**
   * What this slot's agent session reported about its own MCP servers.
   *
   * Null/absent means this slot has no session that reported — render that as
   * absence of knowledge, NOT as "no servers". It is deliberately separate from
   * `/api/mcp/active` and `/api/mcp/probe`, which answer questions about the
   * HOST (what an agent spec declares, what the gateway can start) rather than
   * about this session.
   */
  mcp_report?: McpSessionReport | null
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

/** Which source system an extracted link, issue, or pull request belongs to.
 *
 *  The three built-ins are spelled out so they still autocomplete and so a
 *  `provider === 'github'` narrowing keeps working, but the type is OPEN: a
 *  downstream edition registers its own provider through
 *  `registerSourceProvider` (see `utils/pullRequestLinks`) and its id then flows
 *  through these payloads unchanged. `(string & {})` is the standard way to widen
 *  a literal union without collapsing it to `string` in editor completions.
 *
 *  Declared here rather than in `utils/pullRequestLinks` so the payload types
 *  never have to import from a util (which imports `ChatMessage` from this
 *  module); `PullRequestProvider` there is an alias of this. */
export type SourceProviderId = 'github' | 'gitlab' | 'jira' | (string & {})

/** A pull request / merge request the provider reports as linked to the issue. */
/** A linked change: a pull request (GitHub/GitLab) or a linked issue (Jira). */
export interface IssueLinkedChange {
  provider: SourceProviderId; url: string; number: number; title: string; state: string
  /** Jira link relationship label (e.g. "blocks", "is blocked by"). */
  relation?: string
  /** Full Jira issue key (e.g. "PROJ-123"). */
  issueKey?: string
}

/** Reaction tallies. Null on providers (or issues) that report none. */
export interface IssueReactions {
  total: number; plus1: number; minus1: number; laugh: number; hooray: number
  confused: number; heart: number; rocket: number; eyes: number
}

export interface IssueSource {
  provider: SourceProviderId
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

/** A single contributor to an app's source repository (GitHub only, v1).
 *  Names and avatar URLs are provider-controlled — render as text / <img>. */
export interface AppContributor {
  login: string
  /** Display name, falling back to the login when the profile has none. */
  name: string
  avatarUrl: string
  profileUrl: string
}

export interface PullRequestSource {
  provider: SourceProviderId; url: string; number: number; title: string
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
  /** Tag ids (from the tag vocabulary) copied onto every NEW chat filed into
   *  this folder. Absent = no tags, mirroring the optional `color`. */
  tags?: string[]
  /** Channel namespace when this folder was created by per-channel session filing (e.g. 'discord'). */
  channel?: string
}

export interface ChatTag {
  id: string; name: string; color: string; order: number; status?: boolean
}

export type TagColumnMode = 'any' | 'all' | 'none'

/** Derived board lane a session can be in. Mirrors `_VALID_STATE_KEYS` in
 *  `dashboard/chat_tags.py`, which refuses a column naming anything else. The
 *  rules that map a slot onto one of these live in `pages/chat/sessionLane.ts`. */
export type SessionLaneKey = 'needs_approval' | 'waiting' | 'working' | 'idle'

/** A board column filters either by tags or by the session's live runtime lane.
 *  `source` is absent on every column persisted before lanes existed, so a
 *  missing value MUST be read as `'tags'`. A `'state'` column carries a
 *  `state_key` and ignores `tag_ids`/`mode`/`include_untagged` entirely. */
export interface TagColumn {
  id: string; name: string; tag_ids: string[]; mode: TagColumnMode; order: number; include_untagged?: boolean
  source?: 'tags' | 'state'
  state_key?: SessionLaneKey | ''
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
  /** Model the live session actually resolved to serve, '' when unknown. Folded
   *  from the `model` field on the `subagent_spawn`/`subagent_done`/snapshot WS
   *  frames; shown beside the agent pill in the Subagents panel so a model-pinned
   *  run's real model is visible (#3582). */
  model?: string
  /** The model pin the caller REQUESTED for this subagent (the `requested_model`
   *  field on spawn/snapshot frames). Present only when the caller supplied a pin;
   *  compared against `model` via `isModelDowngrade` to render the amber chip on
   *  the live card (#5326). */
  requestedModel?: string
  /** The sub-agent's OWN session key, where it writes its per-turn context
   *  rows. Carried on the `subagent_spawn`/`subagent_done`/snapshot frames
   *  (backend `conversation_key or subagent:<id>`). The Session Breakdown tree
   *  uses it to fetch this node's own context-trace so each node shows the
   *  composition of ITS window, not the parent's. Absent for native cards. */
  childSession?: string
  status: 'pending' | 'running' | 'tool' | 'done' | 'error' | 'stopped'
  streaming: string; lastTool: string
  startedAt: number; elapsed: number; error?: string
  toolCount?: number      // observed tool calls (incl. auto-approved) — running-card progress
  stalled?: boolean       // reaper flagged this subagent as idle/stalled
  /** Seconds of no stream activity measured when the reaper raised `stalled`
   *  (the `idle_secs` the backend already sends with `subagent_stalled`).
   *  Distinct from `elapsed` (total runtime): only the idle span justifies the
   *  warning, so the card shows this rather than making the user infer it. */
  idleSecs?: number
  /** Client clock when that stall frame arrived. The backend emits `idle_secs`
   *  ONCE, on the not-stalled→stalled transition, so this is what lets the row
   *  advance the figure instead of freezing it beside a live elapsed counter —
   *  while `stalled` holds there is by definition no activity to reset it. */
  stalledAt?: number
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
  execution_started_at?: number // when execution began (after approval); survives remount
  auto?: boolean        // auto-approved tool call
  approval_id?: string  // pending approval ID
  approval_type?: string // 'chat' or 'spawn'
  tool_call_id?: string  // for matching tool results
  rejected?: boolean     // true when approval was rejected
  kind?: string          // ACP tool kind; execute is the legacy shell signal
  is_shell?: boolean     // shell tools can expose an indeterminate live status
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
  /** Present only for Code Review Sage tasks; generic runs leave it absent. */
  review_fix?: ReviewFixMetadata | null
  /** Whether this run uses the dedicated review-fix executor. */
  execution_mode?: string
  commit_policy?: string
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
  kind: 'widget' | 'html' | 'markdown' | 'svg' | 'json' | 'text' | 'webapp' | 'image'
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
  /** Metadata for kind="image" artifacts. The bytes themselves are never inlined
   * here — they are streamed from `/api/artifacts/<slug>/asset` with the
   * server setting Content-Type. Every field is optional because older payloads
   * and minimal saves may omit it, so every consumer must degrade gracefully:
   * `alt` gives the accessible description, `width`/`height` let the UI reserve
   * the correct aspect ratio before the image loads, and the rest are
   * informational (shown in details, used to name a download). */
  image?: {
    mime: string
    ext: string
    size_bytes?: number
    width?: number
    height?: number
    sha256?: string
    original_filename?: string
    alt?: string
  }
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

/** One row of `GET /api/workflows/runs` — the compact run view (no events).
 *
 *  This is the AUTHORITATIVE status of a dynamic-workflow run: the live
 *  `workflow_run_event` WS stream is one-shot, so a client that was closed,
 *  asleep, or disconnected when a run ended never sees its terminal frame.
 *  Field names are the backend's (`RunHandle.snapshot`), snake_case on the wire.
 */
export interface WorkflowRunSummary {
  run_id: string;
  name?: string;
  /** `running` | `finished` | `failed` | `cancelled`. Typed loosely on purpose —
   *  an unrecognised value is treated as "no evidence" rather than coerced. */
  status?: string;
  error?: string | null;
  /** Originating chat session, `""` for a UI-launched run that belongs to no chat. */
  session_key?: string;
  source_format?: 'python' | 'task-plan';
  driver?: 'workflow' | 'taskrunner' | string;
  task_id?: string;
  capabilities?: string[];
  workflow_id?: string;
  workflow_slug?: string;
  workflow_revision?: number;
  /** Title of the most recent `phase_started` event. */
  phase?: string;
  /** Most recent narrator `log` message. */
  last_log?: string;
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
