"use strict";
//
// Pure, injectable gateway-readiness logic, extracted from main.js so it can be
// unit-tested without Electron (mirrors gateway-stop.js). main.js binds the
// real checkBackend / window / status / failure-flag to these helpers.
//
// The headline behavior is FAIL-FAST: a bundled gateway that EXITS during
// startup (e.g. ModuleNotFoundError, or SIGKILL from Gatekeeper) will never
// become healthy, so polling its port for the full timeout is dead time behind
// a misleading "Waiting for gateway…" message. As soon as the spawn watcher
// reports a terminal exit we reject immediately (~1s) and surface the cause +
// the launch log. The maxWaitMs timeout stays purely as the backstop for a
// gateway that is alive but slow to bind its port.

/**
 * Poll `checkBackend` until the gateway answers, the spawned child reports a
 * terminal failure, the window closes, or we hit maxWaitMs.
 *
 * @param {object}   o
 * @param {() => Promise<void>} o.checkBackend  resolve = healthy, reject = not yet
 * @param {() => (object|null)} [o.getFailure]  returns a truthy failure record
 *                                              ({code,signal} or {error}) once the
 *                                              spawned gateway has exited; null while
 *                                              it is still alive / never spawned
 * @param {() => boolean} [o.isWindowAlive]     false => abort (window closed)
 * @param {(msg: string) => void} [o.onStatus]  progress callback
 * @param {() => number} [o.now]                injectable clock (tests)
 * @param {(fn: Function, ms: number) => any} [o.setTimeoutFn]  injectable timer (tests)
 * @param {number} [o.maxWaitMs=30000]
 * @param {number} [o.pollIntervalMs=500]
 * @returns {Promise<void>} resolves when healthy; rejects with a tagged Error
 *   where `err.kind` is one of 'failed' | 'timeout' | 'window-closed', and for
 *   'failed' `err.failure` carries the exit record.
 */
function waitForGateway({
  checkBackend,
  getFailure = () => null,
  isWindowAlive = () => true,
  onStatus = () => {},
  now = Date.now,
  setTimeoutFn = setTimeout,
  maxWaitMs = 30_000,
  pollIntervalMs = 500,
}) {
  const start = now();
  return new Promise((resolve, reject) => {
    const poll = () => {
      if (!isWindowAlive()) {
        const e = new Error("Window closed");
        e.kind = "window-closed";
        return reject(e);
      }
      // Check the spawned child's terminal state BEFORE the timeout: a process
      // that already exited is never coming back, so fail now rather than
      // waiting out the clock.
      const failure = getFailure();
      if (failure) {
        const e = new Error(describeGatewayFailure(failure));
        e.kind = "failed";
        e.failure = failure;
        return reject(e);
      }
      if (now() - start > maxWaitMs) {
        const e = new Error("Backend timeout");
        e.kind = "timeout";
        return reject(e);
      }
      onStatus(`Waiting for gateway… ${Math.round((now() - start) / 1000)}s`);
      checkBackend()
        .then(() => { onStatus("Connected ✓"); resolve(); })
        .catch(() => setTimeoutFn(poll, pollIntervalMs));
    };
    poll();
  });
}

/**
 * One-line, human-readable reason for a gateway start failure. The SIGKILL case
 * carries the Gatekeeper hint because an unsigned/quarantined nested executable
 * being killed on launch is the most common "works for me, not my friend" mode.
 *
 * @param {{code?: number|null, signal?: string|null, error?: string, disabled?: boolean, port?: number}|null} failure
 * @returns {string}
 */
function describeGatewayFailure(failure) {
  if (!failure) return "The gateway failed to start.";
  // Nothing was launched, so there is no exit code and no log to read: the port
  // was silent and this app is set not to start a gateway here. Naming both
  // halves matters because either one alone is a normal, working state.
  //
  // Deliberately does NOT send the user to Settings: the page holding that
  // switch is served by a gateway, which is the thing not running. The error
  // dialog carries a button instead.
  if (failure.disabled) {
    return `No gateway is answering on port ${failure.port}, and Kiro Crew is set `
      + "not to start one on this machine. Start the gateway you connect to (or "
      + "the connection that reaches it) and retry, or start one here.";
  }
  // An incomplete bundle is not a launch failure — the installer is still writing
  // the backend. That message already explains the state and names Retry, so pass
  // it through bare; prefixing "could not be launched" would open with failure
  // vocabulary under a title saying the install is still finishing.
  if (failure.incompleteBundle && failure.error) return failure.error;
  if (failure.error) return `The gateway could not be launched: ${failure.error}`;
  if (failure.signal === "SIGKILL") {
    return "The gateway was killed on launch (SIGKILL). On macOS this usually "
      + "means Gatekeeper blocked an unsigned or quarantined executable — try: "
      + "xattr -cr <path to KiroCrew.app>";
  }
  if (failure.signal) {
    return `The gateway exited on launch (signal ${failure.signal}).`;
  }
  return `The gateway exited on launch (code ${failure.code}). `
    + "The cause is in the launch log below.";
}

/**
 * Last `n` lines of `text`, with trailing blank lines trimmed — for inlining a
 * launch-log tail into an error dialog. Pure (no fs) so it is trivially tested.
 *
 * @param {string} text
 * @param {number} [n=20]
 * @returns {string}
 */
function tailLines(text, n = 20) {
  if (!text) return "";
  const lines = String(text).replace(/\s+$/, "").split("\n");
  return lines.slice(Math.max(0, lines.length - n)).join("\n");
}

/**
 * True if the log text indicates the gateway could not bind its port because
 * something already holds it (a wedged or other KiroCrew gateway). This is a
 * distinct, recoverable failure from a crash: a plain retry can't help while
 * the holder is still there, but force-stopping it can. Pure (no fs/network).
 *
 * @param {string} text  launch-log tail
 * @returns {boolean}
 */
function isPortInUse(text) {
  if (!text) return false;
  return /address already in use|already in use|EADDRINUSE/i.test(String(text));
}

module.exports = { waitForGateway, describeGatewayFailure, tailLines, isPortInUse };
