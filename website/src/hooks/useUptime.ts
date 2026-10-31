import { useAppSelector } from '../store'
import { fmtDuration } from '../i18n/format'
import { useSecondClock } from './useSecondClock'

function fmt(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = secs % 60
  // Seconds are always shown, hours only above an hour. Zero parts are
  // rendered by default, so a sub-minute uptime shows as "0m 38s".
  return h > 0
    ? fmtDuration([[h, 'hour'], [m, 'minute'], [s, 'second']])
    : fmtDuration([[m, 'minute'], [s, 'second']])
}

/**
 * A live uptime string, read from the shared 1 Hz clock.
 *
 * The component that calls this re-renders once a second while the page is
 * visible, so call it from a leaf (see `components/Uptime.tsx`), not from a
 * page body: a page that called it used to re-render whole every second.
 */
export function useUptime(): string {
  const startTime = useAppSelector(s => s.dashboard.status?.start_time)
  // start_time is stamped by the gateway's clock; one running ahead of the
  // browser's would otherwise yield a negative duration.
  const secs = useSecondClock(
    now => (startTime ? Math.max(0, Math.floor(now / 1000 - startTime)) : -1),
    !!startTime,
  )
  return secs < 0 ? '\u2014' : fmt(secs)
}
