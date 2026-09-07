// Diagnostic probe for the "recent chat bubbles briefly disappear then
// reappear" symptom. Opt-in via localStorage (see BUBBLE_PROBE_FLAG); when the
// flag is absent this hook attaches nothing and costs nothing.
//
// The one observation that splits that symptom into a store bug or a renderer
// bug is whether the vanished rows are absent from the store at that moment or
// present-but-unrendered. This probe captures exactly that: a MutationObserver
// watches the transcript scroller, and whenever the number of mounted message
// rows DROPS between frames — or the number of VISIBLE rows drops while the
// mounted count holds — it logs the DOM counts against the store's message
// count and the grouped display-item count at the same instant.
//
// Reading the log:
//   - `storeMessages` fell with `mountedRows`     → store/fetch path.
//   - `displayItems` fell but `storeMessages` held → grouping collapse
//     (loose rows folding into one turn — expected while a turn accumulates).
//   - both held while `mountedRows` fell           → windowing/render path.
//   - `mountedRows` held while `visibleRows` fell  → a mounted row's content
//     was hidden in place (turn collapse via CollapsibleSection's height-0
//     container, marked `data-collapsed="true"`), NOT the windowing path.
//   - `mountedRows` AND `visibleRows` both fell    → an unmount and a collapse
//     landed in the same frame — compare the two deltas before blaming either
//     subsystem for the whole drop.
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
    // Rows whose content is not hidden inside a collapsed turn container.
    // `data-display-index` sits on the virtualizer's row wrapper OUTSIDE
    // TurnBlock, and CollapsibleSection renders INSIDE the row — so the fold
    // is a DESCENDANT of the row, and the right test is "does this row contain
    // a collapsed fold", not `closest()` up the ancestor chain. A collapsed
    // fold keeps its children mounted while animating height to 0, so this
    // count moves when mountedRows does not.
    const visibleRows = () =>
      Array.from(el.querySelectorAll('[data-display-index]')).filter(
        row => !row.querySelector('[data-collapsed="true"]'),
      ).length
    let last = mountedRows()
    let lastVisible = visibleRows()
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
        const nowVisible = visibleRows()
        const mountedDropped = now < last
        // Fourth bucket: mounted rows held while visible rows fell — a row was
        // hidden IN PLACE (turn collapse), not unmounted by the windowing path.
        const hiddenInPlace = !mountedDropped && nowVisible < lastVisible
        if (mountedDropped || hiddenInPlace) {
          const counts = getCounts()
          // eslint-disable-next-line no-console
          console.warn(
            hiddenInPlace
              ? '[bubbleProbe] row content hidden in place (mounted held, visible fell)'
              : '[bubbleProbe] mounted rows dropped',
            {
              kind: hiddenInPlace ? 'hidden-in-place' : 'mounted-drop',
              mountedBefore: last,
              mountedAfter: now,
              visibleBefore: lastVisible,
              visibleAfter: nowVisible,
              storeMessages: counts.store,
              displayItems: counts.display,
              at: new Date().toISOString(),
            },
          )
        }
        last = now
        lastVisible = nowVisible
      })
    })
    // `attributes` + the filter: a turn collapse toggles `data-collapsed` on an
    // existing container and changes NO children, so without attribute
    // observation that transition would never produce a reading.
    mo.observe(el, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['data-collapsed'],
    })
    return () => {
      mo.disconnect()
      if (raf) cancelAnimationFrame(raf)
    }
  }, [scrollerRef, getCounts, rearmKey])
}
