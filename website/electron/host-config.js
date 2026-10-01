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
 * Which port a legacy `remoteHost` should be keyed under.
 *
 * The entry carries a host and no port, so this key is what makes the crew
 * visible to every per-port lookup -- and it has to be the port this launch will
 * actually target, or the launch runs against a port whose remote host is
 * recorded somewhere else. An explicit `KIROCREW_PORT` outranks selection, so it
 * outranks this too, so an override that the launch will actually bind is taken
 * over the configured port. It still has to be a port this app can bind: an
 * unselectable override is refused before it reaches here, and keying a legacy
 * crew under one anyway is what let `remoteHosts["80"]` exist -- the entry whose
 * missed lookup makes a tunnelled crew read as local and leaks the internal
 * secret to it. Otherwise the answer is selection's own shared exit, which is
 * why this needs no store: the two readers reach the same literal.
 *
 * @param {object} input
 * @param {number} input.envPort  usable `KIROCREW_PORT`, or 0 when absent.
 * @param {number|null} input.configuredPort  port from `dashboard.url`, if any.
 * @returns {number}
 */
function legacyMigrationPort({ envPort, configuredPort }) {
  if (isSelectablePort(envPort)) return envPort;
  if (isSelectablePort(configuredPort)) return configuredPort;
  // The same literal selection's shared exit returns, so the key and the
  // target agree by construction rather than by two readers happening to
  // reach the same number.
  return DEFAULT_PORT;
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
 * Does one `remoteHosts` entry name a machine to reach? Entries holding only a
 * `defaultName` are window-title settings rather than a remote target. Both
 * readers in this module share it, so the whole-store scan and the per-port
 * question cannot drift apart.
 *
 * @param {unknown} config
 * @returns {boolean}
 */
function hasHost(config) {
  return typeof config?.host === "string" && config.host !== "";
}

/**
 * Is this port the local end of a link to a crew this app is configured to
 * reach?
 *
 * Selectability is a separate question: a port can name a crew and still be one
 * this app declines to target.
 *
 * @param {{get: (key: string) => unknown}} store
 * @param {unknown} port
 * @returns {boolean}
 */
function namesConfiguredCrew(store, port) {
  return hasHost(getRemoteHostConfig(store, port));
}

/**
 * Port of a remote crew this app is configured to reach, or null when none is
 * configured.
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
    if (!hasHost(config)) continue;
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

function setRemoteHostConfig(
  store,
  port,
  { host, binPath, remotePort, remotePath, manageTunnel } = {},
) {
  const hosts = store.get("remoteHosts") || {};
  if (host) {
    hosts[String(port)] = {
      ...(hosts[String(port)] || {}),
      host,
      binPath: binPath || DEFAULT_REMOTE_BIN,
      remotePort: remotePort || "",
      remotePath: remotePath || "",
      // Opt-in: only a crew reached over a plain SSH forward can have that
      // forward rebuilt by this app. A port carried by a VPN, a kubectl
      // port-forward or the user's own ssh must never be taken over.
      manageTunnel: manageTunnel === true,
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

/** Port a launch falls back to when no stored record names a better one. */
const DEFAULT_PORT = 5476;

/** How far above `DEFAULT_PORT` to look for a port no configured crew claims. */
const FALLBACK_SCAN_PORTS = 64;

/**
 * A local port a NEW gateway may bind: one no configured crew claims.
 *
 * This answers a different question from `selectLaunchPort`, and the difference
 * is the whole point. Selection answers "which port does this launch TARGET",
 * and it must name the crew's port when one is configured, or a live tunnel on
 * that port is never found. This answers "which port may we BIND", where a
 * crew's port is exactly the wrong answer: binding it makes the conflict
 * resolver read our own gateway as that crew.
 *
 * Only a process that has not fixed its port yet can act on this, which is why
 * its caller is the successor's port choice rather than this launch's.
 *
 * `DEFAULT_PORT` is the product default, which makes it both the likeliest free
 * port and the likeliest local end of a tunnel, so the search starts there and
 * walks upward over selectable ports until one is unclaimed.
 *
 * Returns `DEFAULT_PORT` when every port in the window is claimed: a defined
 * answer beats one that depends on how far the search happened to reach, and a
 * store naming sixty-four consecutive crews is a configuration to report rather
 * than one to out-guess.
 *
 * @param {{get: (key: string) => unknown}} store
 * @param {(message: string) => void} [log]
 * @returns {number}
 */
function fallbackLocalPort(store, log = () => {}) {
  const limit = DEFAULT_PORT + FALLBACK_SCAN_PORTS;
  for (let port = DEFAULT_PORT; port < limit; port += 1) {
    if (!isSelectablePort(port)) continue;
    if (namesConfiguredCrew(store, port)) continue;
    if (port !== DEFAULT_PORT) {
      log("A remote crew is configured on " + DEFAULT_PORT + "; using " + port + " instead");
    }
    return port;
  }
  log(
    "Every port from " + DEFAULT_PORT + " to " + (limit - 1) + " names a configured crew; "
    + "using " + DEFAULT_PORT + " and leaving the collision visible",
  );
  return DEFAULT_PORT;
}

/**
 * Which port this launch targets, given the dashboard record and the
 * local-gateway setting.
 *
 * Pure: every input arrives as an argument, so the whole decision is testable
 * without Electron.
 *
 * @param {object} input
 * @param {{get: (key: string) => unknown}} input.store
 * @param {number|null} input.configuredPort  port from `dashboard.url`, if any.
 * @param {boolean} input.localGatewayEnabled  the "Run a local gateway" setting.
 * @param {(message: string) => void} [input.log]
 * @returns {number}
 */
function selectLaunchPort({ store, configuredPort, localGatewayEnabled, log = () => {} }) {
  if (!localGatewayEnabled) {
    // A dashboard.url naming a port that has no remote host of its own records a
    // backend which will not run here: nothing binds it and there is no host to
    // mint a token from. A machine switched from local to remote-only keeps
    // exactly that record, so honouring it would rebuild the dead end the
    // opt-out is meant to avoid. A dashboard.url that DOES name a configured
    // crew still wins -- that is the user choosing between crews rather than a
    // leftover.
    if (
      configuredPort
      && isSelectablePort(configuredPort)
      && namesConfiguredCrew(store, configuredPort)
    ) {
      return configuredPort;
    }
    const remotePort = remoteHostPort(store);
    if (remotePort) {
      log("Local gateway is off; targeting the configured remote crew on port " + remotePort);
      return remotePort;
    }
    // No crew is configured, so there is no better target than the local
    // record: naming the port the user configured beats naming the default.
  }

  // Selectability gates this exit too. Both branches above refuse an
  // unselectable port, so honouring one here is the only way a stored 80
  // reaches a launch -- and 80 is exactly the target whose URL drops its port,
  // which is what makes a remote link read as local.
  if (configuredPort && isSelectablePort(configuredPort)) return configuredPort;
  // DEFAULT_PORT even when a crew is configured there. Stepping off it here is
  // what cost the adoption of a live tunnel on that port, and what sent the
  // first launch after a legacy migration to a port no crew entry named. Whether
  // this launch may BIND what it targets is decided where the answer is
  // knowable -- at bind time, against the port's actual holder.
  log("No usable dashboard.url port in the data home, targeting " + DEFAULT_PORT);
  return DEFAULT_PORT;
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
  // A hostless record (a `defaultName`-only title setting) names no crew, so the
  // only crew this function can still answer with is a host-bearing legacy one
  // under the empty key, and only for a URL whose port that record could have
  // been written under. Everything else names no crew: answer null rather than
  // hand back a hostless record a caller might read `?.host` off as one.
  if (portIsSchemeDefault(url)) {
    const legacy = getRemoteHostConfig(store, "");
    if (typeof legacy?.host === "string" && legacy.host !== "") return legacy;
  }
  return null;
}

/**
 * The window-title suffix stored for the gateway `url` names, honouring a name
 * an older version left under the empty key.
 *
 * The name is a cosmetic setting, so its fallback is wider than the crew
 * resolver's: a legacy record carrying only a `defaultName` (no host) still
 * supplied a title on a scheme-default port, and until that record is retired --
 * which only happens once the user restates the crew -- the resolved key holds
 * no name to read. So a name under the empty key is honoured for a scheme-default
 * URL whether or not the record also names a host, and ignored for every other
 * port, exactly as `getRemoteHostConfigForUrl` scopes its own fallback.
 *
 * @param {{get: (key: string) => unknown}} store
 * @param {string} url
 * @returns {string|undefined}
 */
function getRemoteHostDefaultNameForUrl(store, url) {
  const port = defaultedPort(url);
  if (port === "") return undefined;
  const resolved = getRemoteHostConfig(store, port);
  if (resolved?.defaultName) return resolved.defaultName;
  if (!portIsSchemeDefault(url)) return undefined;
  const legacy = getRemoteHostConfig(store, "");
  return legacy?.defaultName || undefined;
}

/**
 * Drop a host-bearing `remoteHosts[""]` record, carrying its window name onto
 * the record that supersedes it.
 *
 * Called once the user has DURABLY stated what the crew on a scheme-default port
 * is, so the superseded record does not outlive its replacement: without this a
 * user who CLEARS the crew still reads as remote forever, because the resolver
 * above keeps falling back to the record the clear was meant to remove.
 *
 * A `defaultName` on the legacy record is the window title the user pinned for
 * this crew under the empty key the same older versions wrote it to. Deleting
 * the record outright would erase that name -- a datum the user set and would
 * have to re-enter -- so it is migrated to the resolved-port entry first, and
 * only when that entry does not already carry a name of its own (a name stated
 * under the resolved key is the newer statement and wins).
 *
 * @param {{get: Function, set: Function}} store
 * @param {string|number} resolvedPort  the port the superseding record is keyed
 *   under. Required: retirement only ever runs on the save path, past the write
 *   that created that record, so a resolved-key entry always exists to migrate
 *   the pinned name onto.
 * @returns {boolean} whether a record was retired
 */
function retireLegacyEmptyPortHost(store, resolvedPort) {
  const hosts = store.get("remoteHosts") || {};
  const legacy = hosts[""];
  if (!legacy || typeof legacy.host !== "string" || legacy.host === "") return false;
  if (
    resolvedPort !== undefined
    && resolvedPort !== ""
    && typeof legacy.defaultName === "string"
    && legacy.defaultName !== ""
  ) {
    const key = String(resolvedPort);
    const target = hosts[key];
    // Carry the pinned name onto the resolved key, unless a name already stated
    // there wins. The save path always wrote the resolved-key record before this
    // runs, so `target` exists; a name stated under it is the newer statement.
    if (target && !target.defaultName) {
      hosts[key] = { ...target, defaultName: legacy.defaultName };
    }
  }
  delete hosts[""];
  store.set("remoteHosts", hosts);
  return true;
}

module.exports = {
  fallbackLocalPort,
  isSelectablePort,
  legacyMigrationPort,
  migrateRemoteHostConfig,
  remoteHostPort,
  getRemoteHostConfig,
  selectLaunchPort,
  getRemoteHostConfigForUrl,
  getRemoteHostDefaultNameForUrl,
  retireLegacyEmptyPortHost,
  setRemoteHostConfig,
};
