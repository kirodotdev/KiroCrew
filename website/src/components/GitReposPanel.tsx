import { useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api } from '../api/client'
import GitPanel from './GitPanel'
import ErrorNotice from './ErrorNotice'
import { errMessage } from '../utils/thunkError'
import { findReport } from '../utils/errorReport'
import { i18nT } from '../i18n/t'
import { fmtNumber } from '../i18n/format'

interface GitReposPanelProps {
  /** The chat whose repositories are listed. */
  slotKey: string
  onFileOpen?: (path: string) => void
}

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
  const { data, error, isLoading } = useQuery({
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
            message={i18nT('components.gitPanel.repos_failed')}
            report={findReport(serverMessage)}
            askAgent
            testId="git-repos-error"
          />
        </div>
      ) : isLoading ? (
        // The first listing takes a round trip; an empty tab meanwhile reads as
        // "nothing here", which is the empty state's claim, not a wait.
        <div
          role="status"
          aria-busy="true"
          className="flex items-center justify-center gap-2 px-6 pt-8 text-muted text-[13px]"
          data-testid="git-repos-loading"
        >
          <Loader2 size={14} className="animate-spin shrink-0" aria-hidden="true" />
          <span>{i18nT('components.gitPanel.repos_loading')}</span>
        </div>
      ) : repos.length === 0 ? (
        <div role="status" className="px-6 pt-8 text-center text-muted text-[13px]">
          {i18nT('components.gitPanel.repos_empty')}
        </div>
      ) : (
        repos.map((repo, index) => (
          <GitPanel
            key={repo.path}
            projectDir={repo.path}
            onFileOpen={onFileOpen}
            section={{
              label: repo.path,
              badge: repo.source === 'project' ? i18nT('components.gitPanel.source_project') : undefined,
              badgeTitle: repo.source === 'project' ? i18nT('components.gitPanel.source_project_help') : undefined,
              defaultOpen: index === 0,
            }}
          />
        ))
      )}
      {!error && !isLoading && omitted > 0 && (
        <div
          role="status"
          className="px-3 py-2 text-muted text-[13px] shrink-0"
          data-testid="git-repos-omitted"
        >
          {i18nT('components.gitPanel.repos_omitted', {
            count: omitted,
            formattedCount: fmtNumber(omitted),
          })}
        </div>
      )}
    </div>
  )
}
