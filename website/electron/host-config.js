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

module.exports = { migrateRemoteHostConfig, getRemoteHostConfig, setRemoteHostConfig };
