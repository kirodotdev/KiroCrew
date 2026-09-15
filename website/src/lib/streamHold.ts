/**
 * A hold on per-frame streaming flushes while a UI surface is animating.
 *
 * WHY THIS EXISTS. The panel slides run on the compositor — `animateDrawer`
 * (hooks/useDrawerSwipe.ts) carries that rationale and the projection
 * precondition it rests on. This hold covers what the compositor cannot: the
 * work still on the main thread during a slide — the panel's own mount, tool
 * events, subagent status pushes — and the drag path, whose gesture-follow is
 * main-thread by nature. Starving those of their per-frame trigger is cheap
 * insurance on both paths.
 *
 * While a session streams, nearly all main-thread churn is the per-rAF flush
 * pipelines in `useWebSocket` (chunk / subagent-chunk / slot-activity), each of
 * which dispatches into the store and re-renders ChatPage once per frame. Those
 * pipelines already BUFFER between flushes, so deferring the flush costs
 * nothing but latency: this module is the shared clock that tells them to sit
 * out the slide. Nothing is ever dropped — a deferred flush delivers the whole
 * buffer when the hold lapses.
 *
 * Deliberately a deadline, not a lock: every hold carries its own expiry, so an
 * animation that dies without releasing (a stopped tween, an unmounted page)
 * degrades to a short delay instead of a stuck firehose. `HOLD_MAX_MS` caps
 * hostile or buggy callers.
 */

/** No single hold may exceed this. A slide is ~420ms; a full second of frozen
 *  transcript is already at the edge of what reads as "paused on purpose". */
const HOLD_MAX_MS = 1_000

let holdUntil = 0

const now = () =>
  typeof performance !== 'undefined' && typeof performance.now === 'function'
    ? performance.now()
    : Date.now()

/** Quiet the streaming flush pipelines for the next `ms` milliseconds.
 *  Extends an active hold, never shortens it. */
export function holdStreamingFlushes(ms: number): void {
  const deadline = now() + Math.max(0, Math.min(ms, HOLD_MAX_MS))
  if (deadline > holdUntil) holdUntil = deadline
}

/** The animation finished early (or was taken over): let flushes resume on the
 *  next frame instead of waiting out the deadline. */
export function releaseStreamingFlushes(): void {
  holdUntil = 0
}

/** How much longer flushes should stay quiet, in ms. 0 = not held. */
export function streamingFlushHoldMs(): number {
  return Math.max(0, holdUntil - now())
}
