/**
 * True when the device is touch / coarse-pointer. Both `(pointer: coarse)` and
 * `(hover: none)` are checked — stylus-only and some accessibility modes match
 * only the latter. Returns false outside the browser.
 */
export function isTouchDevice(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(pointer: coarse)').matches || window.matchMedia('(hover: none)').matches
}
