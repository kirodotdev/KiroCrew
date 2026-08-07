// Issue Radar shared state + data layer.
//
// Everything the workspace needs — the active repo, the issues/labels/me
// queries, the filter + sort + selection state, the derived (filtered/sorted)
// lists, and the navigation state (which dashboard, which accordion section) —
// lives here behind `useIssueRadar()`. Components and dashboard views pull only
// what they need, so a new view is a self-contained file that never has to
// touch Workspace's prop wiring. That's what lets multiple agents build
// different views in parallel without editing the same file.
import {
  createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode,
} from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  issueRadarApi, DEFAULT_REPO_SETTINGS,
  type ConnectedRepo, type Issue, type PullRequest, type RepoLabel, type RepoMember, type RepoPermissions, type RepoSettings,
} from './api'
import type {
  ActiveRepo, DashboardTab, ExpandedSection, MainView, PrSortKey, PrStateFilter, SettingsTarget, SortDir, SortKey, StateFilter,
} from './lib/types'
import { repoScopeKey } from './lib/links'
import { DEFAULT_BULK_CHUNK } from './lib/prActions'
import {
  asArray, coerceDashboardTab, coerceRefreshPrefs, coerceSortKey, consumeAutoSelectFirstIssue,
  loadUiState, saveUiState,
} from './lib/format'
import type { RefreshPrefs } from './lib/format'
import type { RepoRef } from './lib/refLinks'

/** GitHub author_association values that mark a repo member (maintainer). Kept
 * in sync with the backend's ``_MEMBER_ASSOC_RANK`` and the detail badge's
 * "maintainer" grouping. */
const MEMBER_ASSOCS = new Set(['OWNER', 'MEMBER', 'COLLABORATOR'])

export interface IssueRadarContextValue {
  // ── repos ──
  repos: ConnectedRepo[]
  active: ActiveRepo
  switchRepo: (r: ActiveRepo) => void
  onAddRepo: () => void
  /** The active repo's GitHub permissions (null until the repos list loads). */
  activePermissions: RepoPermissions | null
  /** True when the current gh user can edit issues on the active repo
   * (triage/push/maintain/admin) — gates the label edit + close/reopen UI.
   * A read-only repo degrades to suggest-only (writes are hidden/disabled). */
  canWrite: boolean

  // ── data ──
  me: string | null
  issues: Issue[]
  repoLabels: RepoLabel[]
  issuesLoading: boolean
  /** True while the rendered issues are only the cold-start first page and the
   * full list is still loading behind them (see the progressive first paint). */
  issuesPartial: boolean
  issuesError: Error | null
  labelsLoading: boolean
  labelsError: Error | null
  refresh: () => void
  refreshing: boolean

  // ── per-repo triage settings (for the active repo) ──
  /** The active repo's saved triage settings (defaults until loaded/configured). */
  repoSettings: RepoSettings
  /** True when an issue counts as "needs triage" under the active repo's config
   * (a configured triage label, or — when enabled — no labels at all). */
  needsTriage: (iss: Issue) => boolean
  /** True when an issue carries one of the active repo's good-first-issue labels. */
  isGoodFirstIssue: (iss: Issue) => boolean
  /** Epoch-ms when the issues query last produced data (fetch or refresh);
   * 0 before the first load. Drives the "Updated Nm ago" footer label. */
  issuesUpdatedAt: number

  // ── derived ──
  colorByName: Map<string, string>
  countByLabel: Map<string, number>
  sortedRepoLabels: RepoLabel[]
  /** login -> repo role (admin/maintain/…, or OWNER/MEMBER/COLLABORATOR in the
   * read-only fallback) for repo members, from the cached roster. Lets the
   * detail badge show a member's role instantly and drives the member filter. */
  memberRoleByLogin: Map<string, string>
  filteredIssues: Issue[]
  sortedIssues: Issue[]
  activeIssue: Issue | null

  // ── filters / sort ──
  /** Free-text search over the issue list (title, #number, author, labels).
   * A middle-column concern only — folded into filteredIssues/sortedIssues,
   * which nothing outside the list consumes. */
  query: string
  setQuery: (q: string) => void
  selectedLabels: Set<string>
  toggleLabel: (name: string) => void
  requestedByMe: boolean
  toggleRequestedByMe: () => void
  assignedToMe: boolean
  toggleAssignedToMe: () => void
  /** Filter the list to issues opened by a repo member (OWNER/MEMBER/
   * COLLABORATOR author association). */
  createdByMember: boolean
  toggleCreatedByMember: () => void
  /** True when at least one loaded issue was opened by a repo member — gates
   * the "created by member" filter (disabled when the repo has none). */
  hasMemberIssues: boolean
  stateFilter: StateFilter
  setStateFilter: (s: StateFilter) => void
  anyFilterActive: boolean
  clearFilters: () => void
  sortKey: SortKey
  sortDir: SortDir
  cycleSort: (key: SortKey) => void

  // ── selection ──
  selectedIssue: number | null
  setSelectedIssue: (n: number | null) => void

  // ── pull requests ──
  pulls: PullRequest[]
  pullsLoading: boolean
  /** True when `pulls` holds only the cold-open first page (un-enriched) while
   * the full enriched list loads behind it — the PR twin of `issuesPartial`. */
  pullsPartial: boolean
  pullsError: Error | null
  refreshPulls: () => void
  pullsRefreshing: boolean
  /** Epoch-ms when the pulls query last produced data; 0 before first load. */
  pullsUpdatedAt: number
  /** True when a per-person PR filter is on, so the list came from GitHub SEARCH
   * (whole-repo, complete for that person) rather than the bounded list. The
   * list footer uses it to drop the "capped at 100" caveat. */
  prPersonFilterActive: boolean
  /** Set when the SEARCH result itself hit the server's cap, so the footer can say
   * "newest N" — the search escapes the list's page cap but has one of its own,
   * and claiming completeness anyway would just move the original lie. */
  prSearchTruncatedAt: number | null
  /** Open PR count per label name (drives the PR filter counts). */
  countByPrLabel: Map<string, number>
  /** Free-text search over the PR list (title, #number, author, branch, labels). */
  prQuery: string
  setPrQuery: (q: string) => void
  prSelectedLabels: Set<string>
  togglePrLabel: (name: string) => void
  prAuthoredByMe: boolean
  togglePrAuthoredByMe: () => void
  prAssignedToMe: boolean
  togglePrAssignedToMe: () => void
  prReviewRequestedByMe: boolean
  togglePrReviewRequestedByMe: () => void
  prDraftOnly: boolean
  togglePrDraftOnly: () => void
  /** Keep only PRs opened by a repo member (roster role, or GitHub's
   * author_association as a fallback) — the PR twin of createdByMember. */
  prCreatedByMember: boolean
  togglePrCreatedByMember: () => void
  /** False when the current PR set contains no member-authored PR, so the row
   * can be hidden rather than offering a filter that yields nothing. */
  hasMemberPulls: boolean
  prStateFilter: PrStateFilter
  setPrStateFilter: (s: PrStateFilter) => void
  anyPrFilterActive: boolean
  clearPrFilters: () => void
  prSortKey: PrSortKey
  prSortDir: SortDir
  cyclePrSort: (key: PrSortKey) => void
  selectedPull: number | null
  setSelectedPull: (n: number | null) => void
  filteredPulls: PullRequest[]
  sortedPulls: PullRequest[]
  activePull: PullRequest | null

  // ── bulk PR selection (transient) ──
  /** The PR numbers ticked for a mass action. Deliberately NOT persisted: a
   * restored selection would let a later visit apply an action to rows the user
   * ticked in a different sitting and has since forgotten. */
  checkedPulls: Set<number>
  /** Tick/untick one PR. */
  togglePullChecked: (n: number) => void
  /** Tick every PR currently RENDERED (the filtered+sorted set), or clear them
   * all when they are already ticked. Scoped to what is on screen so "select all"
   * can never reach a row the active filter is hiding. */
  toggleAllPullsChecked: () => void
  /** Drop the whole selection — after a bulk action, a repo switch, or Escape. */
  clearCheckedPulls: () => void
  /** The server's bulk-action cap, so the bulk bar chunks on the real limit. */
  prBulkMax: number

  // ── refresh preferences ──
  // Named `refreshPrefs`, not `refresh`: that name is already the manual-refresh
  // ACTION above, and one identifier meaning both a verb and a settings bag is how a
  // call site ends up invoking the wrong one.
  /** How often the lists and detail panes re-read, how long a fetch stays fresh,
   * and whether polling continues in a backgrounded tab. Every field is validated
   * against its offered choices — see `coerceRefreshPrefs`. */
  refreshPrefs: RefreshPrefs
  /** Patch one or more refresh preferences. Persisted with the rest of the UI
   * state, so it survives leaving the app and coming back. */
  setRefreshPrefs: (patch: Partial<RefreshPrefs>) => void

  // ── cross-reference sheet ──
  /** The open stack of same-repo issue/PR references, innermost LAST. Empty when
   * the sheet is closed. A ref opened from inside the sheet pushes onto it, so
   * "back" walks the trail you followed. */
  refStack: RepoRef[]
  /** Open a same-repo issue/PR in the bottom sheet (or push it onto the stack
   * when the sheet is already open). Re-opening the ref already on top is a
   * no-op, so a double-click can't stack the same target twice. */
  openRef: (ref: RepoRef) => void
  /** Drop the innermost sheet entry — back to the one that referenced it, or
   * closed when it was the only one. */
  popRef: () => void
  /** Close the sheet outright, discarding the whole stack. */
  closeRefs: () => void

  // ── navigation ──
  mainView: MainView
  dashboardTab: DashboardTab
  openDashboard: (tab: DashboardTab) => void
  openIssues: () => void
  openPulls: () => void
  openSettings: (target?: SettingsTarget) => void
  /** What the Settings main area is showing (the General page, or a repo page). */
  settingsTarget: SettingsTarget
  expanded: ExpandedSection
  setExpanded: (s: ExpandedSection) => void
}

const Ctx = createContext<IssueRadarContextValue | null>(null)

export function useIssueRadar(): IssueRadarContextValue {
  const v = useContext(Ctx)
  if (!v) throw new Error('useIssueRadar must be used within <IssueRadarProvider>')
  return v
}

export function IssueRadarProvider({
  repos, active, onSwitch, onAddRepo, children,
}: {
  repos: ConnectedRepo[]
  active: ActiveRepo
  onSwitch: (r: ActiveRepo) => void
  onAddRepo: () => void
  children: ReactNode
}) {
  const queryClient = useQueryClient()
  const { owner, repo } = active
  const scopeKey = repoScopeKey(active)

  // The active repo's GitHub permissions, used to gate the write UI (label
  // edits + close/reopen). Sourced from the connected-repo list (populated at
  // connect + self-healed by /repos), so no extra call is needed.
  const activePermissions = useMemo<RepoPermissions | null>(() => {
    const r = repos.find((x) => x.owner === owner && x.repo === repo)
    return r?.permissions ?? null
  }, [repos, owner, repo])
  const canWrite = !!(
    activePermissions &&
    (activePermissions.triage || activePermissions.push || activePermissions.maintain || activePermissions.admin)
  )

  // Restore the last view / filter / selection state (persisted to localStorage
  // by the effect below) so leaving Issue Radar for another KiroCrew page and
  // returning lands on the same page. The active repo is restored separately in
  // IssueRadarPage via loadActiveRepo.
  const [restored] = useState(loadUiState)

  const [query, setQuery] = useState(restored.query ?? '')
  const [selectedLabels, setSelectedLabels] = useState<Set<string>>(() => new Set(restored.selectedLabels ?? []))
  const [requestedByMe, setRequestedByMe] = useState(restored.requestedByMe ?? false)
  const [assignedToMe, setAssignedToMe] = useState(restored.assignedToMe ?? false)
  const [createdByMember, setCreatedByMember] = useState(restored.createdByMember ?? false)
  const [selectedIssue, setSelectedIssue] = useState<number | null>(restored.selectedIssue ?? null)
  const [stateFilter, setStateFilter] = useState<StateFilter>(restored.stateFilter ?? 'open')
  const [sortKey, setSortKey] = useState<SortKey>(() => coerceSortKey(restored.sortKey))
  const [sortDir, setSortDir] = useState<SortDir>(restored.sortDir ?? 'desc')

  // Refresh preferences. Each field is validated against the OFFERED choices rather
  // than range-clamped: a value outside them means the stored state predates a change
  // to the choices or was hand-edited, and honouring, say, a 1s list poll would burn
  // the provider's hourly request budget and take the app down with 403s.
  const [refreshPrefs, setRefreshState] = useState<RefreshPrefs>(
    () => coerceRefreshPrefs(restored.refresh),
  )

  // Re-validated on WRITE as well as on read, so a caller cannot install an
  // out-of-range interval that the read-side coercion would only fix on next load.
  const setRefreshPrefs = useCallback((patch: Partial<RefreshPrefs>) => {
    setRefreshState((prev) => coerceRefreshPrefs({ ...prev, ...patch }))
  }, [])

  const [mainView, setMainView] = useState<MainView>(restored.mainView ?? 'dashboard')
  const [dashboardTab, setDashboardTab] = useState<DashboardTab>(() => coerceDashboardTab(restored.dashboardTab))
  const [settingsTarget, setSettingsTarget] = useState<SettingsTarget>(restored.settingsTarget ?? { kind: 'general', anchor: 'account' })
  const [expanded, setExpanded] = useState<ExpandedSection>('dashboards')

  // ── pull-request view state (parallels the issue filters/sort/selection) ──
  const [prQuery, setPrQuery] = useState(restored.prQuery ?? '')
  const [prSelectedLabels, setPrSelectedLabels] = useState<Set<string>>(() => new Set(restored.prSelectedLabels ?? []))
  const [prAuthoredByMe, setPrAuthoredByMe] = useState(restored.prAuthoredByMe ?? false)
  const [prAssignedToMe, setPrAssignedToMe] = useState(restored.prAssignedToMe ?? false)
  const [prReviewRequestedByMe, setPrReviewRequestedByMe] = useState(restored.prReviewRequestedByMe ?? false)
  const [prDraftOnly, setPrDraftOnly] = useState(restored.prDraftOnly ?? false)
  const [prCreatedByMember, setPrCreatedByMember] = useState(restored.prCreatedByMember ?? false)
  const [selectedPull, setSelectedPull] = useState<number | null>(restored.selectedPull ?? null)
  const [prStateFilter, setPrStateFilter] = useState<PrStateFilter>(restored.prStateFilter ?? 'open')
  const [prSortKey, setPrSortKey] = useState<PrSortKey>(restored.prSortKey ?? 'number')
  const [prSortDir, setPrSortDir] = useState<SortDir>(restored.prSortDir ?? 'desc')

  // ── cross-reference sheet (transient, never persisted) ──
  // Deliberately NOT part of the persisted UI state: the sheet is a reading
  // detour, and restoring one on next visit would put the app behind a modal
  // nobody asked for.
  const [refStack, setRefStack] = useState<RepoRef[]>([])
  const openRef = useCallback((ref: RepoRef) => {
    setRefStack((prev) => {
      const top = prev[prev.length - 1]
      if (top && top.kind === ref.kind && top.number === ref.number) return prev
      return [...prev, ref]
    })
  }, [])
  const popRef = useCallback(() => setRefStack((prev) => prev.slice(0, -1)), [])
  const closeRefs = useCallback(() => setRefStack([]), [])
  // References are repo-scoped (a bare number means nothing across repos), so a
  // repo switch discards the stack rather than showing the new repo's unrelated
  // #42 — the same reason switchRepo resets selectedPull.
  useEffect(() => { setRefStack([]) }, [owner, repo])

  // Follow-mode: switching main view auto-expands the matching accordion
  // section. A manual header click (setExpanded) overrides until the next
  // mode change.
  const SECTION_FOR_VIEW: Record<MainView, ExpandedSection> = {
    dashboard: 'dashboards',
    issues: 'filters',
    pulls: 'pulls',
    settings: 'settings',
  }
  useEffect(() => {
    setExpanded(SECTION_FOR_VIEW[mainView])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mainView])

  // Persist the view / filter / selection state on every change so navigating
  // away from Issue Radar and back restores the same page (see loadUiState).
  useEffect(() => {
    saveUiState({
      mainView, dashboardTab, settingsTarget,
      selectedIssue, query,
      selectedLabels: [...selectedLabels],
      requestedByMe, assignedToMe, createdByMember,
      stateFilter, sortKey, sortDir,
      selectedPull, prQuery,
      prSelectedLabels: [...prSelectedLabels],
      prAuthoredByMe, prAssignedToMe, prReviewRequestedByMe, prDraftOnly,
      prCreatedByMember,
      prStateFilter, prSortKey, prSortDir,
      refresh: refreshPrefs,
    })
  }, [
    mainView, dashboardTab, settingsTarget, selectedIssue, query,
    selectedLabels, requestedByMe, assignedToMe, createdByMember, stateFilter, sortKey, sortDir,
    selectedPull, prQuery, prSelectedLabels, prAuthoredByMe, prAssignedToMe,
    prReviewRequestedByMe, prDraftOnly, prCreatedByMember, prStateFilter, prSortKey, prSortDir,
    refreshPrefs,
  ])

  // Keyed on the provider + host, not global: the login is not portable across
  // providers, and a cached GitHub login served for a GitLab project would make
  // the "assigned/requested to me" filters silently match nobody.
  const meQuery = useQuery({
    queryKey: ['issue-radar', 'me', active.provider || 'github', active.host || 'github.com'],
    queryFn: () => issueRadarApi.me({ provider: active.provider, host: active.host }),
  })
  const me = meQuery.data?.login ?? null

  // A LIST query polls on ``LIST_POLL_MS``, but its route is cache-first with no
  // server-side TTL: a plain refetch would be answered from that cache and
  // observe nothing new forever. A refetch therefore sends ``poll=1``, which
  // tells the backend "I want current data" and lets IT decide the cost — it
  // answers with one cheap probe call and only pays the paginated fetch when the
  // probe moved. The client deliberately does NOT send ``refresh=1`` here: that
  // is the unconditional cache-bust the manual Refresh button uses, and putting
  // it on a timer is what would make GitHub cost scale with open tabs.
  //
  // The FIRST fetch for a key sends neither, so it is served from cache at any
  // age and the app paints without waiting on `gh`. "Already have data for this
  // key" is exactly that distinction, so it drives the flag. Changing the state
  // filter mints a new key, which correctly reads as a first fetch.
  const isRefetch = useCallback(
    (key: readonly unknown[]) => queryClient.getQueryData(key) !== undefined,
    [queryClient],
  )

  /**
   * `keepPreviousData`, but ONLY within the same repository.
   *
   * Plain `keepPreviousData` retains the previous query's rows for ANY key change,
   * which conflates two very different transitions. Changing the state filter is a
   * different view of the SAME repo, so painting the old rows while the new ones load
   * is exactly the instant-repaint this exists for. Switching REPOS is not: repo A's
   * rows would paint under repo B's identity, and a PR number means something
   * different in each — so a row acted on during that window targets the same number
   * in the wrong repository.
   *
   * The ticked selection is already cleared on `scopeKey` (see the effect below), but
   * that effect runs AFTER the paint, so it closes the window one render late rather
   * than never opening it. Scoping the placeholder itself is the fix that has no
   * window: cross-repo data is never served, so there is nothing to act on.
   *
   * `scopeKey` (not `owner`/`repo`) is the identity, because it carries provider +
   * host — a same-slug repo on GitLab or an Enterprise host is a DIFFERENT repo.
   */
  const keepWithinRepo = useCallback(
    <T,>(previous: T | undefined, previousQuery?: { queryKey: readonly unknown[] }) => {
      // Every list key is ['issue-radar', <kind>, scopeKey, ...], so index 2 is the
      // scope. A previous query from another repo yields undefined -> normal loading
      // state (skeleton), which is the honest render for "we have nothing for this
      // repo yet".
      const previousScope = previousQuery?.queryKey?.[2]
      return previousScope === scopeKey ? previous : undefined
    },
    [scopeKey],
  )

  const issuesKey = ['issue-radar', 'issues', scopeKey, stateFilter] as const
  const issuesQuery = useQuery({
    queryKey: issuesKey,
    queryFn: () => issueRadarApi.issues(active, { state: stateFilter, poll: isRefetch(issuesKey) }),
    // react-query pauses this while the window is unfocused unless the user opts
    // in: a backgrounded tab then costs nothing, at the price of returning to a
    // stale list and waiting out the first poll.
    refetchInterval: refreshPrefs.listPollMs,
    refetchIntervalInBackground: refreshPrefs.pollInBackground,
    staleTime: refreshPrefs.staleTimeMs,
    // Keep the PREVIOUS query's rows on screen while a new key loads, so switching
    // the state filter (or the repo) repaints instantly with stale-but-real data
    // instead of blanking to a spinner. Costs no extra requests — it only changes
    // what is rendered during a fetch that was happening anyway.
    placeholderData: keepWithinRepo,
  })
  // Progressive first paint. The full issues fetch above paginates the WHOLE open
  // backlog before it can resolve — tens of `gh` requests on a large repo — so a
  // COLD open (no cached rows yet) would otherwise sit on a skeleton for seconds.
  // This fetches only the newest page in one request and feeds it to `issues`
  // until the authoritative list lands, then stands down.
  //
  // Deliberately additive and open-state only: it never feeds `issuesQuery.is
  // Success`, so the one-shot auto-select and the members gate (both keyed on it)
  // still wait for the COMPLETE list — a partial page must not satisfy "the repo's
  // issues are loaded". `enabled` gates it to the exact cold window: open state,
  // and the full query has produced nothing for this key yet (`data === undefined`
  // covers first load and a cross-repo switch, where keepWithinRepo yields
  // undefined). Once the full list resolves it is disabled and its rows are
  // ignored below, so it costs exactly one extra request per cold repo-open.
  const firstPageQuery = useQuery({
    queryKey: ['issue-radar', 'issues-first-page', scopeKey],
    queryFn: () => issueRadarApi.issuesFirstPage(active),
    enabled: stateFilter === 'open' && issuesQuery.data === undefined,
    staleTime: Infinity,
    gcTime: 0,
  })
  const labelsQuery = useQuery({
    queryKey: ['issue-radar', 'labels', scopeKey],
    queryFn: () => issueRadarApi.labels(active),
  })
  // Members are DERIVED server-side from the cached issues, so only fetch after
  // the issues query has succeeded: by then a fresh fetch has already built the
  // member cache (or the prior issue cache is present to derive from), and we
  // never trigger a second full open-issues fetch just to compute members.
  const membersQuery = useQuery({
    queryKey: ['issue-radar', 'members', scopeKey],
    queryFn: () => issueRadarApi.members(active),
    enabled: issuesQuery.isSuccess,
  })
  const settingsQuery = useQuery({
    queryKey: ['issue-radar', 'settings', scopeKey],
    queryFn: () => issueRadarApi.getSettings(active),
  })
  const repoSettings = settingsQuery.data?.settings ?? DEFAULT_REPO_SETTINGS

  // Pull requests. 'merged' and 'closed' both fetch the CLOSED set from GitHub
  // (the split is client-side on merged_at), so the fetch key collapses them to
  // 'closed' — one cache entry serves both filters.
  const prFetchState: 'open' | 'closed' = prStateFilter === 'open' ? 'open' : 'closed'
  // Only fetched once the PR surface is actually in use: this request also runs
  // the GraphQL enrichment server-side, so firing it while the user sits on the
  // dashboard or the issue list would spend GitHub API budget on data they may
  // never look at. The rail's Pull requests section counts as "in use" (opening
  // it sets mainView), so the list is already loading by the time it is shown.
  const prSurfaceActive = mainView === 'pulls' || expanded === 'pulls'
  // Per-person filters are answered SERVER-side by GitHub search rather than by
  // filtering the bounded list: the closed list is capped at one page, so a
  // client-side "authored by me" would silently miss your older PRs on a busy
  // repo (a PR merged days ago can already rank outside the window). When any
  // person filter is on we swap the data source to the search query, which
  // covers the whole repo; the list view keeps its bound.
  //
  // Two flags, deliberately: the search query needs the RESOLVED login, but the
  // base list must stand down as soon as a filter is REQUESTED — gating it on
  // `me` too would fire one whole-repo fetch (fully paginated) in the window
  // before /me lands, every time a persisted person filter is restored.
  const prPersonFilterRequested = prAuthoredByMe || prAssignedToMe || prReviewRequestedByMe
  const prPersonFilterActive = !!me && prPersonFilterRequested
  const pullsKey = ['issue-radar', 'pulls', scopeKey, prFetchState] as const
  const pullsQuery = useQuery({
    queryKey: pullsKey,
    queryFn: () => issueRadarApi.pulls(active, { state: prFetchState, poll: isRefetch(pullsKey) }),
    // The two PR sources are MUTUALLY EXCLUSIVE, and only one of them is ever
    // read (see `pulls` below). Enabling both while a person filter is on would
    // poll the provider twice a minute to fill a cache nothing renders.
    // Prefetching lifts the surface gate: the first open of the PR pane is the long
    // wait (a fully-paginated fetch plus the GraphQL enrichment), so the user can
    // choose to pay it in the background at app open instead. Off by default — it
    // spends provider budget on data they may never look at.
    enabled: (prSurfaceActive || refreshPrefs.prefetchPulls) && !prPersonFilterRequested,
    // Same cache-busting refetch as the issue list.
    refetchInterval: refreshPrefs.listPollMs,
    refetchIntervalInBackground: refreshPrefs.pollInBackground,
    staleTime: refreshPrefs.staleTimeMs,
    placeholderData: keepWithinRepo,
  })
  const refreshPullsMutation = useMutation({
    mutationFn: () => issueRadarApi.pulls(active, { refresh: true, state: prFetchState }),
    onSuccess: (data) => {
      queryClient.setQueryData(['issue-radar', 'pulls', scopeKey, prFetchState], data)
    },
  })
  // Progressive first paint for PRs — the same shape as `firstPageQuery` for
  // issues, and the larger win: a cold `pullsQuery` blocks on BOTH the full
  // pagination AND the GraphQL enrichment before it resolves, so the PR pane is
  // the app's slowest cold open. This fetches only the newest page (one request,
  // un-enriched) and feeds it to `pulls` until the authoritative list lands.
  //
  // Gated to the exact cold window: open state, no person filter (search owns
  // that path and is already whole-repo), the PR surface actually in use (so we
  // never spend a request on a pane the user has not opened — same gate as
  // `pullsQuery`), and the full query has produced nothing for this key yet
  // (`data === undefined` covers first load and a cross-repo switch). It never
  // feeds `pullsQuery.isSuccess`, so nothing keyed on "the PRs are loaded" is
  // satisfied by a partial page. Once the full list resolves it disables and its
  // rows are ignored below, so it costs exactly one extra request per cold open.
  const pullsFirstPageQuery = useQuery({
    queryKey: ['issue-radar', 'pulls-first-page', scopeKey],
    queryFn: () => issueRadarApi.pullsFirstPage(active),
    enabled: prSurfaceActive && prStateFilter === 'open'
      && !prPersonFilterRequested && pullsQuery.data === undefined,
    staleTime: Infinity,
    gcTime: 0,
  })

  const prSearchArgs = {
    state: prStateFilter,
    author: prAuthoredByMe && me ? me : undefined,
    assignee: prAssignedToMe && me ? me : undefined,
    reviewRequested: prReviewRequestedByMe && me ? me : undefined,
  }
  const pullsSearchQuery = useQuery({
    queryKey: [
      'issue-radar', 'pulls-search', scopeKey, prStateFilter,
      prSearchArgs.author ?? '', prSearchArgs.assignee ?? '', prSearchArgs.reviewRequested ?? '',
    ],
    queryFn: () => issueRadarApi.searchPulls(active, prSearchArgs),
    enabled: prSurfaceActive && prPersonFilterActive,
    // The search route is uncached server-side, so a plain refetch already goes
    // to the provider — no refresh flag needed here. Gated on the surface as well
    // as the filter: a person filter left on while the user works elsewhere in
    // the app must not keep polling provider search in the background.
    //
    // Deliberately NOT lifted by `prefetchPulls`: this route is uncached, so every
    // poll is a real provider search (a 30/min quota shared with the user's own
    // searches). Prefetching the cached LIST is cheap; prefetching this is not.
    refetchInterval: refreshPrefs.listPollMs,
    // And deliberately NOT given `refetchIntervalInBackground` either — this is the
    // ONE query in the app that opts out of that setting.
    //
    // Every other poll here is probe-gated, so background polling costs one cheap
    // probe that `_PROBE_COALESCE_SEC` shares across tabs. This route has no probe
    // path at all: each poll is a real provider search, up to 3 pages, against the
    // same 30/min quota — with no coalescing to absorb it. Honouring the toggle here
    // would mean a person filter someone left on months ago quietly spends that quota
    // forever, which is exactly what the gate two lines up exists to prevent. The
    // toggle's own hint promises "a constant API cost"; on this route the cost is not
    // constant, it is the most expensive path in the app.
    placeholderData: keepWithinRepo,
  })

  const refreshMutation = useMutation({
    mutationFn: async () => {
      const [issues, labels] = await Promise.all([
        issueRadarApi.issues(active, { refresh: true, state: stateFilter }),
        issueRadarApi.labels(active, { refresh: true }),
      ])
      return { issues, labels }
    },
    onSuccess: ({ issues, labels }) => {
      queryClient.setQueryData(['issue-radar', 'issues', scopeKey, stateFilter], issues)
      queryClient.setQueryData(['issue-radar', 'labels', scopeKey], labels)
      // A fresh issues fetch rebuilds the member cache server-side; re-read it.
      queryClient.invalidateQueries({ queryKey: ['issue-radar', 'members', scopeKey] })
    },
  })

  // The full list once it exists, else the cold-start first page. Falling back
  // only when `issuesQuery.data` is undefined means the authoritative set ALWAYS
  // wins the moment it arrives, and the first page's rows are the newest slice of
  // the same list in the same order, so the swap appends rather than reorders.
  //
  // Gated on `stateFilter === 'open'`: `firstPageQuery` is disabled off the open
  // filter, but disabling a query does NOT clear its cached data. Without the gate,
  // switching to Closed during the cold-open "loading the rest" window — while the
  // closed query has no data yet and its keepWithinRepo placeholder is undefined —
  // would paint the OPEN first-page rows under the Closed filter until the closed
  // fetch lands (filteredIssues does not re-split by lifecycle, so nothing else
  // masks it).
  const issues = useMemo(
    () => asArray<Issue>(
      (issuesQuery.data ?? (stateFilter === 'open' ? firstPageQuery.data : undefined))?.issues,
    ),
    [issuesQuery.data, firstPageQuery.data, stateFilter],
  )
  /** True while the visible issue rows are only the cold-start first page and the
   * complete list is still loading behind them — drives a "loading the rest" hint
   * without blocking the paint. */
  // Same `stateFilter === 'open'` gate as `issues` above: the first page (and its
  // partial flag) only apply to the open list, and its data lingers after the query
  // is disabled — so without the gate the "loading the rest" hint would show under
  // the Closed filter during a cold open.
  const issuesPartial = stateFilter === 'open'
    && issuesQuery.data === undefined && !!firstPageQuery.data?.partial
  const repoLabels = useMemo(() => asArray<RepoLabel>(labelsQuery.data?.labels), [labelsQuery.data])
  const members = useMemo<RepoMember[]>(() => asArray<RepoMember>(membersQuery.data?.members), [membersQuery.data])

  const memberRoleByLogin = useMemo(() => {
    const m = new Map<string, string>()
    for (const mem of members) m.set(mem.login, mem.role)
    return m
  }, [members])

  const colorByName = useMemo(() => {
    const m = new Map<string, string>()
    for (const l of repoLabels) m.set(l.name, l.color)
    return m
  }, [repoLabels])

  const countByLabel = useMemo(() => {
    const m = new Map<string, number>()
    for (const iss of issues) for (const name of iss.labels) m.set(name, (m.get(name) ?? 0) + 1)
    return m
  }, [issues])

  const sortedRepoLabels = useMemo(
    () => [...repoLabels].sort((a, b) => (countByLabel.get(b.name) ?? 0) - (countByLabel.get(a.name) ?? 0)),
    [repoLabels, countByLabel],
  )

  // Triage helpers derived from the active repo's saved settings. With the
  // defaults (no configured labels + unlabeled==untriaged) these reproduce the
  // dashboards' original heuristic exactly, so behaviour is unchanged until the
  // user configures labels on the repo's settings page.
  const triageLabelSet = useMemo(() => new Set(repoSettings.triage_labels), [repoSettings])
  const gfiLabelSet = useMemo(() => new Set(repoSettings.good_first_issue_labels), [repoSettings])
  const needsTriage = useCallback(
    (iss: Issue) =>
      (repoSettings.unlabeled_is_untriaged && iss.labels.length === 0)
      || iss.labels.some((l) => triageLabelSet.has(l)),
    [repoSettings.unlabeled_is_untriaged, triageLabelSet],
  )
  const isGoodFirstIssue = useCallback(
    (iss: Issue) => iss.labels.some((l) => gfiLabelSet.has(l)),
    [gfiLabelSet],
  )

  // "Created by a member": the author is in the repo's member roster, OR (only
  // matters for the read-only fallback / before the roster loads) the issue
  // itself carries a member author_association. The roster is authoritative and
  // complete, so it's the primary signal; the per-issue association is a
  // graceful fallback.
  // Typed on the two fields it actually reads so ONE predicate serves both
  // issues and pull requests (their rows carry the same author identity fields).
  const isMemberAuthored = useCallback(
    (row: { author?: string | null; author_association?: string | null }) =>
      (row.author != null && memberRoleByLogin.has(row.author)) ||
      MEMBER_ASSOCS.has(row.author_association ?? ''),
    [memberRoleByLogin],
  )
  const isMemberIssue = isMemberAuthored
  const isMemberPull = isMemberAuthored
  const hasMemberIssues = useMemo(() => issues.some(isMemberIssue), [issues, isMemberIssue])

  // Every handler below is a useCallback with stable deps (state setters are stable;
  // functional updaters read no captured state). This is what lets the memoized
  // `value` object keep a stable identity across renders that don't change a field
  // it carries — so a poll tick or an unrelated surface's filter change no longer
  // re-renders all ~20 context consumers, only the ones whose data actually moved.
  const openIssues = useCallback(() => setMainView('issues'), [])
  const openDashboard = useCallback((tab: DashboardTab) => {
    setDashboardTab(tab); setMainView('dashboard')
  }, [])
  const openSettings = useCallback((target?: SettingsTarget) => {
    setSettingsTarget(target ?? { kind: 'general', anchor: 'account' })
    setMainView('settings')
  }, [])

  const toggleLabel = useCallback((name: string) => {
    setMainView('issues')
    setSelectedLabels((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [])

  const toggleRequestedByMe = useCallback(() => { setRequestedByMe((v) => !v); setMainView('issues') }, [])
  const toggleAssignedToMe = useCallback(() => { setAssignedToMe((v) => !v); setMainView('issues') }, [])
  const toggleCreatedByMember = useCallback(() => { setCreatedByMember((v) => !v); setMainView('issues') }, [])

  const anyFilterActive = selectedLabels.size > 0 || requestedByMe || assignedToMe || createdByMember
  const clearFilters = useCallback(() => {
    setSelectedLabels(new Set()); setRequestedByMe(false); setAssignedToMe(false); setCreatedByMember(false)
  }, [])

  // `sortKey` is read, so it is a dep — the identity changes only when the sort key
  // does, which is exactly when a consumer of `cycleSort` would need the new closure.
  const cycleSort = useCallback((key: SortKey) => {
    setMainView('issues')
    if (key === sortKey) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else setSortKey(key)
  }, [sortKey])

  const filteredIssues = useMemo(() => {
    const q = query.trim().toLowerCase()
    // "#123" or "123" → match on issue number; otherwise substring-match the
    // title, author, and label names.
    const qNum = q.replace(/^#/, '')
    return issues.filter((iss) => {
      if (requestedByMe && (!me || iss.author !== me)) return false
      if (assignedToMe && (!me || !(iss.assignees ?? []).includes(me))) return false
      if (createdByMember && !isMemberIssue(iss)) return false
      const set = new Set(iss.labels)
      for (const want of selectedLabels) if (!set.has(want)) return false
      if (q) {
        const hit =
          String(iss.number).includes(qNum) ||
          iss.title.toLowerCase().includes(q) ||
          (iss.author ?? '').toLowerCase().includes(q) ||
          iss.labels.some((l) => l.toLowerCase().includes(q))
        if (!hit) return false
      }
      return true
    })
  }, [issues, selectedLabels, requestedByMe, assignedToMe, createdByMember, isMemberIssue, me, query])

  const sortedIssues = useMemo(() => {
    const arr = [...filteredIssues]
    arr.sort((a, b) => {
      let d = 0
      if (sortKey === 'number') d = a.number - b.number
      else if (sortKey === 'updated') d = new Date(a.updated_at).getTime() - new Date(b.updated_at).getTime()
      return sortDir === 'asc' ? d : -d
    })
    return arr
  }, [filteredIssues, sortKey, sortDir])

  // Deliberately resolved from the FILTERED+sorted list only, with no fallback
  // to the unfiltered fetch: if the current filters/search exclude the selected
  // issue, the detail pane clears rather than showing an item the list no longer
  // offers. (A fallback here would also make the behaviour inconsistent with the
  // PR pane, whose "me" filters swap the data source entirely, so nothing is
  // left to fall back to.)
  const activeIssue = sortedIssues.find((i) => i.number === selectedIssue) ?? null

  // ── pull requests: derived list (parallels the issue derivations) ──
  // Source depends on whether a person filter is active (see prPersonFilterActive).
  // On a cold open the authoritative `pullsQuery` is still undefined, so fall back
  // to the first-page rows (open state only — the fast path never runs off open, so
  // its lingering data must not leak into the closed tab, exactly as `firstPageQuery`
  // is gated for issues). The full set ALWAYS wins the moment it lands.
  const pulls = useMemo(
    () => prPersonFilterActive
      ? asArray<PullRequest>(pullsSearchQuery.data?.pulls)
      : asArray<PullRequest>(
        (pullsQuery.data ?? (prStateFilter === 'open' ? pullsFirstPageQuery.data : undefined))?.pulls,
      ),
    [prPersonFilterActive, pullsSearchQuery.data, pullsQuery.data, pullsFirstPageQuery.data, prStateFilter],
  )
  // True while `pulls` holds only the un-enriched first page: open state, no person
  // filter, the full query has produced nothing yet, and the first page said partial.
  const pullsPartial = !prPersonFilterActive && prStateFilter === 'open'
    && pullsQuery.data === undefined && !!pullsFirstPageQuery.data?.partial

  const countByPrLabel = useMemo(() => {
    const m = new Map<string, number>()
    for (const pr of pulls) for (const name of pr.labels) m.set(name, (m.get(name) ?? 0) + 1)
    return m
  }, [pulls])

  const openPulls = useCallback(() => setMainView('pulls'), [])

  const togglePrLabel = useCallback((name: string) => {
    setMainView('pulls')
    setPrSelectedLabels((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [])
  const togglePrAuthoredByMe = useCallback(() => { setPrAuthoredByMe((v) => !v); setMainView('pulls') }, [])
  const togglePrAssignedToMe = useCallback(() => { setPrAssignedToMe((v) => !v); setMainView('pulls') }, [])
  const togglePrReviewRequestedByMe = useCallback(() => { setPrReviewRequestedByMe((v) => !v); setMainView('pulls') }, [])
  const togglePrDraftOnly = useCallback(() => { setPrDraftOnly((v) => !v); setMainView('pulls') }, [])
  const togglePrCreatedByMember = useCallback(() => { setPrCreatedByMember((v) => !v); setMainView('pulls') }, [])
  const hasMemberPulls = useMemo(() => pulls.some(isMemberPull), [pulls, isMemberPull])

  const anyPrFilterActive = prSelectedLabels.size > 0 || prAuthoredByMe || prAssignedToMe
    || prReviewRequestedByMe || prDraftOnly || prCreatedByMember
  const clearPrFilters = useCallback(() => {
    setPrSelectedLabels(new Set())
    setPrAuthoredByMe(false); setPrAssignedToMe(false)
    setPrReviewRequestedByMe(false); setPrDraftOnly(false)
    setPrCreatedByMember(false)
  }, [])

  const cyclePrSort = useCallback((key: PrSortKey) => {
    setMainView('pulls')
    if (key === prSortKey) setPrSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else setPrSortKey(key)
  }, [prSortKey])

  const filteredPulls = useMemo(() => {
    const q = prQuery.trim().toLowerCase()
    const qNum = q.replace(/^#/, '')
    return pulls.filter((pr) => {
      // When the rows came from SEARCH, the state split and the person filters
      // were already applied by the query's qualifiers (is:merged / is:unmerged /
      // author: / assignee: / review-requested:). Re-applying them here would be
      // wrong as well as redundant: search rows carry no requested_reviewers, so
      // a client-side "review requested" check would reject every row.
      if (!prPersonFilterActive) {
        // merged / closed split (both fetched from the closed set on GitHub):
        // 'merged' keeps PRs with a merge timestamp; 'closed' keeps those closed
        // WITHOUT being merged. 'open' needs no split.
        if (prStateFilter === 'merged' && !pr.merged_at) return false
        if (prStateFilter === 'closed' && pr.merged_at) return false
        if (prAuthoredByMe && (!me || pr.author !== me)) return false
        if (prAssignedToMe && (!me || !(pr.assignees ?? []).includes(me))) return false
        if (prReviewRequestedByMe && (!me || !(pr.requested_reviewers ?? []).includes(me))) return false
      }
      if (prDraftOnly && !pr.draft) return false
      if (prCreatedByMember && !isMemberPull(pr)) return false
      const set = new Set(pr.labels)
      for (const want of prSelectedLabels) if (!set.has(want)) return false
      if (q) {
        const hit =
          String(pr.number).includes(qNum) ||
          pr.title.toLowerCase().includes(q) ||
          (pr.author ?? '').toLowerCase().includes(q) ||
          (pr.head ?? '').toLowerCase().includes(q) ||
          (pr.base ?? '').toLowerCase().includes(q) ||
          pr.labels.some((l) => l.toLowerCase().includes(q))
        if (!hit) return false
      }
      return true
    })
  }, [pulls, prPersonFilterActive, prStateFilter, prDraftOnly, prCreatedByMember, isMemberPull,
      prAuthoredByMe, prAssignedToMe, prReviewRequestedByMe, prSelectedLabels, me, prQuery])

  const sortedPulls = useMemo(() => {
    const arr = [...filteredPulls]
    arr.sort((a, b) => {
      let d = 0
      if (prSortKey === 'number') d = a.number - b.number
      else d = new Date(a.updated_at).getTime() - new Date(b.updated_at).getTime()
      return prSortDir === 'asc' ? d : -d
    })
    return arr
  }, [filteredPulls, prSortKey, prSortDir])

  // Same rule as activeIssue: filtered list only, no fallback — the detail pane
  // never outlives the row that opened it.
  const activePull = sortedPulls.find((p) => p.number === selectedPull) ?? null

  // ── bulk PR selection (transient, never persisted) ──
  // A selection is an in-the-moment intent, so restoring one on the next visit
  // would arm a mass action over rows the user no longer remembers ticking.
  const [checkedPulls, setCheckedPulls] = useState<Set<number>>(() => new Set())
  const togglePullChecked = useCallback((n: number) => {
    setCheckedPulls((prev) => {
      const next = new Set(prev)
      if (next.has(n)) next.delete(n)
      else next.add(n)
      return next
    })
  }, [])
  const clearCheckedPulls = useCallback(() => setCheckedPulls(new Set()), [])
  // Scoped to the RENDERED rows, so "select all" can never reach a PR the active
  // filter or search is hiding — the user can only mass-act on what they can see.
  const toggleAllPullsChecked = useCallback(() => {
    setCheckedPulls((prev) => {
      const visible = sortedPulls.map((p) => p.number)
      const allTicked = visible.length > 0 && visible.every((n) => prev.has(n))
      return allTicked ? new Set() : new Set(visible)
    })
  }, [sortedPulls])
  // Drop the selection when the repo changes or the PR set is refiltered.
  //
  // Two reasons, both correctness rather than tidiness: a number ticked in the open
  // list means a DIFFERENT item in the closed one (so carrying it over would act on
  // the wrong PR), and a row that leaves the view is no longer something the user
  // can see they have selected. PrBulkBar also intersects its selection with the
  // rendered rows, so a tick can never reach a hidden PR even between renders —
  // this effect is what stops a stale tick reappearing when the filter is undone.
  // Keyed on `scopeKey`, not `owner, repo`: the slug alone does NOT identify a repo
  // (`acme/widget` exists on GitHub and on every GitLab instance), so switching
  // between two same-slug repos left the ticks in place and pointed an armed bulk
  // action at unrelated items. scopeKey carries provider + host, which is exactly why
  // it exists — see repoScopeKey.
  useEffect(() => { setCheckedPulls(new Set()) }, [
    scopeKey, prStateFilter, prQuery, prDraftOnly, prCreatedByMember,
    prSelectedLabels, prAuthoredByMe, prAssignedToMe, prReviewRequestedByMe,
  ])

  const switchRepo = useCallback((r: ActiveRepo) => {
    setSelectedIssue(null)
    setQuery('')
    clearFilters()
    setSelectedPull(null)
    setPrQuery('')
    clearPrFilters()
    onSwitch(r)
  }, [clearFilters, clearPrFilters, onSwitch])

  // A just-connected repo opens its first issue once the list resolves, so the
  // user lands on real content instead of an empty detail pane. Driven by a
  // one-shot flag (see markAutoSelectFirstIssue) because the connect happens
  // before this query finishes — and, on first run, before this provider even
  // mounts. Consumed exactly once, so a later reload doesn't re-select.
  //
  // Falls back to the UNFILTERED list: the connect flow clears filters, but if
  // any survived (or a restored filter excludes everything in the new repo)
  // sortedIssues can be empty while the repo does have issues — consuming the
  // flag with nothing selected would strand the user on a blank pane.
  //
  // Gated on `active`: while the newly connected repo's issues are still
  // refetching, this effect can run with the PREVIOUS repo's list, and an
  // unscoped flag would select one of its issues.
  useEffect(() => {
    if (!issuesQuery.isSuccess) return
    if (!consumeAutoSelectFirstIssue(active)) return
    const first = sortedIssues[0] ?? issues[0]
    if (first) setSelectedIssue(first.number)
  }, [issuesQuery.isSuccess, sortedIssues, issues, active])

  // Stable so they don't force a new `value` identity every render. The mutation
  // objects are stable references, so no deps are needed.
  const refresh = useCallback(() => refreshMutation.mutate(), [refreshMutation])
  const refreshPulls = useCallback(() => {
    // Refresh targets the ACTIVE source: a refetch of the search query when a
    // person filter is on (the search route is uncached server-side, so a plain
    // refetch already hits GitHub), else the cache-busting list refresh.
    if (prPersonFilterActive) pullsSearchQuery.refetch()
    else refreshPullsMutation.mutate()
  }, [prPersonFilterActive, pullsSearchQuery, refreshPullsMutation])

  const value: IssueRadarContextValue = useMemo(() => ({
    repos, active, switchRepo, onAddRepo,
    activePermissions, canWrite,
    me, issues, repoLabels,
    // The skeleton clears as soon as EITHER the full list or the cold-start first
    // page has rows: the whole point of the first page is to end the blank wait.
    // `issues.length` is the honest signal — it is fed by both queries above.
    issuesLoading: issuesQuery.isLoading && issues.length === 0,
    issuesPartial,
    issuesError: (issuesQuery.error as Error) ?? null,
    labelsLoading: labelsQuery.isLoading,
    labelsError: (labelsQuery.error as Error) ?? null,
    refresh,
    refreshing: refreshMutation.isPending,
    issuesUpdatedAt: issuesQuery.dataUpdatedAt,
    repoSettings, needsTriage, isGoodFirstIssue,
    colorByName, countByLabel, sortedRepoLabels, filteredIssues, sortedIssues, activeIssue,
    memberRoleByLogin,
    query, setQuery,
    selectedLabels, toggleLabel,
    requestedByMe, toggleRequestedByMe,
    assignedToMe, toggleAssignedToMe,
    createdByMember, toggleCreatedByMember, hasMemberIssues,
    stateFilter, setStateFilter,
    anyFilterActive, clearFilters,
    sortKey, sortDir, cycleSort,
    selectedIssue, setSelectedIssue,
    pulls,
    // Covers the window between a person filter being REQUESTED and `me`
    // resolving: the base list is already disabled while the search query is not
    // enabled yet, and react-query reports isLoading=false for a disabled query —
    // so reading either one alone renders "no pull requests" instead of a
    // skeleton every time a persisted person filter is restored. Keyed on
    // meQuery.isLoading rather than `me` being falsy so a FAILED /me falls
    // through to the empty state instead of spinning forever.
    // `&& pulls.length === 0` so the cold-open first page drops the skeleton the
    // moment it paints (the full fetch is still in flight but there are rows to
    // show) — the PR twin of `issuesLoading`.
    pullsLoading: prPersonFilterRequested
      ? (prSurfaceActive && (meQuery.isLoading || pullsSearchQuery.isLoading))
      : (pullsQuery.isLoading && pulls.length === 0),
    pullsPartial,
    // A manual refresh goes through refreshPullsMutation, so its failure has to be
    // reported here too — otherwise the spinner just stops and the stale rows stay
    // on screen as if the refresh had worked.
    pullsError: ((prPersonFilterActive
      ? pullsSearchQuery.error
      : (pullsQuery.error ?? refreshPullsMutation.error)) as Error) ?? null,
    refreshPulls,
    pullsRefreshing: prPersonFilterActive ? pullsSearchQuery.isFetching : refreshPullsMutation.isPending,
    pullsUpdatedAt: prPersonFilterActive ? pullsSearchQuery.dataUpdatedAt : pullsQuery.dataUpdatedAt,
    prPersonFilterActive,
    prSearchTruncatedAt: prPersonFilterActive && pullsSearchQuery.data?.truncated
      ? (pullsSearchQuery.data.limit ?? pullsSearchQuery.data.pulls.length)
      : null,
    // The server's own bulk cap, from whichever pulls source is rendered. Read from
    // the response so the client chunks on the real limit rather than a hardcoded
    // copy that breaks the day the cap changes.
    prBulkMax: (prPersonFilterActive ? pullsSearchQuery.data?.bulk_max : pullsQuery.data?.bulk_max)
      ?? DEFAULT_BULK_CHUNK,
    refreshPrefs, setRefreshPrefs,
    countByPrLabel,
    prQuery, setPrQuery,
    prSelectedLabels, togglePrLabel,
    prAuthoredByMe, togglePrAuthoredByMe,
    prAssignedToMe, togglePrAssignedToMe,
    prReviewRequestedByMe, togglePrReviewRequestedByMe,
    prDraftOnly, togglePrDraftOnly,
    prCreatedByMember, togglePrCreatedByMember, hasMemberPulls,
    prStateFilter, setPrStateFilter,
    anyPrFilterActive, clearPrFilters,
    prSortKey, prSortDir, cyclePrSort,
    selectedPull, setSelectedPull,
    filteredPulls, sortedPulls, activePull,
    checkedPulls, togglePullChecked, toggleAllPullsChecked, clearCheckedPulls,
    refStack, openRef, popRef, closeRefs,
    mainView, dashboardTab, openDashboard, openIssues, openPulls, openSettings, settingsTarget,
    expanded, setExpanded,
  }), [
    repos, active, switchRepo, onAddRepo, activePermissions, canWrite,
    me, issues, repoLabels, issuesQuery.isLoading, issuesQuery.error, issuesQuery.dataUpdatedAt,
    issuesPartial, labelsQuery.isLoading, labelsQuery.error, refresh, refreshMutation.isPending,
    repoSettings, needsTriage, isGoodFirstIssue,
    colorByName, countByLabel, sortedRepoLabels, filteredIssues, sortedIssues, activeIssue,
    memberRoleByLogin, query, setQuery,
    selectedLabels, toggleLabel, requestedByMe, toggleRequestedByMe,
    assignedToMe, toggleAssignedToMe, createdByMember, toggleCreatedByMember, hasMemberIssues,
    stateFilter, setStateFilter, anyFilterActive, clearFilters, sortKey, sortDir, cycleSort,
    selectedIssue, setSelectedIssue, pulls,
    prPersonFilterRequested, prSurfaceActive, meQuery.isLoading, pullsSearchQuery.isLoading,
    pullsQuery.isLoading, prPersonFilterActive, pullsSearchQuery.error, pullsQuery.error,
    refreshPullsMutation.error, refreshPulls, pullsSearchQuery.isFetching, refreshPullsMutation.isPending,
    pullsSearchQuery.dataUpdatedAt, pullsQuery.dataUpdatedAt, pullsSearchQuery.data, pullsQuery.data,
    pullsPartial, pullsFirstPageQuery.data,
    refreshPrefs, setRefreshPrefs, countByPrLabel, prQuery, setPrQuery,
    prSelectedLabels, togglePrLabel, prAuthoredByMe, togglePrAuthoredByMe,
    prAssignedToMe, togglePrAssignedToMe, prReviewRequestedByMe, togglePrReviewRequestedByMe,
    prDraftOnly, togglePrDraftOnly, prCreatedByMember, togglePrCreatedByMember, hasMemberPulls,
    prStateFilter, setPrStateFilter, anyPrFilterActive, clearPrFilters,
    prSortKey, prSortDir, cyclePrSort, selectedPull, setSelectedPull,
    filteredPulls, sortedPulls, activePull,
    checkedPulls, togglePullChecked, toggleAllPullsChecked, clearCheckedPulls,
    refStack, openRef, popRef, closeRefs,
    mainView, dashboardTab, openDashboard, openIssues, openPulls, openSettings, settingsTarget,
    expanded, setExpanded,
  ])

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
