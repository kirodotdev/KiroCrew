import { useSyncExternalStore } from 'react'
import { isTouchDevice } from '../utils/isTouchDevice'

/**
 * Reactive form of `isTouchDevice()`.
 *
 * The predicate itself is NOT redefined here — it stays in
 * `utils/isTouchDevice`, which the eight imperative callers already share (and
 * which checks `(hover: none)` as well as `(pointer: coarse)`, so stylus-only
 * devices count as touch). This hook only adds the subscription an ordinary
 * conditional render needs: a bare `isTouchDevice()` call is a snapshot, so a
 * pointer change would not repaint until something else re-rendered.
 *
 * Deliberately NOT `useIsMobile` (viewport < 768px). The question callers ask is
 * "is there a physical keyboard": a tablet in landscape is wider than 768px and
 * still has none, while a narrow desktop window has one.
 */
const QUERIES = ['(pointer: coarse)', '(hover: none)']

function subscribe(cb: () => void) {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const mqls = QUERIES.map(q => window.matchMedia(q))
  for (const m of mqls) m.addEventListener?.('change', cb)
  return () => { for (const m of mqls) m.removeEventListener?.('change', cb) }
}

export function useIsTouchDevice(): boolean {
  return useSyncExternalStore(subscribe, isTouchDevice, () => false)
}
