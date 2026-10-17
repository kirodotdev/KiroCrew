import { useCallback, useMemo, useRef } from 'react'
import { TOUCH_REPLAY_WINDOW_MS } from '../components/InstantTip'

/**
 * Tells a touch tap's replayed `mouseenter` / `focus` apart from a real hover
 * or tab stop, for anchors that reveal content from `onMouseEnter`.
 *
 * On iOS a tap replays `mouseover` / `mouseenter` before Safari decides whether
 * to send the click. If a handler on that replay shows new content -- at once,
 * or from a single-shot timer of 400ms or less -- WebKit treats the tap as a
 * hover and drops the click, so the user has to tap twice. Elsewhere a reveal
 * opened by a tap has no leave to close it.
 *
 * Spread `pointerProps` on the anchor (or on an ancestor that React delivers
 * the anchor's pointer events to) and return early from the reveal while
 * `fromTouch()` is true. The type is read from the tap's own pointer events,
 * not from the device, so a mouse on a touch laptop or tablet still hovers --
 * its `pointerenter` carries `pointerType: 'mouse'` and clears the window --
 * and a keyboard focus that arrives after the window still opens.
 *
 * The same rule `useInstantTip` applies to its anchors (see
 * `TOUCH_REPLAY_WINDOW_MS`).
 */
export function useTouchReplay() {
  const touchUntilRef = useRef(0)
  const notePointer = useCallback((e: { pointerType: string }) => {
    touchUntilRef.current = e.pointerType === 'touch' ? Date.now() + TOUCH_REPLAY_WINDOW_MS : 0
  }, [])
  const fromTouch = useCallback(() => Date.now() < touchUntilRef.current, [])
  const pointerProps = useMemo(() => ({
    onPointerEnter: notePointer,
    onPointerDown: notePointer,
    onPointerUp: notePointer,
  }), [notePointer])
  return { fromTouch, pointerProps }
}
