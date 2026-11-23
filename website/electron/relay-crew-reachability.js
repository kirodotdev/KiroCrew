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
 *
 * A failure the relay itself names `crew_not_connected` (it has no host
 * connection for this Crew: its gateway stopped) ends the wait sooner, after
 * HOST_GONE_AFTER_MS of those answers and at least HOST_GONE_MIN_ANSWERS of
 * them. That is the relay saying the host is gone, so there is no need to wait
 * out the two minutes, or for the control plane to mark the host offline. The
 * short wait still rides out a relay move, where the same answer can come for
 * a second or two.
 */

const UNREACHABLE_AFTER_MS = 120_000;
const UNREACHABLE_MIN_FAILURES = 10;
/** Well above the page's slowest retry pace (the socket's 10 s ceiling). */
const OUTAGE_GAP_MS = 30_000;
const HOST_GONE_AFTER_MS = 30_000;
/** About half of what the page's 3 s status probe sends in HOST_GONE_AFTER_MS. */
const HOST_GONE_MIN_ANSWERS = 5;

function createReachabilityWatch({ now = Date.now } = {}) {
  let firstFailureAt = null;
  let lastFailureAt = null;
  let failures = 0;
  let hostGoneAt = null;
  let hostGoneAnswers = 0;
  let tripped = false;

  const clearHostGone = () => { hostGoneAt = null; hostGoneAnswers = 0; };
  const clear = () => { firstFailureAt = null; lastFailureAt = null; failures = 0; clearHostGone(); };

  return {
    /** An attempt was answered: the host is there. */
    ok() { if (!tripped) clear(); },
    /** An attempt failed; `hostGone` when the relay answered `crew_not_connected`.
     *  True exactly once, when this failure ends the wait. */
    fail(hostGone = false) {
      if (tripped) return false;
      const t = now();
      if (lastFailureAt === null || t - lastFailureAt > OUTAGE_GAP_MS) {
        firstFailureAt = t;
        failures = 0;
        clearHostGone();
      }
      lastFailureAt = t;
      failures += 1;
      if (hostGone) {
        if (hostGoneAt === null) hostGoneAt = t;
        hostGoneAnswers += 1;
      }
      if ((failures >= UNREACHABLE_MIN_FAILURES && t - firstFailureAt >= UNREACHABLE_AFTER_MS)
        || (hostGoneAnswers >= HOST_GONE_MIN_ANSWERS && t - hostGoneAt >= HOST_GONE_AFTER_MS)) {
        tripped = true;
        return true;
      }
      return false;
    },
    /** A fresh cycle, as after Check again. */
    reset() { tripped = false; clear(); },
  };
}

module.exports = {
  createReachabilityWatch, UNREACHABLE_AFTER_MS, UNREACHABLE_MIN_FAILURES, OUTAGE_GAP_MS, HOST_GONE_AFTER_MS, HOST_GONE_MIN_ANSWERS,
};
