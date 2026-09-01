/**
 * Health signal for the shared Shiki highlight worker pool.
 *
 * `@pierre/diffs`' WorkerPoolManager handles a worker `error` event by logging
 * it and nothing else — the pending highlight request is never rejected, the
 * worker is never respawned, and no request carries a timeout. A worker that
 * dies (or a worker module that fails to LOAD, which also fires `error` on the
 * Worker object) therefore leaves every queued highlight pending forever, and
 * a Pierre surface that is waiting on one paints no rows at all. In chat that
 * shows up as a code block rendered down to its language header with an empty
 * body — the text is still in the DOM-free `code` prop, so Copy works while
 * nothing is visible.
 *
 * We own the `workerFactory`, so we own error detection: the factory reports
 * here, and every mounted surface re-renders with `disableWorkerPool`, which
 * makes Pierre tokenize on the main thread instead. Slower on a huge file, but
 * it paints — and it recovers the already-empty blocks rather than only the
 * next one, because the re-render re-initializes their `<File>` instances.
 *
 * One-way on purpose: a pool that has dropped a worker mid-session is not
 * observably healthy again (the manager keeps no per-worker readiness we can
 * read), so flipping back would risk a second silent blank. A reload restores
 * the worker path.
 */
import { useSyncExternalStore } from 'react'

let broken = false
const listeners = new Set<() => void>()

export function isWorkerPoolBroken(): boolean {
  return broken
}

/** Record that the highlight worker pool can no longer be trusted. Idempotent:
 *  a pool of N workers can fire N error events for one root cause, and only the
 *  first needs to warn or notify. */
export function markWorkerPoolBroken(reason?: unknown): void {
  if (broken) return
  broken = true
  // Deliberately loud: the pre-existing behavior of this failure was a silently
  // blank code block, which is the thing that made it hard to diagnose.
  // eslint-disable-next-line no-console -- failure diagnostic; the fallback below is the user-visible part
  console.warn(
    'Pierre highlight worker failed; falling back to main-thread tokenization for code and diff surfaces.',
    reason,
  )
  for (const listener of listeners) listener()
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)
  return () => {
    listeners.delete(onChange)
  }
}

/** Subscribe a Pierre surface to the pool's health. Returns the value to pass
 *  as `disableWorkerPool`. */
export function useWorkerPoolBroken(): boolean {
  return useSyncExternalStore(subscribe, isWorkerPoolBroken, isWorkerPoolBroken)
}
