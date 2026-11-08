/**
 * The provider calls the shared pull-request panel makes, as one injectable client.
 *
 * `PullRequestPanel` and the `CommentThreads` it renders read and write a pull
 * request through this client, so the same component can be hosted by an app
 * (`app-sdk/PullRequestPanel.tsx`) with a client built over that app's scoped
 * transport.
 *
 * Every member is required: the panel draws every control and runs every poll
 * the provider supports, through whichever client its host provided.
 *
 * With no provider in context the panel uses the dashboard's own client: a
 * dashboard surface never provides one.
 */
import { createContext, useContext, type ReactNode } from 'react'
import { api } from '../api/client'
import type { PullRequestCheck, PullRequestSource, PullRequestStatusBatch } from '../types'

export interface PullRequestClient {
  source(url: string, refresh?: boolean): Promise<PullRequestSource>
  checks(url: string): Promise<{ checks: PullRequestCheck[] }>
  statuses(urls: string[]): Promise<PullRequestStatusBatch>
  markReady(url: string): Promise<{ ready: boolean }>
  enableAutoMerge(url: string, confirmImmediateMerge: boolean): Promise<{ autoMerge: boolean; mergeMethod: string }>
  replyToThread(url: string, threadId: string, body: string): Promise<{ posted: boolean }>
  comment(url: string, body: string): Promise<{ posted: boolean }>
  resolveThread(url: string, threadId: string): Promise<{ resolved: boolean }>
  unresolveThread(url: string, threadId: string): Promise<{ resolved: boolean }>
}

/** The dashboard's client. Each member calls through `api` at call time, so a
 *  test that mocks the client module sees each call with its own arguments. */
export const DASHBOARD_PULL_REQUEST_CLIENT: PullRequestClient = {
  source: (...args) => api.pullRequestSource(...args),
  checks: (...args) => api.pullRequestChecks(...args),
  statuses: (...args) => api.pullRequestStatuses(...args),
  markReady: (...args) => api.markPullRequestReady(...args),
  enableAutoMerge: (...args) => api.enablePullRequestAutoMerge(...args),
  replyToThread: (...args) => api.replyToPullRequestThread(...args),
  comment: (...args) => api.commentOnPullRequest(...args),
  resolveThread: (...args) => api.resolvePullRequestThread(...args),
  unresolveThread: (...args) => api.unresolvePullRequestThread(...args),
}

const PullRequestClientContext = createContext<PullRequestClient | null>(null)

export function PullRequestClientProvider({ client, children }: { client: PullRequestClient; children: ReactNode }) {
  return <PullRequestClientContext.Provider value={client}>{children}</PullRequestClientContext.Provider>
}

/** The client the nearest host provided, else the dashboard's own. */
export function usePullRequestClient(): PullRequestClient {
  return useContext(PullRequestClientContext) ?? DASHBOARD_PULL_REQUEST_CLIENT
}
