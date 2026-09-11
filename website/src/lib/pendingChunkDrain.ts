/**
 * Synchronous drain of useWebSocket's pending chunk buffer, reachable from
 * outside the hook.
 *
 * WHY THIS EXISTS. Chat chunks are buffered in `useWebSocket` and flushed to
 * Redux once per animation frame. A mid-turn steer dispatched inside that
 * window (between buffer-in and flush-out) finds no streaming row in Redux, so
 * finalize-on-steer has nothing to freeze: the pre-steer text later flushes
 * BELOW the steer card and post-steer chunks concatenate onto the same
 * streaming row. Every steer insertion site drains this buffer first, so the
 * trailing streaming row exists and is finalized ABOVE the card, and the next
 * chunk opens a fresh streaming row below it.
 *
 * Same module-singleton shape as lib/streamHold.ts: the hook registers its
 * flush on mount, steer initiators call the drain without needing the hook
 * instance. With no hook mounted the drain is a no-op -- there is no buffer.
 */

let drain: (() => void) | null = null

/** Register the hook's synchronous flush. Returns the unregister function
 *  (identity-guarded, so a stale unmount cannot drop a newer registration). */
export function registerPendingChunkDrain(fn: () => void): () => void {
  drain = fn
  return () => { if (drain === fn) drain = null }
}

/** Flush any buffered streaming chunks to Redux now. Call BEFORE dispatching a
 *  steer row, so finalize-on-steer sees the full pre-steer text. */
export function drainPendingChunks(): void {
  drain?.()
}
