// Provider + hook for the writing-review page. Owns:
//  * the selected review id (URL-anchored on mount, then uncontrolled)
//  * the "new review dialog is open" flag
//  * the active job id (during a scan) so ScanProgress can poll it
//
// All server state lives in TanStack Query cached under
// ``['writing-review', ...]``; components read via the ``useWritingReview``
// hook and mutate through the API client directly.
import type { ReactNode } from 'react'
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { i18next } from '../../i18n/index'
import { writingReviewApi } from './api'
import type { ReviewDetail, ReviewsListResponse, Settings } from './lib/types'
import {
  readActiveScanMirror,
  writeActiveScanMirror,
} from './lib/activeScanMirror'

export interface WritingReviewContextValue {
  selectedReviewId: string | null
  selectReview: (reviewId: string | null) => void

  newReviewDialogOpen: boolean
  openNewReviewDialog: () => void
  closeNewReviewDialog: () => void

  settingsDialogOpen: boolean
  openSettingsDialog: () => void
  closeSettingsDialog: () => void

  activeJobId: string | null
  setActiveJobId: (jobId: string | null) => void

  activeJobDocName: string | null
  setActiveJobDocName: (docName: string | null) => void

  // Live phase of the in-flight scan job, or null when no scan is running.
  // Written by ScanProgress from its polling query so both the main pane
  // and the sidebar in-progress card can render the same phase label at
  // the same time without either component owning its own poll.
  activeJobPhase: string | null
  setActiveJobPhase: (phase: string | null) => void

  reviewsQuery: ReturnType<typeof useReviewsQuery>
  reviewDetailQuery: ReturnType<typeof useReviewDetailQuery>
  settingsQuery: ReturnType<typeof useSettingsQuery>
}

const WritingReviewContext = createContext<WritingReviewContextValue | undefined>(undefined)

function useReviewsQuery() {
  return useQuery<ReviewsListResponse>({
    queryKey: ['writing-review', 'reviews'],
    queryFn: () => writingReviewApi.listReviews(),
  })
}

function useReviewDetailQuery(reviewId: string | null) {
  return useQuery<ReviewDetail>({
    queryKey: ['writing-review', 'review', reviewId],
    queryFn: () => {
      if (reviewId === null) throw new Error('no review selected')
      return writingReviewApi.getReview(reviewId)
    },
    enabled: reviewId !== null,
  })
}

function useSettingsQuery() {
  return useQuery<Settings>({
    queryKey: ['writing-review', 'settings'],
    queryFn: () => writingReviewApi.getSettings(),
  })
}

export interface WritingReviewProviderProps {
  initialReviewId?: string | null
  children: ReactNode
}

export function WritingReviewProvider({
  initialReviewId = null,
  children,
}: WritingReviewProviderProps) {
  const [selectedReviewId, setSelectedReviewId] = useState<string | null>(initialReviewId)
  const [newReviewDialogOpen, setNewReviewDialogOpen] = useState<boolean>(false)
  const [settingsDialogOpen, setSettingsDialogOpen] = useState<boolean>(false)

  // Language reactivity bridge. ``LanguageProvider``'s ``cloneElement``
  // re-render only defeats React's referential-equality bailout at
  // its DIRECT child, not deep into the tree: ``WritingReviewPage``
  // is a stable element ref inside a ``<Route>``, so a language
  // change alone never re-renders our subtree. Subscribing to
  // ``i18next.languageChanged`` here bumps a state value AFTER the
  // catalog has actually swapped, and including it in the
  // ``contextValue`` memo deps below forces a new context value,
  // which re-renders every ``useWritingReview()`` consumer -- context
  // propagation explicitly bypasses React's memo bailouts.
  //
  // Why the ``languageChanged`` event and not ``useLanguage().resolved``:
  // ``resolved`` changes synchronously when the user picks a language,
  // BEFORE ``i18next.changeLanguage`` completes and the catalog is
  // available. Reacting to ``resolved`` fires the consumer re-render
  // in the tiny window where ``i18nT`` still returns the OLD catalog,
  // then never fires again -- because on the SECOND
  // LanguageProvider re-render (post-swap) ``resolved`` is stable.
  // ``languageChanged`` is emitted by i18next AFTER the swap
  // completes, which is the only ordering that renders the new
  // strings. Same ordering rationale that ``LanguageProvider``'s
  // internal ``active`` state uses.
  //
  // App-scoped fix (Option 4 from the diagnosis). The wider codebase
  // issue -- ~600 ``i18nT()`` sites subscribe to nothing and rely on
  // the ``cloneElement`` mechanism that bails out below stable JSX
  // refs -- is out of scope here.
  const [renderedI18nLanguage, setRenderedI18nLanguage] = useState<string>(
    () => i18next.language || '',
  )
  useEffect(() => {
    const handleLanguageChanged = (nextLanguage: string) => {
      setRenderedI18nLanguage(nextLanguage)
    }
    i18next.on('languageChanged', handleLanguageChanged)
    // i18next may have switched between initial render and this
    // subscription; sync once here so the initial state is not stale.
    if (i18next.language && i18next.language !== renderedI18nLanguage) {
      setRenderedI18nLanguage(i18next.language)
    }
    return () => {
      i18next.off('languageChanged', handleLanguageChanged)
    }
    // ``renderedI18nLanguage`` intentionally excluded: the sync inside
    // is a one-shot catch-up on mount, not a reactive dependency; the
    // event listener itself only needs to be attached once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  // Hydrate the active-scan handle from the sessionStorage mirror on
  // first render. Same-tab remounts (route changes -- e.g. navigating
  // to Settings to switch theme, then back) restore the in-flight scan
  // instantly here rather than waiting on the backend fetch below.
  // ``useState`` initializer runs exactly once, so a stale mirror
  // entry from a completed scan gets overwritten by the fetch's truth
  // check without a flicker. Empty mirror is the fresh-tab case:
  // ``persistedActiveScanFromMirror.jobId`` is null and the
  // ``useState`` starts at null just as it did before this fix.
  const persistedActiveScanFromMirror = readActiveScanMirror()
  const [activeJobId, setActiveJobId] = useState<string | null>(
    persistedActiveScanFromMirror.jobId,
  )
  const [activeJobDocName, setActiveJobDocName] = useState<string | null>(
    persistedActiveScanFromMirror.docName,
  )
  const [activeJobPhase, setActiveJobPhase] = useState<string | null>(
    persistedActiveScanFromMirror.phase,
  )

  // Mirror active-scan handle changes to sessionStorage. Every state
  // update flows through this effect (React commits state before firing
  // effects), so the mirror is always in step with what ScanProgress /
  // ReviewList render. On terminal states (all three fields cleared to
  // null), ``writeActiveScanMirror`` also removes the storage entry so
  // the next mount starts clean.
  useEffect(() => {
    writeActiveScanMirror({
      jobId: activeJobId,
      docName: activeJobDocName,
      phase: activeJobPhase,
    })
  }, [activeJobId, activeJobDocName, activeJobPhase])

  // Rehydrate in-flight scan state from the backend on mount. This is
  // the survives-hard-refresh path -- the backend persists ``_JOBS`` to
  // ``jobs.json`` on every state change, and any job left ``running``
  // when the gateway restarted is downgraded to ``interrupted`` at load
  // time. A refresh mid-scan therefore either finds an active running
  // job (rehydrate + resume polling) or an interrupted one (no-op --
  // the completed review record shows in the sidebar once ready).
  //
  // Complements the sessionStorage mirror seeded above: the mirror
  // handles same-tab remounts instantly (theme change, in-app
  // navigation); this fetch handles cross-tab reopens and full
  // reloads. If the mirror seeded a job the backend no longer
  // recognises (pruned after ~1h, or lost on gateway restart), the
  // fetch clears the stale mirror entry via ``clearActiveScanMirror``
  // so a phantom scan card cannot linger on screen.
  useEffect(() => {
    let cancelled = false
    // Capture the mirror-seeded job at effect-fire time. We need this
    // to reconcile: if the backend says no running jobs but the mirror
    // seeded one, the mirror entry is stale (job completed while the
    // tab was away, or was pruned) and must be cleared.
    const mirrorSeededJobIdAtMountTime = persistedActiveScanFromMirror.jobId
    void (async () => {
      try {
        const { jobs } = await writingReviewApi.listJobs('running')
        if (cancelled) return
        if (jobs.length === 0) {
          // Backend has no running jobs. If we seeded from the mirror
          // above, the seed is stale -- clear it so ScanProgress does
          // not render a phantom scan card that never resolves.
          if (mirrorSeededJobIdAtMountTime !== null) {
            setActiveJobId(null)
            setActiveJobDocName(null)
            setActiveJobPhase(null)
            // The mirror-write effect above will call
            // ``writeActiveScanMirror`` with all-null fields, which
            // clears the storage entry. No explicit clear call needed.
          }
          return
        }
        // Backend contract (backend/routes.py::handle_list_jobs): the jobs
        // array is already sorted newest-first by ``updated_at`` before it
        // reaches us, so ``jobs[0]`` is the most recent. Sort here anyway
        // as belt-and-braces — a future backend refactor that changed the
        // ordering would silently pick the wrong job to rehydrate without
        // this defensive sort.
        const sortedJobs = [...jobs].sort(
          (jobA, jobB) => (jobB.updated_at ?? 0) - (jobA.updated_at ?? 0),
        )
        const mostRecent = sortedJobs[0]
        setActiveJobId(mostRecent.id)
        setActiveJobDocName(mostRecent.doc_name ?? null)
        setActiveJobPhase(mostRecent.phase ?? null)
      } catch {
        // A backend that's unreachable at mount is not fatal here --
        // subsequent user actions will surface the error through their
        // own mutation error handlers. The mirror-seeded state (if
        // any) stays as the best-effort placeholder until the next
        // ScanProgress poll or a manual refresh; that beats blanking
        // it on transient network trouble.
      }
    })()
    return () => {
      cancelled = true
    }
    // Empty deps: run once on mount only. State setters are stable
    // (React guarantee) and hard-refresh recovery only cares about the
    // moment the provider first mounts. ``persistedActiveScanFromMirror``
    // is captured by value at first render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const selectReview = useCallback((reviewId: string | null) => {
    setSelectedReviewId(reviewId)
  }, [])

  const openNewReviewDialog = useCallback(() => setNewReviewDialogOpen(true), [])
  const closeNewReviewDialog = useCallback(() => setNewReviewDialogOpen(false), [])

  const openSettingsDialog = useCallback(() => setSettingsDialogOpen(true), [])
  const closeSettingsDialog = useCallback(() => setSettingsDialogOpen(false), [])

  const reviewsQuery = useReviewsQuery()
  const reviewDetailQuery = useReviewDetailQuery(selectedReviewId)
  const settingsQuery = useSettingsQuery()

  const contextValue = useMemo<WritingReviewContextValue>(
    () => ({
      selectedReviewId,
      selectReview,
      newReviewDialogOpen,
      openNewReviewDialog,
      closeNewReviewDialog,
      settingsDialogOpen,
      openSettingsDialog,
      closeSettingsDialog,
      activeJobId,
      setActiveJobId,
      activeJobDocName,
      setActiveJobDocName,
      activeJobPhase,
      setActiveJobPhase,
      reviewsQuery,
      reviewDetailQuery,
      settingsQuery,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- ``renderedI18nLanguage`` is intentionally in the deps to force a new context value after i18next.languageChanged fires. Not a data dependency of the memoised object -- purely a bailout defeat so every ``useWritingReview()`` consumer re-renders and re-resolves its ``i18nT()`` calls against the new catalog. See the ``renderedI18nLanguage`` state block near the top of this component.
    [
      selectedReviewId,
      selectReview,
      newReviewDialogOpen,
      openNewReviewDialog,
      closeNewReviewDialog,
      settingsDialogOpen,
      openSettingsDialog,
      closeSettingsDialog,
      activeJobId,
      activeJobDocName,
      activeJobPhase,
      reviewsQuery,
      reviewDetailQuery,
      settingsQuery,
      // Force a fresh context value AFTER a language catalog swap so
      // every ``useWritingReview()`` consumer re-renders and its
      // ``i18nT()`` calls re-evaluate against the new catalog.
      // Sourced from ``i18next.languageChanged`` (fires post-swap)
      // rather than ``useLanguage().resolved`` (fires pre-swap, before
      // the catalog is loaded). Not a data dependency of the object
      // above; included purely to defeat React's memo bailout on
      // language switch. See the subscription block near the top of
      // this component for the full rationale.
      renderedI18nLanguage,
    ],
  )

  return (
    <WritingReviewContext.Provider value={contextValue}>
      {children}
    </WritingReviewContext.Provider>
  )
}

export function useWritingReview(): WritingReviewContextValue {
  const contextValue = useContext(WritingReviewContext)
  if (contextValue === undefined) {
    throw new Error('useWritingReview must be used inside a WritingReviewProvider')
  }
  return contextValue
}
