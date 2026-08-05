"use strict";
//
// Pure, injectable recovery-strategy decision, extracted from main.js so it can
// be unit-tested without Electron (mirrors gateway-liveness.js / gateway-wait.js
// / gateway-stop.js).
//
// PROBLEM: the post-handoff liveness monitor fires onUnresponsive whenever
// /api/status stops answering. main.js used to react ONE way — assume a wedged
// local backend and force-kill the port + respawn. That is correct only when WE
// spawned the gateway. On the reuse path the port-holder is someone else's
// process, and in the remote-tunnel setup (localhost:<port> is an SSH forward to
// a remote gateway) an unresponsive probe almost always means the SSH tunnel
// dropped (lid close, Wi-Fi<->Ethernet handoff, VPN blip), not a wedged backend.
// Killing the port would tear down the tunnel; the old code then fell through to
// a terminal error dialog that QUIT the app on any button — including Retry (the
// perceived "crash on Retry" on network change).
//
// This helper is the single source of truth for that fork: given whether we own
// the gateway, return which recovery strategy to run. Keeping it pure means the
// ownership rule is covered by tests independently of the Electron plumbing.

/**
 * Decide how to recover an unresponsive gateway.
 *
 * @param {object} o
 * @param {boolean} o.weSpawnedGateway  true only when this app spawned the
 *                                       bundled backend on this port (spawn
 *                                       path). false on the reuse path — a
 *                                       gateway was already answering at boot
 *                                       (remote tunnel or external gateway).
 * @returns {"respawn" | "reconnect"}
 *   "respawn"   — we own the child: kill the wedged tree, free the port, spawn a
 *                 fresh backend, re-run the boot flow.
 *   "reconnect" — we do NOT own the port-holder: never kill it or spawn locally;
 *                 re-probe until the external gateway / tunnel heals, then
 *                 reconnect (re-fetching a token, since the drop likely
 *                 invalidated the old one).
 */
function chooseRecoveryStrategy({ weSpawnedGateway }) {
  return weSpawnedGateway ? "respawn" : "reconnect";
}

module.exports = { chooseRecoveryStrategy };
