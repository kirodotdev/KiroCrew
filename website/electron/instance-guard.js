/**
 * instance-guard.js — cross-app gateway ownership guard.
 *
 * The nightly app ("KiroCrew Nightly.app") and the production app
 * ("KiroCrew.app", stable ⇄ insider via the update channel) are separate
 * INSTALLS sharing ONE bundle identifier (com.amazon.kiro.crew — Finder
 * separates installs by filename; Squirrel validates updates against the
 * host's designated requirement, which pins the id), ONE data home
 * (~/.kiro/crew), and ONE gateway port (5476). That makes the port a mutex:
 * only one KiroCrew-family gateway may run at a time. Electron's
 * requestSingleInstanceLock is keyed on userData (per productName), so it
 * cannot stop "KiroCrew Nightly" launching while "KiroCrew" runs — this
 * module covers that cross-app seam.
 *
 * Decision inputs: our own stamped version (identity family derives from it,
 * mirroring auto-update.js channelForVersion), the running gateway's
 * /api/health payload ({ok, app, version}), and who LOCALLY owns the port's
 * LISTEN socket. Legacy gateways (health without identity fields) and
 * source-run dev gateways keep today's reuse behavior — the guard only
 * interposes when BOTH sides are positively identified, their families differ,
 * AND the port is held by a local KiroCrew process. That last input is what
 * separates a local rival from a REMOTE gateway forwarded in over `ssh -L`,
 * which the health payload alone cannot distinguish.
 *
 * Pure logic only — the Electron shell (main.js) owns the dialog, the
 * quit-by-name AppleScript (bundle id is shared, so targeting must be by
 * app NAME), the port-owner probe, and the port-free wait.
 */

const { channelForVersion } = require("./auto-update");

// The endpoint that carries the gateway's identity fields ({app, version}).
// MUST be /api/health -- the shell's liveness probe uses /api/status, whose
// payload has NO `app` field; probing it would classify every gateway as
// "unidentified" and silently disable the takeover path (caught in review).
const HEALTH_IDENTITY_PATH = "/api/health";

// Identity families. stable + insider are ONE app (the production install)
// on two update lanes; nightly is a separate side-by-side install. Both
// share the bundle id, so the app NAME is the takeover-targeting handle.
const FAMILY_META = {
  // appName is the technical Finder/AppleScript target. displayName is the
  // product spelling shown in dialogs and status text.
  prod: { appName: "KiroCrew", displayName: "Kiro Crew" },
  nightly: { appName: "KiroCrew Nightly", displayName: "Kiro Crew Nightly" },
};

/**
 * Map a stamped version to its identity family.
 * @param {string} version
 * @returns {"prod"|"nightly"|null} null only for missing/unparseable versions
 */
function identityFamily(version) {
  const ch = channelForVersion(version);
  if (ch === "nightly") return "nightly";
  if (ch === "insider" || ch === "stable") return "prod";
  return null;
}

/**
 * Decide what to do about a gateway already listening on our (shared) port.
 *
 * @param {string} ownVersion  app.getVersion() of this shell
 * @param {{ok?:boolean, app?:string, version?:string}|null} remoteHealth
 *        parsed /api/health JSON, or null when unreachable/unparseable
 * @param {object} [opts]
 * @param {"kirocrew"|"foreign"|"none"|"unknown"} [opts.localOwner="unknown"]
 *        who LOCALLY owns the port's LISTEN socket (classifyPortOwner in
 *        gateway-stop.js). Defaults to "unknown" so a caller that forgets to
 *        pass it fails SAFE (reuse) rather than inheriting the old
 *        evict-on-payload-alone behavior.
 * @returns {{action:"reuse", reason:string} |
 *           {action:"takeover-prompt", otherFamily:"prod"|"nightly", otherVersion:string}}
 */
function decideGatewayAction(ownVersion, remoteHealth, { localOwner = "unknown" } = {}) {
  const own = identityFamily(ownVersion);
  // A shell we can't classify never evicts anyone.
  if (own === null) return { action: "reuse", reason: "unclassified-shell" };
  // Legacy (pre-identity health payload) or non-kirocrew responder on the
  // port: keep the historical reuse behavior. The guard turns on organically
  // once both sides carry the identity fields.
  if (!remoteHealth || remoteHealth.app !== "kirocrew" || !remoteHealth.version) {
    return { action: "reuse", reason: "unidentified-gateway" };
  }
  const remote = identityFamily(remoteHealth.version);
  if (remote === null || remote === own) {
    return { action: "reuse", reason: remote === own ? "same-family" : "dev-gateway" };
  }
  // Cross-family: the payload says "a rival KiroCrew owns the port". That is
  // necessary but NOT sufficient to evict, because the payload is identical
  // whether the responder is a local install or a REMOTE gateway forwarded
  // here by `ssh -L <port>:localhost:<port>`. Only a positively identified
  // local KiroCrew LISTEN owner authorises the eviction; a tunnel ("foreign",
  // owner is `ssh`), an unseen socket ("none"), or a failed probe ("unknown")
  // all reuse instead. Evicting a tunnel is doubly wrong: the AppleScript quit
  // targets a local app that is not running, and killing the port would tear
  // down the user's forward. This mirrors chooseRecoveryStrategy in
  // gateway-recovery.js, which already refuses to kill a gateway we did not
  // spawn — the boot path simply never got the same rule.
  if (localOwner !== "kirocrew") {
    return { action: "reuse", reason: `non-local-holder:${localOwner}` };
  }
  return { action: "takeover-prompt", otherFamily: remote, otherVersion: remoteHealth.version };
}

module.exports = { identityFamily, decideGatewayAction, FAMILY_META, HEALTH_IDENTITY_PATH };
