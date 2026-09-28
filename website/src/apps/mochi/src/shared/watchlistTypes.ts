/**
 * Mochi — Watch List type definitions
 *
 * Shared types for mochi-watchlist.json (active) and mochi-watchlist-archive.json.
 * Used by Electron main process (WatchlistService, guard, reminderTimer),
 * MCP tools (get_watchlist, update_watchlist), and agent skills.
 */

// ── Watch Item ─────────────────────────────────────────────────────────────

export type WatchKind = 'url' | 'reminder' | 'meeting' | 'custom' | 'slack-channel' | 'slack-topic'

export type WatchStatus = 'watching' | 'triggered' | 'done' | 'cancelled' | 'failed' | 'expired'

export const TERMINAL_STATUSES: readonly WatchStatus[] = ['done', 'cancelled', 'failed', 'expired']

export type WatchPriority = 'high' | 'normal' | 'low'

export type CompletionReason = 'completed' | 'cancelled' | 'failed' | 'expired'

export interface HistoryEntry {
  checkedAt: string    // ISO timestamp
  result: string       // check result summary
  changed: boolean     // whether status changed from previous check
}

export interface WatchItem {
  id: string                      // unique ID, e.g. "w-1713000000000"
  label: string                   // user-readable label, e.g. "Product price drop"
  kind: WatchKind
  target: string                  // check target: a fetchable URL or a description
  status: WatchStatus
  priority: WatchPriority
  notes?: string                  // user notes
  lastChecked?: string            // ISO timestamp
  lastResult?: string             // latest check summary (for quick agent reads)
  lastChangedAt?: string          // ISO timestamp — auto-set when historyEntry.changed is true
  lastNotifiedAt?: string         // ISO timestamp — set when perform_pet_action notify includes this item's ID
  triggerCondition?: string       // e.g. "status changes to SHIPPED"
  triggerAt?: string              // ISO timestamp (reminder type)
  createdAt: string               // ISO timestamp
  completedAt?: string            // auto-set when status → terminal
  completionReason?: CompletionReason
  previousId?: string             // links to prior watch cycle (set on re-watch)
  nextCheckAfter: string          // ISO timestamp, auto-computed on historyEntry
  checkCount: number
  failCount: number               // consecutive failures
  maxFailCount: number            // default by kind: url/slack=10, custom=5, reminder/meeting=999
  maxWatchDurationHours: number   // default 168 (7 days)
  checkIntervalMins: number       // default 10, min 3, max 120
  notifyOnChange: boolean         // default true
  autoComplete: boolean           // default true
  source: 'chat' | 'heartbeat' | 'api'
  history: HistoryEntry[]         // compressed retention: 24h full, 24h-7d changed-only, 500 cap
}

// ── Watch List (active file) ───────────────────────────────────────────────

export interface WatchList {
  version: 1
  items: WatchItem[]
}

// ── Archive ────────────────────────────────────────────────────────────────

export interface ArchivedWatchItem extends WatchItem {
  archivedAt: string  // ISO timestamp
}

export interface WatchListArchive {
  version: 1
  items: ArchivedWatchItem[]
}

// ── Defaults by kind ───────────────────────────────────────────────────────

export const DEFAULT_MAX_FAIL_COUNT: Record<WatchKind, number> = {
  url: 10,
  reminder: 999,
  meeting: 999,
  custom: 5,
  'slack-channel': 10,
  'slack-topic': 10,
}

export const DEFAULT_CHECK_INTERVAL_MINS = 10
export const MIN_CHECK_INTERVAL_MINS = 3
export const MAX_CHECK_INTERVAL_MINS = Infinity  // no upper limit — UI uses unit selector
export const DEFAULT_MAX_WATCH_DURATION_HOURS = 168  // 7 days
export const MAX_ACTIVE_ITEMS = 50

// ── MCP tool parameter interfaces ──────────────────────────────────────────

export interface GetWatchlistParams {
  status?: WatchStatus
  include_done?: boolean
  include_history?: boolean
  search_archive?: string
  archive_since?: string
  archive_limit?: number
  /** Only return items with history entries after this ISO timestamp. Used for daily briefing. */
  since?: string
}

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
  lastChecked?: string
  nextCheckAfter?: string
  checkCount?: number
  failCount?: number
  status?: WatchStatus
  checkIntervalMins?: number
  priority?: WatchPriority
  notes?: string
  historyEntry?: { result: string; changed: boolean }
  /** Mark this item as notified — sets lastNotifiedAt atomically with other updates.
   *  Use this in the same update_watchlist call as historyEntry to avoid write races. */
  notified?: boolean
}

export interface UpdateWatchlistParams {
  add?: AddWatchItemParams[]
  cancel?: string[]
  update?: UpdateWatchItemParams[]
}

export interface UpdateWatchlistResult {
  updated: boolean
  items: WatchItem[]
  warning?: string
}
