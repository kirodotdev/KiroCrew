import { useCallback, useLayoutEffect, useRef } from 'react'

/** How long a freshly shown control ignores a press, the way a browser's own
 *  permission prompt does. Long enough that a press already on its way when the
 *  control appeared (or when its window came to the front) is not read as a
 *  choice, short enough that a person reading it never notices. */
export const CONFIRM_INPUT_DELAY_MS = 500

/** Returns `ready()`. While `shownKey` is non-null it is false for
 *  `CONFIRM_INPUT_DELAY_MS` after the key is first set, and again after each
 *  time the window gains focus or the tab becomes visible; otherwise it is
 *  true. A handler that checks it drops a press made before the person could
 *  have seen what they were pressing. A new `shownKey` starts the delay again.
 *  Reads `performance.now()`, so a wall-clock change cannot stretch it. */
export function useInputDelay(shownKey: string | null): () => boolean {
  const readyAt = useRef(0)
  // A layout effect starts the delay at commit, before the browser can paint
  // what it guards and deliver a press to it.
  useLayoutEffect(() => {
    if (shownKey === null) return
    const restart = () => { readyAt.current = performance.now() + CONFIRM_INPUT_DELAY_MS }
    // `focus` does not bubble, so a listener on window (not capturing) hears
    // only the window itself gaining focus, never a control inside it.
    const onFocus = () => restart()
    const onVisibility = () => { if (document.visibilityState === 'visible') restart() }
    restart()
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibility)
      readyAt.current = 0
    }
  }, [shownKey])
  return useCallback(() => performance.now() >= readyAt.current, [])
}
