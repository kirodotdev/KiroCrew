import { useCallback, useRef } from 'react'

/** Pixels per line for a `deltaMode` 1 (line) wheel event. */
export const WHEEL_LINE_PX = 16

/**
 * Translates an unmodified vertical wheel into horizontal scroll on a strip
 * with a hidden scrollbar. Mixed, modified, already-cancelled or unusable
 * events are left alone so an ancestor can scroll.
 *
 * The native listener must be non-passive: React 18 registers `onWheel` as
 * passive at the root, where `preventDefault()` has no effect. The stable
 * callback ref detaches from the previous node before attaching to the next.
 */
export function useWheelToHorizontal<T extends HTMLElement>(): (node: T | null) => void {
  const detachRef = useRef<(() => void) | null>(null)

  return useCallback((node: T | null) => {
    detachRef.current?.()
    detachRef.current = null
    if (!node) return

    const onWheel = (e: WheelEvent) => {
      if (e.defaultPrevented) return
      if (e.ctrlKey || e.shiftKey || e.altKey || e.metaKey) return
      if (e.deltaY === 0 || e.deltaX !== 0) return
      const max = node.scrollWidth - node.clientWidth
      if (max <= 0) return
      const scale = e.deltaMode === 1 ? WHEEL_LINE_PX : e.deltaMode === 2 ? node.clientWidth : 1
      const current = node.scrollLeft
      const target = Math.min(Math.max(current + e.deltaY * scale, 0), max)
      // Cannot move this way, so let an ancestor have the event.
      if (target === current) return
      node.scrollLeft = target
      e.preventDefault()
    }

    node.addEventListener('wheel', onWheel, { passive: false })
    detachRef.current = () => node.removeEventListener('wheel', onWheel)
  }, [])
}
