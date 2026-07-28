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
import {
  asArray, coerceDashboardTab, coerceSortKey, consumeAutoSelectFirstIssue, LIST_POLL_MS, loadUiState, saveUiState,
} from './lib/format'
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
    })
  }, [
    mainView, dashboardTab, settingsTarget, selectedIssue, query,
    selectedLabels, requestedByMe, assignedToMe, createdByMember, stateFilter, sortKey, sortDir,
    selectedPull, prQuery, prSelectedLabels, prAuthoredByMe, prAssignedToMe,
    prReviewRequestedByMe, prDraftOnly, prCreatedByMember, prStateFilter, prSortKey, prSortDir,
  ])

  const meQuery = useQuery({ queryKey: ['issue-radar', 'me'], queryFn: () => issueRadarApi.me() })
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

  const issuesKey = ['issue-radar', 'issues', owner, repo, stateFilter] as const
  const issuesQuery = useQuery({
    queryKey: issuesKey,
    queryFn: () => issueRadarApi.issues(owner, repo, { state: stateFilter, poll: isRefetch(issuesKey) }),
    // react-query pauses this while the window is unfocused
    // (refetchIntervalInBackground defaults to false), so a backgrounded tab
    // costs nothing.
    refetchInterval: LIST_POLL_MS,
  })
  const labelsQuery = useQuery({
    queryKey: ['issue-radar', 'labels', owner, repo],
    queryFn: () => issueRadarApi.labels(owner, repo),
  })
  // Members are DERIVED server-side from the cached issues, so only fetch after
  // the issues query has succeeded: by then a fresh fetch has already built the
  // member cache (or the prior issue cache is present to derive from), and we
  // never trigger a second full open-issues fetch just to compute members.
  const membersQuery = useQuery({
    queryKey: ['issue-radar', 'members', owner, repo],
    queryFn: () => issueRadarApi.members(owner, repo),
    enabled: issuesQuery.isSuccess,
  })
  const settingsQuery = useQuery({
    queryKey: ['issue-radar', 'settings', owner, repo],
    queryFn: () => issueRadarApi.getSettings(owner, repo),
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
  const pullsKey = ['issue-radar', 'pulls', owner, repo, prFetchState] as const
  const pullsQuery = useQuery({
    queryKey: pullsKey,
    queryFn: () => issueRadarApi.pulls(owner, repo, { state: prFetchState, poll: isRefetch(pullsKey) }),
    // The two PR sources are MUTUALLY EXCLUSIVE, and only one of them is ever
    // read (see `pulls` below). Enabling both while a person filter is on would
    // poll GitHub twice a minute to fill a cache nothing renders.
    enabled: prSurfaceActive && !prPersonFilterRequested,
    // Same cache-busting refetch as the issue list.
    refetchInterval: LIST_POLL_MS,
  })
  const refreshPullsMutation = useMutation({
    mutationFn: () => issueRadarApi.pulls(owner, repo, { refresh: true, state: prFetchState }),
    onSuccess: (data) => {
      queryClient.setQueryData(['issue-radar', 'pulls', owner, repo, prFetchState], data)
    },
  })

  const prSearchArgs = {
    state: prStateFilter,
    author: prAuthoredByMe && me ? me : undefined,
    assignee: prAssignedToMe && me ? me : undefined,
    reviewRequested: prReviewRequestedByMe && me ? me : undefined,
  }
  const pullsSearchQuery = useQuery({
    queryKey: [
      'issue-radar', 'pulls-search', owner, repo, prStateFilter,
      prSearchArgs.author ?? '', prSearchArgs.assignee ?? '', prSearchArgs.reviewRequested ?? '',
    ],
    queryFn: () => issueRadarApi.searchPulls(owner, repo, prSearchArgs),
    enabled: prSurfaceActive && prPersonFilterActive,
    // The search route is uncached server-side, so a plain refetch already goes
    // to GitHub — no refresh flag needed here. Gated on the surface as well as
    // the filter: a person filter left on while the user works elsewhere in the
    // app must not keep polling GitHub search in the background.
    refetchInterval: LIST_POLL_MS,
  })

  const refreshMutation = useMutation({
    mutationFn: async () => {
      const [issues, labels] = await Promise.all([
        issueRadarApi.issues(owner, repo, { refresh: true, state: stateFilter }),
        issueRadarApi.labels(owner, repo, { refresh: true }),
      ])
      return { issues, labels }
    },
    onSuccess: ({ issues, labels }) => {
      queryClient.setQueryData(['issue-radar', 'issues', owner, repo, stateFilter], issues)
      queryClient.setQueryData(['issue-radar', 'labels', owner, repo], labels)
      // A fresh issues fetch rebuilds the member cache server-side; re-read it.
      queryClient.invalidateQueries({ queryKey: ['issue-radar', 'members', owner, repo] })
    },
  })

  const issues = useMemo(() => asArray<Issue>(issuesQuery.data?.issues), [issuesQuery.data])
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

  const openIssues = () => setMainView('issues')
  const openDashboard = (tab: DashboardTab) => { setDashboardTab(tab); setMainView('dashboard') }
  const openSettings = (target?: SettingsTarget) => {
    setSettingsTarget(target ?? { kind: 'general', anchor: 'account' })
    setMainView('settings')
  }

  const toggleLabel = (name: string) => {
    setMainView('issues')
    setSelectedLabels((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }

  const toggleRequestedByMe = () => { setRequestedByMe((v) => !v); setMainView('issues') }
  const toggleAssignedToMe = () => { setAssignedToMe((v) => !v); setMainView('issues') }
  const toggleCreatedByMember = () => { setCreatedByMember((v) => !v); setMainView('issues') }

  const anyFilterActive = selectedLabels.size > 0 || requestedByMe || assignedToMe || createdByMember
  const clearFilters = () => {
    setSelectedLabels(new Set()); setRequestedByMe(false); setAssignedToMe(false); setCreatedByMember(false)
  }

  const cycleSort = (key: SortKey) => {
    setMainView('issues')
    if (key === sortKey) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else setSortKey(key)
  }

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
  // offers. (A fallback here also made the behaviour inconsistent with the PR
  // pane, whose "me" filters swap the data source entirely, so nothing was left
  // to fall back to.)
  const activeIssue = sortedIssues.find((i) => i.number === selectedIssue) ?? null

  // ── pull requests: derived list (parallels the issue derivations) ──
  // Source depends on whether a person filter is active (see prPersonFilterActive).
  const pulls = useMemo(
    () => prPersonFilterActive
      ? asArray<PullRequest>(pullsSearchQuery.data?.pulls)
      : asArray<PullRequest>(pullsQuery.data?.pulls),
    [prPersonFilterActive, pullsSearchQuery.data, pullsQuery.data],
  )

  const countByPrLabel = useMemo(() => {
    const m = new Map<string, number>()
    for (const pr of pulls) for (const name of pr.labels) m.set(name, (m.get(name) ?? 0) + 1)
    return m
  }, [pulls])

  const openPulls = () => setMainView('pulls')

  const togglePrLabel = (name: string) => {
    setMainView('pulls')
    setPrSelectedLabels((prev) => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }
  const togglePrAuthoredByMe = () => { setPrAuthoredByMe((v) => !v); setMainView('pulls') }
  const togglePrAssignedToMe = () => { setPrAssignedToMe((v) => !v); setMainView('pulls') }
  const togglePrReviewRequestedByMe = () => { setPrReviewRequestedByMe((v) => !v); setMainView('pulls') }
  const togglePrDraftOnly = () => { setPrDraftOnly((v) => !v); setMainView('pulls') }
  const togglePrCreatedByMember = () => { setPrCreatedByMember((v) => !v); setMainView('pulls') }
  const hasMemberPulls = useMemo(() => pulls.some(isMemberPull), [pulls, isMemberPull])

  const anyPrFilterActive = prSelectedLabels.size > 0 || prAuthoredByMe || prAssignedToMe
    || prReviewRequestedByMe || prDraftOnly || prCreatedByMember
  const clearPrFilters = () => {
    setPrSelectedLabels(new Set())
    setPrAuthoredByMe(false); setPrAssignedToMe(false)
    setPrReviewRequestedByMe(false); setPrDraftOnly(false)
    setPrCreatedByMember(false)
  }

  const cyclePrSort = (key: PrSortKey) => {
    setMainView('pulls')
    if (key === prSortKey) setPrSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else setPrSortKey(key)
  }

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

  const switchRepo = (r: ActiveRepo) => {
    setSelectedIssue(null)
    setQuery('')
    clearFilters()
    setSelectedPull(null)
    setPrQuery('')
    clearPrFilters()
    onSwitch(r)
  }

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

  const value: IssueRadarContextValue = {
    repos, active, switchRepo, onAddRepo,
    activePermissions, canWrite,
    me, issues, repoLabels,
    issuesLoading: issuesQuery.isLoading,
    issuesError: (issuesQuery.error as Error) ?? null,
    labelsLoading: labelsQuery.isLoading,
    labelsError: (labelsQuery.error as Error) ?? null,
    refresh: () => refreshMutation.mutate(),
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
    pullsLoading: prPersonFilterRequested
      ? (prSurfaceActive && (meQuery.isLoading || pullsSearchQuery.isLoading))
      : pullsQuery.isLoading,
    // A manual refresh goes through refreshPullsMutation, so its failure has to be
    // reported here too — otherwise the spinner just stops and the stale rows stay
    // on screen as if the refresh had worked.
    pullsError: ((prPersonFilterActive
      ? pullsSearchQuery.error
      : (pullsQuery.error ?? refreshPullsMutation.error)) as Error) ?? null,
    // Refresh targets the ACTIVE source: a refetch of the search query when a
    // person filter is on (the search route is uncached server-side, so a plain
    // refetch already hits GitHub), else the cache-busting list refresh.
    refreshPulls: () => {
      if (prPersonFilterActive) pullsSearchQuery.refetch()
      else refreshPullsMutation.mutate()
    },
    pullsRefreshing: prPersonFilterActive ? pullsSearchQuery.isFetching : refreshPullsMutation.isPending,
    pullsUpdatedAt: prPersonFilterActive ? pullsSearchQuery.dataUpdatedAt : pullsQuery.dataUpdatedAt,
    prPersonFilterActive,
    prSearchTruncatedAt: prPersonFilterActive && pullsSearchQuery.data?.truncated
      ? (pullsSearchQuery.data.limit ?? pullsSearchQuery.data.pulls.length)
      : null,
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
    refStack, openRef, popRef, closeRefs,
    mainView, dashboardTab, openDashboard, openIssues, openPulls, openSettings, settingsTarget,
    expanded, setExpanded,
  }

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
