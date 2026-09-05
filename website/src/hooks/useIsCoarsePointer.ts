import * as React from 'react'

/**
 * True when the primary pointer is coarse (touch). Listens to
 * `(pointer: coarse)` so a window resize or device rotation updates
 * without a reload. Falls back to false when `matchMedia` is unavailable
 * (e.g. jsdom) — wide-pointer is the safe default because it keeps the
 * native Radix flyout rather than collapsing to inline.
 */
export function useIsCoarsePointer(): boolean {
  const [isCoarse, setIsCoarse] = React.useState(false)
  React.useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const m = window.matchMedia('(pointer: coarse)')
    setIsCoarse(m.matches)
    const handler = (e: MediaQueryListEvent) => setIsCoarse(e.matches)
    if (typeof m.addEventListener === 'function') m.addEventListener('change', handler)
    else m.addListener(handler as unknown as (e: MediaQueryListEvent) => void)
    return () => {
      if (typeof m.removeEventListener === 'function') m.removeEventListener('change', handler)
      else m.removeListener(handler as unknown as (e: MediaQueryListEvent) => void)
    }
  }, [])
  return isCoarse
}
