// Pure, side-effect-free helpers + localStorage accessors + constants for
// Issue Radar. No React, no component imports — safe to pull into any module.
import { Clock, Hash, type LucideIcon } from 'lucide-react'
import { fmtRelative, toDate } from '../../../i18n/format'
import { loadColumnCollapsed, loadColumnWidth } from '../../../lib/columnWidth'
import { DASHBOARD_TABS, SORT_KEYS } from './types'
import type { ActiveRepo, DashboardTab, MainView, PrSortKey, PrStateFilter, SettingsTarget, SortDir, SortKey, StateFilter } from './types'

export const ACTIVE_KEY = 'kc:issue-radar:active-repo'
export const LIST_WIDTH_KEY = 'kc:issue-radar:list-width'
export const DEFAULT_LIST_WIDTH = 320
export const MIN_LIST_WIDTH = 240
export const MAX_LIST_WIDTH = 600

export const RAIL_WIDTH_KEY = 'kc:issue-radar:rail-width'
export const RAIL_COLLAPSED_KEY = 'kc:issue-radar:rail-collapsed'
/** Matches the rail's original fixed `w-72`, so an existing user sees no jump. */
export const DEFAULT_RAIL_WIDTH = 288
export const MIN_RAIL_WIDTH = 220
export const MAX_RAIL_WIDTH = 460
/** Width of the collapsed rail: a vertical rounded-rect strip showing only the
 * repo logo and the full owner/repo turned on its side. Dragging the rail well
 * past its minimum snaps to this instead of stopping at a stubborn wall. */
export const COLLAPSED_RAIL_WIDTH = 48

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

/** Poll interval for the issue / pull-request LISTS.
 *
 * Deliberately 6x the detail interval. A list poll is not one item's worth of
 * traffic: the open-issue fetch is fully paginated, so its cost scales with the
 * repo (a 2,600-issue repo is 27 `gh api` pages plus a multi-MB cache rewrite
 * per poll). At 60s that stays comfortably inside GitHub's 5,000/hr
 * authenticated budget on a large repo; at 10s the same repo would need ~9,700
 * requests an hour and blow through it.
 *
 * 60s also matches the backend new-issue watcher (``watch.POLL_INTERVAL_SEC``),
 * so a "new issue" bell notification and the list row it refers to land in the
 * same window instead of the notification arriving a refresh ahead of the list.
 */
export const LIST_POLL_MS = 60_000

/** Compact "now / 5m ago / 3h ago / 2d ago" from an epoch-ms timestamp.
 * Used for the issue-list "Updated …" footer; returns '' for a falsy input
 * (e.g. before the first fetch).
 *
 * Formatting is delegated to the locale-aware seam (`src/i18n/format.ts`): the
 * previous ladder of template literals rendered English in every language, and
 * carried its own `month`/`months` plural morphology, which is unexpressible
 * outside English. */
export function relativeTime(ms: number): string {
  if (!ms) return ''
  return fmtRelative(ms)
}

/** Timeline-friendly label: within the last 24h it reads as a compact elapsed
 * time ("now / 12m ago / 3h ago"); anything older falls back to the
 * calendar-based relativeDate ("yesterday / 5 days ago / 2 months ago").
 * Future timestamps (clock skew) defer to relativeDate. */
export function relativeTimeOrDate(iso: string): string {
  const then = toDate(iso)
  if (!then) return ''
  const secs = Math.floor((Date.now() - then.getTime()) / 1000)
  // Inside a day, show elapsed time compactly; the calendar wording below is
  // only meaningful once a date boundary has been crossed.
  if (secs >= 0 && secs < 86400) return fmtRelative(then)
  return relativeDate(iso)
}

/** Human "today / yesterday / N days ago" from an ISO timestamp.
 *
 * Counts whole CALENDAR days rather than elapsed seconds — 23:59 to 00:01 is
 * "yesterday", not "now" — then lets CLDR word the result. `numeric: 'auto'`
 * inside `fmtRelative` is what produces "yesterday"/"昨天"/"gestern" instead of
 * a mechanical "1 day ago", and it removes the hand-rolled English plural
 * suffixes this function used to carry for months and years.
 *
 * The `style: 'long'` override is deliberate: this label sits in a timeline
 * where "5 days ago" reads better than the compact "5d ago". */
export function relativeDate(iso: string): string {
  const then = toDate(iso)
  if (!then) return ''
  const now = new Date()
  const d0 = new Date(then.getFullYear(), then.getMonth(), then.getDate())
  const n0 = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const days = Math.round((n0.getTime() - d0.getTime()) / 86400000)
  // Re-anchor onto whole days so the relative formatter picks the day/month/year
  // unit from a calendar difference rather than from a partial-day remainder.
  const anchored = new Date(n0.getTime() - days * 86400000)
  // `unit: 'day'` is required: this function has already reduced its input to
  // whole calendar days, and with an auto-picked unit a zero delta would mean
  // "under one second" and render "now" for something that happened earlier
  // today. Pinning the day unit renders "today" / "今天".
  return fmtRelative(anchored, { style: 'long', now: n0.getTime(), unit: 'day' })
}


export function loadListWidth(): number {
  return loadColumnWidth(LIST_WIDTH_KEY, MIN_LIST_WIDTH, MAX_LIST_WIDTH, DEFAULT_LIST_WIDTH)
}

export function loadRailWidth(): number {
  return loadColumnWidth(RAIL_WIDTH_KEY, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, DEFAULT_RAIL_WIDTH)
}

/** Collapsed state is stored apart from the width so collapsing and re-expanding
 * the rail returns it to the width the user had chosen, not the default. */
export function loadRailCollapsed(): boolean {
  return loadColumnCollapsed(RAIL_COLLAPSED_KEY)
}

export function loadActiveRepo(): ActiveRepo | null {
  try {
    const raw = localStorage.getItem(ACTIVE_KEY)
    if (!raw) return null
    const p = JSON.parse(raw)
    // Only owner/repo are required: a value persisted before GitLab support has
    // no provider/host, and rejecting it would silently drop the user's repo on
    // upgrade. The absent fields mean public GitHub, which is what it was.
    if (p && typeof p.owner === 'string' && typeof p.repo === 'string') {
      return {
        owner: p.owner,
        repo: p.repo,
        ...(typeof p.provider === 'string' ? { provider: p.provider } : {}),
        ...(typeof p.host === 'string' ? { host: p.host } : {}),
      }
    }
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

/** Sort options rendered in the Filters section. */
export const SORT_FIELDS: { key: SortKey; label: string; icon: LucideIcon }[] = [
  { key: 'number', label: 'Number', icon: Hash },
  { key: 'updated', label: 'Last update', icon: Clock },
]

/** Sort options for the pull-request list — same fields as the issue list. */
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

/** Coerce a persisted sort key back into a currently-supported one. A key that
 * was removed from the app since it was written (e.g. the retired AI 'ranking'
 * order) must not survive a reload, or the list would render unsorted with no
 * matching option highlighted in the rail. */
export function coerceSortKey(value: unknown): SortKey {
  return (SORT_KEYS as readonly string[]).includes(value as string) ? (value as SortKey) : 'number'
}

/** Same idea for the dashboard tab: a tab whose view no longer exists falls
 * back to Overview instead of rendering an empty main area. */
export function coerceDashboardTab(value: unknown): DashboardTab {
  return (DASHBOARD_TABS as readonly string[]).includes(value as string) ? (value as DashboardTab) : 'overview'
}

export function saveUiState(state: PersistedUiState) {
  try {
    localStorage.setItem(UI_STATE_KEY, JSON.stringify(state))
  } catch {
    /* quota exceeded / private mode — persistence is best-effort */
  }
}

/** Merge a single field into the persisted UI state, leaving the rest intact.
 *
 * Needed for the connect flow: after connecting a repo the user should land on
 * the issue list, but on FIRST RUN the provider isn't mounted yet (the welcome
 * carousel renders in its place), so there is no live `setMainView` to call —
 * the provider will read this stored value when it mounts a moment later. The
 * already-mounted case (the "connect another repo" modal) switches view through
 * the context instead. */
export function patchUiState(patch: Partial<PersistedUiState>) {
  try {
    localStorage.setItem(UI_STATE_KEY, JSON.stringify({ ...loadUiState(), ...patch }))
  } catch {
    /* best-effort, same as saveUiState */
  }
}

/** Pending "open the first issue" intent, set at connect time.
 *
 * A MODULE-SCOPED variable, deliberately not localStorage/sessionStorage: the
 * gap this must survive is only the one between `onConnected` and the provider
 * mounting (which, on first run, happens moments later in the SAME JS session —
 * no reload occurs). Persisting it would outlive that gap, so a user who closes
 * the tab before the issues query resolves, or whose query errors, would have
 * the flag fire on their next visit and yank selection to the first issue of
 * whatever repo is active. Storage is also shared across tabs, where whichever
 * tab resolved first would consume the other's intent. */
let autoSelectFirstIssue: { owner: string; repo: string } | null = null

/** GitHub names are case-preserving but not case-sensitive. */
const repoKey = (r: { owner: string; repo: string }) => `${r.owner}/${r.repo}`.toLowerCase()

/** Ask the workspace to open the first open issue once the list has loaded.
 * SCOPED to the repo that was just connected: the provider may still be
 * showing the previous repo while its issues refetch, and an unscoped flag
 * would be consumed by that render and select an issue from the OLD repo. */
export function markAutoSelectFirstIssue(repo: { owner: string; repo: string }) {
  autoSelectFirstIssue = { owner: repo.owner, repo: repo.repo }
}

/** Read AND clear the flag, but only when `active` is the repo the intent was
 * recorded for. Returns true only for that repo's first caller. */
export function consumeAutoSelectFirstIssue(active: { owner: string; repo: string }): boolean {
  if (!autoSelectFirstIssue) return false
  if (repoKey(autoSelectFirstIssue) !== repoKey(active)) return false
  autoSelectFirstIssue = null
  return true
}
