import { useCallback, useEffect, useRef, useState } from 'react'

/** How long an action's done state (a check and "Quoted") stays up. */
export const DONE_FLASH_MS = 2000

/**
 * A short done state for an action whose result lands somewhere else on the
 * page (a quote staged in the composer): `done` is the key last flashed, or
 * null, and goes back to null `DONE_FLASH_MS` after the last `flash`. The timer
 * is cleared on unmount.
 */
export function useDoneFlash<K = true>(): [K | null, (key: K) => void] {
  const [done, setDone] = useState<K | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
  const flash = useCallback((key: K) => {
    setDone(() => key)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setDone(null), DONE_FLASH_MS)
  }, [])
  return [done, flash]
}
