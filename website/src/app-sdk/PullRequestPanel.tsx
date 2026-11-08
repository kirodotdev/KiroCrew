/**
 * The dashboard's pull-request panel, offered to apps (`@kirocrew/app-sdk`).
 *
 * It is the same `components/PullRequestPanel` the chat window's side panel
 * renders (description, checks, review threads, the file diff, commits), so an
 * app showing a pull request does not restate it. What differs is the client
 * behind it:
 *
 * - An EXTERNAL app gets a client built over its own scoped transport, once
 *   its manifest's `permissions.api` declares the read
 *   (`/api/source/pull-request`). Without it the panel renders a refusal and
 *   sends nothing. Every other endpoint the panel calls is a child of the
 *   read's path, and a bare path covers its children, so declaring the read
 *   declares the whole family: there is no read-only panel, and an app that
 *   declares the read can also mark ready, arm auto-merge and write review
 *   threads. This grants nothing new: the scoped transport still checks every
 *   request against the declaration, the same guardrail `useAppApi()` applies,
 *   and the gateway's token check uses the same prefix rule.
 * - A BUILTIN page is host code. It keeps the dashboard's client, exactly as it
 *   keeps the dashboard's query client (`appQueryClient.ts`).
 * - With no app identity at all the panel fails closed, like an external app
 *   with nothing declared.
 *
 * The chat handoff props (`onAddToChat`, `onDismiss`, `onReconcile`) are not
 * offered: they write into the dashboard's composer and source strip, which an
 * app page does not have.
 *
 * The host panel loads behind a lazy boundary. This module is part of the SDK
 * barrel the dashboard shell loads up front for every app, while the panel
 * itself is only needed once a page shows a pull request.
 */
import { lazy, Suspense, useContext, useMemo } from 'react'
import { PullRequestClientProvider, type PullRequestClient } from '../components/pullRequestClient'
import ErrorNotice from '../components/ErrorNotice'
import { ContentSkeleton } from '../components/ui'
import type { PullRequestLink } from '../utils/pullRequestLinks'
import type { PullRequestCheck, PullRequestSource, PullRequestStatusBatch } from '../types'
import { i18nT } from '../i18n/t'
import { useAppIdentity } from './identity'
import { AppSdkContext, apiPathAllowed, type AppApi } from './scopedApi'

const HostPullRequestPanel = lazy(() => import('../components/PullRequestPanel'))

/** The endpoint behind each client member. Each is a child of `source`, so the
 *  declaration that admits the read admits all of them. */
export const PULL_REQUEST_PANEL_PATHS = {
  source: '/api/source/pull-request',
  checks: '/api/source/pull-request/checks',
  statuses: '/api/source/pull-request/status',
  markReady: '/api/source/pull-request/ready',
  enableAutoMerge: '/api/source/pull-request/auto-merge',
  replyToThread: '/api/source/pull-request/reply',
  comment: '/api/source/pull-request/comment',
  resolveThread: '/api/source/pull-request/resolve',
  unresolveThread: '/api/source/pull-request/unresolve',
} as const

const NO_PATHS: readonly string[] = []

/** The client for these declared paths, or null when the read is not declared.
 *  Each request body is the one the dashboard's own client sends. Exported for
 *  tests. */
export function scopedPullRequestClient(api: AppApi, allowedPaths: readonly string[]): PullRequestClient | null {
  const P = PULL_REQUEST_PANEL_PATHS
  if (!apiPathAllowed(allowedPaths, P.source)) return null
  return {
    source: (url, refresh = false) => api.post<PullRequestSource>(P.source, { url, refresh }),
    checks: url => api.post<{ checks: PullRequestCheck[] }>(P.checks, { url }),
    statuses: urls => api.post<PullRequestStatusBatch>(P.statuses, { urls }),
    markReady: url => api.post(P.markReady, { url }),
    enableAutoMerge: (url, confirmImmediateMerge) => api.post(P.enableAutoMerge, { url, confirmImmediateMerge }),
    replyToThread: (url, threadId, body) => api.post(P.replyToThread, { url, threadId, body }),
    comment: (url, body) => api.post(P.comment, { url, body }),
    resolveThread: (url, threadId) => api.post(P.resolveThread, { url, threadId }),
    unresolveThread: (url, threadId) => api.post(P.unresolveThread, { url, threadId }),
  }
}

export interface AppPullRequestPanelProps {
  /** The pull requests the panel's strip offers; the first few are shown. */
  sources: PullRequestLink[]
  /** The url of the one to show. */
  selectedUrl: string
  /** Called when the user picks another tab in the strip. */
  onSelect: (url: string) => void
}

export default function PullRequestPanel({ sources, selectedUrl, onSelect }: AppPullRequestPanelProps) {
  const identity = useAppIdentity()
  const sdk = useContext(AppSdkContext)
  const builtin = identity?.origin === 'builtin'
  // Read off the context directly, not through useAppApi(), which throws with
  // no provider: a surface with no provider must fail CLOSED.
  const allowedPaths = sdk?.info.permissions.api ?? NO_PATHS
  const api = sdk?.api
  const client = useMemo(
    () => (builtin || !api ? null : scopedPullRequestClient(api, allowedPaths)),
    [builtin, api, allowedPaths],
  )
  const panel = (
    <Suspense fallback={<ContentSkeleton rows={6} />}>
      <HostPullRequestPanel sources={sources} selectedUrl={selectedUrl} onSelect={onSelect} />
    </Suspense>
  )
  if (builtin) return panel
  if (!client) {
    return (
      <ErrorNotice
        variant="inline"
        testId="app-pull-request-panel-not-declared"
        message={i18nT('appSdk.pullRequestPanel.not_declared', { path: PULL_REQUEST_PANEL_PATHS.source })}
        askAgent
      />
    )
  }
  return <PullRequestClientProvider client={client}>{panel}</PullRequestClientProvider>
}
