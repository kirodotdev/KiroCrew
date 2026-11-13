"use strict";

// The single owner of "a gateway respawn is in flight". Liveness recovery, the
// failure dialog's retry actions and the recovery after a failed update install
// all respawn through it, so they never start two launches at once: a second
// caller joins the launch already running instead of starting its own.
//
// startGateway() sets the supervisor's child slot only after its own async
// probes, so a caller cannot tell from that slot alone that a launch is under
// way. This owner can, because every respawn passes through it.
//
// An update install owns the gateway from the moment it is dispatched. A
// respawn asked for while an install is in flight does not start, and one that
// was running when an install was dispatched is superseded: its spawn is
// refused, and its caller must not go on to connect. A failed install starts a
// recovery of its own, which gets a fresh launch rather than joining the stale
// one.

/** What a respawn resolves to when an update install has taken over. */
const RESPAWN_SUPERSEDED = "superseded";

/**
 * @param {object} deps
 * @param {(options: {isStale: () => boolean}) => Promise<boolean>} deps.startGateway
 *   the supervisor's launch; it refuses to spawn once `isStale()` is true.
 * @param {() => number} deps.getInstallEpoch  bumped on every install dispatch.
 * @param {() => boolean} deps.isInstallingUpdate  an install is in flight now.
 * @param {(message: string) => void} [deps.log]
 */
function createRespawnOwner({ startGateway, getInstallEpoch, isInstallingUpdate, log = () => {} }) {
  let inFlight = null;

  /**
   * Respawn the gateway, or join the respawn already running.
   * @returns {Promise<boolean|typeof RESPAWN_SUPERSEDED>} what startGateway
   *   resolved to, or RESPAWN_SUPERSEDED when an install took over.
   */
  function respawnGateway() {
    if (isInstallingUpdate()) {
      log("respawn: an update install is in flight; not starting a gateway");
      return Promise.resolve(RESPAWN_SUPERSEDED);
    }
    const epoch = getInstallEpoch();
    if (inFlight && inFlight.epoch === epoch) {
      log("respawn: a respawn is already in flight; joining it");
      return inFlight.promise;
    }
    // An older launch belongs to an epoch an install has since ended. It stops
    // at its spawn check; waiting for it keeps two launches from probing the
    // port at the same time.
    const previous = inFlight ? inFlight.promise : null;
    const stale = () => getInstallEpoch() !== epoch;
    const entry = { epoch, promise: null };
    entry.promise = (async () => {
      if (previous) await previous.catch(() => {});
      if (stale() || isInstallingUpdate()) return RESPAWN_SUPERSEDED;
      const started = await startGateway({ isStale: stale });
      return stale() ? RESPAWN_SUPERSEDED : started;
    })().finally(() => {
      if (inFlight === entry) inFlight = null;
    });
    inFlight = entry;
    return entry.promise;
  }

  return { respawnGateway };
}

module.exports = { createRespawnOwner, RESPAWN_SUPERSEDED };
