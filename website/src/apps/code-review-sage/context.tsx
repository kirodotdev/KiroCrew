// Shared state for Code Review Sage: the run (thread) registry, the selected
// thread's report, repo/PR discovery, and navigation.
//
// The backend owns run state, so this is a thin react-query layer over it —
// consumers read what they need from `useSage()` rather than threading props
// through the shell. Modelled on Issue Radar's context: one provider, one hook,
// polling that slows down when nothing is happening.
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
} from '@tanstack/react-query'
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import { sageApi } from './api'
import { changeKey, prRefFromChange, repoOfRun, runCoversChange } from './lib/format'
import { IDLE_POLL_MS, LIVE_POLL_MS } from './lib/layout'
import {
  coerceListTab, coerceMainView, loadUiState, readSnapshot, saveUiState,
  writeSnapshot,
} from './lib/persist'
import type {
  ListTab,
  RepoPrsResponse,
  RunsResponse,
  MainView,
  PinnedRepo,
  PoolStats,
  RailSection,
  RecentReposResponse,
  PrRef,
  RepoPr,
  ReviewerInfo,
  Run,
  RunReport,
  UserReposResponse,
} from './lib/types'

/** A repo the picker is currently showing PRs for. */
export interface ActiveRepo {
  owner: string
  repo: string
}

export function repoUrl(r: ActiveRepo): string {
  return `https://github.com/${r.owner}/${r.repo}`
}

export function repoSlug(r: ActiveRepo): string {
  return `${r.owner}/${r.repo}`
}

export interface SageContextValue {
  // --- Threads (runs) ---
  runs: Run[]
  runsLoading: boolean
  runsError: Error | null
  pool: PoolStats | null
  reviewer: ReviewerInfo | null
  /** True while any run is live — drives the poll cadence and the rail badge. */
  anyRunning: boolean
  /** Runs belonging to the active repo. Empty when no repo is selected. */
  repoRuns: Run[]
  /** change_ids covered by a LIVE run. A PR already being reviewed must not be
   *  selectable for a second review — the backend would refuse the duplicate
   *  anyway (the in-flight claim registry), so offering it is a lie. */
  /** Change URLs (via `changeKey`) under review right now. Keyed by URL, not
   *  change_id: that id is a lossily-sanitized filename stem and collides
   *  across repos differing only by `-` vs `_`. */
  reviewingChangeUrls: Set<string>
  selectedRunId: string | null
  selectRun: (runId: string | null) => void
  activeRun: Run | null

  // --- The selected thread's report ---
  report: RunReport | null
  reportLoading: boolean
  reportError: Error | null

  // --- Thread actions ---
  cancelRun: (runId: string) => void
  cancelling: boolean
  deleteRun: (runId: string) => void
  deleting: boolean
  archiveRun: (runId: string) => void
  archiving: boolean
  archiveError: Error | null
  /** Publish a finished run's findings to its pull request (never automatic).
   *  With a selection, only those comments are sent. */
  postComments: (runId: string, select?: { changeId: string; keys?: string[] }) => void
  /** Publish a selection that spans several changes, ONE request at a time.
   *
   *  The backend rejects a second post while one is in flight (`already_posting`),
   *  so firing a group per change concurrently gets the first accepted and the
   *  rest 409'd — while the UI has already cleared the selection, leaving those
   *  comments silently unpublished. Sequencing keeps the multi-change selection
   *  and makes every group actually land. */
  postCommentGroups: (
    runId: string,
    groups: { changeId: string; keys: string[] }[],
  ) => Promise<void>
  posting: boolean
  /** The selection currently being posted: `undefined` when idle, `null` when the
   *  whole review is going out, otherwise the specific comment keys. */
  postingSelection?: { changeId: string; keys?: string[] } | null
  postError: Error | null

  // --- Starting a review ---
  startReview: UseMutationResult<{ run_id: string; changes: string[] }, Error, string[]>
  startReviewLinks: UseMutationResult<{ run_id: string; changes: string[] }, Error, string>
  startRepoReview: UseMutationResult<
    { run_id?: string; repo: string; changes: string[]; skipped: number; status: string; message?: string },
    Error,
    { repo: string; force: boolean }
  >

  // --- Repo + PR discovery ---
  pinnedRepos: PinnedRepo[]
  pinnedLoading: boolean
  recent: RecentReposResponse | null
  recentLoading: boolean
  recentError: Error | null
  /** Recent-repo discovery is a live `gh` call, so it is opt-in. */
  discoveryEnabled: boolean
  enableDiscovery: () => void
  /** Every repo the gh user can reach (not just recently-touched ones). */
  mine: UserReposResponse | null
  mineLoading: boolean
  mineError: Error | null
  refreshMine: () => void
  pinRepo: (owner: string, repo: string) => void
  pinRepoUrl: (url: string) => void
  unpinRepo: (owner: string, repo: string) => void
  pinError: Error | null

  activeRepo: ActiveRepo | null
  setActiveRepo: (r: ActiveRepo | null) => void
  /** The PR whose detail + review the detail pane is showing. */
  selectedPr: PrRef | null
  selectPr: (pr: PrRef | null) => void
  /** The most recent run that covered `selectedPr`, if any. */
  prRun: Run | null
  prs: RepoPr[]
  prsLoading: boolean
  prsError: Error | null
  refreshPrs: () => void

  // --- Navigation ---
  mainView: MainView
  setMainView: (v: MainView) => void
  /** Which learning namespace the Learning view is reading. */
  selectedNamespace: string | null
  selectNamespace: (ns: string | null) => void
  /** Which list the middle column shows. */
  listTab: ListTab
  setListTab: (t: ListTab) => void
  expanded: RailSection
  setExpanded: (s: RailSection) => void
  /** True when the "new review" composer is open in the detail pane. */
  composing: boolean
  openComposer: () => void
  closeComposer: () => void
  /** True when the "Add repos" picker owns the detail pane. */
  addingRepos: boolean
  openAddRepos: () => void
  closeAddRepos: () => void
}

const Ctx = createContext<SageContextValue | null>(null)

export function useSage(): SageContextValue {
  const v = useContext(Ctx)
  if (!v) throw new Error('useSage must be used within <SageProvider>')
  return v
}

const RUNS_KEY = ['code-review-sage', 'runs'] as const

export function SageProvider({ children, initialRunId }: {
  children: ReactNode
  initialRunId?: string | null
}) {
  const qc = useQueryClient()
  // Read once, at mount: the last state this app was left in. A `?run=` deep
  // link (from a finished-review notification) is a deliberate destination, so
  // it outranks whatever was restored.
  const [restored] = useState(loadUiState)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(
    initialRunId ?? restored.selectedRunId ?? null)
  const [mainView, setMainView] = useState<MainView>(
    () => coerceMainView(restored.mainView))
  // Defaults to the namespace every install has, so opening Learning reads
  // something instead of an empty pane.
  const [selectedNamespace, selectNamespace] = useState<string | null>('default')
  const [listTab, setListTab] = useState<ListTab>(
    () => coerceListTab(restored.listTab))
  const [expanded, setExpanded] = useState<RailSection>('reviews')
  const [composing, setComposing] = useState(false)
  const [addingRepos, setAddingRepos] = useState(false)
  const [activeRepo, setActiveRepo] = useState<ActiveRepo | null>(
    restored.activeRepo ?? null)
  const [selectedPr, setSelectedPr] = useState<PrRef | null>(
    initialRunId ? null : restored.selectedPr ?? null)
  const [discoveryEnabled, setDiscoveryEnabled] = useState(false)
  const prsRef = useRef<RepoPr[]>([])

  // --- Runs -----------------------------------------------------------------
  // One query drives the whole thread list. The interval is derived from the
  // data each render, so a run finishing drops the app back to the idle cadence
  // without any explicit teardown.
  // Seeded with the last payload, timestamped when it was actually fetched, so
  // the list paints instantly on reload and refetches straight away rather than
  // showing a skeleton for the round-trip. A live run's status is a few hundred
  // milliseconds out of date for that window — acceptable, and the progress bar
  // corrects itself on the first response.
  const runsSnapshot = useMemo(() => readSnapshot<RunsResponse>('runs'), [])
  const runsQuery = useQuery({
    queryKey: RUNS_KEY,
    queryFn: () => sageApi.runs(),
    initialData: runsSnapshot?.data,
    initialDataUpdatedAt: runsSnapshot?.at,
    refetchInterval: (q) =>
      (q.state.data?.runs ?? []).some((r) => r.status === 'running')
        ? LIVE_POLL_MS
        : IDLE_POLL_MS,
  })
  // Memoized so the `activeRun` lookup below (and every consumer that depends on
  // `runs` identity) doesn't see a fresh array on every render.
  const runs = useMemo(() => runsQuery.data?.runs ?? [], [runsQuery.data])
  const anyRunning = runs.some((r) => r.status === 'running')
  // The middle column is the SELECTED REPO's surface — its pull requests and its
  // reviews. The rail keeps the unfiltered list so a review is always reachable
  // regardless of which repo (if any) is in focus.
  const repoRuns = useMemo(() => {
    if (!activeRepo) return []
    const want = repoSlug(activeRepo).toLowerCase()
    return runs.filter((r) => repoOfRun(r) === want)
  }, [runs, activeRepo])
  // Keyed by change URL, not change_id: the id is the lossily-sanitized filename
  // stem, so two repos differing only by `-` vs `_` collapse to one id and an
  // unrelated PR would render as already under review (and be un-tickable).
  const reviewingChangeUrls = useMemo(() => {
    const urls = new Set<string>()
    for (const r of runs) {
      if (r.status !== 'running') continue
      for (const c of r.changes ?? []) urls.add(changeKey(c))
    }
    return urls
  }, [runs])
  const activeRun = useMemo(
    () => runs.find((r) => r.run_id === selectedRunId) ?? null,
    [runs, selectedRunId],
  )

  // Derived from `selectedRunId`, NOT from a fresh search over `runs`. The report
  // query is keyed by `selectedRunId`, so searching live runs for the PR's newest
  // review let this jump to a DIFFERENT run than the one on screen — another tab
  // finishing a newer review of the same PR moved `prRun` while the pane still
  // rendered the older report, and the actions hanging off `prRun` (cancel, post)
  // then targeted a run whose findings the user was not looking at. Scoped to the
  // selected PR so picking an unrelated run from the list yields null rather than
  // showing someone else's review here. `selectPr` and the run-started handler
  // both set `selectedRunId`, so the intended run still lands.
  const prRun = useMemo(() => {
    if (!selectedPr || !activeRun) return null
    return runCoversChange(activeRun, selectedPr.url) ? activeRun : null
  }, [activeRun, selectedPr])

  // Selecting a PR points the report query at that PR's review, so the pane can
  // show an in-flight run's progress or a finished run's findings without a
  // second query keyed differently.
  const selectPr = useCallback((pr: PrRef | null) => {
    setSelectedPr(pr)
    setComposing(false)
    setAddingRepos(false)
    if (!pr) return
    const run = runs.find((r) => runCoversChange(r, pr.url))
    setSelectedRunId(run?.run_id ?? null)
  }, [runs])

  // --- The selected thread's report ----------------------------------------
  // Polled while its run is live so the report appears the moment it is written;
  // a finished run's report never changes, so it stops polling entirely.
  // The report is the main content of the app, so it gets the same
  // stale-while-revalidate treatment as the lists: reopening a review you have
  // already read paints its findings immediately and refetches behind that,
  // instead of showing a skeleton for the round-trip.
  const reportSnapshot = useMemo(
    () => (selectedRunId ? readSnapshot<RunReport>(`report:${selectedRunId}`) : undefined),
    [selectedRunId],
  )
  const reportQuery = useQuery({
    queryKey: ['code-review-sage', 'report', selectedRunId],
    queryFn: () => sageApi.runReport(selectedRunId as string),
    enabled: !!selectedRunId,
    initialData: reportSnapshot?.data,
    initialDataUpdatedAt: reportSnapshot?.at,
    refetchInterval: () => (activeRun?.status === 'running' ? LIVE_POLL_MS : false),
  })

  // --- Persistence ----------------------------------------------------------
  // Snapshots are written from the query data rather than inside the query fn so
  // a cached (not refetched) render is not mistaken for a fresh fetch — the
  // timestamp has to reflect the SERVER response, or a stale entry would keep
  // renewing its own expiry.
  useEffect(() => {
    if (runsQuery.data && runsQuery.isSuccess && !runsQuery.isPlaceholderData) {
      writeSnapshot('runs', runsQuery.data)
    }
  }, [runsQuery.data, runsQuery.isSuccess, runsQuery.isPlaceholderData])

  useEffect(() => {
    // Only a READY report is worth replaying: a not-ready one describes a moment
    // in a run's life, and showing it again later would misreport a finished
    // review as still working.
    if (selectedRunId && reportQuery.data?.ready && reportQuery.isSuccess) {
      writeSnapshot(`report:${selectedRunId}`, reportQuery.data)
    }
  }, [selectedRunId, reportQuery.data, reportQuery.isSuccess])

  const invalidateRuns = useCallback(() => {
    void qc.invalidateQueries({ queryKey: RUNS_KEY })
  }, [qc])

  const selectRun = useCallback((runId: string | null) => {
    setSelectedRunId(runId)
    if (!runId) return
    setComposing(false)
    setAddingRepos(false)
    // A run over a SINGLE pull request is, to the user, that pull request — so
    // open it with its full context (description / comments / checks) rather
    // than a bare run view that drops everything about the PR. Prefer the loaded
    // list row (it has the title and author already); fall back to deriving a
    // reference from the change URL, whose missing fields the provider fetch
    // fills in. Multi-PR runs keep the run view: there is no single subject.
    const run = runs.find((r) => r.run_id === runId)
    const changes = run?.changes ?? []
    if (run && changes.length === 1) {
      const cid = run.change_ids?.[0] ?? changes[0]
      // Match on the change URL, not `change_id`: ids collapse across repos, so
      // an id-equality lookup can return a different PR that happens to share
      // one and open it instead of the run's actual subject, hiding the report
      // the user just asked for. The URL is the identity that does not collide.
      const want = changeKey(changes[0])
      const known = prsRef.current.find((p) => changeKey(p.url) === want)
      setSelectedPr(known ?? prRefFromChange(changes[0], cid))
    } else {
      setSelectedPr(null)
    }
  }, [runs])

  // --- Thread actions -------------------------------------------------------
  const cancelMut = useMutation({
    mutationFn: (runId: string) => sageApi.cancelRun(runId),
    onSuccess: invalidateRuns,
  })
  const deleteMut = useMutation({
    mutationFn: (runId: string) => sageApi.deleteRun(runId),
    onSuccess: (_d, runId) => {
      // Dropping the open thread must clear the selection, or the detail pane
      // would keep rendering a run that no longer exists.
      setSelectedRunId((cur) => (cur === runId ? null : cur))
      invalidateRuns()
    },
  })
  const postMut = useMutation({
    mutationFn: ({ runId, select }: {
      runId: string
      select?: { changeId: string; keys?: string[] }
    }) => sageApi.postComments(runId, select),
    // The run's posting/posted fields arrive through the runs poll, so the
    // button's state is driven by the server rather than local optimism.
    onSuccess: invalidateRuns,
  })

  const postGroupsMut = useMutation({
    mutationFn: ({ runId, groups }: {
      runId: string
      groups: { changeId: string; keys?: string[] }[]
    }) => sageApi.postCommentGroups(runId, groups),
    onSuccess: invalidateRuns,
  })

  const archiveMut = useMutation({
    mutationFn: (runId: string) => sageApi.archiveRun(runId),
    onSuccess: (_d, runId) => {
      invalidateRuns()
      void qc.invalidateQueries({ queryKey: ['code-review-sage', 'report', runId] })
    },
  })

  // --- Starting reviews -----------------------------------------------------
  // Each opens the new thread immediately: the user asked for a review, so the
  // useful next screen is that review's live progress.
  const onStarted = useCallback((runId: string | undefined) => {
    invalidateRuns()
    if (runId) {
      setComposing(false)
      setAddingRepos(false)
      setMainView('reviews')
      setExpanded('reviews')
      // Surface the run where it lives: the detail pane shows its progress, and
      // the middle column switches to the thread list so it is not hidden behind
      // the tab the user was just on.
      setListTab('reviews')
      setSelectedRunId(runId)
    }
  }, [invalidateRuns])

  const startReview = useMutation({
    mutationFn: (changes: string[]) => sageApi.review(changes),
    onSuccess: (d) => onStarted(d.run_id),
  })
  const startReviewLinks = useMutation({
    mutationFn: (links: string) => sageApi.reviewLinks(links),
    onSuccess: (d) => onStarted(d.run_id),
  })
  const startRepoReview = useMutation({
    mutationFn: ({ repo, force }: { repo: string; force: boolean }) =>
      sageApi.reviewRepo(repo, force),
    // A repo review can legitimately start nothing (every PR already reviewed);
    // that comes back as status "noop" with no run_id, so don't navigate.
    onSuccess: (d) => onStarted(d.run_id),
  })

  // --- Repos ----------------------------------------------------------------
  const pinnedSnapshot = useMemo(() => readSnapshot<{ repos: PinnedRepo[] }>('repos'), [])
  const pinnedQuery = useQuery({
    queryKey: ['code-review-sage', 'repos'],
    queryFn: () => sageApi.pinnedRepos(),
    initialData: pinnedSnapshot?.data,
    initialDataUpdatedAt: pinnedSnapshot?.at,
  })
  const recentQuery = useQuery({
    queryKey: ['code-review-sage', 'recent-repos'],
    queryFn: () => sageApi.recentRepos(),
    // A live `gh` call per mount would be rude; the picker turns it on.
    enabled: discoveryEnabled,
    staleTime: 5 * 60_000,
  })

  const mineQuery = useQuery({
    queryKey: ['code-review-sage', 'my-repos'],
    queryFn: () => sageApi.myRepos(),
    // Same reasoning as recent-repos: a live `gh` call, so opt in rather than
    // firing on every mount of the app.
    enabled: discoveryEnabled,
    staleTime: 5 * 60_000,
  })

  useEffect(() => {
    if (pinnedQuery.data && pinnedQuery.isSuccess) {
      writeSnapshot('repos', pinnedQuery.data)
    }
  }, [pinnedQuery.data, pinnedQuery.isSuccess])

  const invalidateRepos = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ['code-review-sage', 'repos'] })
    void qc.invalidateQueries({ queryKey: ['code-review-sage', 'recent-repos'] })
    void qc.invalidateQueries({ queryKey: ['code-review-sage', 'my-repos'] })
  }, [qc])

  const pinMut = useMutation({
    mutationFn: ({ owner, repo }: ActiveRepo) => sageApi.pinRepo(owner, repo),
    onSuccess: (_d, v) => { invalidateRepos(); setActiveRepo(v) },
  })
  const pinUrlMut = useMutation({
    mutationFn: (url: string) => sageApi.pinRepoUrl(url),
    onSuccess: (d) => {
      invalidateRepos()
      const added = d.added ?? d.repos?.[0]
      if (added) setActiveRepo({ owner: added.owner, repo: added.repo })
      // A pasted PULL REQUEST link means the user wants that pull request, not a
      // repo they now have to find it in — so open it.
      if (d.pull_request) {
        setMainView('reviews')
        setListTab('pulls')
        setAddingRepos(false)
        selectPr({
          url: d.pull_request.url,
          change_id: d.pull_request.change_id,
          number: d.pull_request.number,
        })
      }
    },
  })
  const unpinMut = useMutation({
    mutationFn: ({ owner, repo }: ActiveRepo) => sageApi.unpinRepo(owner, repo),
    onSuccess: (_d, v) => {
      invalidateRepos()
      setActiveRepo((cur) =>
        cur && cur.owner === v.owner && cur.repo === v.repo ? null : cur)
    },
  })

  // --- PRs for the active repo ---------------------------------------------
  // Keyed per repo: switching back to a repo you were just looking at shows its
  // PRs immediately instead of re-running a `gh` call you already paid for.
  const prsKey = activeRepo ? `prs:${repoSlug(activeRepo).toLowerCase()}` : ''
  const prsSnapshot = prsKey ? readSnapshot<RepoPrsResponse>(prsKey) : undefined
  const prsQuery = useQuery({
    queryKey: ['code-review-sage', 'repo-prs',
      activeRepo ? repoSlug(activeRepo) : ''],
    queryFn: () => sageApi.repoPrs(repoUrl(activeRepo as ActiveRepo)),
    enabled: !!activeRepo,
    initialData: prsSnapshot?.data,
    initialDataUpdatedAt: prsSnapshot?.at,
    // Reviewed/stale annotations go stale as runs finish, but a `gh` call per
    // focus change is expensive — a minute is a fair compromise, and finishing a
    // run invalidates this key explicitly (see below).
    staleTime: 60_000,
  })

  useEffect(() => {
    if (prsKey && prsQuery.data && prsQuery.isSuccess) {
      writeSnapshot(prsKey, prsQuery.data)
    }
  }, [prsKey, prsQuery.data, prsQuery.isSuccess])

  // A restored repo that has since been unpinned would leave the column stuck
  // on a repo the rail no longer lists, so drop it once the real list arrives.
  useEffect(() => {
    const list = pinnedQuery.data?.repos
    if (!activeRepo || !list) return
    const known = list.some((r) => r.owner === activeRepo.owner && r.repo === activeRepo.repo)
    if (!known) setActiveRepo(null)
  }, [pinnedQuery.data, activeRepo])

  // Same for a restored review: runs are evicted past a cap, so the id may no
  // longer exist. Clearing it avoids a detail pane waiting on a 404 forever.
  useEffect(() => {
    if (!selectedRunId || !runsQuery.isSuccess) return
    if (!runs.some((r) => r.run_id === selectedRunId)) {
      setSelectedRunId(null)
      setSelectedPr(null)
    }
  }, [runs, runsQuery.isSuccess, selectedRunId])

  // Persist where the user is, on every change. Cheap (one small JSON write) and
  // it means a reload — or a trip to another Kiro Crew page — comes back here.
  useEffect(() => {
    saveUiState({
      mainView,
      listTab,
      activeRepo: activeRepo ? { owner: activeRepo.owner, repo: activeRepo.repo } : null,
      selectedRunId,
      selectedPr,
      detailTab: null,
    })
  }, [mainView, listTab, activeRepo, selectedRunId, selectedPr])

  // Read inside selectRun without adding the polled list to its deps.
  prsRef.current = prsQuery.data?.prs ?? []

  const value: SageContextValue = {
    runs,
    runsLoading: runsQuery.isLoading,
    runsError: (runsQuery.error as Error) ?? null,
    pool: runsQuery.data?.pool ?? null,
    reviewer: runsQuery.data?.reviewer ?? null,
    anyRunning,
    repoRuns,
    reviewingChangeUrls,
    selectedRunId,
    selectRun,
    activeRun,

    report: reportQuery.data ?? null,
    reportLoading: reportQuery.isLoading,
    reportError: (reportQuery.error as Error) ?? null,

    cancelRun: (runId: string) => cancelMut.mutate(runId),
    cancelling: cancelMut.isPending,
    deleteRun: (runId: string) => deleteMut.mutate(runId),
    deleting: deleteMut.isPending,
    archiveRun: (runId: string) => archiveMut.mutate(runId),
    archiving: archiveMut.isPending,
    archiveError: (archiveMut.error as Error) ?? null,
    postComments: (runId, select) => postMut.mutate({ runId, select }),
    postCommentGroups: async (runId, groups) => {
      // ONE request, not one per group: `posting` is a per-run flag that only
      // the poster clears, and this endpoint returns as soon as it dispatches
      // the poster. Sending group 2 as a second request — even strictly after
      // group 1 resolved — got `already_posting`, so the comments chosen on
      // every change after the first were never published.
      await postGroupsMut.mutateAsync({ runId, groups })
    },
    posting: postMut.isPending || postGroupsMut.isPending,
    // WHICH comments are in flight, so a per-finding post marks only the card
    // that was clicked. A single boolean fanned out to every unposted card, so
    // sending one finding read as "the whole review is being published" — a
    // misleading claim about an external write. `null` means the whole review.
    postingSelection: postMut.isPending
      ? (postMut.variables?.select ?? null)
      : undefined,
    postError: (postMut.error as Error) ?? (postGroupsMut.error as Error) ?? null,

    startReview,
    startReviewLinks,
    startRepoReview,

    pinnedRepos: pinnedQuery.data?.repos ?? [],
    pinnedLoading: pinnedQuery.isLoading,
    recent: recentQuery.data ?? null,
    recentLoading: recentQuery.isLoading,
    recentError: (recentQuery.error as Error) ?? null,
    discoveryEnabled,
    enableDiscovery: () => setDiscoveryEnabled(true),
    mine: mineQuery.data ?? null,
    mineLoading: discoveryEnabled && mineQuery.isLoading,
    mineError: (mineQuery.error as Error) ?? null,
    refreshMine: () => {
      void mineQuery.refetch()
      void recentQuery.refetch()
    },
    pinRepo: (owner: string, repo: string) => pinMut.mutate({ owner, repo }),
    pinRepoUrl: (url: string) => pinUrlMut.mutate(url),
    unpinRepo: (owner: string, repo: string) => unpinMut.mutate({ owner, repo }),
    pinError: ((pinMut.error ?? pinUrlMut.error ?? unpinMut.error) as Error) ?? null,

    activeRepo,
    setActiveRepo,
    selectedPr,
    selectPr,
    prRun,
    prs: prsQuery.data?.prs ?? [],
    prsLoading: prsQuery.isLoading,
    prsError: (prsQuery.error as Error) ?? null,
    refreshPrs: () => { void prsQuery.refetch() },

    mainView,
    setMainView,
    selectedNamespace,
    selectNamespace,
    listTab,
    setListTab,
    expanded,
    setExpanded,
    composing,
    openComposer: () => {
      setComposing(true)
      setAddingRepos(false)
      setSelectedRunId(null)
    },
    closeComposer: () => setComposing(false),
    addingRepos,
    // Opening the picker arms discovery: landing on a repo list that shows
    // nothing until you click a second button is worse than the one-time call.
    // It deliberately does NOT touch the view or the selected run — it overlays
    // the workspace, so throwing away what the user was looking at would be
    // surprising when they dismiss it.
    openAddRepos: () => {
      setDiscoveryEnabled(true)
      setAddingRepos(true)
    },
    closeAddRepos: () => setAddingRepos(false),
  }

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
