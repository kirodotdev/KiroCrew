import { useRef, useEffect } from 'react'

interface SwipeEdgeOptions {
  enabled: boolean
  edge?: 'left' | 'right'
  edgeZone?: number
  threshold?: number
  onSwipe: () => void
}

export function useSwipeEdge(
  ref: React.RefObject<HTMLElement | null>,
  { enabled, edge = 'left', edgeZone = 30, threshold = 60, onSwipe }: SwipeEdgeOptions,
) {
  const startX = useRef(0)
  const startY = useRef(0)
  const tracking = useRef(false)

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
      }
    }

    const onTouchEnd = (e: TouchEvent) => {
      if (!tracking.current) return
      tracking.current = false
      const touch = e.changedTouches[0]
      const dx = touch.clientX - startX.current
      const dy = Math.abs(touch.clientY - startY.current)
      if (dy > Math.abs(dx)) return
      const swipedRight = dx > threshold
      const swipedLeft = dx < -threshold
      if (edge === 'left' && swipedRight) onSwipe()
      if (edge === 'right' && swipedLeft) onSwipe()
    }

    const onTouchCancel = () => { tracking.current = false }

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
