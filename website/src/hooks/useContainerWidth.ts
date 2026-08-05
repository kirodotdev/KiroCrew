import { useEffect, useRef, useState } from 'react'

/**
 * Observe an element's content-box width via ResizeObserver.
 *
 * Returns `null` until the first measurement (callers should treat null as
 * "assume wide" to avoid a narrow-layout flash on mount). Falls back to null
 * forever when ResizeObserver is unavailable (older test DOMs) — the caller's
 * null-handling then picks the default layout.
 *
 * The ref is typed as React 18's non-null `RefObject<T>` (via the
 * `useRef<T>(null)` overload) so it is directly assignable to a JSX `ref`
 * prop — `RefObject<T | null>` is not, under @types/react 18.
 */
export function useContainerWidth<T extends HTMLElement>(): [React.RefObject<T>, number | null] {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState<number | null>(null)

  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(entries => {
      const w = entries[0]?.contentRect.width
      if (typeof w === 'number') setWidth(w)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  return [ref, width]
}
