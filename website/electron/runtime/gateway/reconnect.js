"use strict";

const { decideGatewayAction } = require("../../instance-guard");
const {
  classifyAdoptedGateway,
  waitForServiceRebind,
  snapshotIncumbentForRespawn,
  recoverIncumbentWithBackoff,
  INCUMBENT_RETRY_STATUS,
} = require("../../gateway-recovery");

const ADOPTED_RECOVERY_WAIT_MS = 30_000;

/**
 * The two liveness recoveries that never kill the gateway on the port: waiting
 * for an external one (a crew's tunnel, a process this app did not start) to
 * answer again, and giving an adopted local one a bounded wait before starting
 * this app's own once the old one has gone.
 *
 * The supervisor picks the strategy from the gateway's ownership and keeps the
 * kill-and-respawn path for the child it spawned.
 *
 * @param {object} deps
 * @param {() => string} deps.getGatewayOwnership  how the gateway on the port is
 *        owned; a service-managed one gets its manager's rebind grace.
 * @param {(ownership: string) => void} deps.setGatewayOwnership  re-records the
 *        ownership of a gateway a service manager rebound.
 * @param {(failure: object|null) => void} deps.setGatewayStartFailure  clears
 *        the start-failure record before each reconnect.
 */
function createReconnect({
  app,
  path,
  dirname,
  port: PORT,
  backendUrl: BACKEND_URL,
  healthUrl: HEALTH_URL,
  log: glog,
  quitting,
  sendStatus,
  splashQuery,
  setTimeoutFn,
  getGatewayOwnership,
  setGatewayOwnership,
  setGatewayStartFailure,
  checkBackend,
  startGateway,
  showLoadingThenConnect,
  showUnrecoverableGatewayError,
  waitForPortFree,
  probeGatewayPortBinding,
  probeGatewayPortOwner,
  fetchHealthInfo,
  fetchGatewayReadiness,
  snapshotGatewayPortPids,
  unverifiedIncumbent,
  waitForIncumbentExit,
  probeLockHolderPid,
}) {
  async function reconnectExternalGateway(window) {
    const webContents = window.webContents;
    try { webContents.loadFile(path.join(dirname, "loading.html"), { query: splashQuery(window, { reconnect: true }) }); }
    catch { /* window may be tearing down */ }
    if (!window || window.isDestroyed() || quitting()) return;
    // No reveal here: network/tunnel healing must not re-surface a window the
    // user minimized or hid to tray.
    sendStatus("Connection lost — waiting for the gateway to come back…");
    for (;;) {
      if (!window || window.isDestroyed() || quitting()) return;
      let healthy = false;
      try { await checkBackend(HEALTH_URL); healthy = true; }
      catch { /* still down */ }
      if (healthy) break;
      await new Promise((resolve) => setTimeout(resolve, 5000));
    }
    if (!window || window.isDestroyed() || quitting()) return;
    glog("liveness: external gateway reachable again — refetching token and reconnecting");
    setGatewayStartFailure(null);
    return showLoadingThenConnect(window, BACKEND_URL, { reconnect: true });
  }

  async function reconnectOrRespawnAdoptedGateway(window) {
    const webContents = window.webContents;
    try { webContents.loadFile(path.join(dirname, "loading.html"), { query: splashQuery(window, { reconnect: true }) }); }
    catch { /* window may be tearing down */ }
    if (!window || window.isDestroyed() || quitting()) return;
    sendStatus("Gateway stopped responding — waiting for it to recover…");
    const deadline = Date.now() + ADOPTED_RECOVERY_WAIT_MS;
    while (Date.now() < deadline) {
      if (!window || window.isDestroyed() || quitting()) return;
      let healthy = false;
      try { await checkBackend(HEALTH_URL); healthy = true; }
      catch { /* still down */ }
      if (healthy) {
        if (!window || window.isDestroyed() || quitting()) return;
        glog("liveness: adopted local gateway answering again — reconnecting");
        setGatewayStartFailure(null);
        return showLoadingThenConnect(window, BACKEND_URL, { reconnect: true });
      }
      await new Promise((resolve) => setTimeout(resolve, 2500));
    }
    if (!window || window.isDestroyed() || quitting()) return;
    glog(`liveness: adopted local gateway did not recover within ${ADOPTED_RECOVERY_WAIT_MS}ms — waiting for :${PORT} to clear, then spawning our own backend`);
    sendStatus("Waiting for the previous gateway to exit…");
    // The incumbent may be draining: a socket lookup can name no PID while the
    // gateway sheds its socket, and once it has released the socket but still
    // holds gateway.lock a socket probe can never name it. Take one lookup;
    // when it names no one, fall back to the CLI's lock-holder probe, which
    // reads the incumbent from the lock file and outlives the socket. Then wait
    // for that process to exit (releasing the lock) before spawning. When the
    // incumbent stays unnameable for a transient reason (lock held past the
    // wait, or a probe that timed out), recovery never spawns blind and
    // never stops: it tries again on a capped backoff behind the reconnect
    // splash, whose close control and the tray's Quit stay available
    // throughout. When the probe refuses (no bundled backend, a probe that
    // cannot start or exits without an answer, or an indeterminate lock),
    // time cannot fix it, so recovery ends on the probe-failed dialog, which
    // tells the user what to do.
    const capture = await recoverIncumbentWithBackoff({
      capture: () => snapshotIncumbentForRespawn({
        snapshot: () => snapshotGatewayPortPids(PORT),
        blocksRespawn: (pids) => unverifiedIncumbent(pids),
        sleep: (ms) => new Promise((resolve) => setTimeoutFn(resolve, ms)),
        lockHolderProbe: () => probeLockHolderPid(),
        log: (message) => glog(`liveness: ${message}`),
      }),
      sleep: (ms) => new Promise((resolve) => setTimeoutFn(resolve, ms)),
      cancelled: () => !window || window.isDestroyed() || quitting(),
      onWaiting: () => sendStatus(INCUMBENT_RETRY_STATUS),
      log: (message) => glog(`liveness: ${message}`),
    });
    if (capture.verdict === "cancelled" || !window || window.isDestroyed() || quitting()) return;
    if (capture.verdict === "refused") {
      glog(`liveness: could not verify the incumbent on :${PORT} — refusing an automatic respawn that could race gateway.lock`);
      return showUnrecoverableGatewayError(window, PORT, { probeFailed: true });
    }
    sendStatus("Waiting for the previous gateway to exit…");
    // "released" means the lock itself is released (incumbent fully exited), so
    // there is no pid to wait on; "captured" names the pid to outwait. Both
    // then confirm the socket is free before spawning.
    const incumbentPids = capture.verdict === "released" ? null : capture.pids;
    if (!(await waitForPortFree())) {
      glog(`liveness: :${PORT} still held by a process we did not spawn — surfacing port-held instead of waiting forever`);
      return showUnrecoverableGatewayError(window, PORT, "held");
    }
    if (!window || window.isDestroyed() || quitting()) return;

    if (getGatewayOwnership() === "reused-service") {
      // A real manager may rebind after the socket release. Orphans share the
      // same classification, so this is a bounded grace, never an exemption.
      glog("liveness: adopted gateway was service-managed — waiting a bounded grace for its manager to respawn it before spawning our own");
      sendStatus("Waiting for the gateway to restart…");
      const verdict = await waitForServiceRebind({
        isPortBound: async () => (await probeGatewayPortBinding(PORT)) !== "free",
        sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      });
      if (window.isDestroyed() || quitting()) return;
      if (verdict === "rebound") {
        // Reuse the full boot decision table for the new holder. A weaker ad-hoc
        // health check could silently adopt a cross-family or draining process.
        const owner = await probeGatewayPortOwner(PORT);
        const health = await fetchHealthInfo();
        const decision = decideGatewayAction(app.getVersion(), health, {
          localOwner: owner,
        });
        const readiness = await fetchGatewayReadiness();
        if (window.isDestroyed() || quitting()) return;
        if (decision.action === "reuse" && readiness !== "shutting-down") {
          glog(`liveness: service manager re-bound :${PORT} (owner=${owner}, reason=${decision.reason}, readiness=${readiness}) — reconnecting to the restarted gateway`);
          setGatewayOwnership(classifyAdoptedGateway({
            reason: decision.reason,
            localOwner: owner,
          }));
          setGatewayStartFailure(null);
          return showLoadingThenConnect(window, BACKEND_URL, { reconnect: true });
        }
        glog(`liveness: :${PORT} was re-bound by an unusable holder (owner=${owner}, action=${decision.action}, readiness=${readiness}) — cannot reconnect or spawn over it`);
        return showUnrecoverableGatewayError(window, PORT, "held");
      }
      glog(`liveness: :${PORT} stayed free past the rebind grace — no manager respawned it; spawning our own backend`);
    }

    await waitForIncumbentExit(incumbentPids, "liveness");
    sendStatus("Starting a fresh gateway…");
    setGatewayStartFailure(null);
    await startGateway();
    if (window.isDestroyed() || quitting()) return;
    return showLoadingThenConnect(window, BACKEND_URL, { reconnect: true });
  }

  return {
    reconnectExternalGateway,
    reconnectOrRespawnAdoptedGateway,
  };
}

module.exports = { createReconnect };
