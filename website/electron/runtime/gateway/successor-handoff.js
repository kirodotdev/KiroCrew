"use strict";

const { gatewayWaitTimeoutMs } = require("../../gateway-wait");
const { readBakedEdition } = require("../../edition");
const {
  classifyGatewayReadiness,
  HEALTH_IDENTITY_PATH,
  READY_PATH,
} = require("../../instance-guard");

const RESTARTING_STATUS = "Restarting Kiro Crew to finish the update…";
// How long this instance stays alive waiting for a successor copy of the app
// to prove itself by serving on the gateway port (relaunchViaConfirmedSuccessor):
// Electron boot, then the successor's own gateway budget.
//
// That budget is not one number. `gatewayWaitTimeoutMs` gives a primary local
// gateway on Windows 120s, because importing a freshly installed bundled Python
// tree exceeds the ordinary deadline there -- and both callers here spawn exactly
// that: a primary local gateway, on a machine that may have just installed one.
// So the deadline is derived from the same function the successor will apply to
// itself, rather than stated as a single constant. Stating it was wrong on
// Windows: 60s expired while the successor was still inside its own 120s budget,
// and `fail()` killed a gateway that was starting normally.
const SUCCESSOR_BOOT_MARGIN_MS = 30_000;
const SUCCESSOR_POLL_MS = 500;

/**
 * Restarting this app through a successor copy of itself that has to prove it
 * is alive -- its own gateway answering on the port this instance watches --
 * before this instance exits; whether the failure dialog may offer Start Local
 * Gateway, which is that handoff on a crew's port and an in-place start on this
 * launch's own; and the three questions asked of a port over HTTP: does
 * anything answer, who answers (`/api/health` identity), and is the answer
 * ready (`/api/ready`). The launch and recovery owners read the last two too.
 *
 * The supervisor decides when to hand off (a stale bundle that persists after
 * a re-resolve, or Start Local Gateway on a crew's port) and keeps the failure
 * bookkeeping each caller passes in.
 *
 * @param {object} deps
 * @param {() => object|null} deps.getLivenessMonitor  the supervisor's
 *        post-handoff liveness monitor, stopped for the length of a handoff so
 *        it cannot force-stop the port the successor is binding.
 * @param {(monitor: object|null) => void} deps.setLivenessMonitor  clears that
 *        slot once the monitor is stopped.
 */
function createSuccessorHandoff({
  app,
  http,
  spawn,
  processObj,
  path,
  dirname,
  port: PORT,
  backendUrl: BACKEND_URL,
  log: glog,
  quitting,
  mainWindow,
  sendStatus,
  splashQuery,
  currentThemeAccent,
  showLoadingThenConnect,
  probeGatewayPortOwner,
  canRelaunchThisApp,
  getLivenessMonitor,
  setLivenessMonitor,
  setTimeoutFn,
  clearTimeoutFn,
}) {
  /**
   * May the failure dialog offer "Start Local Gateway"?
   *
   * Reversing the opt-out is reachable only here: the settings page that writes
   * it is served by the gateway that is not running. On this launch's own port
   * the action starts a gateway in place. On a crew's port it cannot -- the
   * spawn binds PORT and would shadow the crew -- so it re-runs port selection
   * in a fresh process, which needs an executable to re-exec and one attempt
   * that has not already failed.
   *
   * @param {boolean} localGatewayOff  this launch started nothing.
   * @param {string} remoteTarget      the crew this launch targeted, if any.
   * @returns {boolean}
   */
  function canOfferLocalStart(localGatewayOff, remoteTarget) {
    if (!localGatewayOff) return false;
    if (!remoteTarget) return true;
    return canRelaunchThisApp();
  }

  /**
   * Restart the app by starting a fresh copy of it and exiting only once that
   * copy is demonstrably alive.
   *
   * app.relaunch() is not usable here: it returns nothing and only schedules a
   * re-exec for exit time, so when the bundle is pruned between the
   * canRelaunchThisApp probe and that re-exec the app exits into nothing and
   * no code is left to notice. Spawning the successor ourselves lets this
   * process observe it, but
   * Node's "spawn" event only proves the exec succeeded: a bundle intact
   * enough to exec and broken enough to crash during initialization would
   * still take the app down. The handshake is therefore the successor's own
   * gateway answering on the port, and it carries that meaning only while the
   * port has nobody on it to begin with. That is checked here rather than
   * assumed: a gateway a separate install or a terminal launched answers exactly
   * like a successor would, and confirming on it would exit this instance on a
   * stranger's liveness. The test is whether ANY responder is there, not what it
   * says, because a gateway too old to serve the readiness endpoint is still a
   * gateway holding the port. So an occupied port abandons the handoff with this
   * instance untouched, and only a port that nothing answered on can confirm
   * one. An answer on such a port means the successor booted, ran its
   * supervisor, and started a serving backend. A 503 "starting" counts too: the
   * successor's gateway is bound and only still restoring sessions.
   *
   * What remains is a bind between the check and the successor's own bind. That
   * window is the spawn itself, and losing it costs a killed successor and a
   * surfaced failure rather than an app that exits into nothing, because the
   * check is what stands between a foreign gateway and `app.exit`.
   *
   * Which port carries that handshake belongs to the caller. A bundle upgrade
   * re-execs the same configuration, so the successor binds this process's own
   * port and inherits the environment that chose it. Turning the local gateway
   * back on does not: the successor would re-run port selection with the setting
   * now on, and an inherited `KIROCREW_PORT` outranks that selection, so the
   * successor could bind a port this process is not watching and be killed while
   * healthy. That caller therefore pins its chosen port into the successor's
   * environment, which makes the watched port and the bound port one value
   * instead of two answers that have to agree.
   *
   * Everything short of that is a failure with this instance still alive:
   * a spawn error (ENOENT on a pruned bundle), the successor exiting before
   * its gateway answered, or the bounded wait running out. The failure path
   * kills a successor that is still running so exactly one instance remains,
   * takes the single-instance lock back so a later manual launch still routes
   * here, and runs the caller's ordinary failure bookkeeping.
   *
   * The liveness monitor is stopped for the duration: were it to fire during
   * the handoff it would force-stop the port the successor's gateway is
   * binding. Mid-session the boot splash is put back first, since only it
   * renders status and the user would otherwise watch the window vanish
   * unannounced; a failed mid-session handoff then surfaces the failure
   * dialog itself, the way the monitor's recovery would have.
   *
   * @param {(outcome: {reason: string, port: number}) => void} onFailed  the
   *        caller's failure bookkeeping. `reason` is "port-busy" when the port
   *        was already held and nothing was spawned, or "successor-failed" when
   *        a successor ran and never served. The two need different wording: one
   *        reports a restart that never happened.
   * @param {object} [options]
   * @param {number} [options.expectPort]  port the successor will serve on.
   * @param {boolean} [options.pinPort]  put expectPort in the successor's
   *        environment, for a caller choosing a port rather than predicting the
   *        one the successor would select for itself.
   * @param {string} [options.restartingStatus]  what the splash says while the
   *        handoff runs, since only the caller knows why it is restarting.
   */
  async function relaunchViaConfirmedSuccessor(
    onFailed,
    { expectPort = PORT, pinPort = false, restartingStatus = RESTARTING_STATUS } = {},
  ) {
    const readyUrl = `http://localhost:${expectPort}${READY_PATH}`;
    const target = processObj.execPath;
    const args = Array.isArray(processObj.argv) ? processObj.argv.slice(1) : [];
    // Before anything is torn down, since abandoning the handoff has to leave
    // this instance exactly as it stands.
    if (await portHasAnyResponder(readyUrl)) {
      glog(`:${expectPort} already has a responder — cannot tell a successor from it, so surfacing the failure instead of handing off`);
      // Nothing was spawned and nothing was torn down, which is a different
      // state from a successor that ran and never served: the caller must not
      // report a restart that did not happen, and the condition is external so
      // it can clear on its own.
      onFailed({ reason: "port-busy", port: expectPort });
      return;
    }
    const livenessMonitor = getLivenessMonitor();
    const midSession = livenessMonitor !== null;
    if (livenessMonitor) {
      livenessMonitor.stop();
      setLivenessMonitor(null);
    }
    if (midSession) {
      // Only the boot splash renders status messages; the dashboard has no
      // listener. Put the splash back so the announcement lands where the
      // user is looking, without stealing focus from a hidden window.
      const window = mainWindow();
      if (window && !window.isDestroyed()) {
        try {
          window.webContents.loadFile(path.join(dirname, "loading.html"), {
            query: splashQuery(window, { accent: currentThemeAccent() }),
          });
        } catch { /* window may be tearing down */ }
      }
    }
    sendStatus(restartingStatus);
    app.releaseSingleInstanceLock();
    let settled = false;
    let gone = false;
    let pollTimer = null;
    let deadlineTimer = null;
    const clearTimers = () => {
      if (pollTimer) { clearTimeoutFn(pollTimer); pollTimer = null; }
      if (deadlineTimer) { clearTimeoutFn(deadlineTimer); deadlineTimer = null; }
    };
    const spawnOptions = { detached: true, stdio: "ignore" };
    if (pinPort) {
      spawnOptions.env = { ...processObj.env, KIROCREW_PORT: String(expectPort) };
    }
    const successor = spawn(target, args, spawnOptions);

    // `failureReason` is what the CALLER is told, and it is separate from the log
    // line because one of these outcomes is not a successor that failed. When the
    // ownership probe cannot run, the successor answered readiness and was then
    // stopped by this function -- so reporting "successor-failed" would make the
    // dialog deny something the user just watched happen, and would invite the
    // same click, which repeats identically because a probe that cannot run on
    // this host cannot run on the retry either.
    const fail = (reason, { failureReason = "successor-failed" } = {}) => {
      if (settled) return;
      settled = true;
      clearTimers();
      glog(`successor app ${reason} — cannot relaunch; surfacing the failure instead`);
      if (!gone) {
        try { successor.kill(); }
        catch (killError) { glog(`could not stop the unconfirmed successor: ${killError && killError.message}`); }
      }
      try {
        if (!app.requestSingleInstanceLock()) glog("could not re-take the single-instance lock: another instance holds it");
      } catch (lockError) {
        glog(`could not re-take the single-instance lock: ${lockError && lockError.message}`);
      }
      onFailed({ reason: failureReason, port: expectPort });
      if (!midSession) return;
      const window = mainWindow();
      if (!window || window.isDestroyed() || quitting()) return;
      showLoadingThenConnect(window, BACKEND_URL, { reconnect: true })
        .catch((error) => glog(`surfacing the failed relaunch failed: ${error && error.message}`));
    };
    const confirm = () => {
      if (settled) return;
      settled = true;
      clearTimers();
      successor.unref();
      glog(`successor app (pid ${successor.pid}) is serving on :${expectPort} — exiting this instance`);
      app.exit(0);
    };
    // Readiness says something answered on that port; it cannot say the answer
    // came from the successor's gateway. A manual SSH tunnel that binds AFTER the
    // pre-spawn probe passes this poll by construction, and confirmation is an
    // unconditional exit with no recovery step -- the next launch then classifies
    // the tunnel as a local gateway and the idle heartbeat sends it
    // X-Internal-Secret. So an HTTP answer is necessary and not sufficient: the
    // LISTEN owner has to be positively identified, which is the same gate
    // ipc-registrar applies to host-capability IPC for the same tunnel-on-the-
    // local-port reason.
    //
    // The four outcomes are deliberately not collapsed, because they call for
    // three different actions:
    //   kirocrew / service -- positively ours. Confirm. A successor spawns its
    //     own gateway child, so the holder is a grandchild of this process and
    //     never the pid we spawned; this classifies the process, not the
    //     instance, which is why it still passes an ordinary handoff.
    //   foreign -- someone else holds it. Nothing this poll waits for can change
    //     that, so it fails immediately with the reason rather than spending the
    //     remaining window and then reporting a timeout.
    //   none -- answered with no visible local listener: a race, or a socket we
    //     cannot see. Keep polling and let the deadline decide.
    //   unknown -- the probe itself could not run (no lsof, EACCES). This
    //     refuses too, because classifyPortOwner's own rule is never to mistake
    //     "couldn't look" for "safe to kill", and confirming is the more
    //     destructive of the two answers available here: it is an unconditional
    //     app.exit(0) that also makes the next launch treat the holder as local,
    //     which is what sends it X-Internal-Secret. Refusing costs a surfaced
    //     failure on a host whose port-probe tooling is missing -- a state both
    //     platforms have their own lister for, so it is a broken host rather
    //     than a platform -- and the failure path re-offers the button. Waiting
    //     instead would only reach the deadline and then report a successor that
    //     never served, which is the wrong cause.
    const successorOwnsPort = async () => {
      const owner = await probeGatewayPortOwner(expectPort);
      if (owner === "kirocrew" || owner === "service") return "confirm";
      if (owner === "foreign") return "foreign";
      if (owner === "unknown" || owner === "unidentified") {
        glog(
          `successor check: listener probe unavailable on :${expectPort} `
          + "-- refusing to hand over to a holder that cannot be identified",
        );
        return "unverifiable";
      }
      return "wait";
    };
    const poll = async () => {
      pollTimer = null;
      if (settled) return;
      // The splash loads asynchronously and may have missed the first send.
      sendStatus(restartingStatus);
      const readiness = await fetchGatewayReadiness(readyUrl);
      if (settled) return;
      if (readiness === "ready" || readiness === "starting") {
        const verdict = await successorOwnsPort();
        if (settled) return;
        if (verdict === "confirm") { confirm(); return; }
        if (verdict === "foreign") {
          fail(`:${expectPort} is held by another process, not the restarted app`);
          return;
        }
        if (verdict === "unverifiable") {
          // Its gateway DID answer; what could not be established is which
          // process holds the port. So this is not a successor that failed, and
          // the caller is told so by name -- the kill still happens, because two
          // live instances with the lock held by the one we cannot vouch for is a
          // worse state than one surfaced failure.
          fail(
            `could not check which process holds :${expectPort}, so this app kept running instead of handing over`,
            { failureReason: "successor-unverifiable" },
          );
          return;
        }
      }
      pollTimer = setTimeoutFn(poll, SUCCESSOR_POLL_MS);
    };

    successor.once("spawn", () => {
      if (settled) return;
      glog(`successor app started (pid ${successor.pid}) — waiting for its gateway to answer on :${expectPort}`);
      // The successor is a primary local gateway it spawns itself, so the budget
      // it will apply is the watchSpawn one -- on Windows 120s, elsewhere 30s,
      // plus the pre-bind check budget in an enterprise build.
      const readyTimeoutMs = gatewayWaitTimeoutMs({
        platform: processObj.platform,
        watchSpawn: true,
        edition: readBakedEdition(),
      }) + SUCCESSOR_BOOT_MARGIN_MS;
      deadlineTimer = setTimeoutFn(
        () => fail(`did not answer on :${expectPort} within ${readyTimeoutMs / 1000}s`),
        readyTimeoutMs,
      );
      void poll();
    });
    successor.once("error", (error) => {
      gone = true;
      fail(`failed to start (${error.code || error.message}) from ${target}`);
    });
    successor.once("exit", (code, signal) => {
      gone = true;
      fail(`exited (code=${code} signal=${signal}) before its gateway answered`);
    });
  }

  // NOTE: /api/health carries app identity; /api/status does not.
  function fetchHealthInfo(healthUrl = `${BACKEND_URL}${HEALTH_IDENTITY_PATH}`) {
    return new Promise((resolve) => {
      const req = http.get(healthUrl, { timeout: 2000 }, (res) => {
        let body = "";
        res.on("data", (chunk) => { body += chunk; });
        res.on("end", () => {
          try { resolve(JSON.parse(body)); } catch { resolve(null); }
        });
      });
      req.on("error", () => resolve(null));
      req.on("timeout", () => { req.destroy(); resolve(null); });
    });
  }

  // Whether ANYTHING is serving on a port, as opposed to what it says. The
  // readiness classifier cannot answer this: it folds a legacy gateway's 404
  // and a refused connection into the same "unknown", and a handoff that treats
  // that as an empty port hands a successor a port someone else holds. Only a
  // transport failure is an empty port. A connection that opens and then says
  // nothing counts as occupied, because refusing a handoff costs a surfaced
  // failure while confirming a stranger's costs the app.
  function portHasAnyResponder(url) {
    return new Promise((resolve) => {
      const req = http.get(url, { timeout: 2000 }, (response) => {
        response.resume();
        resolve(true);
      });
      req.on("error", () => resolve(false));
      req.on("timeout", () => { req.destroy(); resolve(true); });
    });
  }

  // /api/status and /api/health remain 200 while a gateway drains. /api/ready
  // is the only probe that prevents adopting a process which is about to exit.
  function fetchGatewayReadiness(readyUrl = `${BACKEND_URL}${READY_PATH}`) {
    return new Promise((resolve) => {
      const req = http.get(readyUrl, { timeout: 2000 }, (res) => {
        let body = "";
        res.on("data", (chunk) => { body += chunk; });
        res.on("end", () => {
          let payload = null;
          try { payload = JSON.parse(body); } catch { /* classify on status alone */ }
          resolve(classifyGatewayReadiness(res.statusCode, payload));
        });
      });
      req.on("error", () => resolve("unknown"));
      req.on("timeout", () => { req.destroy(); resolve("unknown"); });
    });
  }

  return {
    canOfferLocalStart,
    relaunchViaConfirmedSuccessor,
    fetchHealthInfo,
    fetchGatewayReadiness,
  };
}

module.exports = { createSuccessorHandoff };
