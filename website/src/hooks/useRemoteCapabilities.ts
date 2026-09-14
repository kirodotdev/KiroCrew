import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { ChatSlot, RemoteCrewCapabilities } from '../types'

export type { RemoteCrewCapabilities }

export const REMOTE_CAPABILITIES_RECOVERY_INTERVAL_MS = 8_000

/** A partial response from a reachable, version-compatible peer is transient.
 *
 * The capability endpoint deliberately answers 200 when one roster read fails,
 * carrying that field in `unavailable`. Without a refetch interval React Query
 * treats the partial document as fresh for five minutes, leaving an empty picker
 * long after a cold `/api/models` request has recovered. A disconnected or
 * version-skewed peer does not poll: neither can serve a pick, and retrying it on
 * a timer would only hammer a dead tunnel.
 */
export function remoteCapabilitiesRefetchInterval(query: {
  state: { data?: RemoteCrewCapabilities }
}): number | false {
  const capabilities = query.state.data
  return capabilities?.version_match && Object.keys(capabilities.unavailable).length > 0
    ? REMOTE_CAPABILITIES_RECOVERY_INTERVAL_MS
    : false
}

/**
 * The bound crew's capabilities for *slot*, or `undefined` for a local session.
 *
 * Keyed by instance so two sessions on the same crew share one fetch, and
 * disabled entirely for a local slot — an ordinary session must not pay a
 * round-trip for a question it never asks.
 *
 * Request-level failures are deliberately NOT retried: the common failure is a
 * disconnected peer. Successful partial responses from a compatible peer use
 * `remoteCapabilitiesRefetchInterval` instead, so one cold roster can recover
 * without polling a peer that is wholly unreachable.
 */
export function useRemoteCapabilities(slot: ChatSlot | null | undefined) {
  const instanceId = slot?.executor === 'remote' ? slot.instance_id || '' : ''
  const query = useQuery({
    queryKey: ['remote-capabilities', instanceId],
    queryFn: () => api.instancesCapabilities(instanceId),
    enabled: !!instanceId,
    retry: false,
    refetchInterval: remoteCapabilitiesRefetchInterval,
    // The peer's rosters change when someone edits config over there, which is
    // rare and never mid-conversation. A long window keeps the shelf from
    // re-fetching on every tab switch; the recovery interval above is explicit
    // and still runs while a partial response is fresh.
    staleTime: 5 * 60 * 1000,
  })
  const modelsUnavailable = !!(
    query.data?.version_match && query.data.unavailable.models
  )
  return {
    /** True while this session is bound to a peer, regardless of fetch state — so
     *  a caller can switch its data source before the fetch resolves rather than
     *  briefly offering local options for a remote session. */
    isRemote: !!instanceId,
    capabilities: query.data,
    isLoading: query.isLoading,
    /** True until a compatible peer has returned a usable model roster. */
    modelsLoading: query.isLoading || modelsUnavailable,
    /** The read itself failed (not a per-field failure inside a good reply). */
    failed: query.isError,
  }
}
