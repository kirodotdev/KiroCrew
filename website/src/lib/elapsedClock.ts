/**
 * Whole seconds as a clock reading: `0:07`, `12:05`, and `1:02:03` from the
 * first hour on. Minutes are not zero-padded below an hour, so a short
 * recording reads `0:07` and not `00:07`.
 *
 * A value that is not a finite, non-negative number reads `0:00`: this labels a
 * live recording, where a blank or `NaN:NaN` would look like a broken recorder.
 */
export function formatElapsedClock(totalSeconds: number): string {
  const whole = Number.isFinite(totalSeconds) && totalSeconds > 0 ? Math.floor(totalSeconds) : 0
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const seconds = String(whole % 60).padStart(2, '0')
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${seconds}`
  return `${minutes}:${seconds}`
}
