/**
 * One rule for a background read of a slow endpoint, so a few open windows cannot
 * fill the browser's 6 connections per origin and queue a composer send behind them.
 *
 * - Polled only while the owning panel/tab is selected (`usePollRule`); react-query
 *   already pauses `refetchInterval` while the document is hidden. Coming back
 *   (selected and visible again) re-reads once.
 * - Single flight, with one owed re-read: `refreshDashboardReads` (api/dashboardRefresh).
 * - Backoff: the gap is `crewWorkersGapMs` (it doubles past a slow answer), doubled
 *   again for each consecutive 502/503/504, dropped link or deadline.
 * - Pushes (`requestRefresh`): a push marks the key dirty; a dirty key is drained (one
 *   read now, one trailing read per gap) whenever the document is visible, so a push that
 *   cannot be read yet (hidden) is read once on return.
 */
import { useQueryClient, type QueryClient, type QueryKey } from '@tanstack/react-query'
import { useEffect, useRef, useSyncExternalStore } from 'react'
import { refreshDashboardReads } from '../api/dashboardRefresh'
import { isDeadlineError, isUnansweredRead } from '../api/queryClient'
import { CREW_POLL_MAX_MS, crewWorkersGapMs, noteReadMs, resetCrewWorkersPoll } from './crewWorkersPoll'

const failures = new Map<string, number>()
interface Pushes {
  /** When a pushed read last started, per id. */
  lastRun: Map<string, number>
  /** The one read queued behind the gap, per id. */
  trailing: Map<string, ReturnType<typeof setTimeout>>
  /** Pushes not yet read: the key to re-read, per id. */
  dirty: Map<string, { queryKey: QueryKey; baseMs: number }>
}
const pushes = new WeakMap<QueryClient, Pushes>()

export const documentVisible = (): boolean => typeof document === 'undefined' || document.visibilityState !== 'hidden'

const subscribeVisibility = (cb: () => void) => {
  document.addEventListener('visibilitychange', cb)
  return () => document.removeEventListener('visibilitychange', cb)
}

/** Whether the document is visible, re-rendering on change. */
const useDocumentVisible = (): boolean => useSyncExternalStore(subscribeVisibility, documentVisible, () => true)

/** Run one read of `id`, timing the answer and counting a consecutive failed one. */
export async function timedPoll<T>(id: string, read: () => Promise<T>): Promise<T> {
  const t0 = Date.now()
  try {
    const value = await read()
    failures.delete(id)
    return value
  } catch (error) {
    if (isUnansweredRead(error) || isDeadlineError(error)) failures.set(id, (failures.get(id) ?? 0) + 1)
    throw error
  } finally {
    noteReadMs(id, Date.now() - t0)
  }
}

/** The wait before the next read of `id` (`baseMs` when answers are fast and healthy). */
export function pollGapMs(id: string, baseMs: number): number {
  return Math.min(crewWorkersGapMs(id, baseMs) * 2 ** Math.min(failures.get(id) ?? 0, 6), CREW_POLL_MAX_MS)
}

/**
 * The `refetchInterval` for one polled `useQuery`; wrap its `queryFn` in
 * `timedPoll(id, ...)` so slow and failed answers stretch the gap.
 * `selected` is whether the owning panel/tab is the one on screen.
 */
export function usePollRule(id: string, baseMs: number, selected: boolean, queryKey: QueryKey) {
  const queryClient = useQueryClient()
  const live = useDocumentVisible() && selected
  const wasLive = useRef(live)
  const key = useRef(queryKey)
  key.current = queryKey
  useEffect(() => {
    if (live && !wasLive.current) refreshDashboardReads(queryClient, key.current)
    wasLive.current = live
  }, [live, queryClient])
  return (): number | false => (selected ? pollGapMs(id, baseMs) : false)
}

/** Read the dirty key `id` once: now, or after the gap; nothing while hidden (return drains it). */
function drain(queryClient: QueryClient, mine: Pushes, id: string): void {
  const entry = mine.dirty.get(id)
  if (!entry || mine.trailing.has(id) || !documentVisible()) return
  const run = () => {
    mine.trailing.delete(id)
    if (!documentVisible()) return
    mine.dirty.delete(id)
    mine.lastRun.set(id, Date.now())
    refreshDashboardReads(queryClient, entry.queryKey)
  }
  const wait = (mine.lastRun.get(id) ?? 0) + pollGapMs(id, entry.baseMs) - Date.now()
  if (wait <= 0) run()
  else mine.trailing.set(id, setTimeout(run, wait))
}

/**
 * A push (socket frame, list change) asks for a re-read of `queryKey`. The key is marked
 * stale and dirty at once. A dirty key is drained when the document is visible: the read
 * is single flight, a push that lands during one owes one more (`refreshDashboardReads`),
 * and bursts inside the gap collapse into one trailing read. A push while hidden stays
 * dirty and is drained once when the document is visible again.
 *
 * shortcut: one visibilitychange listener per client, never removed; clients live as long
 * as the page.
 */
export function requestRefresh(queryClient: QueryClient, id: string, queryKey: QueryKey, baseMs: number): void {
  void queryClient.invalidateQueries({ queryKey, refetchType: 'none' })
  let mine = pushes.get(queryClient)
  if (!mine) {
    const created: Pushes = (mine = { lastRun: new Map(), trailing: new Map(), dirty: new Map() })
    pushes.set(queryClient, created)
    document.addEventListener('visibilitychange', () => created.dirty.forEach((_, dirtyId) => drain(queryClient, created, dirtyId)))
  }
  mine.dirty.set(id, { queryKey, baseMs })
  drain(queryClient, mine, id)
}

/** Test seam: forget every answer time and failure count. */
export function resetPollRule(): void {
  failures.clear()
  resetCrewWorkersPoll()
}
