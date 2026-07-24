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
  type ConnectedRepo, type Issue, type RepoLabel, type RepoMember, type RepoPermissions, type RepoSettings,
} from './api'
import type {
  ActiveRepo, DashboardTab, ExpandedSection, MainView, SettingsTarget, SortDir, SortKey, StateFilter,
} from './lib/types'
import { asArray, loadUiState, saveUiState } from './lib/format'

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

  // ── navigation ──
  mainView: MainView
  dashboardTab: DashboardTab
  openDashboard: (tab: DashboardTab) => void
  openIssues: () => void
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
  const [sortKey, setSortKey] = useState<SortKey>(restored.sortKey ?? 'number')
  const [sortDir, setSortDir] = useState<SortDir>(restored.sortDir ?? 'desc')

  const [mainView, setMainView] = useState<MainView>(restored.mainView ?? 'dashboard')
  const [dashboardTab, setDashboardTab] = useState<DashboardTab>(restored.dashboardTab ?? 'overview')
  const [settingsTarget, setSettingsTarget] = useState<SettingsTarget>(restored.settingsTarget ?? { kind: 'general', anchor: 'account' })
  const [expanded, setExpanded] = useState<ExpandedSection>('dashboards')

  // Follow-mode: switching main view auto-expands the matching accordion
  // section. A manual header click (setExpanded) overrides until the next
  // mode change.
  const SECTION_FOR_VIEW: Record<MainView, ExpandedSection> = {
    dashboard: 'dashboards',
    issues: 'filters',
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
    })
  }, [
    mainView, dashboardTab, settingsTarget, selectedIssue, query,
    selectedLabels, requestedByMe, assignedToMe, createdByMember, stateFilter, sortKey, sortDir,
  ])

  const meQuery = useQuery({ queryKey: ['issue-radar', 'me'], queryFn: () => issueRadarApi.me() })
  const me = meQuery.data?.login ?? null

  const issuesQuery = useQuery({
    queryKey: ['issue-radar', 'issues', owner, repo, stateFilter],
    queryFn: () => issueRadarApi.issues(owner, repo, { state: stateFilter }),
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
  const isMemberIssue = useCallback(
    (iss: Issue) =>
      (iss.author != null && memberRoleByLogin.has(iss.author)) ||
      MEMBER_ASSOCS.has(iss.author_association ?? ''),
    [memberRoleByLogin],
  )
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
      else d = 0 // 'ranking' — AI-generated order, not implemented yet
      return sortDir === 'asc' ? d : -d
    })
    return arr
  }, [filteredIssues, sortKey, sortDir])

  const activeIssue = sortedIssues.find((i) => i.number === selectedIssue)
    ?? issues.find((i) => i.number === selectedIssue)
    ?? null

  const switchRepo = (r: ActiveRepo) => {
    setSelectedIssue(null)
    setQuery('')
    clearFilters()
    onSwitch(r)
  }

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
    mainView, dashboardTab, openDashboard, openIssues, openSettings, settingsTarget,
    expanded, setExpanded,
  }

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
