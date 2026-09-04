// Tracks whether the model list currently served for a given provider came from
// a LIVE /api/models success or from a DEGRADED fallback (cached list or
// auto-only). The model-picker query polls until a live fetch succeeds, so the
// self-heal decision must key off this explicit signal — NOT the shape/length
// of the list, which cannot distinguish a live single-model backend from a
// degraded fallback, and would stop polling the moment a
// possibly-stale cached multi-entry list is served.
//
// Keyed by provider id (read from the ['available-models', <providerId>] query
// key) so it is provider-safe. Default is "not degraded" (undefined → false):
// a provider whose adapter never marks itself only ever stops polling, so this
// can never regress an unmarked provider into perpetual polling.
import { useSyncExternalStore } from 'react'

const degradedByProvider = new Map<string, boolean>()
const subscribers = new Set<() => void>()

/** Record whether the last fetch for a provider was degraded (fallback) or
 *  live. The adapter calls this on every fetch outcome. */
export function markModelsDegraded(providerId: string, degraded: boolean): void {
  if (degradedByProvider.get(providerId) === degraded) return
  degradedByProvider.set(providerId, degraded)
  for (const cb of subscribers) cb()
}

function subscribe(cb: () => void): () => void {
  subscribers.add(cb)
  return () => {
    subscribers.delete(cb)
  }
}

/** True only when the provider's last served list is known to be a degraded
 *  fallback. Unknown/never-fetched providers report false (not degraded). */
export function modelsDegraded(providerId: string): boolean {
  return degradedByProvider.get(providerId) === true
}

/**
 * Reactive form of `modelsDegraded`, for components that RENDER something from
 * the flag rather than just deciding a refetch cadence.
 *
 * A plain call cannot be read during render: a failed fetch resolves
 * SUCCESSFULLY with the last-good cached list, so when that list is
 * structurally identical to the one React Query already holds it hands back the
 * same reference and notifies nobody. The flag flips with no re-render, and the
 * component keeps rendering the previous decision until something unrelated
 * happens to re-render it.
 */
export function useModelsDegraded(providerId: string): boolean {
  return useSyncExternalStore(
    subscribe,
    () => modelsDegraded(providerId),
    () => false,
  )
}

/**
 * refetch cadence for the ['available-models', <providerId>] query: poll every
 * 8s WHILE the served list is a degraded fallback, then stop the instant a LIVE
 * fetch succeeds. Reads the provider id from the query key (index 1), so it is
 * decoupled from the list shape and safe across providers. RQ v5 passes the
 * Query instance.
 */
export function modelListRefetchInterval(
  query: { queryKey: readonly unknown[] },
): number | false {
  const providerId = typeof query.queryKey[1] === 'string' ? query.queryKey[1] : ''
  return modelsDegraded(providerId) ? 8_000 : false
}
