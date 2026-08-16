import { useCallback, useRef, useState } from 'react'

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
 * Returns a CALLBACK ref, and that is load-bearing: the scroller can mount
 * later than the component holding this hook — a strip that exists only below a
 * breakpoint appears on a mid-session resize, long after mount. A mount-only
 * effect would see a null node, attach nothing, and never run again, leaving the
 * cues frozen while the reader scrolls. Binding from the ref callback attaches
 * whenever the node arrives and detaches when it leaves.
 *
 * `remeasure` is for content changes no observer reports — the scroller keeps
 * its own box while its children change, e.g. a tab appearing behind a flag.
 */
export function useScrollEdges<T extends HTMLElement>(): [(node: T | null) => void, ScrollEdges, () => void] {
  const elRef = useRef<T | null>(null)
  const detachRef = useRef<(() => void) | null>(null)
  const [edges, setEdges] = useState<ScrollEdges>({ left: false, right: false })

  const remeasure = useCallback(() => {
    const el = elRef.current
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

  // Stable, so React does not detach and re-attach on every render.
  const attach = useCallback((node: T | null) => {
    detachRef.current?.()
    detachRef.current = null
    elRef.current = node
    if (!node) {
      // No scroller means nothing is clipped; a surviving cue would point at
      // content that is not there.
      setEdges({ left: false, right: false })
      return
    }
    remeasure()
    node.addEventListener('scroll', remeasure, { passive: true })
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(remeasure)
    ro?.observe(node)
    detachRef.current = () => {
      node.removeEventListener('scroll', remeasure)
      ro?.disconnect()
    }
  }, [remeasure])

  return [attach, edges, remeasure]
}
