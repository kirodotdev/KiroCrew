// Pure, side-effect-free helpers + localStorage accessors + constants for
// Issue Radar. No React, no component imports — safe to pull into any module.
import { Clock, Hash, type LucideIcon } from 'lucide-react'
import { fmtRelative, toDate } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
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

/** The poll interval an item deserves, given whether it is still open.
 *
 * `openMs` overrides the default OPEN cadence with the user's preference. A closed
 * item keeps its own backed-off interval regardless: its expensive parts (the diff
 * shape, the check runs, the commit list) are frozen, so polling it faster spends
 * calls to observe state that cannot change — which is not what the setting is
 * asking for even when it names a shorter interval.
 */
export function detailPollMs(open: boolean, openMs: number = DETAIL_POLL_MS): number {
  return open ? openMs : CLOSED_DETAIL_POLL_MS
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

// ── Refresh preferences ───────────────────────────────────────────────────
// The intervals above are the DEFAULTS, not the whole story: how fresh the app
// feels and how much of the provider's request budget it spends are the same
// dial, and only the operator knows which side they want. So the three levers
// are user-settable, persisted with the rest of the UI state, and each one
// falls back to the constant above when unset.
//
// The bounds are the load-bearing part, and the binding constraint is NOT the one
// you would guess. A list poll does not pay for a full fetch: the route is
// probe-gated (`routes._poll_can_serve_cache`), so the steady-state cost of a poll
// is ONE search call, and the paginated refetch happens only when that probe moves
// or the 10-minute staleness ceiling fires. So the 5,000/hr *core* budget is
// comfortable — what runs out first is GitHub's **30/min SEARCH quota**, which the
// probe spends and which the user's own `gh search` shares.
//
// That quota is what sets the floor. `routes._PROBE_COALESCE_SEC` (15s) shares one
// probe reading per (repo, kind) across every open tab, so a 30s interval costs at
// most 2 probes/min/kind however many tabs are open. **The floor below is 30s
// precisely because 30 >= 2 x 15** — halve it and each kind doubles its probe rate
// while coalescing stops absorbing anything. If that backend constant ever changes,
// this floor has to move with it; nothing enforces the relationship mechanically,
// which is why it is written down here.
//
// Worst case at the floor, stated honestly: 2 probe kinds (issues + PRs) x 2/min =
// 4 probes/min for one repo, so roughly 7 repos polling at once would saturate the
// quota. A probe failure is handled (the route keeps serving cache) but is SILENT
// beyond the staleness ceiling — the app looks healthy while lists sit up to 10
// minutes stale — so this is a real ceiling, not a theoretical one.

/** Selectable list-poll intervals, in ms.
 *
 * The floor is 30s, and it is not arbitrary: see the note above — it is twice the
 * backend's 15s probe-coalescing window, which is what keeps the shared 30/min search
 * quota bounded no matter how many tabs are open. */
export const LIST_POLL_CHOICES_MS = [30_000, 60_000, 120_000, 300_000] as const

/** Selectable detail-poll intervals, in ms. A detail poll is ONE item's worth of
 * traffic (a handful of calls), so it can safely be faster than a list poll. */
export const DETAIL_POLL_CHOICES_MS = [15_000, 30_000, 60_000, 120_000] as const

/** How long a fetched list stays "fresh" before react-query will refetch it on
 * mount/focus. Raising it is what makes returning to the app paint instantly
 * from cache instead of re-fetching. */
export const STALE_TIME_CHOICES_MS = [0, 30_000, 120_000, 600_000] as const

/** Defaults for the refresh preferences — the historical hardcoded behaviour, so
 * an existing user's app behaves identically until they change something. */
export const REFRESH_DEFAULTS = {
  listPollMs: LIST_POLL_MS,
  detailPollMs: DETAIL_POLL_MS,
  staleTimeMs: 30_000,
  /** Keep polling while the tab is in the BACKGROUND. Default off, which is
   * react-query's own default: a backgrounded tab costs nothing, at the price of
   * returning to a stale list and waiting for the first poll. */
  pollInBackground: false,
  /** Load the pull-request list as soon as the app opens, rather than waiting for
   * the PR surface to be opened. Default off because that fetch also runs the
   * GraphQL enrichment, so it spends budget on data the user may never look at. */
  prefetchPulls: false,
} as const

/** The user's refresh preferences. */
export interface RefreshPrefs {
  listPollMs: number
  detailPollMs: number
  staleTimeMs: number
  pollInBackground: boolean
  prefetchPulls: boolean
}

/** Coerce a persisted interval back into one of its OFFERED choices.
 *
 * Not a range clamp: a value outside the list means the persisted state predates
 * a change to the choices (or was hand-edited in localStorage), and silently
 * honouring, say, a 1s list poll would exhaust the provider's request budget and
 * take the app down with 403s. An unrecognized value falls back to the default.
 */
export function coerceInterval(
  value: unknown, choices: readonly number[], fallback: number,
): number {
  return typeof value === 'number' && choices.includes(value) ? value : fallback
}

/** The refresh preferences from persisted state, each field validated. */
export function coerceRefreshPrefs(raw: Partial<RefreshPrefs> | undefined): RefreshPrefs {
  return {
    listPollMs: coerceInterval(
      raw?.listPollMs, LIST_POLL_CHOICES_MS, REFRESH_DEFAULTS.listPollMs,
    ),
    detailPollMs: coerceInterval(
      raw?.detailPollMs, DETAIL_POLL_CHOICES_MS, REFRESH_DEFAULTS.detailPollMs,
    ),
    staleTimeMs: coerceInterval(
      raw?.staleTimeMs, STALE_TIME_CHOICES_MS, REFRESH_DEFAULTS.staleTimeMs,
    ),
    pollInBackground: typeof raw?.pollInBackground === 'boolean'
      ? raw.pollInBackground
      : REFRESH_DEFAULTS.pollInBackground,
    prefetchPulls: typeof raw?.prefetchPulls === 'boolean'
      ? raw.prefetchPulls
      : REFRESH_DEFAULTS.prefetchPulls,
  }
}

/** Compact "now / 5m ago / 3h ago / 2d ago" from an epoch-ms timestamp.
 * Used for the issue-list "Updated …" footer; returns '' for a falsy input
 * (e.g. before the first fetch).
 *
 * Formatting is delegated to the locale-aware seam (`src/i18n/format.ts`) so it
 * renders in the active language rather than English-only, and so
 * `month`/`months` plural morphology comes from CLDR rather than being
 * hand-rolled (which is unexpressible outside English). */
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
 * a mechanical "1 day ago", and it avoids hand-rolled English plural
 * suffixes for months and years.
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

/** Sort options rendered in the Filters section.
 *
 * `label` is a GETTER, not a value: this table is evaluated once at import, so an
 * `i18nT()` call in the initialiser would freeze the boot language and never
 * re-resolve on a language switch. A getter runs on every property access, and
 * the access is `{f.label}` inside FiltersSection's render — so the consumers
 * need no change. */
export const SORT_FIELDS: { key: SortKey; label: string; icon: LucideIcon }[] = [
  { key: 'number', get label() { return i18nT('apps.issueRadar.lib.format.number') }, icon: Hash },
  { key: 'updated', get label() { return i18nT('apps.issueRadar.lib.format.last_update') }, icon: Clock },
]

/** Sort options for the pull-request list — same fields, and deliberately the
 *  same two catalog keys, as the issue list above. */
export const PR_SORT_FIELDS: { key: PrSortKey; label: string; icon: LucideIcon }[] = [
  { key: 'number', get label() { return i18nT('apps.issueRadar.lib.format.number') }, icon: Hash },
  { key: 'updated', get label() { return i18nT('apps.issueRadar.lib.format.last_update') }, icon: Clock },
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
  // ── refresh preferences ──
  // Persisted with the rest of the UI state rather than in a separate store: they
  // are per-browser view preferences like the filters, not repo configuration (a
  // per-repo home would make "how often does this poll" a property of the repo,
  // which is not what the setting means).
  refresh: RefreshPrefs
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
export function patchUiState(
  // `refresh` is deliberately EXCLUDED from what this may write. It is the one
  // persisted field with a validated domain — an out-of-range interval is a real
  // provider-budget hazard (see `coerceInterval`) — and this is the only write path
  // that does not go through `setRefreshPrefs`. The read side coerces anyway, so a bad
  // value could not take effect, but keeping it out of the type means a future caller
  // cannot introduce the bypass in the first place.
  patch: Partial<Omit<PersistedUiState, 'refresh'>>,
) {
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
