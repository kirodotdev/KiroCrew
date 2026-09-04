import { useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { ChatSlot, RemoteCrewCapabilities } from '../types'

export type { RemoteCrewCapabilities }

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
  })
  return {
    /** True while this session is bound to a peer, regardless of fetch state — so
     *  a caller can switch its data source before the fetch resolves rather than
     *  briefly offering local options for a remote session. */
    isRemote: !!instanceId,
    capabilities: query.data,
    isLoading: query.isLoading,
    /** The read itself failed (not a per-field failure inside a good reply). */
    failed: query.isError,
  }
}
