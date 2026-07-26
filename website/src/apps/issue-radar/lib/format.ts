// Pure, side-effect-free helpers + localStorage accessors + constants for
// Issue Radar. No React, no component imports — safe to pull into any module.
import { Clock, Hash, Sparkles, type LucideIcon } from 'lucide-react'
import type { ActiveRepo, DashboardTab, MainView, PrSortKey, PrStateFilter, SettingsTarget, SortDir, SortKey, StateFilter } from './types'

export const ACTIVE_KEY = 'kc:issue-radar:active-repo'
export const LIST_WIDTH_KEY = 'kc:issue-radar:list-width'
export const DEFAULT_LIST_WIDTH = 320
export const MIN_LIST_WIDTH = 240
export const MAX_LIST_WIDTH = 600

export const APP_VERSION = '0.1.0'

/** Coerce an API/cache value to an array. A non-array — an unexpected response
 * shape, a 200 that carried an error object, or a stale backend/cache still
 * serving an older contract — becomes `[]` instead of throwing
 * "… .map is not a function" / "… is not iterable" when the value is later
 * mapped / spread / `for…of`-ed. Without this, one bad response blanks the
 * whole view behind the route error boundary. `?? []` alone is NOT enough: it
 * only replaces null/undefined, not a truthy non-array (e.g. `{}`). The shared
 * provider (context.tsx) guards its derivations the same way; views that run
 * their OWN queries must too. */
export function asArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : []
}

/** How often an OPEN detail pane re-reads its item from GitHub. A detail pane is
 * a thing you leave on screen while work happens elsewhere (a review lands, CI
 * flips, someone replies), so it polls rather than going stale silently.
 *
 * 30s is chosen for watching CI: a check flipping red is the thing you are
 * waiting on, and each poll costs a handful of `gh` calls for ONE item (not the
 * whole list), so the traffic stays proportionate. */
export const DETAIL_POLL_MS = 30_000

/** Poll interval for a MERGED or CLOSED item. Its expensive parts — the diff
 * shape, the check run, the commit list — are frozen; only late commentary can
 * still arrive. Polling those at the open-item rate spends the same 5-7 `gh`
 * calls every 30s to observe state that cannot change, so closed items back off
 * by an order of magnitude instead of being switched off entirely. */
export const CLOSED_DETAIL_POLL_MS = 300_000

/** The poll interval an item deserves, given whether it is still open. */
export function detailPollMs(open: boolean): number {
  return open ? DETAIL_POLL_MS : CLOSED_DETAIL_POLL_MS
}

/** Compact "just now / 5m ago / 3h ago / 2d ago" from an epoch-ms timestamp.
 * Used for the issue-list "Updated …" footer; returns '' for a falsy input
 * (e.g. before the first fetch). */
export function relativeTime(ms: number): string {
  if (!ms) return ''
  const secs = Math.max(0, Math.floor((Date.now() - ms) / 1000))
  if (secs < 45) return 'just now'
  const mins = Math.floor(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}

/** Timeline-friendly label: within the last 24h it reads as a compact elapsed
 * time ("just now / 12m ago / 3h ago"); anything older falls back to the
 * calendar-based relativeDate ("Yesterday / 5 days ago / 2 months ago").
 * Future timestamps (clock skew) defer to relativeDate. */
export function relativeTimeOrDate(iso: string): string {
  const then = new Date(iso)
  if (isNaN(then.getTime())) return ''
  const secs = Math.floor((Date.now() - then.getTime()) / 1000)
  if (secs >= 0 && secs < 86400) {
    if (secs < 45) return 'just now'
    const mins = Math.floor(secs / 60)
    if (mins < 60) return `${mins}m ago`
    return `${Math.floor(mins / 60)}h ago`
  }
  return relativeDate(iso)
}

/** Human "Today / Yesterday / N days ago" from an ISO timestamp. */
export function relativeDate(iso: string): string {
  const then = new Date(iso)
  if (isNaN(then.getTime())) return ''
  const now = new Date()
  const d0 = new Date(then.getFullYear(), then.getMonth(), then.getDate())
  const n0 = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const days = Math.round((n0.getTime() - d0.getTime()) / 86400000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 30) return `${days} days ago`
  if (days < 365) { const m = Math.floor(days / 30); return `${m} month${m > 1 ? 's' : ''} ago` }
  const y = Math.floor(days / 365)
  return `${y} year${y > 1 ? 's' : ''} ago`
}

export function loadListWidth(): number {
  const raw = Number(localStorage.getItem(LIST_WIDTH_KEY))
  if (raw >= MIN_LIST_WIDTH && raw <= MAX_LIST_WIDTH) return raw
  return DEFAULT_LIST_WIDTH
}

export function loadActiveRepo(): ActiveRepo | null {
  try {
    const raw = localStorage.getItem(ACTIVE_KEY)
    if (!raw) return null
    const p = JSON.parse(raw)
    if (p && typeof p.owner === 'string' && typeof p.repo === 'string') return p
  } catch {
    /* corrupted value — ignore */
  }
  return null
}

export function saveActiveRepo(repo: ActiveRepo) {
  localStorage.setItem(ACTIVE_KEY, JSON.stringify(repo))
}

/** Pick a black/white foreground that stays legible on a GitHub label colour
 * (6-hex, no leading '#'). Uses the standard sRGB luminance threshold. */
export function readableText(hex: string): string {
  const h = (hex || '').replace('#', '')
  if (h.length !== 6) return '#000'
  const r = parseInt(h.slice(0, 2), 16)
  const g = parseInt(h.slice(2, 4), 16)
  const b = parseInt(h.slice(4, 6), 16)
  const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
  return luminance > 0.6 ? '#000' : '#fff'
}

/** A translucent tint of a GitHub label colour, for the unselected (light
 * filled) label-row state. */
export function hexToRgba(hex: string, alpha: number): string {
  const h = (hex || '').replace('#', '')
  if (h.length !== 6) return `rgba(136,136,136,${alpha})`
  const r = parseInt(h.slice(0, 2), 16)
  const g = parseInt(h.slice(2, 4), 16)
  const b = parseInt(h.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/** Sort options rendered in the Filters section. 'ranking' is AI-ordered and
 * not implemented yet (flagged `soon`). */
export const SORT_FIELDS: { key: SortKey; label: string; icon: LucideIcon; soon?: boolean }[] = [
  { key: 'ranking', label: 'Ranking', icon: Sparkles, soon: true },
  { key: 'number', label: 'Number', icon: Hash },
  { key: 'updated', label: 'Last update', icon: Clock },
]

/** Sort options for the pull-request list — same fields as issues minus the
 * AI ``ranking`` (an issue-only concept). */
export const PR_SORT_FIELDS: { key: PrSortKey; label: string; icon: LucideIcon }[] = [
  { key: 'number', label: 'Number', icon: Hash },
  { key: 'updated', label: 'Last update', icon: Clock },
]

// ── Persisted UI state ────────────────────────────────────────────────────
// The whole app view (which dashboard / issues / settings page is showing, the
// selected issue, and the active filters + sort) is persisted here so leaving
// Issue Radar for another KiroCrew page and coming back restores exactly where
// you were. Mirrors loadActiveRepo above (the active repo is persisted on its
// own key); together they fully restore the app on return.
export const UI_STATE_KEY = 'kc:issue-radar:ui-state'

export interface PersistedUiState {
  mainView: MainView
  dashboardTab: DashboardTab
  settingsTarget: SettingsTarget
  selectedIssue: number | null
  query: string
  selectedLabels: string[]
  requestedByMe: boolean
  assignedToMe: boolean
  createdByMember: boolean
  stateFilter: StateFilter
  sortKey: SortKey
  sortDir: SortDir
  // ── pull-request view ──
  selectedPull: number | null
  prQuery: string
  prSelectedLabels: string[]
  prAuthoredByMe: boolean
  prAssignedToMe: boolean
  prReviewRequestedByMe: boolean
  prDraftOnly: boolean
  prCreatedByMember: boolean
  prStateFilter: PrStateFilter
  prSortKey: PrSortKey
  prSortDir: SortDir
}

/** Load the persisted UI state. Partial by design — any missing field falls
 * back to its default at the call site. Returns {} on first run / corruption. */
export function loadUiState(): Partial<PersistedUiState> {
  try {
    const raw = localStorage.getItem(UI_STATE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

export function saveUiState(state: PersistedUiState) {
  try {
    localStorage.setItem(UI_STATE_KEY, JSON.stringify(state))
  } catch {
    /* quota exceeded / private mode — persistence is best-effort */
  }
}
