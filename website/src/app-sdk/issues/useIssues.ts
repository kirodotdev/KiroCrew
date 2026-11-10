/**
 * `useIssues`: one page-at-a-time issue list from GitHub, GitLab, Jira or Linear,
 * served by `POST /api/source/issues`.
 *
 * Which transport answers is decided the way `PullRequestPanel` decides it, by
 * the identity the host published and never by the app:
 *
 * - an EXTERNAL app posts through its own scoped client once its manifest's
 *   `permissions.api` covers `/api/source/issues`. Undeclared, the hook sends
 *   nothing and reports an `AppApiPermissionError`. `/api/source/issue` (the
 *   single-issue read) does not cover it: the matcher stops at a slash boundary.
 * - a BUILTIN page is host code and keeps the dashboard's client.
 * - no identity at all fails closed, like an external app with nothing declared.
 *
 * Paging follows the response's `nextCursor`. The query, minus `cursor`, is the
 * cache key, so any change to it (a filter, the scope) starts again from the
 * first page.
 */
import { useContext, useMemo } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import { api as dashboardApi } from '../../api/client'
import type { IssueSearchRequest, IssueSearchResponse, IssueSummary } from '../../types'
import { AppApiPermissionError } from '../apiError'
import { i18nT } from '../../i18n/t'
import { useAppIdentity } from '../identity'
import { AppSdkContext, apiPathAllowed, type AppApi } from '../scopedApi'

export const ISSUES_PATH = '/api/source/issues'

/** Fetches one page for a query. */
export type IssuesTransport = (query: IssueSearchRequest) => Promise<IssueSearchResponse>

const NO_PATHS: readonly string[] = []

/** The transport over an app's scoped client, or null when the list is not
 *  declared. Exported for tests. */
export function scopedIssuesTransport(api: AppApi, allowedPaths: readonly string[]): IssuesTransport | null {
  if (!apiPathAllowed(allowedPaths, ISSUES_PATH)) return null
  return query => api.post<IssueSearchResponse>(ISSUES_PATH, query)
}

const builtinTransport: IssuesTransport = query => dashboardApi.fetchIssues(query)

/** The transport this surface may use, and the refusal when it may use none. */
export function useIssuesTransport(): { transport: IssuesTransport | null; refusal: AppApiPermissionError | null } {
  const identity = useAppIdentity()
  const sdk = useContext(AppSdkContext)
  const builtin = identity?.origin === 'builtin'
  // Read off the context directly, not through useAppApi(), which throws with
  // no provider: a surface with no provider must fail CLOSED.
  const allowedPaths = sdk?.info.permissions.api ?? NO_PATHS
  const api = sdk?.api
  const appLabel = sdk?.info.name ?? identity?.appId ?? ''
  return useMemo(() => {
    if (builtin) return { transport: builtinTransport, refusal: null }
    const transport = api ? scopedIssuesTransport(api, allowedPaths) : null
    if (transport) return { transport, refusal: null }
    return { transport: null, refusal: new AppApiPermissionError(i18nT('appSdk.issues.not_declared', { path: ISSUES_PATH }), appLabel) }
  }, [builtin, api, allowedPaths, appLabel])
}

export interface UseIssuesOptions {
  /** False holds the request back (for example while the scope is still being chosen). */
  enabled?: boolean
}

export interface UseIssuesResult {
  /** Every page loaded so far, in order. */
  issues: IssueSummary[]
  /** True only while the FIRST page is loading. */
  isLoading: boolean
  /** The last failure, or an `AppApiPermissionError` when the list is not declared. */
  error: Error | null
  hasNextPage: boolean
  /** Loads the next page; does nothing on the last one. */
  fetchNextPage: () => void
  refetch: () => void
  /** True while a next page is loading. */
  isFetchingNextPage: boolean
}

const NO_ISSUES: IssueSummary[] = []

export function useIssues(query: IssueSearchRequest, opts: UseIssuesOptions = {}): UseIssuesResult {
  const { transport, refusal } = useIssuesTransport()
  // The cursor is the page parameter, never part of the key.
  const { cursor: _cursor, ...keyed } = query
  const enabled = transport != null && opts.enabled !== false
  const result = useInfiniteQuery({
    queryKey: ['app-sdk-issues', keyed],
    queryFn: ({ pageParam }) => {
      if (!transport) throw refusal
      return transport({ ...keyed, cursor: pageParam })
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last: IssueSearchResponse) => last.nextCursor || undefined,
    enabled,
  })
  const pages = result.data?.pages
  const issues = useMemo(() => (pages ? pages.flatMap(p => p.issues) : NO_ISSUES), [pages])
  const { fetchNextPage, refetch, hasNextPage, isFetchingNextPage } = result
  return {
    issues,
    isLoading: enabled && result.isPending,
    error: refusal ?? (result.error as Error | null),
    hasNextPage: enabled && hasNextPage,
    fetchNextPage: () => { if (enabled && hasNextPage && !isFetchingNextPage) void fetchNextPage() },
    refetch: () => { if (enabled) void refetch() },
    isFetchingNextPage,
  }
}
