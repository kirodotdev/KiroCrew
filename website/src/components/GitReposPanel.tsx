import { useCallback, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '../api/client'
import GitPanel from './GitPanel'
import ErrorNotice from './ErrorNotice'
import { Btn } from './ui'
import { errMessage } from '../utils/thunkError'
import { findReport } from '../utils/errorReport'
import { i18nT } from '../i18n/t'
import { fmtNumber } from '../i18n/format'

interface GitReposPanelProps {
  /** The chat whose repositories are listed. */
  slotKey: string
  onFileOpen?: (path: string) => void
}

/** How many open sections poll their status on the interval at once. */
export const MAX_POLLING_SECTIONS = 3

/**
 * The Git tab: one collapsible section per repository this chat works in.
 *
 * Most chats start in the shared workspace, which is not a repository, and then
 * edit files or run commands in repositories elsewhere. The gateway notices
 * those from the chat's own tool calls (`GET /api/project/git/repos`), so the
 * tab lists them without the user picking a project folder first. The chat's
 * project, when it is in a repository, comes first.
 */
export default function GitReposPanel({ slotKey, onFileOpen }: GitReposPanelProps) {
  const { data, error, isLoading, isFetching, refetch } = useQuery({
    queryKey: ['git-repos', slotKey],
    queryFn: () => api.projectGitRepos(slotKey),
    enabled: !!slotKey,
    // A repository joins the list mid-turn, as soon as the agent touches it.
    refetchInterval: 10_000,
    refetchOnWindowFocus: true,
    retry: 1,
  })
  const repos = data?.repos ?? []
  const omitted = data?.omitted ?? 0
  const listed = data?.listed ?? 0
  const hasProject = repos.some(repo => repo.source === 'project')
  // Which sections are open, as each one reports it (a section's open state is
  // partly derived from its own status query, so it cannot be computed here).
  // The "Show changes and history" hint is drawn on the FIRST collapsed
  // section only: the rows are alike, so one hint reads for all of them, and
  // twenty-four of them made the resting state the loudest thing in the panel.
  const [openByPath, setOpenByPath] = useState<Record<string, boolean>>({})
  // Open sections in the order they opened, newest last. Only the newest
  // MAX_POLLING_SECTIONS poll their status: the repositories the agent edited
  // open by themselves, so without a cap each of up to twenty-four dirty
  // sections would run several git subprocesses every 5 s. A section the user
  // opens becomes the newest, so the one they are looking at always polls.
  const [openOrder, setOpenOrder] = useState<string[]>([])
  const handleOpenChange = useCallback((path: string, open: boolean) => {
    setOpenByPath(prev => (prev[path] === open ? prev : { ...prev, [path]: open }))
    setOpenOrder(prev => {
      const rest = prev.filter(p => p !== path)
      if (!open) return rest.length === prev.length ? prev : rest
      return prev[prev.length - 1] === path ? prev : [...rest, path]
    })
  }, [])
  const listedPaths = new Set(repos.map(repo => repo.path))
  const pollingPaths = new Set(
    openOrder.filter(path => listedPaths.has(path)).slice(-MAX_POLLING_SECTIONS),
  )
  const firstCollapsedPath = repos.find(repo => openByPath[repo.path] === false)?.path
  // React Query keeps the last good `data` when a background refetch fails
  // (a gateway restart mid-session, say) and sets `error` beside it. The list
  // is still the one the chat worked in, so it stays mounted -- unmounting it
  // would reset every section's open state and refetch every status -- and the
  // failure is a notice above it. Only a listing that never loaded is the
  // whole panel's error.
  const hasListing = data !== undefined
  // The server's sentence is English whatever the user's language, so the
  // visible message is always the catalog's. The raw text is kept only as the
  // journal lookup key: the hand-off recovers endpoint, status and code from
  // it, which a localized message cannot match.
  const serverMessage = errMessage(error)

  return (
    <div className="flex flex-col h-full min-h-0 overflow-y-auto" data-testid="git-repos-panel">
      {error ? (
        <div className="p-3">
          <ErrorNotice
            // A first load that never produced a listing is the whole panel's
            // failure ("couldn't list"). A background refresh that failed while
            // the previous list stays mounted below must not say the same: the
            // reader can see the repositories right there, so the notice names
            // the refresh and warns the shown list may be stale instead.
            message={i18nT(
              hasListing
                ? 'components.gitPanel.repos_refresh_failed'
                : 'components.gitPanel.repos_failed',
            )}
            report={findReport(serverMessage)}
            askAgent
            actionPlacement="below"
            testId="git-repos-error"
            // The listing is a plain read with nothing to lose, so a retry sits
            // right in the notice (the same footer shape KiroPrerequisiteGate
            // uses), beside the hand-off: a transient failure should not need
            // the agent's help to clear.
            footer={
              <Btn
                type="button"
                disabled={isFetching}
                onClick={() => { void refetch() }}
                data-testid="git-repos-retry"
              >
                {i18nT('components.gitPanel.repos_retry')}
              </Btn>
            }
          />
        </div>
      ) : null}
      {!hasListing ? (
        // The first listing takes a round trip; an empty tab meanwhile reads as
        // "nothing here", which is the empty state's claim, not a wait. A first
        // listing that failed has its notice above and nothing to list.
        isLoading ? (
          <div
            role="status"
            aria-busy="true"
            className="flex items-center justify-center gap-2 px-6 pt-8 text-muted text-[13px]"
            data-testid="git-repos-loading"
          >
            <Loader2 size={14} className="animate-spin shrink-0" aria-hidden="true" />
            <span>{i18nT('components.gitPanel.repos_loading')}</span>
          </div>
        ) : error ? null : (
          <div role="status" className="px-6 pt-8 text-center text-muted text-[13px]">
            {i18nT('components.gitPanel.repos_empty')}
          </div>
        )
      ) : repos.length === 0 ? (
        <div role="status" className="px-6 pt-8 text-center text-muted text-[13px]">
          {i18nT('components.gitPanel.repos_empty')}
        </div>
      ) : (
        <>
          {/* What the rows are, visible without hovering the Project tag: one
              caption names where every row came from, and a second sentence is
              added only when the chat's own project is among them, to say it
              leads the list. */}
          <p
            className="px-3 pt-2 pb-1 text-muted text-[12px] leading-snug shrink-0"
            data-testid="git-repos-caption"
          >
            {i18nT('components.gitPanel.repos_caption')}
            {hasProject ? <> {i18nT('components.gitPanel.repos_caption_project_first')}</> : null}
          </p>
          {repos.map((repo, index) => (
            <GitPanel
              key={repo.path}
              projectDir={repo.path}
              onFileOpen={onFileOpen}
              onOpenChange={handleOpenChange}
              section={{
                label: repo.path,
                badge: repo.source === 'project' ? i18nT('components.gitPanel.source_project') : undefined,
                badgeTitle: repo.source === 'project' ? i18nT('components.gitPanel.source_project_help') : undefined,
                defaultOpen: index === 0,
                expandHintVisible: repo.path === firstCollapsedPath,
                poll: pollingPaths.has(repo.path),
              }}
            />
          ))}
        </>
      )}
      {hasListing && data.incomplete && (
        <div
          role="status"
          className="px-3 py-2 text-muted text-[13px] shrink-0"
          data-testid="git-repos-incomplete"
        >
          {i18nT('components.gitPanel.repos_incomplete')}
        </div>
      )}
      {hasListing && omitted > 0 && (
        // A fixed rule, not an action: the server keeps only so many
        // repositories and has nothing more to reveal, so the line names the
        // total this chat worked in, how many of them are listed, and how a
        // hidden one comes back -- no "show more". Both numbers come from the
        // server's `listed` (agent-touched repositories actually shown), not
        // from the rows: an untouched project row is a row the agent never
        // worked in, and a held repository whose .git is gone is counted by
        // the cap but never drawn, so neither the row count nor the cap says
        // how many of the agent's repositories are on screen.
        <div
          role="status"
          className="px-3 py-2 text-muted text-[13px] shrink-0"
          data-testid="git-repos-omitted"
        >
          {i18nT('components.gitPanel.repos_omitted', {
            count: omitted,
            formattedTotal: fmtNumber(listed + omitted),
            formattedLimit: fmtNumber(listed),
          })}
        </div>
      )}
    </div>
  )
}
