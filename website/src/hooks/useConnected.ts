import { useAppSelector } from '../store'

/**
 * Single source of truth for the dashboard↔gateway connection flag.
 *
 * Wraps the dashboardSlice selector so store-connected components don't each
 * re-derive it, and gives one seam to evolve later (e.g. distinguishing
 * ws-offline from auth-offline). Presentational children that can't reach the
 * store (e.g. ChatInput) keep receiving `connected` as a prop — this hook is
 * for store-connected components only.
 */
export function useConnected(): boolean {
  return useAppSelector(s => s.dashboard.connected)
}

/**
 * True while this page's first socket is still opening. `connected` starts
 * false, so until that socket opens or closes nothing has failed yet: the
 * shell and the composer say they are connecting instead of reporting an
 * outage. A first socket that closes without opening is an outage like any
 * later drop.
 */
export function useGatewayConnecting(): boolean {
  return useAppSelector(s => !s.dashboard.connected && s.dashboard.firstSocketPending)
}
