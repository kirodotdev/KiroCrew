// Thin fetch wrapper for the Mochi backend (registered directly on the main
// gateway's aiohttp Application — see builtins/mochi/backend/routes.py — so
// the base path is /api/apps/mochi, same convention as issue-radar).
//
// Types mirror the Python side's wire format, which itself mirrors the
// original Mochi (TypeScript) shapes byte-for-byte: absent optional keys stay
// ABSENT (never null) — see the watchlist_file port's notes.

const API = '/api/apps/mochi'

// ── Watchlist ───────────────────────────────────────────────────────────────

// Preset kinds are just suggestions — the backend intentionally accepts any
// kind string (characterized in the port: unknown kinds persist unvalidated),
// so users can create their own categories. `string & {}` keeps literal
// autocomplete while admitting arbitrary strings.
// Watchlist types come from the VENDORED original (src/shared/watchlistTypes.ts)
// so there is ONE vocabulary across the seam. The port previously redeclared
// them here; the two were structurally similar but nominally distinct, so every
// bridge call that crossed the boundary failed to typecheck.
export type {
  HistoryEntry,
  WatchItem,
  WatchKind,
  WatchPriority,
  WatchStatus,
} from './src/shared/watchlistTypes'
import type {
  WatchItem,
  WatchKind,
  WatchPriority,
  WatchStatus,
} from './src/shared/watchlistTypes'

export const TERMINAL_STATUSES: readonly WatchStatus[] = [
  'done',
  'cancelled',
  'failed',
  'expired',
]

// Interval floor, mirrored from the Python port's watchlist_file constants.
export const MIN_CHECK_INTERVAL_MINS = 3

export interface AddWatchItemParams {
  label: string
  kind: WatchKind
  target: string
  triggerCondition?: string
  triggerAt?: string
  checkIntervalMins?: number
  notifyOnChange?: boolean
  autoComplete?: boolean
  priority?: WatchPriority
  notes?: string
  maxWatchDurationHours?: number
}

export interface UpdateWatchItemParams {
  id: string
  lastResult?: string
  nextCheckAfter?: string
  checkCount?: number
  failCount?: number
  status?: WatchStatus
  checkIntervalMins?: number
  priority?: WatchPriority
  notes?: string
}

export interface UpdateWatchlistResult {
  updated: boolean
  items: WatchItem[]
  warning?: string
}

// ── Stats / pinned / soul ───────────────────────────────────────────────────

export interface CompanionStats {
  firstLaunch: string
  streak: number
  lastActiveDate: string
  companionSeconds: number
  messages: { sent: number; received: number }
  walkSteps: number
  screenshots: number
  peeks: number
  drags: number
  thinkingSeconds: number
  latestActiveTime: string
  earliestActiveTime: string
  moods: Record<string, number>
  longestChat: number
  busiestDay: { date: string; messages: number }
  lastMemoryHour: number
  celebratedMilestones: string[]
}

export interface PinnedFileEntry {
  path: string
  label: string
  pinnedAt: number
  updatedAt?: number
}

export interface SoulInfo {
  soul: string
  petName: string
  isDefault: boolean
}

// ── Client ──────────────────────────────────────────────────────────────────

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    credentials: 'same-origin',
    ...init,
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`mochi api ${path}: ${res.status} ${body.slice(0, 200)}`)
  }
  return (await res.json()) as T
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}

export function getWatchlist(): Promise<{ items: WatchItem[] }> {
  return request('/watchlist')
}

export function updateWatchlist(params: {
  add?: AddWatchItemParams[]
  cancel?: string[]
  /** Hard delete by id. `cancel` only marks an item terminal. */
  remove?: string[]
  update?: UpdateWatchItemParams[]
}): Promise<UpdateWatchlistResult> {
  return post('/watchlist/update', params)
}

export function getStats(): Promise<CompanionStats> {
  return request('/stats')
}

export function getPinned(): Promise<{ pins: PinnedFileEntry[] }> {
  return request('/pinned')
}

export function unpinFile(path: string): Promise<{ ok: boolean }> {
  return post('/pinned/unpin', { path })
}

export function markPinnedSeen(path: string): Promise<{ ok: boolean }> {
  return post('/pinned/mark-seen', { path })
}

export function getSoul(): Promise<SoulInfo> {
  return request('/soul')
}

// ── Dashboard page: pet state / plan / activity ─────────────────────────────

/** Behaviour states the PetStateManager can report. `offline` is the cold start. */
export type PetState =
  | 'idle'
  | 'thinking'
  | 'working'
  | 'walking'
  | 'error'
  | 'offline'

export interface PetStateInfo {
  state: PetState
  mood: string
}

/** One entry of the planner's action queue. Keys mirror queue_file.QueueTask. */
export interface PlanTask {
  id?: string
  type?: string
  action?: unknown
  execute_after?: string
  done?: boolean
  requires_agent?: boolean
}

/**
 * The planner's queue. `narrative` and `mood` are planner metadata written by
 * the mochi-plan skill (see queue_file.apply_update), not queue mechanics.
 */
export interface MochiPlan {
  tasks?: PlanTask[]
  narrative?: string
  mood?: string
  planned_at?: string
  planned_until?: string
  needs_replan?: boolean
  note?: string
}

export interface ActivityEntry {
  ts: string
  type: string
  content: string
}

export function getPetState(): Promise<PetStateInfo> {
  return request('/pet-state')
}

export function getPlan(): Promise<MochiPlan> {
  return request('/plan')
}

export function getActivity(): Promise<{ entries: ActivityEntry[] }> {
  return request('/activity')
}

/**
 * Whether the Mochi app is enabled, without treating "disabled" as an error.
 *
 * The backend's `_require_enabled` guard answers 403 while disabled and 503
 * while the runtime has not come up yet, which is precisely the distinction the
 * dashboard's landing state needs — a disabled app is offerable (enable it), a
 * not-yet-started runtime is transient. Any other failure is reported as an
 * error so a genuine outage is not painted as "sleeping".
 */
export async function probeEnabled(): Promise<'enabled' | 'disabled' | 'starting'> {
  const res = await fetch(`${API}/pet-state`, { credentials: 'same-origin' })
  if (res.ok) return 'enabled'
  if (res.status === 403) return 'disabled'
  if (res.status === 503) return 'starting'
  throw new Error(`mochi api /pet-state: ${res.status}`)
}

/** Enable the app — reuses core's app registry route, not a Mochi-specific one. */
export async function enableMochi(): Promise<void> {
  const res = await fetch('/api/apps/mochi/enable', {
    method: 'POST',
    credentials: 'same-origin',
  })
  if (!res.ok) throw new Error(`enable mochi: ${res.status}`)
}

/**
 * The app's own version, read from core's manifest route.
 *
 * The original dashboard took this from the plan payload because its backend was
 * a separate versioned process. Here the version is the builtin's manifest
 * version, which core already serves — adding a Mochi route for it would be a
 * second source of truth.
 */
export async function getMochiVersion(): Promise<string> {
  const res = await fetch('/api/apps/mochi/manifest', { credentials: 'same-origin' })
  if (!res.ok) return ''
  const body = (await res.json()) as { version?: unknown; manifest?: { version?: unknown } }
  const v = body?.version ?? body?.manifest?.version
  return typeof v === 'string' ? v : ''
}



/** Behavior mode; default is `quiet` (see settings.py MODE_QUIET). */
export type MochiMode = 'quiet' | 'normal' | 'active'

/**
 * The complete Mochi settings shape — mirrors settings.py `_DEFAULTS` EXACTLY.
 * These are the ONLY keys the backend persists; it drops unknown keys and
 * raises on invalid values, so this type is the authoritative allow-list for
 * any settings write. Do not add fields here without a matching `_DEFAULTS`
 * key on the Python side.
 */
export interface MochiSettings {
  /**
   * Which instance's Mochi the pet window shows: `'self'` or an instance id
   * from `GET /api/instances`. Stored on the LOCAL instance because the pet is
   * a machine-wide resource, and stored opaquely so a temporarily-absent
   * instance survives a restart. (No UI owner at present — the instance
   * selector was retired with the hand-written dashboard widgets.)
   */
  petInstance: string
  /** Behavior mode; editable in the settings panel. Defaults to `quiet`. */
  mode: MochiMode
  /** Cat coat colorway id (cat pack only). Owned by ColorCustomizer. */
  catPreset: string | null
  /**
   * Whether Mochi may use the MCP servers the user installed in KiroCrew.
   * Defaults to `false` (access withheld). Editable in the settings panel.
   */
  allowMcpServers: boolean
  /**
   * Display name. Empty means "use the avatar's own name" (Kiro / Mochi), so a
   * rename follows the avatar until the user overrides it. Reaches the agent
   * prompt via soul_loader as well as the panel and pet title bars.
   */
  petName: string
  /**
   * Language for Mochi's own windows: `''` follows the browser locale, otherwise
   * an i18n bundle id. The pet overlay already read this key before it had a
   * writer — see settings.py LANGUAGES.
   */
  language: '' | 'en' | 'zh'
  /**
   * Suppress the completion notification for background work (planning, watch
   * checks). Results still reach the chat; this only silences the interruption.
   */
  silentSubagents: boolean
  /**
   * The pet's identity: a built-in pack id ('default-mochi' / 'kiro-ghost') or a
   * user pack in the appearance store. This ONE key drives the art, the persona,
   * and the default pet name. Owned by the Avatars window.
   */
  activeAppearance: string
  /**
   * User-editable global accelerators, in Electron accelerator syntax. `''` means
   * the user unbound that action. Mirrors settings.py SHORTCUT_ACTIONS; the store
   * REJECTS a malformed accelerator, so a write can 400.
   */
  shortcuts: { toggleWindow: string; hideAll: string; screenCapture: string }
  /**
   * Keys the ORIGINAL Mochi kept under `config.mochi` and its ported renderer
   * still edits. They live in the same flat store (settings.py
   * `_PASSTHROUGH_DEFAULTS`) and are reshaped into the original's nested config
   * tree by `src/mochiApi.ts`.
   *
   * `extraMcpServers` is the one that carries real behaviour: the original let
   * the user configure each MCP server individually (which agents may use it,
   * which tools auto-approve, which are disabled) — something the older
   * `allowMcpServers` boolean could not express. That boolean is now DERIVED on
   * write from whether any server is configured.
   */
  extraMcpServers: (string | McpServerEntry)[]
  /** Per-pack colour overrides, keyed by pack id. Owned by the Avatars window. */
  colorMaps: Record<string, unknown>
  /** User-created cat colorways. Owned by the Avatars window. */
  customPresets: unknown[]
  quietPeriodMins: number
  breakReminderMins: number
  restoreSessions: boolean
  sessionHistoryDays: number
  firstLaunchDone: boolean
  chatAlwaysOnTop: boolean
  activityLogMaxEntries: number
}

/** One configured MCP server, as the original's settings panel writes it. */
export interface McpServerEntry {
  name: string
  agents: ('chat' | 'bg')[]
  autoApprove: string[]
  disabledTools: string[]
}

export function getSettings(): Promise<MochiSettings> {
  return request('/settings')
}

/**
 * A settings patch. `shortcuts` is deliberately PARTIAL-within-partial: the store
 * merges per action, so rebinding one key must not require restating the other
 * (which would let a stale read clobber a concurrent change).
 */
export type MochiSettingsPatch = Partial<Omit<MochiSettings, 'shortcuts'>> & {
  shortcuts?: Partial<MochiSettings['shortcuts']>
}

export function updateSettings(patch: MochiSettingsPatch): Promise<MochiSettings> {
  return post('/settings', patch)
}
