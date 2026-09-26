import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { ChatSlot, RemoteCrewCapabilities } from '../types'

export type { RemoteCrewCapabilities }

/** How often (ms) to re-read a version-compatible peer whose capability
 *  document is still partial. Matches the peer's own /api/models poll cadence:
 *  each round trip either lands the missing roster or re-confirms the peer is
 *  still warming, and one request every 8s is cheap next to the open tunnel. */
export const CAPABILITY_REPOLL_MS = 8_000

/** Per-field codes a re-read can improve on; see `capabilityDocIsRetriablePartial`. */
const RETRIABLE_CAPABILITY_CODES: ReadonlySet<string> = new Set([
  'capability_unreachable',
  'capability_peer_revalidating',
])

/**
 * True when *caps* is a partial document worth re-reading: a version-compatible
 * peer answered, but at least one field is transient — it timed out in transit
 * (`capability_unreachable`), the shape a cold peer produces while its model
 * list is still being discovered, or the peer's models read is momentarily
 * revalidating its entitlement snapshot (`capability_peer_revalidating`), a
 * deliberate 503 that clears on the next read.
 *
 * Deliberately narrow. A version-skewed peer is not polled (the session cannot
 * dispatch to it anyway, so a fresher roster changes nothing), and neither is a
 * peer that answered with a terminal per-field code: `capability_peer_too_old`
 * needs an upgrade, not a retry, `capability_peer_refused` is a refusal a
 * re-read will not change, and `capability_peer_not_connected` /
 * `capability_no_credential` mean the tunnel itself is down — hammering it
 * would just delay the honest "crew unreachable" state.
 */
export function capabilityDocIsRetriablePartial(
  caps: RemoteCrewCapabilities | undefined,
): boolean {
  if (!caps || !caps.version_match) return false
  return Object.values(caps.unavailable ?? {}).some(code => RETRIABLE_CAPABILITY_CODES.has(code))
}

/**
 * The bound crew's capabilities for *slot*, or `undefined` for a local session.
 *
 * Keyed by instance so two sessions on the same crew share one fetch, and
 * disabled entirely for a local slot — an ordinary session must not pay a
 * round-trip for a question it never asks.
 *
 * Deliberately NOT retried on failure: the common failure is a peer that
 * disconnected, and hammering a dead tunnel delays the honest "crew unreachable"
 * state the caller renders from `unavailable`.
 *
 * A PARTIAL document from a version-compatible peer is different: the request
 * succeeded, but one field (typically `models`, whose cold read runs real work
 * on the peer) timed out and came back `capability_unreachable`. Caching that
 * as fresh for the full stale window leaves the model picker empty for five
 * minutes on a peer that recovers within seconds, so the query
 * re-polls every `CAPABILITY_REPOLL_MS` until the document is complete —
 * and stops for disconnected, version-skewed, or terminally-failing peers,
 * where a re-read cannot improve the answer.
 */
export function useRemoteCapabilities(slot: ChatSlot | null | undefined) {
  const instanceId = slot?.executor === 'remote' ? slot.instance_id || '' : ''
  const query = useQuery({
    queryKey: ['remote-capabilities', instanceId],
    queryFn: () => api.instancesCapabilities(instanceId),
    enabled: !!instanceId,
    retry: false,
    // The peer's rosters change when someone edits config over there, which is
    // rare and never mid-conversation. A long window keeps the shelf from
    // re-fetching on every tab switch.
    staleTime: 5 * 60 * 1000,
    refetchInterval: q =>
      // Stop on error even when the STALE data is still a retriable partial:
      // a failing poll must hand over to the ErrorNotice + Retry surface
      // rather than hammer the peer and spin the loading row beside an error.
      q.state.status !== 'error' && capabilityDocIsRetriablePartial(q.state.data)
        ? CAPABILITY_REPOLL_MS
        : false,
  })
  return {
    /** True while this session is bound to a peer, regardless of fetch state — so
     *  a caller can switch its data source before the fetch resolves rather than
     *  briefly offering local options for a remote session. */
    isRemote: !!instanceId,
    capabilities: query.data,
    isLoading: query.isLoading,
    /** The read itself failed (not a per-field failure inside a good reply).
     *  Consumers surface this through `ErrorNotice` — a failed read rendered as
     *  an empty picker would claim the peer offers nothing. */
    failed: query.isError,
    /** In-place retry for the failed-read surface, so the user recovers without
     *  a navigation that would discard a composer draft. */
    refetch: query.refetch,
    /** The failed read's in-place retry is in flight. `failed` stays true until
     *  the refetch settles, so without this the Retry button looks dead for the
     *  whole round trip — the button reflects the pending retry instead. */
    retrying: query.isError && query.isFetching,
    /** The peer's model list is not renderable YET, but a fresher answer is on
     *  its way: the first read is still in flight, or the peer answered a
     *  partial document this hook is re-polling. The pickers render a loading
     *  row from this instead of an empty list — empty means "the peer has no
     *  models", which is not what a timed-out cold read says. Never true in
     *  the error state (including a poll that failed AFTER a partial): failed
     *  owns that state, and a loading row beside an ErrorNotice would promise
     *  a retry that is not scheduled. */
    modelsPending:
      !!instanceId &&
      !query.isError &&
      (query.isLoading ||
        (capabilityDocIsRetriablePartial(query.data) && 'models' in (query.data?.unavailable ?? {}))),
  }
}
