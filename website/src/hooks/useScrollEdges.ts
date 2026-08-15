import { useCallback, useEffect, useRef, useState } from 'react'

export interface ScrollEdges {
  /** Content is hidden past the scroller's left edge. */
  left: boolean
  /** Content is hidden past the scroller's right edge. */
  right: boolean
}

/**
 * Which edges of a horizontal scroller hide content, measured rather than
 * inferred from a breakpoint — a strip inside a resizable pane overflows at
 * widths the viewport knows nothing about.
 *
 * This exists because a scroller with a hidden scrollbar reads as COMPLETE: the
 * row simply ends, and nothing says four of seventeen tabs are on screen. The
 * caller paints an edge cue from these flags so the clipping is visible, which
 * is the signal a horizontal overflow needs (a hidden scrollbar leaves none,
 * and a tooltip or a scroll-position dot is not a substitute on touch).
 *
 * Physical `left`/`right`, not logical start/end: every shipped locale is LTR,
 * so a logical mapping would be untested indirection.
 *
 * `remeasure` is returned for content changes a ResizeObserver cannot see — the
 * scroller keeps its own box while its children change, e.g. a tab appearing
 * behind a feature flag.
 */
export function useScrollEdges<T extends HTMLElement>(): [React.RefObject<T>, ScrollEdges, () => void] {
  const ref = useRef<T>(null)
  const [edges, setEdges] = useState<ScrollEdges>({ left: false, right: false })

  const remeasure = useCallback(() => {
    const el = ref.current
    if (!el) return
    const hidden = el.scrollWidth - el.clientWidth
    const scrolled = el.scrollLeft
    // 1px of slack: fractional layout widths leave scrollWidth a hair above
    // clientWidth on a row that is not actually scrollable, and painting a
    // permanent cue on a row that fits is its own lie.
    const next = { left: scrolled > 1, right: hidden - scrolled > 1 }
    // Same-value writes are dropped so a scroll event per frame does not
    // re-render the whole page shell while the strip is being dragged.
    setEdges(prev => (prev.left === next.left && prev.right === next.right ? prev : next))
  }, [])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    remeasure()
    el.addEventListener('scroll', remeasure, { passive: true })
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(remeasure)
    ro?.observe(el)
    return () => {
      el.removeEventListener('scroll', remeasure)
      ro?.disconnect()
    }
  }, [remeasure])

  return [ref, edges, remeasure]
}
