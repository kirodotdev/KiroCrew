import type { PullRequestStatus, PullRequestStatusBatch } from '../types'

/**
 * Payload of the `source_status` websocket event: a single pull request whose
 * cached lifecycle/CI status just CHANGED on the gateway.
 *
 * `origin` records where the change was observed — `'chip'` (lightweight sweep
 * or turn-boundary refresh) or `'detail'` (a full fetch). The client refetches
 * the detail payload for both origins so every owner window converges; the tag
 * is retained for diagnostics and potential requester-aware routing later.
 */
export interface SourceStatusDelta extends PullRequestStatus {
  url: string
  origin?: 'chip' | 'detail'
}

/** Narrow an untrusted websocket payload to a usable delta. */
export function parseStatusDelta(data: unknown): SourceStatusDelta | null {
  if (!data || typeof data !== 'object') return null
  const record = data as Record<string, unknown>
  const url = record.url
  if (typeof url !== 'string' || !url) return null
  const delta: SourceStatusDelta = { url }
  const state = record.state
  if (state === 'open' || state === 'draft' || state === 'merged' || state === 'closed') {
    delta.state = state
  }
  const ci = record.ci
  if (ci === 'running' || ci === 'passed' || ci === 'failed') delta.ci = ci
  const origin = record.origin
  if (origin === 'chip' || origin === 'detail') delta.origin = origin
  return delta
}

/**
 * Merge a delta into a cached status batch, returning a new object only when
 * something actually changed (so react-query subscribers don't re-render on a
 * no-op event). The delta is authoritative for the fields it carries and is
 * written even for URLs absent from this batch — extra keys are ignored by
 * consumers, which look up only their own sources.
 */
export function applyStatusDelta(
  batch: PullRequestStatusBatch | undefined,
  delta: SourceStatusDelta,
): PullRequestStatusBatch | undefined {
  if (!batch) return batch
  // A delta whose every field was stripped by parseStatusDelta (version skew:
  // the server grew a new state/ci value this client doesn't know yet) carries
  // no usable information. Treating it as authoritative would blank a populated
  // {state, ci} entry — worse than the stale glyph it would replace — so ignore
  // it and let the retained poll reconcile once vocabularies realign.
  if (!delta.state && !delta.ci) return batch
  const next: PullRequestStatus = {}
  if (delta.state) next.state = delta.state
  if (delta.ci) next.ci = delta.ci
  const current = batch.statuses?.[delta.url]
  if (current && current.state === next.state && current.ci === next.ci) return batch
  return {
    ...batch,
    statuses: { ...batch.statuses, [delta.url]: next },
    // The value just landed, so this URL is no longer awaiting a refresh —
    // leaving it listed would hold the panel on its fast follow-up interval.
    refreshing: batch.refreshing?.filter(url => url !== delta.url),
  }
}
