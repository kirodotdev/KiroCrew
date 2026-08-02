import { useState, useEffect } from 'react'
import { useAppSelector } from '../store'
import { fmtDuration } from '../i18n/format'

function fmt(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = secs % 60
  // Thresholds unchanged: seconds are always shown, hours only above an hour.
  // Zero parts are rendered by default, so "0m 38s" survives exactly as the
  // previous template produced it.
  return h > 0
    ? fmtDuration([[h, 'hour'], [m, 'minute'], [s, 'second']])
    : fmtDuration([[m, 'minute'], [s, 'second']])
}

/** Returns a live uptime string that ticks every second. */
export function useUptime(): string {
  const startTime = useAppSelector(s => s.dashboard.status?.start_time)
  const [display, setDisplay] = useState('—')

  useEffect(() => {
    if (!startTime) return
    const tick = () => setDisplay(fmt(Math.floor(Date.now() / 1000 - startTime)))
    tick()
    const iv = setInterval(tick, 1000)
    return () => clearInterval(iv)
  }, [startTime])

  return display
}
