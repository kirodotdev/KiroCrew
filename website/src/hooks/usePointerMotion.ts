import { useCallback, useEffect, useRef } from 'react'

/**
 * Tell real pointer motion apart from the hover events a browser fires under a
 * pointer that has not moved, so a list selects a row on hover only when the
 * reader actually pointed at it.
 *
 * The browser fires mouseover, mouseenter and even mousemove when rows
 * re-render or scroll under a resting pointer. A filtered list re-ranks its
 * rows on every keystroke and resets the selection to the best match. A row
 * that selected itself on `onMouseEnter` let the resting pointer overwrite
 * that reset, so Enter chose whatever row landed under the cursor.
 *
 * While `active`, the hook records the pointer from a document capture-phase
 * `mousemove` listener, so every move counts, over a row, a header, or outside
 * the list. Capture runs before any row handler, which then asks
 * `moved(e)` whether the event's position differs from the move before it.
 * Wire rows as `onMouseMove={e => { if (moved(e)) select(i) }}`, never as
 * `onMouseEnter`.
 *
 * The first event after the list becomes active only records: with nothing
 * to compare against it cannot be told apart from a re-render under a
 * resting pointer, and one swallowed event of motion is the cost. The trail
 * is cleared on every activation and deactivation, so a reopened list never
 * compares against a position from before it opened.
 */
export function usePointerMotion(active: boolean): (e: { clientX: number; clientY: number }) => boolean {
  const trailRef = useRef<{ prev: { x: number; y: number } | null; last: { x: number; y: number } | null }>({
    prev: null,
    last: null,
  })

  useEffect(() => {
    if (!active) return
    const trail = trailRef.current
    trail.prev = null
    trail.last = null
    const onMove = (e: MouseEvent) => {
      trail.prev = trail.last
      trail.last = { x: e.clientX, y: e.clientY }
    }
    document.addEventListener('mousemove', onMove, true)
    return () => {
      document.removeEventListener('mousemove', onMove, true)
      trail.prev = null
      trail.last = null
    }
  }, [active])

  return useCallback((e: { clientX: number; clientY: number }) => {
    const prev = trailRef.current.prev
    return !!prev && (prev.x !== e.clientX || prev.y !== e.clientY)
  }, [])
}
