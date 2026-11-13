/**
 * `capabilities.browse` as the dashboard reads it: `browse_enabled` on
 * `GET /api/dashboard/config`.
 *
 * Only a definite `false` counts. Every browser route on the gateway refuses on
 * its own under a deny, so an unknown answer (a failed read, an older gateway)
 * keeps the page as it was rather than hiding a browser that works.
 */
export function browsePolicyOff(cfg: { browse_enabled?: boolean } | undefined): boolean {
  return cfg?.browse_enabled === false
}
