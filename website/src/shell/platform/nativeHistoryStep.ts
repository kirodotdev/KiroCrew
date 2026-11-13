import { useEffect } from 'react'
import { useGuardedHistoryStep } from '../../components/NavigationLeaveGuard'
import { isMacElectron } from '../../lib/electron'
import { canGoBack, canGoForward } from '../../lib/routeHistoryPosition'
import { subscribeNativeHistoryStep } from './electronBridge'

/**
 * A mouse's Back/Forward buttons in the desktop shell, taking the same guarded
 * step as the top-bar arrows and the ⌘/Ctrl+←/→ chords: a dirty draft is asked
 * about first.
 *
 * Two ways in. Mouse software such as Logitech's Options sends the buttons as
 * swipes, which the shell relays (see electron/swipe-history.js); a swipe with
 * nowhere to go does nothing. Raw buttons reach the page as a `mouseup` with
 * `button` 3 or 4. In the macOS app, the one platform checked to stop
 * Chromium's own step when that press is cancelled, a press with a route entry
 * to go to is cancelled and taken here: Chromium's step is a bare pop that only
 * `NavigationBackGuard`'s best-effort trap can catch. Any other press keeps
 * Chromium's step, which after a reconnect still reaches the routes of the
 * dashboard's earlier load.
 *
 * Inert while `narrow`, like the chords: the arrows are hidden there, drill-ins
 * navigate by component state, and a step would walk a stack the screen does
 * not show.
 */
export function useNativeHistoryStep(narrow: boolean): void {
  const step = useGuardedHistoryStep()
  useEffect(() => {
    if (narrow) return
    const canGo = (delta: -1 | 1) => (delta === -1 ? canGoBack() : canGoForward())
    const unsubscribe = subscribeNativeHistoryStep(delta => {
      if (canGo(delta)) step(delta)
    })
    if (!isMacElectron) return unsubscribe
    const onMouseUp = (e: MouseEvent) => {
      if (e.button !== 3 && e.button !== 4) return
      const delta = e.button === 3 ? -1 : 1
      if (!canGo(delta)) return
      // Cancelling the mouseup is what stops Chromium's own step.
      e.preventDefault()
      step(delta)
    }
    window.addEventListener('mouseup', onMouseUp)
    return () => {
      unsubscribe?.()
      window.removeEventListener('mouseup', onMouseUp)
    }
  }, [narrow, step])
}
