"use strict";
//
// Pure, injectable post-handoff liveness monitor, extracted from main.js so it
// can be unit-tested without Electron (mirrors gateway-wait.js / gateway-stop.js).
//
// PROBLEM: waitForGateway runs exactly ONCE, at boot. After the dashboard loads,
// nothing re-checks the backend. The "Connected (live)" badge tracks the
// websocket, which stays TCP-connected even when the backend's asyncio loop is
// wedged — so a wedged gateway leaves the UI on an eternal spinner with a green
// badge and no recovery. (Observed: a blocking close() on the loop thread froze
// the backend for ~10h while the app sat connected-but-blank.)
//
// This monitor polls a real loop-turning endpoint (/api/status, via the same
// checkBackend used at boot) on an interval AFTER handoff. N consecutive
// failures mean the backend is alive-but-unresponsive — a distinct condition
// from "the process exited", which main.js already handles via the spawn 'exit'
// watcher. On the Nth failure it fires onUnresponsive ONCE so main.js can
// kill -9 + respawn + reconnect; if the backend answers again before the
// threshold trips it fires onRecovered and resets.
//
// The decision logic lives in a plain async tick() so tests can drive it with a
// stubbed probe and no timers; start()/stop() only wrap the interval.

/**
 * @param {object} o
 * @param {() => Promise<unknown>} o.probe        resolve = backend answered,
 *                                                reject = unresponsive this probe
 * @param {() => void} o.onUnresponsive           fired once when failures reach the
 *                                                threshold (kick off recovery)
 * @param {() => void} [o.onRecovered]            fired when a probe succeeds after
 *                                                >=1 prior failure (transient blip)
 * @param {() => boolean} [o.isWindowAlive]       false => stop (window closed)
 * @param {number} [o.failureThreshold=3]         consecutive failures before firing
 * @param {number} [o.intervalMs=10000]           poll cadence
 * @param {(fn: Function, ms: number) => any} [o.setIntervalFn]
 * @param {(h: any) => void} [o.clearIntervalFn]
 * @param {(msg: string) => void} [o.log]
 * @returns {{start: () => void, stop: () => void, tick: () => Promise<void>,
 *            getState: () => {consecutiveFailures: number, fired: boolean, running: boolean}}}
 */
function createLivenessMonitor({
  probe,
  onUnresponsive,
  onRecovered = () => {},
  isWindowAlive = () => true,
  failureThreshold = 3,
  intervalMs = 10_000,
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
  log = () => {},
}) {
  let consecutiveFailures = 0;
  let fired = false; // onUnresponsive already fired for the current episode
  let inFlight = false; // a probe is still outstanding (don't overlap)
  let timer = null;

  async function tick() {
    // Once we've fired, we pause until the caller stop()s us and starts a fresh
    // monitor after a successful reconnect — never fire recovery twice.
    if (fired) return;
    if (!isWindowAlive()) {
      stop();
      return;
    }
    if (inFlight) return; // previous probe slower than the interval; skip
    inFlight = true;
    try {
      await probe();
      if (consecutiveFailures > 0) {
        log(`backend responsive again after ${consecutiveFailures} failed probe(s)`);
        try { onRecovered(); } catch (e) { log(`onRecovered threw: ${e && e.message}`); }
      }
      consecutiveFailures = 0;
    } catch {
      consecutiveFailures += 1;
      log(`backend probe failed (${consecutiveFailures}/${failureThreshold})`);
      if (consecutiveFailures >= failureThreshold && !fired) {
        fired = true;
        log("backend unresponsive — triggering recovery");
        try { onUnresponsive(); } catch (e) { log(`onUnresponsive threw: ${e && e.message}`); }
      }
    } finally {
      inFlight = false;
    }
  }

  function start() {
    if (timer) return;
    consecutiveFailures = 0;
    fired = false;
    inFlight = false;
    timer = setIntervalFn(() => { void tick(); }, intervalMs);
    // Don't let the poll timer keep the process alive at quit time.
    if (timer && typeof timer.unref === "function") timer.unref();
  }

  function stop() {
    if (timer) {
      clearIntervalFn(timer);
      timer = null;
    }
  }

  function getState() {
    return { consecutiveFailures, fired, running: !!timer };
  }

  return { start, stop, tick, getState };
}

module.exports = { createLivenessMonitor };
