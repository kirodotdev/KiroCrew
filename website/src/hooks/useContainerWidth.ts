import { useCallback, useRef, useState } from 'react'

/**
 * Observe an element's content-box width via ResizeObserver.
 *
 * Returns `null` until the first measurement (callers should treat null as
 * "assume wide" to avoid a narrow-layout flash on mount). Falls back to null
 * forever when ResizeObserver is unavailable (older test DOMs) — the caller's
 * null-handling then picks the default layout.
 *
 * Returns a CALLBACK ref, for the same reason `useColumnCount` does: an effect
 * that reads `ref.current` once on mount never observes an element rendered
 * conditionally (behind query data or a collapsed branch) after that first
 * commit. The callback runs whenever the element actually mounts or unmounts,
 * so the observer attaches exactly when there is something to observe and
 * disconnects when it leaves.
 */
export function useContainerWidth<T extends HTMLElement>(): [React.RefCallback<T>, number | null] {
  const [width, setWidth] = useState<number | null>(null)
  const observerRef = useRef<ResizeObserver | null>(null)

  const ref = useCallback((el: T | null) => {
    observerRef.current?.disconnect()
    observerRef.current = null
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(entries => {
      const w = entries[0]?.contentRect.width
      // Zero-width guard: an element attached but not laid out yet (hidden
      // ancestor, layout-less test DOM) reports 0 — keep the last real
      // measurement (or the initial null = "assume wide") rather than
      // switching layouts on a width nobody rendered.
      if (typeof w === 'number' && w > 0) setWidth(w)
    })
    ro.observe(el)
    observerRef.current = ro
  }, [])

  return [ref, width]
}
