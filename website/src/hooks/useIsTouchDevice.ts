import { useState, useSyncExternalStore } from 'react'
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

/**
 * `isTouchDevice()` read ONCE, when the component mounts, and held for its
 * lifetime.
 *
 * For a decision that picks one of two editors (`lexicalComposer={!touch}`) the
 * reactive form above is the wrong shape: a pointer-capability change mid-session
 * — a mouse attached to a tablet, a keyboard detached from a convertible — would
 * flip the prop and hard-swap `<LexicalComposerInput>` for the `<textarea>`
 * under a live draft, discarding caret, selection, an IME composition and the
 * undo history. The composer kind is therefore settled at mount; the next mount
 * (a reload, a session that re-creates the host) reads the capability afresh.
 */
export function useTouchDeviceAtMount(): boolean {
  const [touch] = useState(isTouchDevice)
  return touch
}
