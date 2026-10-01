import { useEffect, useRef, useState } from 'react'

/** Mirrors the backend's `STOP_DECLINED_ESCALATION_SECS` (session_lifecycle.py). */
export const STOP_DECLINED_HINT_MS = 60_000

/**
 * Whether the composer should say "Click again to force stop".
 *
 * The backend samples `stop_declined` into a slots frame only when a frame is
 * pushed, and nothing pushes one at the marker's expiry. So a compaction that
 * outlives the window would leave the hint promising a force that the next
 * press no longer gets. The hint is therefore timed HERE, from the frame that
 * carried the decline: on once `stopDeclined` turns true, off after the same
 * window the backend uses, or as soon as the backend reports it cleared.
 */
export function useStopDeclinedHint(
  stopDeclined: boolean,
  timeoutMs: number = STOP_DECLINED_HINT_MS,
): boolean {
  const [armed, setArmed] = useState(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
    if (stopDeclined) {
      setArmed(true)
      timerRef.current = setTimeout(() => setArmed(false), timeoutMs)
    } else {
      setArmed(false)
    }
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }
  }, [stopDeclined, timeoutMs])

  return armed
}
