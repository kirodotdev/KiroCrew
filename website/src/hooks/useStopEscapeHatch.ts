import { useState, useEffect, useRef } from 'react'

/**
 * Escape-hatch timer for the `killing` stop state.
 *
 * When `stopState` enters `'killing'`, starts a countdown. After `timeoutMs`
 * (default 15 000 ms), sets `escaped` to true — the caller should re-enable
 * the stop button as a "Force reset" affordance.
 *
 * Resets automatically when `stopState` changes away from `'killing'`.
 */
export const KILLING_ESCAPE_MS = 15_000

export function useStopEscapeHatch(
  stopState: 'idle' | 'soft_pending' | 'killing' | undefined,
  timeoutMs: number = KILLING_ESCAPE_MS,
): { escaped: boolean } {
  const [escaped, setEscaped] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    // Clear any existing timer when state changes
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }

    if (stopState === 'killing') {
      setEscaped(false)
      timerRef.current = setTimeout(() => {
        setEscaped(true)
      }, timeoutMs)
    } else {
      setEscaped(false)
    }

    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }
  }, [stopState, timeoutMs])

  return { escaped }
}
