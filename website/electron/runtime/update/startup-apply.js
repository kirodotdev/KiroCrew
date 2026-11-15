"use strict";

const { createManagedExec } = require("./managed-exec");
const { createUpdateArm } = require("./update-arm");
const { createRelaunch, applyArmedAtStart } = require("./armed-apply");
const { readPolicyGovernance } = require("./policy-source");

/**
 * Run an armed update at app start, before the gateway spawns and before any
 * window opens. Only an install whose EXTERNALLY-MANAGED marker names an
 * updateCommand can have been armed, so every other install returns at once.
 *
 * Never rejects: a throw here would strand the launch, so it is logged and the
 * installed version starts.
 */
async function runStartupApply({ app, store, managed, managedPath, log, probeGateway = null, notifyGatewayRunning = null, readGovernance = readPolicyGovernance }) {
  try {
    if (!managed || !managed.updateCommand) return { outcome: "none" };
    const arm = createUpdateArm({ store });
    if (!arm.read()) return { outcome: "none" };
    // A security policy provider that governed this install owns its updates.
    // The marker's command is not run on its behalf; the arm stays. The verdict
    // the last run stored is not enough on its own: a provider installed since
    // then would be bypassed. The policy file is read again now, and a file that
    // cannot be read counts as governed, since it may hold a provider.
    let current = "unknown";
    try { current = readGovernance(); } catch { current = "unknown"; }
    if (arm.wasGoverned() || current !== "ungoverned") {
      log.info("[update] a policy provider governs this install (or the policy cannot be read) - the armed update is left to it, not applied at start");
      // The quit handler runs before the dashboard reports a live verdict and
      // falls back to the stored one, so a stale "ungoverned" would let the
      // marker's command run over the provider. Store what this start found; the
      // first live verdict replaces it.
      try { arm.recordGoverned(true); } catch { /* an unwritable store keeps the last verdict */ }
      return { outcome: "governed" };
    }
    const result = await applyArmedAtStart({
      arm,
      exec: createManagedExec({ managed, managedPath, log }),
      currentVersion: app.getVersion(),
      relaunch: createRelaunch({ managed, app, log }),
      app,
      log,
      probeGateway,
    });
    if (result.outcome === "gateway-running" && notifyGatewayRunning) {
      try { notifyGatewayRunning(result.stopCommand); } catch { /* notifications optional */ }
    }
    return result;
  } catch (err) {
    log.error("[update] start-time apply threw - starting the installed version", err);
    return { outcome: "failed", error: String((err && err.message) || err) };
  }
}

module.exports = { runStartupApply };
