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
import { sameRepoRef } from './lib/links'
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

  // Resolve the active repository to the CONNECTED RECORD, on full identity.
  //
  // Two things used to go wrong here, and both produced a ref with no forge on it.
  // The fallback arm built `{owner, repo}` from `repos[0]` and discarded that
  // record's provider and host; and the membership test compared owner and repo
  // alone, so a STORED slug-only pointer satisfied it and was handed back
  // unenriched — `loadActiveRepo` accepts one deliberately, because a value
  // persisted before GitLab support has no forge and rejecting it would drop the
  // user's repository on upgrade. So the legacy pointer was never healed even
  // though the record standing beside it carried the missing half.
  //
  // A forge-less ref is not merely incomplete, it reads as a DIFFERENT repository:
  // `repoScopeKey` resolves an absent provider/host to public GitHub, so every
  // surface keying a cache on `active` filed a GitLab or Azure repository's issues,
  // labels and settings under GitHub's key, and every request that took the ref
  // omitted the provider the backend needs to answer for the right forge.
  //
  // Returning the record itself fixes both arms at once: whatever identity the
  // match was made on, what goes down is what the connect flow actually stored.
  // The slug match is also gone — `sameRepoRef` compares the forge too, so on a
  // mixed install a stored pointer can no longer resolve to the same slug on the
  // wrong provider.
  //
  // Matching on identity ALONE is deliberate, and a slug fallback for the
  // forge-less case was tried and reverted. A pointer with no provider/host is
  // not a pointer of unknown forge: `repoScopeKey` resolves absent fields to
  // public GitHub, so it NAMES github.com/owner/repo, which is why
  // `sameRepoRef` pairs it with its GitHub record and why the predicate's own
  // test refuses to pair it with a GitLab one. Resolving it to a same-slug
  // record on another forge would therefore reassign the user's repository to a
  // forge they never chose -- the exact "a slug is not an identity" error this
  // fix exists to remove, reintroduced one layer up. When no GitHub record is
  // connected the stored repository simply is not connected, so it falls back
  // like any other missing one.
  const connected = (active && repos.find((r) => sameRepoRef(r, active))) || repos[0]
  const resolved: ActiveRepo = {
    owner: connected.owner,
    repo: connected.repo,
    ...(connected.provider ? { provider: connected.provider } : {}),
    ...(connected.host ? { host: connected.host } : {}),
  }

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
