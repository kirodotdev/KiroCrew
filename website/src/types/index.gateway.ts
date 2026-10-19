// Shared types for this gateway and the crews it connects to: the status push
// (`StatusData`), the update check, host metrics, session storage and its
// inventory, and each connected crew's version, capabilities, approval state
// and remote update. Consumers import them from `types/index.ts`.

export interface StatusData {
  uptime: string
  start_time?: number
  sessions: number
  messages: number
  /**
   * `null` means UNKNOWN — the shared count cache has not refreshed
   * successfully yet (e.g. the lesson store is failing). StatCard renders null
   * as a loading skeleton; publishing 0 instead would assert an authoritative
   * false zero (issue #7204). All three transports (WS push, /api/status, SSE)
   * read the same cache and may send null.
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
   * What an available update leads to on this install: `install` (with the
   * auto-update switch on), `notify` (the switch cannot install here),
   * `mandatory` (a policy floor installs it regardless), or `unknown` before
   * the gateway first derives it. The update loop acts on the same answer.
   */
  update_auto_effect?: 'install' | 'notify' | 'mandatory' | 'unknown'
  /**
   * Whether the desktop app bundles and launches this gateway, from the baked
   * packaging stamp. Independent of `update_managed_by`, which reads `command`
   * on that same bundle when a policy `updates` provider is configured. There,
   * `update_auto_effect` reports the PROVIDER's answer, since the provider is
   * what this gateway's loop runs; without one it reports `notify`, the app's
   * own updater owning the update.
   */
  update_bundled_by_app?: boolean
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
  /** Auto-approve modes forbidden by the `approval_modes` policy scope. Today the
   * scope governs `yolo` only, so this is `['yolo']` or empty; `normal`, `trust`
   * and `trust_reads` are non-deniable and never appear. The approval-mode picker
   * HIDES each named mode, except one still selected when the policy lands — that
   * row stays visible and disabled, so the button label always has a matching row.
   * Absent/empty means every mode is selectable. List-driven so widening the
   * scope's vocabulary needs no type change. */
  disabled_approval_modes?: string[]
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
   * which is what Settings > Messaging Channels reads.
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
  /** This install can use the nonce-backed host approval flow. */
  can_arm?: boolean
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
  /** Whether config.local.json sets `auto_update`, so the switch cannot change it. */
  overlay_override?: boolean
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
/** `GET /api/instances/{id}/approval-state`: one field of the peer's status. */
export interface RemoteCrewApprovalState {
  /** Whether the peer's machine-wide YOLO is on; null when its status could not be read. */
  yolo: boolean | null
}

export interface RemoteCrewCapabilities {
  instance_id: string
  /** The peer's gateway version, or "" when it could not be read. */
  version: string
  local_version: string
  /** The equality gate the backend enforces on every dispatch. False also covers
   *  "could not be read": an unknown version cannot be proven equal. */
  version_match: boolean
  /** The peer runs a strictly older build than this machine (exact version,
   *  so a patch skew counts). Absent from a gateway that predates the field. */
  behind?: boolean
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

/** One CONNECTED crew's version beside this machine's (`GET /api/instances/versions`). */
export interface InstanceVersionRow {
  id: string
  name: string
  /** The peer's gateway version, or "" when it did not answer. */
  version: string
  /** Strictly older than `local_version`; never true for an unknown version. */
  behind: boolean
  /** This dashboard can run the crew's updater: false for a chained crew
   *  (no shell from here) and a fargate or headless one (no gateway). */
  updatable: boolean
  /** Why the crew's version could not be read (the carrier's code), or "" when
   *  it answered. A crew with an error is never `behind`: its version is unknown. */
  error?: string
}

export interface InstanceVersions {
  local_version: string
  instances: InstanceVersionRow[]
}

/** The latest "Update remote" job for one crew (`GET /api/instances/{id}/update`). */
export interface RemoteUpdateJob {
  /** `succeeded`: the crew came back on a different version. `unchanged`: both
   *  commands exited 0 but it came back on the same version (its updater
   *  installed nothing). `unverified`: it did not answer with a version in time. */
  state: 'running' | 'succeeded' | 'unchanged' | 'unverified' | 'failed'
  /** Where the job is, or where it failed: the remote `kirocrew update`, then
   *  `kirocrew restart`, then reading the crew's version back. */
  phase: 'updating' | 'restarting' | 'verifying' | 'done'
  started_at: number
  finished_at: number | null
  elapsed_secs: number
  /** The remote's redacted error tail on a failure, else "". */
  error: string
  /** The crew's version before the update and after its restart; "" when it
   *  did not answer. */
  version_before: string
  version_after: string
}
