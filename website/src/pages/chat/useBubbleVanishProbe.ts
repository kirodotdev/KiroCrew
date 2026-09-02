// Diagnostic probe for the "recent chat bubbles briefly disappear then
// reappear" symptom. Opt-in via localStorage (see BUBBLE_PROBE_FLAG); when the
// flag is absent this hook attaches nothing and costs nothing.
//
// The one observation that splits that symptom into a store bug or a renderer
// bug is whether the vanished rows are absent from the store at that moment or
// present-but-unrendered. This probe captures exactly that: a MutationObserver
// watches the transcript scroller, and whenever the number of mounted message
// rows DROPS between frames it logs the DOM count against the store's message
// count and the grouped display-item count at the same instant.
//
// Reading the log:
//   - `storeMessages` fell with `mountedRows`     → store/fetch path.
//   - `displayItems` fell but `storeMessages` held → grouping collapse
//     (loose rows folding into one turn — expected while a turn accumulates).
//   - both held while `mountedRows` fell           → windowing/render path.
import { useEffect, type RefObject } from 'react'

/** Set `localStorage[BUBBLE_PROBE_FLAG] = '1'` and reload to enable. */
export const BUBBLE_PROBE_FLAG = 'kirocrew_debug_bubble_probe'

export interface BubbleProbeCounts {
  /** Raw message count in the store for the active slot. */
  store: number
  /** Grouped display-item count handed to the virtualizer. */
  display: number
}

/**
 * Watch `scrollerRef`'s subtree for drops in the mounted-row count and log a
 * snapshot for each. `getCounts` must be identity-stable (read refs inside);
 * `rearmKey` re-attaches the observer when the scroller node can have been
 * replaced (e.g. a slot switch).
 */
export function useBubbleVanishProbe(
  scrollerRef: RefObject<HTMLDivElement | null>,
  getCounts: () => BubbleProbeCounts,
  rearmKey?: unknown,
): void {
  useEffect(() => {
    let enabled = false
    try {
      enabled = localStorage.getItem(BUBBLE_PROBE_FLAG) === '1'
    } catch {
      return // storage unavailable (privacy mode) — probe stays off
    }
    if (!enabled || typeof MutationObserver === 'undefined') return
    const el = scrollerRef.current
    if (!el) return
    const mountedRows = () => el.querySelectorAll('[data-display-index]').length
    let last = mountedRows()
    let raf = 0
    const mo = new MutationObserver(() => {
      // Coalesce a mutation burst into one per-frame reading — a React commit
      // fires many records for what is a single transcript state. Cancel-and-
      // reschedule (never latch on a pending handle): a handle whose callback
      // never fires would otherwise block every later reading permanently.
      if (raf) cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        raf = 0
        const now = mountedRows()
        if (now < last) {
          const counts = getCounts()
          // eslint-disable-next-line no-console
          console.warn('[bubbleProbe] mounted rows dropped', {
            mountedBefore: last,
            mountedAfter: now,
            storeMessages: counts.store,
            displayItems: counts.display,
            at: new Date().toISOString(),
          })
        }
        last = now
      })
    })
    mo.observe(el, { childList: true, subtree: true })
    return () => {
      mo.disconnect()
      if (raf) cancelAnimationFrame(raf)
    }
  }, [scrollerRef, getCounts, rearmKey])
}
