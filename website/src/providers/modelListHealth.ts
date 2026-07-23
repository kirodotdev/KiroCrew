// Tracks whether the model list currently served for a given provider came from
// a LIVE /api/models success or from a DEGRADED fallback (cached list or
// auto-only). The model-picker query polls until a live fetch succeeds, so the
// self-heal decision must key off this explicit signal — NOT the shape/length
// of the list, which cannot distinguish a live single-model backend from a
// degraded fallback, and (the bug this replaces) stops polling the moment a
// possibly-stale cached multi-entry list is served.
//
// Keyed by provider id (read from the ['available-models', <providerId>] query
// key) so it is provider-safe. Default is "not degraded" (undefined → false):
// a provider whose adapter never marks itself only ever stops polling, so this
// can never regress an unmarked provider into perpetual polling.

const degradedByProvider = new Map<string, boolean>()

/** Record whether the last fetch for a provider was degraded (fallback) or
 *  live. The adapter calls this on every fetch outcome. */
export function markModelsDegraded(providerId: string, degraded: boolean): void {
  degradedByProvider.set(providerId, degraded)
}

/** True only when the provider's last served list is known to be a degraded
 *  fallback. Unknown/never-fetched providers report false (not degraded). */
export function modelsDegraded(providerId: string): boolean {
  return degradedByProvider.get(providerId) === true
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
