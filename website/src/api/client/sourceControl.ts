/**
 * Source-control provider panels (/api/source): the pull-request source,
 * checks and statuses, review threads and comments, auto-merge, ready, pending
 * review and submission, the issue source, and repository contributors.
 */

import type { PullRequestSource, PullRequestCheck, PullRequestStatusBatch, IssueSource, AppContributor } from '../../types'
import type { ClientTransport } from './transport'

export function createSourceControlEndpoints({ post, j }: ClientTransport) {
  const providers = {
    pullRequestSource: (url: string, refresh = false) => post('/api/source/pull-request', { url, refresh }).then(j) as Promise<PullRequestSource>,
    pullRequestChecks: (url: string) => post('/api/source/pull-request/checks', { url }).then(j) as Promise<{ checks: PullRequestCheck[] }>,
    pullRequestStatuses: (urls: string[]) => post('/api/source/pull-request/status', { urls }).then(j) as Promise<PullRequestStatusBatch>,
    resolvePullRequestThread: (url: string, threadId: string) => post('/api/source/pull-request/resolve', { url, threadId }).then(j) as Promise<{ resolved: boolean }>,
    unresolvePullRequestThread: (url: string, threadId: string) => post('/api/source/pull-request/unresolve', { url, threadId }).then(j) as Promise<{ resolved: boolean }>,
    /** Reply into an existing review thread. Owner-only on the gateway. */
    replyToPullRequestThread: (url: string, threadId: string, body: string) => post('/api/source/pull-request/reply', { url, threadId, body }).then(j) as Promise<{ posted: boolean }>,
    /** Top-level comment on the pull request conversation. */
    commentOnPullRequest: (url: string, body: string) => post('/api/source/pull-request/comment', { url, body }).then(j) as Promise<{ posted: boolean }>,
    enablePullRequestAutoMerge: (url: string, confirmImmediateMerge = false) => post('/api/source/pull-request/auto-merge', { url, confirmImmediateMerge }).then(j) as Promise<{ autoMerge: boolean; mergeMethod: string }>,
    markPullRequestReady: (url: string) => post('/api/source/pull-request/ready', { url }).then(j) as Promise<{ ready: boolean }>,
    pullRequestPendingReview: (url: string) => post('/api/source/pull-request/pending-review', { url }).then(j) as Promise<{ reviewId: string; body: string; comments?: { path: string; line: number | null; body: string }[]; commitId: string; headSha: string; stale: boolean; contentRedacted: boolean; autoMergeArmed: boolean; contentDigest: string; staleDismissalEnabled: boolean }>,
    submitPullRequestReview: (url: string, reviewId: string, event: 'APPROVE' | 'REQUEST_CHANGES' | 'COMMENT', contentDigest: string) =>
      post('/api/source/pull-request/submit-review', { url, reviewId, event, contentDigest }).then(j) as Promise<{ submitted: boolean; event: string }>,
    // Issue sources. `refresh` bypasses the server's cached payload; the panel
    // never polls, so a refresh is always an explicit user action.
    fetchIssueSource: (url: string, refresh = false) => post('/api/source/issue', { url, refresh }).then(j) as Promise<IssueSource>,
    /** Top contributors to an app's source repo (GitHub only). Owner-gated. */
    appContributors: (url: string, refresh = false) => post('/api/source/contributors', { url, refresh }).then(j) as Promise<{ contributors: AppContributor[] }>,
  }

  return { providers }
}
