// Per-port remote host configuration helpers.
// Split out from main.js so the migration and config logic can be unit-tested
// without spinning up Electron.

const { DEFAULT_REMOTE_BIN } = require("./remote-token");
const { defaultedPort, portIsSchemeDefault } = require("./gateway-auth-hint");

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
 * default port. A per-port lookup keyed off that raw property therefore misses,
 * which is why every such lookup normalizes through `defaultedPort` first.
 *
 * The port stays unselectable because the erasure is a property of the URL API
 * rather than of any one call site: a target whose port does not survive the
 * round trip through the URL the shell builds is one more place for a future
 * lookup to read the empty key, and the consequence there is a tunnelled crew
 * classified as a gateway on this machine. So 80 is not offered.
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

/**
 * The remote-host entry for the gateway `url` names, honouring a record left
 * under the empty key by an older version.
 *
 * `URL.port` is "" for a scheme default, and a version that keyed this map off
 * that raw property wrote its crew under `remoteHosts[""]`. Such a record names
 * a port that cannot be recovered from the record itself, so it is honoured for
 * exactly the shape of URL that could have produced it -- one whose port is
 * its scheme's default -- and ignored for every other. Reading "no crew" there would
 * classify a tunnelled crew as a gateway on this machine, which is the answer
 * that puts this machine's internal secret through the tunnel.
 *
 * Only a host-bearing legacy record counts. An entry holding just a
 * `defaultName` is a window-title setting, and the same older versions wrote
 * those under the empty key too.
 *
 * @param {{get: (key: string) => unknown}} store
 * @param {string} url
 * @returns {object|null}
 */
function getRemoteHostConfigForUrl(store, url) {
  const port = defaultedPort(url);
  // An unparseable URL names no port, and `defaultedPort` says so with "". Left
  // unguarded that would read `remoteHosts[""]` through the ordinary path, which
  // is the one key this function must reach only by the deliberate route below.
  if (port === "") return null;
  const resolved = getRemoteHostConfig(store, port);
  if (resolved?.host) return resolved;
  if (!portIsSchemeDefault(url)) return resolved;
  const legacy = getRemoteHostConfig(store, "");
  if (typeof legacy?.host === "string" && legacy.host !== "") return legacy;
  return resolved;
}

/**
 * Drop a host-bearing `remoteHosts[""]` record.
 *
 * Called once the user has DURABLY stated what the crew on a scheme-default port
 * is, so the superseded record does not outlive its replacement: without this a
 * user who CLEARS the crew still reads as remote forever, because the resolver
 * above keeps falling back to the record the clear was meant to remove.
 *
 * The whole record goes, including any `defaultName` on it. Nothing reads a
 * window name under that key -- every name lookup is keyed by a resolved port --
 * so preserving one would keep a field no code path can reach.
 *
 * @param {{get: Function, set: Function}} store
 * @returns {boolean} whether a record was retired
 */
function retireLegacyEmptyPortHost(store) {
  const hosts = store.get("remoteHosts") || {};
  const legacy = hosts[""];
  if (!legacy || typeof legacy.host !== "string" || legacy.host === "") return false;
  delete hosts[""];
  store.set("remoteHosts", hosts);
  return true;
}

module.exports = {
  isSelectablePort,
  migrateRemoteHostConfig,
  remoteHostPort,
  getRemoteHostConfig,
  getRemoteHostConfigForUrl,
  retireLegacyEmptyPortHost,
  setRemoteHostConfig,
};
