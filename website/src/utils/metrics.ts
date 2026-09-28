/**
 * Narrowing predicates for probe-derived `/api/system` metrics.
 *
 * The server builds that payload key-by-key with a per-probe
 * `try/except: pass`, seeded from cached static system info, so any
 * probe-derived field (`cpu_pct`, the `mem_*_gb` trio, the `disk_*_gb`
 * pair) can be absent on an ordinary frame — a memory total with no used
 * value is normal, not corrupt. A failed probe can also surface as
 * NaN/Infinity when a consumer divides by an unmeasured total.
 *
 * Every reader of those fields (the topbar metrics capsule and the System
 * page tabs) narrows through these two helpers, so the frame is proven
 * formattable in exactly one way instead of one way per consumer.
 */

/** Narrow a possibly-absent metric to a formattable number (excludes NaN/Infinity). */
export function isMetricNumber(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/**
 * Coerce a possibly-absent metric to a finite number, absent/NaN/Infinity -> 0.
 *
 * Paired with `isMetricNumber`: the guard decides whether a readout is SHOWN,
 * this makes the formatting itself unable to throw, so a future gate that
 * forgets a field degrades to a wrong-looking 0 instead of unmounting the app.
 */
export function metricNumber(v: unknown): number {
  return isMetricNumber(v) ? v : 0
}
