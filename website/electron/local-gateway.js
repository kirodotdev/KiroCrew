"use strict";
//
// Whether this app manages a gateway of its own, extracted from main.js so the
// decision is unit-testable without Electron (mirrors gateway-wait.js).
//
// The app works as a pure client: with a remote host configured and a tunnel or
// externally-started gateway answering on the port, nothing has to run locally.
// Starting one anyway costs a Python backend plus a kiro-cli process per session
// on a machine that is only drawing the UI, which is why this is a choice rather
// than a consequence of launch order.

/** electron-store key holding the user's choice. */
const LOCAL_GATEWAY_KEY = "runLocalGateway";

/**
 * The user's choice, defaulting to ON: an install that never opens Settings
 * keeps managing its own gateway, so turning this off is always deliberate.
 *
 * Only an explicit `false` disables it. A store that has never held the key
 * reads as undefined, and a truthy-but-not-true value (a hand-edited config)
 * is not a request to stop starting the gateway.
 *
 * @param {{get: (key: string) => unknown}} store
 * @returns {boolean}
 */
function isLocalGatewayEnabled(store) {
  return store.get(LOCAL_GATEWAY_KEY) !== false;
}

/**
 * Record the user's choice. Coerced to a real boolean so a renderer that sends
 * a truthy string cannot write a value `isLocalGatewayEnabled` would then read
 * back as enabled-by-accident.
 *
 * @param {{set: (key: string, value: unknown) => void}} store
 * @param {unknown} enabled
 * @returns {boolean} the value written
 */
function setLocalGatewayEnabled(store, enabled) {
  const value = !!enabled;
  store.set(LOCAL_GATEWAY_KEY, value);
  return value;
}

/**
 * Which story the gateway error dialog should tell.
 *
 * The client-only case is checked FIRST on purpose. Nothing was launched in that
 * state, but the launch log persists across launches, so an "address already in
 * use" line left by an earlier run would otherwise classify a silent port as a
 * conflict and offer to force-stop a holder that does not exist.
 *
 * The incomplete-bundle case is likewise not a crash: the spawn was refused
 * because the installer is still writing the backend, so a plain retry resolves
 * it and "failed to start" would send the user hunting a defect that is not there.
 *
 * @param {object} o
 * @param {boolean} o.failedToStart      the wait rejected with kind === 'failed'
 * @param {{disabled?: boolean, incompleteBundle?: boolean}|null} [o.failure]
 *   the failure record it carried
 * @param {boolean} [o.isOwnPort]        this window points at our own gateway port
 * @param {boolean} [o.portInUseInLog]   the launch log tail reports a bound port
 * @returns {"client-only"|"installing"|"port-conflict"|"failed"|"unreachable"}
 */
function classifyStartFailure({
  failedToStart,
  failure = null,
  isOwnPort = false,
  portInUseInLog = false,
} = {}) {
  if (failedToStart && failure && failure.disabled) return "client-only";
  if (failedToStart && failure && failure.incompleteBundle) return "installing";
  if (failedToStart && isOwnPort && portInUseInLog) return "port-conflict";
  if (failedToStart) return "failed";
  return "unreachable";
}

module.exports = {
  LOCAL_GATEWAY_KEY,
  isLocalGatewayEnabled,
  setLocalGatewayEnabled,
  classifyStartFailure,
};
