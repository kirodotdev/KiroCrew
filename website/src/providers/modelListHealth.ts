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
// Set when a provider's model list is degraded SPECIFICALLY because kiro-cli's
// login expired or is signed out (GET /api/models answered 503 with
// `code: kiro_prerequisite_required`). Separate from `degradedByProvider`
// because the two answer different questions: "keep polling?" (any degraded
// cause) versus "is this an auth wall the user must clear with `kiro-cli
// login`?" (this one cause). The Settings ▸ Chat banner reads this to show the
// one actionable sign-in message instead of the generic config-load failure.
// Cleared on the next LIVE success, like the degraded flag.
const authRequiredByProvider = new Map<string, boolean>()
const subscribers = new Set<() => void>()

/** Record whether the last fetch for a provider was degraded (fallback) or
 *  live. The adapter calls this on every fetch outcome. */
export function markModelsDegraded(providerId: string, degraded: boolean): void {
  // A live success is the one event that clears the auth-required flag: the
  // list loaded, so whatever sign-in wall there was is gone. A degraded fetch
  // leaves the flag to `markModelsAuthRequired`, which the adapter calls with
  // the actual cause — degraded alone does not imply auth.
  let changed = false
  if (!degraded && authRequiredByProvider.get(providerId)) {
    authRequiredByProvider.set(providerId, false)
    changed = true
  }
  if (degradedByProvider.get(providerId) !== degraded) {
    degradedByProvider.set(providerId, degraded)
    changed = true
  }
  if (changed) for (const cb of subscribers) cb()
}

/** Record whether the provider's degraded model list is due to an expired /
 *  signed-out kiro-cli login. The adapter calls this from the one place the
 *  /api/models error is formed, reading the response `code`; it never threads
 *  the code through the pickers. */
export function markModelsAuthRequired(providerId: string, authRequired: boolean): void {
  if (authRequiredByProvider.get(providerId) === authRequired) return
  authRequiredByProvider.set(providerId, authRequired)
  for (const cb of subscribers) cb()
}

/** True only when the provider's degraded list is a kiro-cli sign-in wall. */
export function modelsAuthRequired(providerId: string): boolean {
  return authRequiredByProvider.get(providerId) === true
}

/** Reactive form of `modelsAuthRequired`, for a banner that renders from it. */
export function useModelsAuthRequired(providerId: string): boolean {
  return useSyncExternalStore(
    subscribe,
    () => modelsAuthRequired(providerId),
    () => false,
  )
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
  // Another backend's list (`['available-models', <providerId>, <backend>]`) heals on its own flag.
  const backend = typeof query.queryKey[2] === 'string' ? query.queryKey[2] : undefined
  return modelsDegraded(modelHealthKey(providerId, backend)) ? 8_000 : false
}

/** The degraded-flag key for the configured list, or for one backend's own list. */
export function modelHealthKey(providerId: string, backend?: string): string {
  return backend === undefined ? providerId : `${providerId}:${backend}`
}
