import { useRef, useEffect } from 'react'

interface SwipeEdgeOptions {
  enabled: boolean
  edge?: 'left' | 'right'
  edgeZone?: number
  threshold?: number
  onSwipe: () => void
}

/**
 * Nearest ancestor of `from`, up to and including `root`, that scrolls
 * horizontally. Returns null when the touch did not start inside one.
 */
function findHorizontalScroller(from: EventTarget | null, root: HTMLElement): HTMLElement | null {
  let node: Element | null = from instanceof Element ? from : null
  while (node) {
    if (node instanceof HTMLElement && node.scrollWidth - node.clientWidth > 1) {
      const overflowX = getComputedStyle(node).overflowX
      if (overflowX === 'auto' || overflowX === 'scroll') return node
    }
    if (node === root) break
    node = node.parentElement
  }
  return null
}

export function useSwipeEdge(
  ref: React.RefObject<HTMLElement | null>,
  { enabled, edge = 'left', edgeZone = 30, threshold = 60, onSwipe }: SwipeEdgeOptions,
) {
  const startX = useRef(0)
  const startY = useRef(0)
  const tracking = useRef(false)
  const scroller = useRef<HTMLElement | null>(null)
  const scrollerLeft = useRef(0)

  useEffect(() => {
    const el = ref.current
    if (!el || !enabled) return

    const onTouchStart = (e: TouchEvent) => {
      const touch = e.touches[0]
      const x = touch.clientX
      const zone = edgeZone <= 1 ? window.innerWidth * edgeZone : edgeZone
      const inZone = edge === 'left' ? x <= zone : x >= window.innerWidth - zone
      if (inZone) {
        startX.current = x
        startY.current = touch.clientY
        tracking.current = true
        scroller.current = findHorizontalScroller(e.target, el)
        scrollerLeft.current = scroller.current ? scroller.current.scrollLeft : 0
      }
    }

    const onTouchEnd = (e: TouchEvent) => {
      if (!tracking.current) return
      tracking.current = false
      const sc = scroller.current
      scroller.current = null
      const touch = e.changedTouches[0]
      const dx = touch.clientX - startX.current
      const dy = Math.abs(touch.clientY - startY.current)
      if (dy > Math.abs(dx)) return
      // A horizontal scroller under the finger owns the gesture; take it over
      // only once that scroller has not moved and cannot reveal more this way.
      if (sc) {
        if (sc.scrollLeft !== scrollerLeft.current) return
        const maxScrollLeft = sc.scrollWidth - sc.clientWidth
        const canReveal = dx < 0 ? sc.scrollLeft < maxScrollLeft - 1 : sc.scrollLeft > 1
        if (canReveal) return
      }
      const swipedRight = dx > threshold
      const swipedLeft = dx < -threshold
      if (edge === 'left' && swipedRight) onSwipe()
      if (edge === 'right' && swipedLeft) onSwipe()
    }

    const onTouchCancel = () => { tracking.current = false; scroller.current = null }

    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchend', onTouchEnd, { passive: true })
    el.addEventListener('touchcancel', onTouchCancel, { passive: true })
    return () => {
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchend', onTouchEnd)
      el.removeEventListener('touchcancel', onTouchCancel)
    }
  }, [ref, enabled, edge, edgeZone, threshold, onSwipe])
}
