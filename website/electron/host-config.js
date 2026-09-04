// Per-port remote host configuration helpers.
// Split out from main.js so the migration and config logic can be unit-tested
// without spinning up Electron.

const { DEFAULT_REMOTE_BIN } = require("./remote-token");

// Migrate legacy single-host config (remoteHost + kirocrewBinPath) to the
// per-port remoteHosts map. Returns true if migration occurred.
function migrateRemoteHostConfig(store, port) {
  const legacy = store.get("remoteHost");
  if (legacy && Object.keys(store.get("remoteHosts") || {}).length === 0) {
    const bin = store.get("kirocrewBinPath") || DEFAULT_REMOTE_BIN;
    store.set("remoteHosts", { [port]: { host: legacy, binPath: bin } });
    store.delete("remoteHost");
    store.delete("kirocrewBinPath");
    return true;
  }
  return false;
}

/**
 * The one port this app must never SELECT as a launch target.
 *
 * The shell reaches its gateway over `http://localhost:<port>`, and
 * `new URL("http://localhost:80").port` is `""` -- the URL API strips a scheme's
 * default port. Every per-port lookup that derives its key from that URL then
 * misses: `isGatewayLocalForWindow` reads `remoteHosts[""]`, finds no host, and
 * reports a tunnelled crew as a gateway on this machine, after which the
 * host-presence heartbeat sends this machine's internal secret over the tunnel.
 *
 * The classifier is where that belongs fixed, and it is wrong on port 80
 * independently of this module. Until it is, selecting 80 from stored config is
 * a target this app cannot classify, so it is not offered.
 */
const UNSELECTABLE_PORT = 80;

/**
 * Whether a port may be chosen as this launch's target.
 *
 * @param {unknown} port
 * @returns {boolean}
 */
function isSelectablePort(port) {
  return Number.isInteger(port)
    && port >= 1
    && port <= 65535
    && port !== UNSELECTABLE_PORT;
}

/**
 * Port of a remote crew this app is configured to reach, or null when none is
 * configured. Entries holding only a `defaultName` are window-title settings
 * rather than a remote target, so they are skipped.
 *
 * Ports are compared numerically and the lowest wins, so the answer is stable
 * for a given store instead of depending on key insertion order.
 *
 * @param {{get: (key: string) => unknown}} store
 * @returns {number|null}
 */
function remoteHostPort(store) {
  const hosts = store.get("remoteHosts") || {};
  let lowest = null;
  for (const [key, config] of Object.entries(hosts)) {
    if (!config || typeof config.host !== "string" || config.host === "") continue;
    // Only a canonical decimal key names a port. parseInt alone reads
    // "5477-old" as 5477, which would dial a port whose own entry does not
    // exist -- so the launch would carry no host for the port it targeted.
    const port = Number.parseInt(key, 10);
    if (!isSelectablePort(port)) continue;
    if (String(port) !== key) continue;
    if (lowest === null || port < lowest) lowest = port;
  }
  return lowest;
}

function getRemoteHostConfig(store, port) {
  const hosts = store.get("remoteHosts") || {};
  return hosts[String(port)] || null;
}

function setRemoteHostConfig(store, port, { host, binPath, remotePort, remotePath } = {}) {
  const hosts = store.get("remoteHosts") || {};
  if (host) {
    hosts[String(port)] = {
      ...(hosts[String(port)] || {}),
      host,
      binPath: binPath || DEFAULT_REMOTE_BIN,
      remotePort: remotePort || "",
      remotePath: remotePath || "",
    };
  } else {
    // Clear SSH fields but preserve defaultName
    const existing = hosts[String(port)];
    if (existing?.defaultName) {
      hosts[String(port)] = { defaultName: existing.defaultName };
    } else {
      delete hosts[String(port)];
    }
  }
  store.set("remoteHosts", hosts);
}

module.exports = {
  isSelectablePort,
  migrateRemoteHostConfig,
  remoteHostPort,
  getRemoteHostConfig,
  setRemoteHostConfig,
};
