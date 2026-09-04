// Agent -> Electron browser command channel (the CLIENT side).
//
// The agent's `browser_*` MCP tool calls do NOT originate in the Electron main
// process — they originate in the Python gateway, where the MCP surface lives.
// But the native browser panel (browser-view.js + browser-control.js) is owned
// by THIS process: the embedded `WebContentsView` and its CDP control plane only
// exist here. So a command raised on the Python side has to cross the process
// boundary to reach the panel that can actually run it.
//
// Rather than have the gateway reach INTO Electron (it has no handle on our
// windows, and exposing an inbound control port on this process is exactly the
// dashboard-exposure hazard the design spike ruled out — see browser-control.js
// header §"Why PLAYWRIGHT needs NO debug port"), the gateway exposes a loopback
// HTTP *command queue* and Electron PULLS from it:
//
//   • Electron long-polls  POST /api/browser/command-drain  for the next command
//     for any session that currently has a native panel.
//   • Electron dispatches that command to the right panel's control plane.
//   • Electron posts the outcome back to POST /api/browser/command-result.
//
// This keeps the direction of control outbound-only from Electron (the same
// shape the frame pump already uses), so no new inbound surface is added to the
// process that holds the dashboard session cookie.
//
// ── Why the loop must be defensive ───────────────────────────────────────────
//
// This loop runs for the life of the app. Three failure modes must NOT be able
// to kill it or make it hot-spin:
//   1. A single browser op throwing (bad selector, page gone) — we post ok:false
//      and keep polling. One failed command must never stop the channel.
//   2. A transport error or non-2xx from the gateway (restart, blip) — we back
//      off by a BOUNDED delay before retrying. A previous PR shipped a drain
//      client that tight-looped on a non-2xx (see browser-panel-input work);
//      the bounded backoff here is the fix for that exact class of bug.
//   3. A malformed / non-JSON drain body — we log via onError and continue,
//      never letting a parse error escape the loop.
//
// When no panel is open there is nothing to drive, so the loop idles (a short
// sleep, no HTTP) instead of holding a long-poll open for sessions that cannot
// receive a command.
//
// ── Testability ──────────────────────────────────────────────────────────────
//
// EVERY external dependency is injected (mirroring browser-view.js's factory
// style), so the whole channel is unit-testable with a fake `fetchFn` and no
// Electron and no real network or timers-of-consequence:
//
//   fetchFn(url, { method, headers, body })   -> a Response-like { ok, status,
//                                                 async json(), async text() }
//   getGatewayUrl()   -> base URL of the loopback gateway (e.g. http://127.0.0.1:PORT)
//   getSecret()       -> the X-Internal-Secret value (NEVER read a file here)
//   listPanelIds()    -> session keys that currently have a native panel
//   dispatch(sessionKey, op, args) -> Promise of the op's result (or rejects)
//   onError(err, context)          -> log sink; must never throw meaningfully
//   waitMs            -> long-poll wait hint sent to the gateway (default 25000)
//   backoffMs         -> bounded delay after an error / while idle (default 1000)
"use strict";

const DRAIN_PATH = "/api/browser/command-drain";
const RESULT_PATH = "/api/browser/command-result";

// Drain long-poll wait (ms). Deliberately SHORT: the loop must re-read
// listPanelIds this often to pick up a newly declared chat slot and register it
// on the gateway bus. A long hold would strand a fresh session's first navigate
// until it ended; aborting the hold instead would race an in-flight command the
// bus already popped (dropping it), so a bounded re-poll is the race-free way to
// stay responsive. Must stay below the bus's submit-side panel wait
// (command_bus.py DEFAULT_PANEL_WAIT_MS) so registration lands inside it.
const DEFAULT_WAIT_MS = 1500;
const DEFAULT_BACKOFF_MS = 1000;
// How long to idle when no panel exists. Kept short so a freshly-opened panel
// starts being served promptly, but non-zero so we never busy-spin.
const DEFAULT_IDLE_MS = 500;

/** Join a base URL and a path without doubling or dropping the slash. */
function joinUrl(base, path) {
  const b = String(base || "").replace(/\/+$/, "");
  return `${b}${path}`;
}

/**
 * Build the agent command channel.
 *
 * All deps injected — see the header for the contract. Returns an object with
 * `start()` / `stop()` plus test seams (`isRunning`).
 */
function createAgentCommandChannel(deps) {
  const {
    fetchFn,
    getGatewayUrl,
    getSecret,
    listPanelIds,
    dispatch,
    onError = () => {},
    waitMs = DEFAULT_WAIT_MS,
    backoffMs = DEFAULT_BACKOFF_MS,
    idleMs = DEFAULT_IDLE_MS,
    // Whether the gateway being polled is on THIS machine. The idle heartbeat
    // fires only when true, so a window connected to a REMOTE gateway never
    // pushes the local secret through the tunnel (which the remote would 403 —
    // the same reason listPanelIds returns [] there). Defaults to false so a
    // caller that does not wire it up gets the safe no-heartbeat behaviour.
    isGatewayLocal = () => false,
  } = deps || {};

  if (typeof fetchFn !== "function") throw new Error("createAgentCommandChannel: fetchFn is required");
  if (typeof getGatewayUrl !== "function") throw new Error("createAgentCommandChannel: getGatewayUrl is required");
  if (typeof getSecret !== "function") throw new Error("createAgentCommandChannel: getSecret is required");
  if (typeof listPanelIds !== "function") throw new Error("createAgentCommandChannel: listPanelIds is required");
  if (typeof dispatch !== "function") throw new Error("createAgentCommandChannel: dispatch is required");

  let running = false;
  // A resolver for the current in-flight sleep, so stop() can interrupt an
  // idle/backoff wait promptly instead of leaving the loop parked.
  let wakeSleep = null;
  // Tracks the loop promise so callers/tests can be sure it has unwound.
  let loopDone = null;
  // Set by poke() when the tracked-slot set changed while the loop was idling;
  // tells the idle branch to re-read listPanelIds at once instead of sleeping.
  let poked = false;

  const report = (err, context) => {
    try {
      onError(err, context);
    } catch {
      /* a log sink must never break the loop */
    }
  };

  /** Sleep that resolves early if stop() is called. */
  function sleep(ms) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        wakeSleep = null;
        resolve();
      };
      wakeSleep = finish;
      setTimeout(finish, Math.max(0, ms));
    });
  }

  // Yield to the MACROTASK queue (a real event-loop tick), not just the
  // microtask queue. The "poll again immediately" (204) path and the
  // command-handled path otherwise chain only microtasks when the gateway and
  // dispatch resolve synchronously — e.g. a gateway that returns 204 instantly
  // instead of holding the long-poll open. A pure-microtask loop starves the
  // event loop: timers (including our own stop()-interruptible sleeps) never
  // fire, and the loop spins until it exhausts memory. One macrotask tick per
  // iteration keeps "immediately" honest while letting the event loop breathe.
  function yieldTick() {
    return new Promise((resolve) => {
      if (typeof setImmediate === "function") setImmediate(resolve);
      else setTimeout(resolve, 0);
    });
  }

  function headers() {
    const h = { "Content-Type": "application/json" };
    const secret = getSecret();
    if (secret) h["X-Internal-Secret"] = secret;
    return h;
  }

  /**
   * Long-poll for one command. Returns:
   *   - a command object `{ id, session_key, op, args }` on HTTP 200,
   *   - `null` on 204 (nothing arrived; caller polls again immediately),
   *   - `undefined` on a transport error / non-2xx / malformed body (caller
   *     backs off). The distinction between null and undefined is what lets the
   *     caller poll-immediately vs back-off without inspecting HTTP itself.
   */
  async function drainOnce(sessionKeys) {
    let res;
    try {
      res = await fetchFn(joinUrl(getGatewayUrl(), DRAIN_PATH), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ session_keys: sessionKeys, wait_ms: waitMs }),
      });
    } catch (err) {
      report(err, { phase: "drain-fetch" });
      return undefined;
    }
    if (!res || typeof res.status !== "number") {
      report(new Error("drain: malformed response object"), { phase: "drain-response" });
      return undefined;
    }
    if (res.status === 204) return null;
    if (!res.ok) {
      report(new Error(`drain: HTTP ${res.status}`), { phase: "drain-status", status: res.status });
      return undefined;
    }
    let body;
    try {
      body = await res.json();
    } catch (err) {
      report(err, { phase: "drain-parse" });
      return undefined;
    }
    // A 200 with no usable command is malformed; treat it like an error so we
    // back off rather than immediately hammering the gateway.
    if (!body || typeof body !== "object" || typeof body.id !== "string" || typeof body.op !== "string") {
      report(new Error("drain: malformed command payload"), { phase: "drain-validate", body });
      return undefined;
    }
    return body;
  }

  /** Host-presence heartbeat: a zero-wait drain with no session keys. It queues
   *  nothing and returns 204 at once, but tells the gateway's command bus that a
   *  local Electron host is polling, so a ``navigate`` racing a fresh slot's
   *  registration is held briefly instead of falling back to Playwright. Fired
   *  only while idle (no panels to drive) and only against a local gateway.
   *  Best-effort: a failure here is reported but never breaks the loop. */
  async function heartbeat() {
    try {
      await fetchFn(joinUrl(getGatewayUrl(), DRAIN_PATH), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify({ session_keys: [], wait_ms: 0 }),
      });
    } catch (err) {
      report(err, { phase: "heartbeat" });
    }
  }

  /** POST the outcome of one command back to the gateway. Best-effort: a failure
   *  here is reported but never propagates into the loop. */
  async function postResult(payload) {
    let res;
    try {
      res = await fetchFn(joinUrl(getGatewayUrl(), RESULT_PATH), {
        method: "POST",
        headers: headers(),
        body: JSON.stringify(payload),
      });
    } catch (err) {
      report(err, { phase: "result-fetch", id: payload && payload.id });
      return;
    }
    if (!res || !res.ok) {
      report(new Error(`result: HTTP ${res && res.status}`), { phase: "result-status", id: payload && payload.id });
    }
  }

  /** Run one command against its panel and post the result. A rejecting or
   *  throwing dispatch becomes an ok:false result — it never escapes. */
  async function handleCommand(cmd) {
    let payload;
    try {
      const result = await dispatch(cmd.session_key, cmd.op, cmd.args || {});
      payload = { id: cmd.id, ok: true, result };
    } catch (err) {
      payload = { id: cmd.id, ok: false, error: String((err && err.message) || err) };
    }
    await postResult(payload);
  }

  async function loop() {
    while (running) {
      // Fresh each iteration; poke() sets it while this iteration is awaiting
      // (a drain long-poll or an idle wait) to mean "the tracked-slot set
      // changed — re-read listPanelIds now instead of backing off".
      poked = false;

      let sessionKeys;
      try {
        sessionKeys = listPanelIds();
      } catch (err) {
        report(err, { phase: "list-panels" });
        sessionKeys = null;
      }

      // Nothing to drive — idle instead of holding a long-poll open or
      // busy-spinning. On a LOCAL gateway, first send a host-presence heartbeat
      // so a navigate racing a fresh slot's registration is held briefly rather
      // than dropped to the Playwright mirror. A poke() interrupts the idle wait.
      if (!Array.isArray(sessionKeys) || sessionKeys.length === 0) {
        if (isGatewayLocal()) await heartbeat();
        // A poke() during the heartbeat (no idle sleep was active for it to
        // wake) still means the tracked set changed — re-read at once instead
        // of sleeping out the interval.
        if (poked) continue;
        if (running) await sleep(idleMs);
        continue;
      }

      const cmd = await drainOnce(sessionKeys);
      if (!running) break;

      if (cmd === undefined) {
        // Transport error / non-2xx / malformed — bounded backoff, never tight-loop.
        await sleep(backoffMs);
        continue;
      }
      if (cmd === null) {
        // 204: nothing this cycle, poll again immediately (but yield a tick so
        // an instant-204 gateway cannot starve the event loop).
        await yieldTick();
        continue;
      }
      // A failing op must never kill the loop.
      try {
        await handleCommand(cmd);
      } catch (err) {
        report(err, { phase: "handle", id: cmd && cmd.id });
      }
      // Yield a tick before the next drain so a burst of instantly-resolving
      // commands also cannot starve the loop.
      await yieldTick();
    }
  }

  return {
    /** Start the drain loop. Idempotent — a second start() is a no-op while
     *  already running. */
    start() {
      if (running) return;
      running = true;
      loopDone = loop().catch((err) => {
        // The loop is written not to throw; this is a last-resort guard so an
        // unexpected escape marks the channel stopped rather than leaving a
        // dangling running=true.
        report(err, { phase: "loop-crash" });
        running = false;
      });
    },

    /** Nudge the loop to re-read listPanelIds NOW. Called when the tracked-slot
     *  set changes (a chat slot was declared / withdrawn). It wakes an in-flight
     *  idle/backoff wait so the loop re-reads at once; a drain long-poll is left
     *  to finish on its own (it is short — see DEFAULT_WAIT_MS — and aborting it
     *  could drop a command the bus already popped). Safe to call when stopped. */
    poke() {
      poked = true;
      if (wakeSleep) wakeSleep();
    },

    /** Stop the loop cleanly, interrupting any in-flight idle/backoff sleep.
     *  Resolves once the loop has fully unwound. */
    async stop() {
      running = false;
      if (wakeSleep) wakeSleep();
      const done = loopDone;
      loopDone = null;
      if (done) await done;
    },

    /** Test seam. */
    isRunning: () => running,
  };
}

module.exports = {
  DRAIN_PATH,
  RESULT_PATH,
  DEFAULT_WAIT_MS,
  DEFAULT_BACKOFF_MS,
  DEFAULT_IDLE_MS,
  joinUrl,
  createAgentCommandChannel,
};
