"use strict";

/**
 * When a relay Crew window should stop waiting for its host.
 *
 * The window's page is the remote Crew's own dashboard. While its socket is
 * down it retries forever: the socket on a 1 s to 10 s backoff and a
 * `/api/status` probe every 3 s. Main sees each of those attempts in the Crew's
 * session, and this watch reads them as failed or answered.
 *
 * A host is called unreachable after UNREACHABLE_AFTER_MS of failures with no
 * answer between them, and at least UNREACHABLE_MIN_FAILURES of them. Two
 * minutes is long enough for what reconnects on its own (a relay drain, a Wi-Fi
 * change, the host's gateway restarting) and short enough that someone looking
 * at a gone host is told why soon, not never. By then the page has made about a
 * dozen socket attempts and forty probes.
 *
 * The minimum count keeps elapsed time alone from deciding: a gap longer than
 * OUTAGE_GAP_MS between two failures (the machine slept, or a hidden window's
 * timers were throttled) starts a new outage instead of tripping on the first
 * failure after it.
 */

const UNREACHABLE_AFTER_MS = 120_000;
const UNREACHABLE_MIN_FAILURES = 10;
/** Well above the page's slowest retry pace (the socket's 10 s ceiling). */
const OUTAGE_GAP_MS = 30_000;

function createReachabilityWatch({ now = Date.now } = {}) {
  let firstFailureAt = null;
  let lastFailureAt = null;
  let failures = 0;
  let tripped = false;

  const clear = () => { firstFailureAt = null; lastFailureAt = null; failures = 0; };

  return {
    /** An attempt was answered: the host is there. */
    ok() { if (!tripped) clear(); },
    /** An attempt failed. True exactly once, when this failure ends the wait. */
    fail() {
      if (tripped) return false;
      const t = now();
      if (lastFailureAt === null || t - lastFailureAt > OUTAGE_GAP_MS) {
        firstFailureAt = t;
        failures = 0;
      }
      lastFailureAt = t;
      failures += 1;
      if (failures >= UNREACHABLE_MIN_FAILURES && t - firstFailureAt >= UNREACHABLE_AFTER_MS) {
        tripped = true;
        return true;
      }
      return false;
    },
    /** A fresh cycle, as after Check again. */
    reset() { tripped = false; clear(); },
  };
}

module.exports = { createReachabilityWatch, UNREACHABLE_AFTER_MS, UNREACHABLE_MIN_FAILURES, OUTAGE_GAP_MS };
