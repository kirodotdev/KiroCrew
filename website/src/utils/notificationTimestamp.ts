/** Parses a notification `ts` in any of the shapes the producers emit: an
 *  epoch number or digit string in seconds, ms, us or ns, or an ISO 8601
 *  string. Returns an Invalid Date for anything unparseable or pre-2020. */
export function parseTs(ts: string | number): Date {
  // A numeric epoch (number, or an all-digits string) can arrive in any unit —
  // seconds, milliseconds, microseconds, or nanoseconds — depending on the
  // producer. Detect the unit by magnitude and normalize to milliseconds.
  //
  // Detecting the unit up front (rather than `new Date(ts)` with a
  // `new Date(parseFloat(ts) * 1000)` fallback) is required because a
  // millisecond epoch passed as a string is Invalid Date in V8, so the fallback
  // would treat it as seconds and render the year as ~58527. It also handles the
  // microsecond-as-number case.
  const num =
    typeof ts === 'number'
      ? ts
      : /^\s*\d+(\.\d+)?\s*$/.test(ts)
        ? parseFloat(ts)
        : NaN
  let d: Date
  if (!isNaN(num)) {
    let ms: number
    if (num >= 1e17) ms = num / 1e6 // nanoseconds → ms
    else if (num >= 1e14) ms = num / 1e3 // microseconds → ms
    else if (num >= 1e11) ms = num // milliseconds (already)
    else ms = num * 1e3 // seconds → ms
    d = new Date(ms)
  } else {
    d = new Date(ts) // ISO 8601 / RFC date string
  }
  if (isNaN(d.getTime()) || d.getTime() < Date.UTC(2020, 0, 1)) return new Date(NaN)
  return d
}
