/**
 * useCrewWarmSet — which remote Crews stay warm (lib/crewWarmSet.ts), from the
 * shared ['instances'] and relay Crew list caches, and the opened order
 * useCrewWarmKeeper writes.
 *
 * Relay Crews count only in the desktop shell with the pane bridge: a browser
 * opens a relay Crew in another tab, so no pane of this window can hold it.
 */
import { useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import { isEmbeddedPane } from '../lib/embedded'
import { relayPaneBridge } from '../lib/relayCrewPanes'
import { rankWarmCrews, recentCrews, subscribeRecentCrews, type WarmCrews } from '../lib/crewWarmSet'
import { WARM_SET_CAP_AUTO_CEILING, hasDashboardPane } from '../utils/remoteCrew'
import { isInstancesDisabledError } from '../utils/instancesDisabled'
import { RELAY_VIEWER_CREWS_KEY, useRelayViewerCrews } from './useRelayViewerCrews'

function relayListAnswered(queryClient: QueryClient): boolean {
  const s = queryClient.getQueryState(RELAY_VIEWER_CREWS_KEY)
  return !!s && (s.dataUpdatedAt > 0 || s.errorUpdatedAt > 0)
}

/** Whether the relay Crew list has answered once, with a list or a refusal. Until then
 *  the set is not ranked, so SSH crews do not take slots a relay Crew is due. */
function useRelayListSettled(): boolean {
  const queryClient = useQueryClient()
  const [settled, setSettled] = useState(() => relayListAnswered(queryClient))
  useEffect(() => {
    if (settled) return
    return queryClient.getQueryCache().subscribe(() => { if (relayListAnswered(queryClient)) setSettled(true) })
  }, [queryClient, settled])
  return settled
}

/** The Crews to keep warm now, with the opened order they were ranked from, or null until the
 *  lists they come from have answered. */
export function useCrewWarmSet(): (WarmCrews & { order: string[] }) | null {
  const embedded = isEmbeddedPane()
  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    enabled: !embedded,
  })
  const { crews } = useRelayViewerCrews(!embedded)
  const relaySettled = useRelayListSettled()
  const order = useSyncExternalStore(subscribeRecentCrews, recentCrews)
  const panes = relayPaneBridge() !== null
  const data = instancesQuery.data
  // Remote Crew (SSH) turned off: no SSH crews, and the relay Crews keep the automatic cap.
  const sshOff = !data && isInstancesDisabledError(instancesQuery.error)
  return useMemo(() => {
    if (embedded || (!data && !sshOff) || (panes && !relaySettled)) return null
    const ssh = (data?.instances ?? [])
      .filter(i => !i.disabled && !i.cloud_stopped && hasDashboardPane(i))
      .map(i => i.id)
    const relay = panes ? crews.map(c => c.endpointId) : []
    const cap = data?.warm_set_cap || WARM_SET_CAP_AUTO_CEILING
    return { ...rankWarmCrews(order, ssh, relay, cap), order }
  }, [embedded, data, sshOff, panes, relaySettled, crews, order])
}
