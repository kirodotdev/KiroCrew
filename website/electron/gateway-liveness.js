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

// How long ONE post-handoff probe waits for /api/status before it counts as a
// miss. This is deliberately NOT the 2s the boot poll uses. The boot poll runs
// every 500ms and a slow answer just means "poll again"; here three misses in a
// row force-kill the gateway, so the budget has to tell a loop that is merely
// SLOW from one that is WEDGED. Observed on a Windows desktop under a full test
// run plus several subagents: the event loop logged 1-9s of lag from synchronous
// disk I/O, still answered every request, and was killed anyway because three
// 2s probes in a row timed out — 6 subagents orphaned for a loop that was never
// stuck. A wedged loop answers nothing at all, so 8s costs it only ~6s of extra
// detection (3 probes × 10s interval ≈ 28s vs 22s), still inside the in-process
// loop-stall watchdog's own 25s budget, which reports a true wedge through the
// spawn 'exit' watcher rather than through this monitor.
const LIVENESS_PROBE_TIMEOUT_MS = 8_000;

/**
 * Build the probe the post-handoff monitor polls with: one GET against the
 * loop-turning status endpoint, resolving on any non-5xx answer and rejecting
 * on a connection error, a 5xx, or `timeoutMs` of silence.
 *
 * Pure and injectable like the monitor itself: `httpMod` is whatever exposes
 * `get(url, options, callback)`, so tests can assert the option the request is
 * made with instead of racing a real socket.
 *
 * @param {object} o
 * @param {{get: Function}} o.httpMod
 * @param {string} o.url
 * @param {number} [o.timeoutMs=LIVENESS_PROBE_TIMEOUT_MS]
 * @returns {() => Promise<void>}
 */
function createBackendProbe({ httpMod, url, timeoutMs = LIVENESS_PROBE_TIMEOUT_MS }) {
  return () => new Promise((resolve, reject) => {
    const req = httpMod.get(url, { timeout: timeoutMs }, (res) => {
      res.resume();
      if (res.statusCode < 500) resolve();
      else reject(new Error(`status ${res.statusCode}`));
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(); reject(new Error(`no answer in ${timeoutMs}ms`)); });
  });
}

module.exports = { createLivenessMonitor, createBackendProbe, LIVENESS_PROBE_TIMEOUT_MS };
