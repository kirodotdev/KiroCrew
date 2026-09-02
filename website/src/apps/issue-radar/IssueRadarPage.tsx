// Issue Radar entry point — thin bootstrap only.
//
// Resolves the connected-repo list (GET /repos), shows the WelcomeCarousel when
// there are none (or the user is adding one), otherwise picks the active repo
// and hands off to <Workspace> wrapped in <IssueRadarProvider>. All UI state
// and data fetching live in context.tsx; the layout lives in Workspace.tsx.
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { queryClient } from '../../api/queryClient'
import { issueRadarApi } from './api'
import {
  CACHE_RETENTION_MS, loadActiveRepo, markAutoSelectFirstIssue, patchUiState, saveActiveRepo,
} from './lib/format'
import type { ActiveRepo } from './lib/types'
import { IssueRadarProvider } from './context'
import Workspace from './Workspace'
import RefSheet from './components/RefSheet'
import WelcomeCarousel from './WelcomeCarousel'
import ConnectRepoModal from './ConnectRepoModal'

import { i18nT } from '../../i18n/t'
// Keep every Issue Radar query's data resident long enough to survive moving between
// surfaces, set ONCE for the whole `['issue-radar', ...]` key space rather than repeated
// across ~20 call sites (a per-site option is one a new query silently forgets).
//
// The problem it fixes: each dashboard mounts its own queries and unmounts them on the way
// out, because the views are SWAPPED not hidden (`views/registry.tsx`). Data for an
// unmounted query lives only `gcTime` longer, and the app-wide default is react-query's 5
// minutes, which is shorter than an ordinary triage session. Leave Tagging for six minutes
// and its queue has been evicted, so returning shows a loading line and refetches
// everything, once per tab click.
//
// Retention is not freshness: `staleTime` and the poll intervals still decide when a
// refetch happens, so this only changes whether there is something to paint WHILE that
// refetch runs. Module scope so it is applied before the first child query mounts.
queryClient.setQueryDefaults(['issue-radar'], { gcTime: CACHE_RETENTION_MS })

export default function IssueRadarPage() {
  const queryClient = useQueryClient()
  const [active, setActive] = useState<ActiveRepo | null>(loadActiveRepo)
  const [connectingNew, setConnectingNew] = useState(false)

  const reposQuery = useQuery({
    queryKey: ['issue-radar', 'repos'],
    queryFn: () => issueRadarApi.repos(),
  })

  const repos = reposQuery.data?.repos ?? []

  const onConnected = (repo: ActiveRepo) => {
    saveActiveRepo(repo)
    setActive(repo)
    setConnectingNew(false)
    // Land on the issue list, not wherever the user happened to be (typically
    // Settings, since that's where "Connect repo" lives), showing OPEN issues
    // so the auto-selected first issue is an open one. On first run the
    // provider isn't mounted yet, so the intent is persisted for it to restore;
    // when it IS already mounted, ConnectRepoModal switches the view live
    // through the context.
    patchUiState({
      mainView: 'issues',
      stateFilter: 'open',
      selectedIssue: null,
      // Filters from a previous session would otherwise apply to the new repo
      // and can hide every issue in it.
      query: '',
      selectedLabels: [],
      requestedByMe: false,
      assignedToMe: false,
      createdByMember: false,
      // The PR side needs the same reset, and for a sharper reason than the
      // issue side: `selectedPull` is a NUMBER, so a leftover #42 silently
      // auto-opens the new repo's unrelated #42. Mirrors `switchRepo`.
      selectedPull: null,
      prQuery: '',
      prSelectedLabels: [],
      prAuthoredByMe: false,
      prAssignedToMe: false,
      prReviewRequestedByMe: false,
      prDraftOnly: false,
      prCreatedByMember: false,
    })
    // Open the first issue once the list resolves (consumed by the provider,
    // but only once THIS repo is the active one — see markAutoSelectFirstIssue).
    markAutoSelectFirstIssue(repo)
    queryClient.invalidateQueries({ queryKey: ['issue-radar', 'repos'] })
  }

  if (reposQuery.isLoading) {
    return <div className="flex h-full items-center justify-center text-muted text-xs">{i18nT('apps.issueRadar.issueRadarPage.loading')}</div>
  }

  // First run (no repos yet): the full-screen onboarding carousel. Adding
  // ANOTHER repo when some already exist instead overlays a modal on the
  // current view (see connectingNew below), so the workspace/settings page
  // stays put behind a blurred backdrop.
  if (repos.length === 0) {
    return <WelcomeCarousel onConnected={onConnected} />
  }

  const resolved = active && repos.some((r) => r.owner === active.owner && r.repo === active.repo)
    ? active
    : { owner: repos[0].owner, repo: repos[0].repo }

  return (
    <IssueRadarProvider
      repos={repos}
      active={resolved}
      onSwitch={(r) => { saveActiveRepo(r); setActive(r) }}
      onAddRepo={() => setConnectingNew(true)}
    >
      <div className="relative h-full">
        <Workspace />
        <RefSheet />
        {connectingNew && (
          <ConnectRepoModal onConnected={onConnected} onClose={() => setConnectingNew(false)} />
        )}
      </div>
    </IssueRadarProvider>
  )
}
