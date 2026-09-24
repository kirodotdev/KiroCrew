// Configuring a remote crew from the pre-dashboard gateway-failure dialog.
//
// When the offer is coherent, and the validated store write it performs, live
// here so both are unit-testable without an Electron runtime. The dialog markup
// and the input window stay in the supervisor.

const { DEFAULT_REMOTE_BIN } = require("./remote-token");
const { isSelectablePort, setRemoteHostConfig } = require("./host-config");
const { validateRemoteSettings } = require("./validation");

const MISSING_HOST_ERROR = "Enter the host your crew runs on.";

function unselectablePortError(port) {
  return `Port ${port} cannot address a remote crew. `
    + "Launch on another port, then add the crew.";
}

/**
 * Which remote-crew action the gateway-failure dialog can offer, or null when
 * none of them fits the state.
 *
 * A client-only launch is the whole condition: the local gateway is off, so the
 * only thing that can answer this port is a crew on another machine. Whether one
 * is stored decides WHICH action this is, not whether there is one. A stored
 * address that nothing answers is as likely to be a typo as it is to be a crew
 * that is down, and withholding the form there would leave that typo reachable
 * only by editing the store by hand -- the same dead end, one mistake deep.
 *
 * Whether the dialog can retry at all is the dialog's own condition, applied
 * where it renders the action.
 *
 * @param {{localGatewayOff?: unknown, remoteHost?: unknown}} state
 * @returns {"add"|"edit"|null}
 */
function remoteCrewAction({ localGatewayOff = false, remoteHost = "" } = {}) {
  if (!localGatewayOff) return null;
  return String(remoteHost || "").trim() === "" ? "add" : "edit";
}

/**
 * Values the input window opens with: what the user typed when a save was
 * refused, otherwise what the store holds for this port. A refused save writes
 * nothing, so the store cannot supply them.
 *
 * @param {{host?: unknown, binPath?: unknown, remotePort?: unknown, remotePath?: unknown}} source
 * @returns {{host: string, binPath: string, remotePort: string, remotePath: string}}
 */
function remoteCrewDraft(source = {}) {
  return {
    host: String(source.host || ""),
    binPath: String(source.binPath || ""),
    remotePort: String(source.remotePort || ""),
    remotePath: String(source.remotePath || ""),
  };
}

/**
 * Fields the input window reports through its document title, or null when it
 * was dismissed without saving. Only a JSON object payload counts as a save.
 *
 * @param {unknown} title
 * @returns {{host: string, binPath: string, remotePort: string, remotePath: string}|null}
 */
function parseRemoteCrewFields(title) {
  const payload = String(title || "");
  if (!payload.startsWith("{")) return null;
  let parsed;
  try { parsed = JSON.parse(payload); }
  catch { return null; }
  if (!parsed || typeof parsed !== "object") return null;
  return {
    host: String(parsed.host || "").trim(),
    binPath: String(parsed.binPath || "").trim(),
    remotePort: String(parsed.remotePort || "").trim(),
    remotePath: String(parsed.remotePath || "").trim(),
  };
}

/**
 * Store the collected fields as the remote crew for `port`.
 *
 * Nothing is written unless the port can carry a crew and every field
 * validates, so a rejected form leaves the launch untouched and the dialog can
 * ask again.
 *
 * @param {{get: (key: string) => unknown, set: (key: string, value: unknown) => void}} store
 * @param {number|string} port
 * @param {{host?: unknown, binPath?: unknown, remotePort?: unknown, remotePath?: unknown}} fields
 * @returns {{saved: boolean, error: string|null}}
 */
function saveRemoteCrewConfig(store, port, fields = {}) {
  const host = String(fields.host || "").trim();
  if (!host) return { saved: false, error: MISSING_HOST_ERROR };
  // A port the per-port lookup cannot key on reads a tunnelled crew as a
  // gateway on this machine, after which the host-presence heartbeat sends this
  // machine's secret over the tunnel. Refuse the write rather than create it.
  if (!isSelectablePort(Number(port))) {
    return { saved: false, error: unselectablePortError(port) };
  }
  const binPath = String(fields.binPath || "").trim() || DEFAULT_REMOTE_BIN;
  const remotePort = String(fields.remotePort || "").trim();
  const remotePath = String(fields.remotePath || "").trim();
  const error = validateRemoteSettings(host, binPath, remotePort, remotePath);
  if (error) return { saved: false, error };
  setRemoteHostConfig(store, port, { host, binPath, remotePort, remotePath });
  return { saved: true, error: null };
}

module.exports = {
  MISSING_HOST_ERROR,
  parseRemoteCrewFields,
  remoteCrewAction,
  remoteCrewDraft,
  saveRemoteCrewConfig,
};
