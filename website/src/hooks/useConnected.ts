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
