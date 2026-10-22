import { useEffect } from 'react'
import { useGuardedHistoryStep } from '../../components/NavigationLeaveGuard'
import { canGoBack, canGoForward } from '../../lib/routeHistoryPosition'
import { subscribeNativeHistoryStep } from './electronBridge'

/**
 * The desktop shell's relayed swipe, which is how a mouse's Back/Forward buttons
 * reach the macOS app when mouse software such as Logitech's Options sends them
 * as swipes (see electron/swipe-history.js). It takes the same guarded step as
 * the top-bar arrows and the ⌘/Ctrl+←/→ chords: a dirty draft is asked about
 * first, and a step with nowhere to go does nothing.
 *
 * Inert while `narrow`, like the chords: the arrows are hidden there, drill-ins
 * navigate by component state, and a step would walk a stack the screen does
 * not show.
 */
export function useNativeHistoryStep(narrow: boolean): void {
  const step = useGuardedHistoryStep()
  useEffect(() => {
    if (narrow) return
    return subscribeNativeHistoryStep(delta => {
      if (delta === -1 ? canGoBack() : canGoForward()) step(delta)
    })
  }, [narrow, step])
}
