import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { TunnelStatus } from '../api/client/system'
import { originOf, setInternalNavHandler } from '../utils/internalNav'

/**
 * Install the in-app link navigator (`utils/internalNav`) for this window.
 *
 * The origins that count as this dashboard, besides the page's own, are the
 * configured `dashboard.url` and the tunnel's public URL, each by its exact
 * origin. Either may be unset, and a failed read leaves only the page's own
 * origin, so a link to the dashboard's other address then opens in a tab as
 * before. No loopback alias is added: `localhost` and `127.0.0.1` on another
 * port may be a different gateway.
 */
export function useInternalNavHandler(enabled: boolean, open: (path: string) => void): void {
  const { data: config } = useQuery<{ dashboard?: { url?: unknown } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled,
    staleTime: 60_000,
  })
  const { data: tunnel } = useQuery<TunnelStatus>({
    queryKey: ['tunnel-status'],
    queryFn: () => api.tunnelStatus(),
    enabled,
    staleTime: 60_000,
  })
  const dashboardOrigin = originOf(config?.dashboard?.url)
  const tunnelOrigin = originOf(tunnel?.url)
  const origins = useMemo(
    () => [dashboardOrigin, tunnelOrigin].filter((o): o is string => o !== null),
    [dashboardOrigin, tunnelOrigin],
  )
  useEffect(() => {
    if (!enabled) return
    return setInternalNavHandler({ origins, open })
  }, [enabled, origins, open])
}
