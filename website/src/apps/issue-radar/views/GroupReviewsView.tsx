import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'

import { issueRadarApi, type GitLabGroup, type GitLabGroupReviewsResponse } from '../api'
import ErrorNotice from '../../../components/ErrorNotice'
import { ProviderLogo } from '../components/ProviderBadge'
import { useIssueRadar } from '../context'
import { relativeTimeOrDate } from '../lib/format'
import { gitlabGroupErrorText, groupReviewsQueryKey, useGitlabGroups } from '../lib/gitlabGroups'
import { safeHttpUrl } from '../../../lib/safeUrl'

import { i18nT } from '../../../i18n/t'

const GITLAB_REF = { provider: 'gitlab' as const }

/** A group search walks up to four pages per refresh, so it never polls faster
 * than this however short the list refresh is set. */
const GROUP_REVIEWS_MIN_POLL_MS = 120_000

/** The same search on GitLab's own merge-request list, for the rows past the cap. */
function gitlabSearchUrl(data: GitLabGroupReviewsResponse): string | undefined {
  const q = new URLSearchParams({ state: 'opened', reviewer_username: data.login })
  return safeHttpUrl(`https://${data.host}/groups/${data.path}/-/merge_requests?${q.toString()}`) ?? undefined
}

/** "Review requests" dashboard: the open merge requests waiting for your review
 * across every GitLab group you follow, subgroups included. Each group is one
 * group-scoped search on the server, refreshed on the list cadence. */
export default function GroupReviewsView() {
  const { openSettings } = useIssueRadar()
  const groups = useGitlabGroups()

  return (
    <div className="px-4 md:px-6 pt-4 pb-6 flex flex-col gap-4">
      <div>
        <h1 className="text-[18px] font-semibold">{i18nT('apps.issueRadar.views.groupReviewsView.title')}</h1>
        <p className="text-[13px] text-muted mt-1">{i18nT('apps.issueRadar.views.groupReviewsView.intro')}</p>
      </div>

      {groups.isError && (
        <ErrorNotice message={gitlabGroupErrorText(groups.error)} variant="inline" askAgent />
      )}

      {groups.data && groups.data.length === 0 && (
        <div className="rounded-xl border border-dashed border-border p-5 flex flex-col items-start gap-3">
          <p className="text-[13px] text-muted">{i18nT('apps.issueRadar.views.groupReviewsView.empty')}</p>
          <button
            type="button"
            onClick={() => openSettings({ kind: 'general', anchor: 'gitlabGroups' })}
            className="px-3 py-1.5 rounded-md border border-accent text-accent bg-transparent text-xs font-semibold cursor-pointer hover:bg-accent-subtle"
          >
            {i18nT('apps.issueRadar.views.groupReviewsView.follow_a_group')}
          </button>
        </div>
      )}

      {groups.data?.map((g) => <GroupCard key={`${g.host}/${g.path}`} group={g} />)}
    </div>
  )
}

/** One followed group: its count, refresh, and merge-request rows, or its own error. */
function GroupCard({ group }: { group: GitLabGroup }) {
  const { refreshPrefs } = useIssueRadar()
  const reviews = useQuery({
    queryKey: groupReviewsQueryKey(group),
    queryFn: () => issueRadarApi.gitlabGroupReviews(group),
    refetchInterval: Math.max(refreshPrefs.listPollMs, GROUP_REVIEWS_MIN_POLL_MS),
    refetchIntervalInBackground: refreshPrefs.pollInBackground,
    retry: false,
  })
  const data = reviews.data

  return (
    <section
      className="rounded-xl border border-border bg-bg-elevated shadow-sm p-4"
      aria-label={group.path}
      data-testid="gitlab-group-card"
    >
      <div className="flex items-center gap-2">
        <ProviderLogo repoRef={GITLAB_REF} size={14} />
        <h2 className="text-[14px] font-medium truncate">{group.path}</h2>
        {group.host !== 'gitlab.com' && <span className="text-[12px] text-muted truncate">{group.host}</span>}
        <span className="flex-1" />
        <span className="text-[12px] text-muted tabular-nums" aria-live="polite">
          {reviews.isPending && i18nT('apps.issueRadar.views.groupReviewsView.loading')}
          {data && (data.truncated
            ? i18nT('apps.issueRadar.views.groupReviewsView.count_capped', { total: data.pulls.length })
            : i18nT('apps.issueRadar.views.groupReviewsView.count', { total: data.pulls.length }))}
        </span>
        <button
          type="button"
          onClick={() => { void reviews.refetch() }}
          disabled={reviews.isFetching}
          aria-label={i18nT('apps.issueRadar.views.groupReviewsView.refresh_group', { path: group.path })}
          className="p-1 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-0 disabled:opacity-30 disabled:cursor-default"
        >
          <RefreshCw size={13} aria-hidden="true" className={reviews.isFetching ? 'animate-spin' : ''} />
        </button>
      </div>

      {reviews.isError && (
        <ErrorNotice
          message={gitlabGroupErrorText(reviews.error)}
          variant="inline"
          askAgent
          className="mt-2"
        />
      )}

      {data && data.pulls.length === 0 && (
        <p className="text-[12px] text-muted mt-2">{i18nT('apps.issueRadar.views.groupReviewsView.no_reviews')}</p>
      )}

      {data && data.pulls.length > 0 && (
        <ul className="mt-3 flex flex-col gap-2 list-none p-0 m-0">
          {data.pulls.map((pr) => {
            const href = safeHttpUrl(pr.url) ?? undefined
            const title = pr.truncated_fields.includes('title') ? `${pr.title}…` : pr.title
            return (
              <li key={`${pr.project}!${pr.number}`} className="min-w-0" data-testid="gitlab-group-review">
                {href ? (
                  <a href={href} target="_blank" rel="noreferrer" className="block text-[13px] text-text hover:text-accent hover:underline truncate">
                    {title}
                  </a>
                ) : (
                  <span className="block text-[13px] text-text truncate">{title}</span>
                )}
                {/* Wraps at narrow widths: the author and the age move to their own
                    line instead of squeezing the project identifier, and a long
                    author name truncates with its full text in the tooltip. */}
                <div className="flex flex-wrap min-w-0 items-baseline gap-x-1 text-[12px] text-muted">
                  <span className="font-mono truncate min-w-0 max-w-full">{pr.project}!{pr.number}</span>
                  {pr.author && <span className="truncate min-w-0 max-w-full" title={pr.author}>· {pr.author}</span>}
                  {pr.updated_at && <span className="whitespace-nowrap">· {relativeTimeOrDate(pr.updated_at)}</span>}
                </div>
              </li>
            )
          })}
        </ul>
      )}

      {data?.truncated && (
        <p className="text-[12px] text-muted mt-2">
          {i18nT('apps.issueRadar.views.groupReviewsView.capped', { limit: data.limit })}{' '}
          {gitlabSearchUrl(data) && (
            <a
              href={gitlabSearchUrl(data)}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline"
            >
              {i18nT('apps.issueRadar.views.groupReviewsView.see_all')}
            </a>
          )}
        </p>
      )}
    </section>
  )
}
