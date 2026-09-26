// Persistence for Code Review Sage: where you were, and what you last saw.
//
// Two separate concerns, deliberately kept apart:
//
//  1. UI STATE (this repo, this review, this tab) — small, and authoritative.
//     Restoring it means a reload or a trip through another Kiro Crew page lands
//     you back where you were instead of on an empty shell.
//
//  2. QUERY SNAPSHOTS — the last successful payload of each list, replayed on
//     mount as react-query `initialData` with its ORIGINAL timestamp. Because
//     that timestamp is already older than the query's staleTime, react-query
//     renders the cached data immediately and refetches in the background:
//     stale-while-revalidate, without a skeleton on every reload.
//
// The project has no query-cache persister (no `@tanstack/query-persist-client`
// anywhere), so this is scoped to Sage rather than adding dependencies to a repo
// that is going public.
//
// Both are best-effort: every read is guarded and every failure degrades to the
// current behaviour (default state, loading skeleton). Persistence must never be
// able to break the app — a corrupt or outdated value is discarded, not trusted.
import type { ListTab, MainView, PrRef } from './types'

const PREFIX = 'kc:code-review-sage'
export const UI_STATE_KEY = `${PREFIX}:ui-state`
const CACHE_PREFIX = `${PREFIX}:cache:`

/** Bump when a persisted shape changes incompatibly — old entries are dropped
 * rather than fed to code that no longer understands them. */
const SCHEMA = 1

/** How long a snapshot is worth replaying. Past this it is likely to mislead
 * more than it helps (runs evicted, PRs merged), so we take the skeleton. */
const MAX_AGE_MS = 24 * 60 * 60 * 1000

/** Per-entry cap. PR lists for a busy repo are the large ones; a payload past
 * this is skipped rather than risking the 5MB origin-wide storage budget that
 * the rest of the dashboard also draws on. */
const MAX_BYTES = 256 * 1024

const RECENT_KEY = `${PREFIX}:recent-repos`

/** How many recently-picked repos to keep. Enough to cover what you are actually
 *  working on this week without turning the section into a second full list. */
const MAX_RECENT = 5

/** The repos you picked most recently, newest first.
 *
 * Separate from the pinned list, which is server-side and ordered by when each
 * repo was added — useless for finding the two or three you keep returning to. */
export function loadRecentRepos(): { owner: string; repo: string }[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!parsed || parsed.v !== SCHEMA || !Array.isArray(parsed.repos)) return []
    return parsed.repos
      .filter((r: unknown): r is { owner: string; repo: string } => (
        !!r && typeof (r as { owner?: unknown }).owner === 'string'
        && typeof (r as { repo?: unknown }).repo === 'string'
      ))
      .slice(0, MAX_RECENT)
  } catch {
    return []
  }
}

/** Record a pick, moving it to the front. */
export function rememberRecentRepo(
  repo: { owner: string; repo: string },
): { owner: string; repo: string }[] {
  const next = [
    { owner: repo.owner, repo: repo.repo },
    ...loadRecentRepos().filter(
      (r) => !(r.owner === repo.owner && r.repo === repo.repo)),
  ].slice(0, MAX_RECENT)
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify({ v: SCHEMA, repos: next }))
  } catch {
    /* best-effort */
  }
  return next
}

export interface PersistedUiState {
  mainView: MainView
  listTab: ListTab
  activeRepo: { owner: string; repo: string } | null
  selectedRunId: string | null
  selectedPr: PrRef | null
  /** Which tab of the PR detail pane was open (Sage Review / Description / …). */
  detailTab: string | null
}

const MAIN_VIEWS: readonly string[] = ['reviews', 'local', 'learning', 'settings']
const LIST_TABS: readonly string[] = ['pulls', 'reviews']

/** Coerce a persisted view back to one that still exists. A view removed since
 * the value was written must not survive a reload, or the main area renders
 * blank with nothing selected in the rail. */
export function coerceMainView(value: unknown): MainView {
  return (MAIN_VIEWS.includes(value as string) ? value : 'reviews') as MainView
}

export function coerceListTab(value: unknown): ListTab {
  return (LIST_TABS.includes(value as string) ? value : 'pulls') as ListTab
}

export function loadUiState(): Partial<PersistedUiState> {
  try {
    const raw = localStorage.getItem(UI_STATE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return {}
    if (parsed.v !== SCHEMA) return {}
    const s = parsed.state
    return s && typeof s === 'object' ? (s as Partial<PersistedUiState>) : {}
  } catch {
    return {}
  }
}

export function saveUiState(state: PersistedUiState) {
  try {
    localStorage.setItem(UI_STATE_KEY, JSON.stringify({ v: SCHEMA, state }))
  } catch {
    /* quota exceeded / private mode — persistence is best-effort */
  }
}

/** A replayed query payload plus WHEN it was fetched. The timestamp is the whole
 * point: handing it to react-query as `initialDataUpdatedAt` is what makes the
 * data render-now-refetch-now instead of looking fresh and suppressing the
 * refetch. */
export interface Snapshot<T> {
  data: T
  at: number
}

export function readSnapshot<T>(key: string): Snapshot<T> | undefined {
  try {
    const raw = localStorage.getItem(CACHE_PREFIX + key)
    if (!raw) return undefined
    const parsed = JSON.parse(raw)
    if (!parsed || parsed.v !== SCHEMA || typeof parsed.at !== 'number') {
      return undefined
    }
    if (Date.now() - parsed.at > MAX_AGE_MS) {
      localStorage.removeItem(CACHE_PREFIX + key)
      return undefined
    }
    return { data: parsed.data as T, at: parsed.at }
  } catch {
    return undefined
  }
}

export function writeSnapshot(key: string, data: unknown) {
  try {
    const body = JSON.stringify({ v: SCHEMA, at: Date.now(), data })
    if (body.length > MAX_BYTES) return
    localStorage.setItem(CACHE_PREFIX + key, body)
  } catch {
    /* best-effort */
  }
}

/** Drop every Sage snapshot. Used when a payload stops being trustworthy for a
 * reason a timestamp cannot express (the app's data dir was reset). */
export function clearSnapshots() {
  try {
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i)
      if (k && k.startsWith(CACHE_PREFIX)) doomed.push(k)
    }
    for (const k of doomed) localStorage.removeItem(k)
  } catch {
    /* best-effort */
  }
}
