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
// This helper is the single source of truth for that fork: given how the
// gateway was obtained (the launcher's single ownership state), return which
// recovery strategy to run. Keeping it pure means the ownership rule is covered
// by tests independently of the Electron plumbing.

// The launcher's gateway-ownership vocabulary (main.js keeps exactly one of
// these in a single module-level state; see GATEWAY_OWNERSHIP_STATES):
//   "none"           — no gateway yet, or an adopted holder that could NOT be
//                      positively identified as local (tunnel / external).
//   "spawned"        — this app spawned the bundled backend on this port.
//   "reused-local"   — adopted a local same-family Kiro Crew process.
//   "reused-service" — like reused-local, but the holder was SERVICE-classified.
const GATEWAY_OWNERSHIP_STATES = Object.freeze(["none", "spawned", "reused-local", "reused-service"]);

/**
 * Classify an adopted (reuse-path) gateway into the ownership vocabulary.
 * Positive identification requires BOTH a same-family health answer and a
 * local LISTEN owner ("kirocrew"/"service"); anything less (tunnel, no visible
 * owner, probe failure) stays "none" — the never-kill/never-respawn external
 * classification. Pure so the classification rule is unit-testable without
 * Electron.
 *
 * @param {object} o
 * @param {string} o.reason      the reuse decision's reason (from
 *                               decideGatewayAction); only "same-family" is a
 *                               positive family identification.
 * @param {string} o.localOwner  the LISTEN-owner classification for the port
 *                               ("kirocrew" | "service" | "other" | "none" | …).
 * @returns {"reused-service" | "reused-local" | "none"}
 */
function classifyAdoptedGateway({ reason, localOwner }) {
  const local = reason === "same-family" && (localOwner === "kirocrew" || localOwner === "service");
  if (!local) return "none";
  return localOwner === "service" ? "reused-service" : "reused-local";
}

/**
 * Decide how to recover an unresponsive gateway.
 *
 * @param {object} o
 * @param {string} o.gatewayOwnership  one of GATEWAY_OWNERSHIP_STATES: how the
 *                                     gateway on this port was obtained.
 *                                     "spawned" is the only owned state; the
 *                                     reused-* states are adopted local
 *                                     gateways; "none" (or anything
 *                                     unrecognized) is external/unknown.
 * @returns {"respawn" | "reconnect-bounded" | "reconnect"}
 *   "respawn"   — we own the child: kill the wedged tree, free the port, spawn a
 *                 fresh backend, re-run the boot flow.
 *   "reconnect-bounded" — we adopted a LOCAL same-family gateway we do not own.
 *                 Never kill it, but its death is not a tunnel blip: nothing
 *                 will ever bring it back, so wait a BOUNDED interval for it to
 *                 recover, then (once the port clears on its own) spawn our own
 *                 backend. Waiting forever here is the dead-window
 *                 bug: the shell classified a dead local gateway as "a gateway
 *                 we did not spawn (remote tunnel)" and never respawned.
 *   "reconnect" — genuinely external holder (tunnel / unidentified): never kill
 *                 it or spawn locally; re-probe until the external gateway /
 *                 tunnel heals, then reconnect (re-fetching a token, since the
 *                 drop likely invalidated the old one).
 */
function chooseRecoveryStrategy({ gatewayOwnership }) {
  if (gatewayOwnership === "spawned") return "respawn";
  if (gatewayOwnership === "reused-local" || gatewayOwnership === "reused-service") return "reconnect-bounded";
  // "none", undefined, or anything unrecognized: ownership defaults to "not
  // ours" — the safe strategy is the non-destructive reconnect, never a
  // port-kill.
  return "reconnect";
}

// launchd throttles KeepAlive respawns to ~10s between starts; systemd's
// default RestartSec is far shorter. 15s covers both with margin, and the
// cost of over-waiting lands only on the orphan case (a one-time delay
// before we spawn), never on a live rebind (we adopt the instant it binds).
const SERVICE_REBIND_GRACE_MS = 15_000;

/**
 * After a SERVICE-classified port-holder releases the port, the OS service
 * manager (launchd KeepAlive / systemd Restart=) may be about to respawn it:
 * at the moment the socket closes, a transient release during a service
 * restart is indistinguishable from a permanent exit. Spawning immediately
 * races the manager for the port — one side loses with EADDRINUSE.
 *
 * But "service-classified" does not guarantee a manager will respawn it: an
 * orphaned gateway reparents to init (PPID 1) and classifies as
 * service-managed too, and an orphan has no KeepAlive — nothing will ever
 * rebind. So: wait a bounded grace for a rebind, report "rebound" the moment
 * something takes the port back (the caller adopts/reconnects to it), and
 * report "spawn" only when the grace expires with the port still free.
 *
 * Pure/injectable so the decision is unit-testable without Electron.
 *
 * @param {object} o
 * @param {() => Promise<boolean>} o.isPortBound  does the port have a LISTEN owner again?
 * @param {(ms:number) => Promise<void>} o.sleep
 * @param {number} [o.graceMs]
 * @param {number} [o.pollMs]
 * @returns {Promise<"rebound" | "spawn">}
 */
async function waitForServiceRebind({ isPortBound, sleep, graceMs = SERVICE_REBIND_GRACE_MS, pollMs = 500 }) {
  const deadline = Date.now() + graceMs;
  for (;;) {
    if (await isPortBound()) return "rebound";
    if (Date.now() >= deadline) return "spawn";
    await sleep(pollMs);
  }
}

// A graceful gateway stop releases the LISTEN socket EARLY — the process keeps
// running to drain in-flight turns and flush session files, and it holds the
// exclusive gateway.lock flock for its full lifetime. So "port is free" does
// NOT mean "safe to spawn": a replacement started in that window is refused by
// the singleton lock and exits, then the incumbent exits — leaving no gateway
// at all. This helper waits (bounded) for the captured incumbent pids to
// actually die; the kernel releases the flock atomically on process exit.
// Pure/injectable for unit tests. 15s comfortably covers the backend's
// graceful-stop budget (drain ≤5s + flush) after the port has already cleared.
const INCUMBENT_EXIT_GRACE_MS = 15_000;

async function waitForProcessExit({ pids, isAlive, sleep, timeoutMs = INCUMBENT_EXIT_GRACE_MS, pollMs = 250 }) {
  const watched = (pids || []).filter((p) => Number.isInteger(p) && p > 0);
  if (!watched.length) return "exited";
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (!watched.some((p) => isAlive(p))) return "exited";
    if (Date.now() >= deadline) return "timeout";
    await sleep(pollMs);
  }
}

module.exports = {
  chooseRecoveryStrategy,
  classifyAdoptedGateway,
  GATEWAY_OWNERSHIP_STATES,
  waitForServiceRebind,
  waitForProcessExit,
  SERVICE_REBIND_GRACE_MS,
  INCUMBENT_EXIT_GRACE_MS,
};
