/**
 * The one scheduler behind every read of a root's worker list
 * (`GET /api/chat/slots/{root}/crew-workers`), so a long crew cannot fill the
 * browser's 6 connections per origin and queue a composer send behind them.
 *
 * - No overlap: a re-read never cancels and restarts one in flight (callers pass
 *   `cancelRefetch: false`), so a root has at most one list request at a time.
 * - Backoff: `crewWorkersGapMs` is the wait before the next re-read. When the last
 *   answer took longer than the base wait it doubles until it is longer than the
 *   answer (up to `CREW_POLL_MAX_MS`); a fast answer resets it to the base.
 * - Cap: at most `CREW_POLL_CAP` list requests are in flight per origin. A re-read
 *   that finds the cap full and a cached list returns the cached list, so four of
 *   the six connections are always free for sends and everything else.
 *
 * shortcut: the cap counts this route only; other background polls are not
 * counted. Move them onto this scheduler if they start to crowd sends.
 */
import type { QueryClient } from '@tanstack/react-query'
import { api } from '../api/client'

export const CREW_POLL_CAP = 2
export const CREW_POLL_MAX_MS = 60_000

let inFlight = 0
const lastMs = new Map<string, number>()

/** Read the list, timing the answer; a re-read over the cap returns the cached list. */
export async function readCrewWorkers(qc: QueryClient, key: readonly unknown[], root: string) {
  const cached = qc.getQueryData<Awaited<ReturnType<typeof api.crewWorkers>>>(key)
  if (cached && inFlight >= CREW_POLL_CAP) return cached
  inFlight++
  const t0 = Date.now()
  try {
    return await api.crewWorkers(root)
  } finally {
    inFlight--
    lastMs.set(root, Date.now() - t0)
  }
}

/** The wait before the next re-read of this root's list (`baseMs` when answers are fast). */
export function crewWorkersGapMs(root: string, baseMs: number): number {
  const last = lastMs.get(root) ?? 0
  let g = baseMs
  while (g < last && g < CREW_POLL_MAX_MS) g *= 2
  return Math.max(baseMs, Math.min(g, CREW_POLL_MAX_MS))
}

/** Test seam: forget the schedule. */
export function resetCrewWorkersPoll() {
  inFlight = 0
  lastMs.clear()
}
