import type { PullRequestStatus, PullRequestStatusBatch } from '../types'

/**
 * Payload of the `source_status` websocket event: a single pull request whose
 * cached lifecycle/CI status just CHANGED on the gateway.
 *
 * `origin` records where the change was observed — `'chip'` (lightweight sweep
 * or turn-boundary refresh) or `'detail'` (a full fetch). The client refetches
 * the detail payload for both origins so every owner window converges; the tag
 * is retained for diagnostics and potential requester-aware routing later.
 *
 * The merge pair rides the same event, so a branch that starts conflicting while
 * the panel is open invalidates the pinned detail payload like any other status
 * change and the merge-blocker banner follows without a manual refresh.
 */
export interface SourceStatusDelta extends PullRequestStatus {
  url: string
  origin?: 'chip' | 'detail'
}

/** Normalized merge-state values are compared for equality and switched on by
 *  the panel (an unrecognized one simply renders no banner), so they are shape-
 *  checked rather than enumerated — a value this client does not know yet stays
 *  harmless, and enumerating would silently drop it on version skew. */
function mergeStateValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 && value.length <= 32
    && /^[a-z_]+$/.test(value)
    ? value
    : undefined
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
  const mergeable = mergeStateValue(record.mergeable)
  if (mergeable) delta.mergeable = mergeable
  const mergeStateStatus = mergeStateValue(record.mergeStateStatus)
  if (mergeStateStatus) delta.mergeStateStatus = mergeStateStatus
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
  if (!delta.state && !delta.ci && !delta.mergeable && !delta.mergeStateStatus) return batch
  const next: PullRequestStatus = {}
  if (delta.state) next.state = delta.state
  if (delta.ci) next.ci = delta.ci
  if (delta.mergeable) next.mergeable = delta.mergeable
  if (delta.mergeStateStatus) next.mergeStateStatus = delta.mergeStateStatus
  const current = batch.statuses?.[delta.url]
  if (
    current
    && current.state === next.state
    && current.ci === next.ci
    && current.mergeable === next.mergeable
    && current.mergeStateStatus === next.mergeStateStatus
  ) return batch
  return {
    ...batch,
    statuses: { ...batch.statuses, [delta.url]: next },
    // The value just landed, so this URL is no longer awaiting a refresh —
    // leaving it listed would hold the panel on its fast follow-up interval.
    refreshing: batch.refreshing?.filter(url => url !== delta.url),
  }
}
