// Tracks whether the model list currently served for a given provider came from
// a LIVE /api/models success or from a DEGRADED fallback (cached list or
// auto-only). The model-picker query polls until a live fetch succeeds, so the
// self-heal decision must key off this explicit signal — NOT the shape/length
// of the list, which cannot distinguish a live single-model backend from a
// degraded fallback, and would stop polling the moment a
// possibly-stale cached multi-entry list is served.
//
// Keyed by provider id plus the useAvailableModels scope (`slot:…` /
// `config:…`) so a Settings config-namespace 503 cannot restart the live
// chat's 8s poll. Default is "not degraded" (undefined → false): a provider
// whose adapter never marks itself only ever stops polling, so this can never
// regress an unmarked provider into perpetual polling.
import { useSyncExternalStore } from 'react'

const degradedByProvider = new Map<string, Map<string | undefined, boolean>>()
const subscribers = new Set<() => void>()

/** Scope string that matches `useAvailableModels`' query-key third slot. */
export function modelListScope(slot?: string, backend?: string | null): string {
  return slot ? `slot:${slot}` : `config:${backend ?? ''}`
}

function scopedHealth(providerId: string): Map<string | undefined, boolean> {
  let health = degradedByProvider.get(providerId)
  if (!health) {
    health = new Map()
    degradedByProvider.set(providerId, health)
  }
  return health
}

/** Record whether the last fetch for a provider+scope was degraded (fallback)
 *  or live. The adapter calls this on every fetch outcome. */
export function markModelsDegraded(
  providerId: string,
  degraded: boolean,
  scope?: string,
): void {
  const health = scopedHealth(providerId)
  if (health.get(scope) === degraded) return
  health.set(scope, degraded)
  for (const cb of subscribers) cb()
}

function subscribe(cb: () => void): () => void {
  subscribers.add(cb)
  return () => {
    subscribers.delete(cb)
  }
}

/** True only when the provider+scope's last served list is known to be a
 *  degraded fallback. Unknown/never-fetched keys report false (not degraded). */
export function modelsDegraded(providerId: string, scope?: string): boolean {
  return degradedByProvider.get(providerId)?.get(scope) === true
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
export function useModelsDegraded(providerId: string, scope?: string): boolean {
  return useSyncExternalStore(
    subscribe,
    () => modelsDegraded(providerId, scope),
    () => false,
  )
}

/**
 * refetch cadence for the ['available-models', <providerId>, <scope>] query:
 * poll every 8s WHILE the served list is a degraded fallback, then stop the
 * instant a LIVE fetch succeeds. Reads provider id (index 1) and scope
 * (index 2) from the query key so a config-namespace 503 cannot flap a
 * live-slot poll. RQ v5 passes the Query instance.
 */
export function modelListRefetchInterval(
  query: { queryKey: readonly unknown[] },
): number | false {
  const providerId = typeof query.queryKey[1] === 'string' ? query.queryKey[1] : ''
  const scope = typeof query.queryKey[2] === 'string' ? query.queryKey[2] : undefined
  return modelsDegraded(providerId, scope) ? 8_000 : false
}
