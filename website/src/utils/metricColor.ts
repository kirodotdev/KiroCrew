/**
 * Usage color class for a 0..1 utilization ratio: muted (<=70%), warn (70-90%),
 * danger (>90%).
 *
 * Its own module rather than a member of `App.tsx` so a test — or any other
 * pure consumer — can reach it without importing the app root. `App.tsx` pulls
 * the whole eager graph (router, store, react-query, every registered page, the
 * i18n catalogs), which a three-assertion test of a one-line pure function has
 * no reason to pay for.
 */
export function metricColor(pct: number): string {
  return pct > 0.9 ? 'text-danger' : pct > 0.7 ? 'text-warn' : 'text-muted'
}
