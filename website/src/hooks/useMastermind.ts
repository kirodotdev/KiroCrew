/**
 * The Mastermind session: one chat (the slot keyed exactly `mastermind`) whose
 * memory is a summary tree that never fills up. The backend owns it: enabling
 * creates and pins the slot server-side, and while it is enabled the slot
 * refuses close / archive / delete with 409 `mastermind_protected`.
 *
 * One query key for every reader (the Sessions sidebar decorates the row from
 * it, Settings > Mastermind shows the stats), so a toggle or a clear on the
 * Settings page refreshes the sidebar too.
 */
import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useAppDispatch, useAppSelector } from '../store'
import { setMastermindEnabledState } from '../store/dashboardSlice'
import { api, type MastermindInfo } from '../api/client'
import { MASTERMIND_SLOT_KEY } from '../lib/mastermind'

export { MASTERMIND_SLOT_KEY, isMastermindProtected } from '../lib/mastermind'

/** Shared react-query key for `GET /api/mastermind`. */
export const MASTERMIND_QUERY_KEY = ['mastermind'] as const

/** Shared react-query key prefix for the memory view and its zooms. */
export const MASTERMIND_VIEW_QUERY_KEY = ['mastermind', 'view'] as const

/**
 * `GET /api/mastermind`, polled every `refreshMs`. React Query pauses the poll
 * while the tab is hidden. `enabled: false` reads nothing (and returns no data).
 */
export function useMastermindInfo({ enabled = true, refreshMs }: { enabled?: boolean; refreshMs: number }) {
  const query = useQuery<MastermindInfo>({
    queryKey: MASTERMIND_QUERY_KEY,
    queryFn: () => api.mastermind(),
    enabled,
    staleTime: Math.min(refreshMs, 30_000) / 2,
    refetchInterval: refreshMs,
  })
  // Mirrored into the store, where the close thunk and the session menus read it.
  // Only a definite answer is mirrored: an unanswered read keeps the last one.
  const dispatch = useAppDispatch()
  const known = typeof query.data?.enabled === 'boolean' ? query.data.enabled : null
  useEffect(() => {
    if (known !== null) dispatch(setMastermindEnabledState(known))
  }, [known, dispatch])
  return query
}

/** Is `slotKey` the Mastermind session while it is enabled (the store's mirror of
 *  the last status read)? Fetches nothing, so a per-row menu can call it. */
export function useMastermindLocked(slotKey: string): boolean {
  const enabled = useAppSelector(s => s.dashboard.mastermindEnabled === true)
  return slotKey === MASTERMIND_SLOT_KEY && enabled
}
