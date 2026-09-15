/**
 * Shared scroll-quiescence signal between the transcript scroller (writer) and
 * the paging thunks (waiters).
 *
 * WHY THIS EXISTS: the older-history trigger already refuses to FIRE until the
 * scroller has settled, but on a slow network the RESPONSE arrives during the
 * reader's next gesture. Splicing hundreds of rows into the transcript while a
 * scroll (especially an iOS momentum glide) is in flight puts the pre-paint
 * anchor machinery in a race against the pixel-addressed window recompute the
 * gesture itself schedules -- measured on the 4x-throttled phone rig as
 * per-landing kilopixel jumps whose compensation consumed against a mis-bound
 * node (delta 0) and stood down. Landing is CHEAP to defer and catastrophic to
 * interleave, so the thunk holds the payload here until the scroller has been
 * quiet for a beat; the FETCH still overlaps the gesture, so no latency is
 * added to the data, only to the mutation.
 *
 * Module-level rather than React state: the writer is a passive scroll handler
 * that must stay allocation-free, and the waiter is a redux thunk with no
 * component scope. One transcript scroller exists per app instance.
 */

/** Scroller must be quiet this long before an older page may land. */
export const OLDER_FLUSH_QUIET_MS = 160

/** Never hold a fetched page longer than this: a reader who keeps flinging
 *  upward is exactly the reader waiting for that page, so land it on the next
 *  gap even if quiescence never arrives (spinner-wedge backstop). */
export const OLDER_FLUSH_MAX_WAIT_MS = 2500

let lastScrollActivityAt = 0

/** Called by the transcript scroller on every USER scroll event (self-scroll
 *  pin writes are filtered by the caller -- our own corrections must not hold
 *  the flush hostage). */
export function noteUserScrollActivity(): void {
  lastScrollActivityAt = Date.now()
}

/** Test seam. */
export function resetScrollQuiet(): void {
  lastScrollActivityAt = 0
}

/**
 * Resolve once the scroller has been quiet for OLDER_FLUSH_QUIET_MS, or after
 * OLDER_FLUSH_MAX_WAIT_MS, whichever comes first. Polling (rather than a
 * subscriber list) keeps the writer side a single timestamp store.
 */
export function whenScrollQuiet(signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const started = Date.now()
    const tick = () => {
      if (signal?.aborted) { resolve(); return }
      const now = Date.now()
      if (now - lastScrollActivityAt >= OLDER_FLUSH_QUIET_MS) { resolve(); return }
      if (now - started >= OLDER_FLUSH_MAX_WAIT_MS) { resolve(); return }
      setTimeout(tick, 50)
    }
    tick()
  })
}
