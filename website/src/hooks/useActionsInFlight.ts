import { useCallback, useState } from 'react'

/**
 * Tracks every action in flight on one page, not just the latest one.
 *
 * A page whose action buttons all change the same object locks them all while
 * any action runs (`actionBusy`), and each button still reads its own entry for
 * its spinner and label (`isActionRunning`). Keeping a set rather than a single
 * "current action" value is what stops one action ending from unlocking the
 * buttons while another action is still running.
 */
export function useActionsInFlight() {
  const [inFlight, setInFlight] = useState<ReadonlySet<string>>(() => new Set())
  const startAction = useCallback((action: string) => {
    setInFlight(prev => (prev.has(action) ? prev : new Set(prev).add(action)))
  }, [])
  const finishAction = useCallback((action: string) => {
    setInFlight(prev => {
      if (!prev.has(action)) return prev
      const next = new Set(prev)
      next.delete(action)
      return next
    })
  }, [])
  const isActionRunning = useCallback((action: string) => inFlight.has(action), [inFlight])
  return { startAction, finishAction, isActionRunning, actionBusy: inFlight.size > 0 }
}
