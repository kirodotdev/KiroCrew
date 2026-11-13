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
 * - Cap: at most `CREW_POLL_CAP` list requests are in flight per origin. A read that
 *   finds the cap full (first read or re-read) waits its turn in a FIFO queue and
 *   then reads fresh data, so four of the six connections are always free for sends
 *   and everything else. A slot is released in `finally`, so a rejected read frees it.
 *
 * shortcut: the cap counts this route only; other background polls are not
 * counted. Move them onto this scheduler if they start to crowd sends.
 */
import { api } from '../api/client'

export const CREW_POLL_CAP = 2
export const CREW_POLL_MAX_MS = 60_000

let inFlight = 0
const waiters: Array<() => void> = []
const lastMs = new Map<string, number>()

/** Take a slot, in arrival order. A freed slot is handed straight to the first waiter. */
const acquire = (): Promise<void> => {
  if (inFlight < CREW_POLL_CAP) { inFlight++; return Promise.resolve() }
  return new Promise(resolve => waiters.push(resolve))
}
const release = () => {
  const next = waiters.shift()
  if (next) next()
  else inFlight--
}

/** Record how long a read of `id` took; `crewWorkersGapMs` backs off from it. `lib/pollRule` records its reads here too. */
export function noteReadMs(id: string, ms: number) {
  lastMs.set(id, ms)
}

/** Read the list, waiting for a free slot when the cap is full; times the answer. */
export async function readCrewWorkers(root: string) {
  await acquire()
  const t0 = Date.now()
  try {
    return await api.crewWorkers(root)
  } finally {
    release()
    noteReadMs(root, Date.now() - t0)
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
  waiters.length = 0
  lastMs.clear()
}
