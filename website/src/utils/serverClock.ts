/**
 * The gateway's clock as this tab reads it, for counting up from a gateway stamp
 * (`run_started_ts`, `step_started_at`) on a dashboard whose browser runs on
 * another machine than the gateway.
 *
 * Each `dashboard` status frame carries `server_now`; the offset between it and
 * the browser's clock is taken once per frame. Network latency adds its one-way
 * delay to that offset, which a seconds display does not show. 0 until a frame
 * arrives, so a tab that never heard one counts on its own clock.
 */
let offsetMs = 0

/** Called with each status frame's `server_now`. */
export function noteServerNow(serverNow: number | undefined): void {
  if (typeof serverNow === 'number' && Number.isFinite(serverNow)) {
    offsetMs = Date.now() - serverNow * 1000
  }
}

/** The gateway's epoch milliseconds at the browser's *browserNowMs*. */
export function serverNowMs(browserNowMs: number): number {
  return browserNowMs - offsetMs
}

/** Test seam: forget the offset. */
export function resetServerClock(): void {
  offsetMs = 0
}
