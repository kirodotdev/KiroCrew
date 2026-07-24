// Issue Radar entry point — thin bootstrap only.
//
// Resolves the connected-repo list (GET /repos), shows the WelcomeCarousel when
// there are none (or the user is adding one), otherwise picks the active repo
// and hands off to <Workspace> wrapped in <IssueRadarProvider>. All UI state
// and data fetching live in context.tsx; the layout lives in Workspace.tsx.
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { issueRadarApi } from './api'
import { loadActiveRepo, saveActiveRepo } from './lib/format'
import type { ActiveRepo } from './lib/types'
import { IssueRadarProvider } from './context'
import Workspace from './Workspace'
import WelcomeCarousel from './WelcomeCarousel'

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
    queryClient.invalidateQueries({ queryKey: ['issue-radar', 'repos'] })
  }

  if (reposQuery.isLoading) {
    return <div className="flex h-full items-center justify-center text-muted text-xs">Loading…</div>
  }

  if (repos.length === 0 || connectingNew) {
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
      <Workspace />
    </IssueRadarProvider>
  )
}
