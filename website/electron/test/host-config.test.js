const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { migrateRemoteHostConfig, getRemoteHostConfig, setRemoteHostConfig } = require("../host-config");

// Minimal mock of electron-store (get/set/delete on a plain object)
function mockStore(initial = {}) {
  const data = { ...initial };
  return {
    get: (k) => data[k],
    set: (k, v) => { data[k] = v; },
    delete: (k) => { delete data[k]; },
    _data: data,
  };
}

describe("migrateRemoteHostConfig", () => {
  it("migrates legacy remoteHost to remoteHosts[port]", () => {
    const store = mockStore({ remoteHost: "myhost.corp.example.com", kirocrewBinPath: "~/.local/bin/kirocrew", remoteHosts: {} });
    const result = migrateRemoteHostConfig(store, 7778);
    assert.equal(result, true);
    assert.deepEqual(store._data.remoteHosts, { 7778: { host: "myhost.corp.example.com", binPath: "~/.local/bin/kirocrew" } });
    assert.equal(store._data.remoteHost, undefined);
    assert.equal(store._data.kirocrewBinPath, undefined);
  });

  it("uses DEFAULT_REMOTE_BIN when kirocrewBinPath is empty", () => {
    const store = mockStore({ remoteHost: "host.com", kirocrewBinPath: "", remoteHosts: {} });
    migrateRemoteHostConfig(store, 7777);
    assert.equal(store._data.remoteHosts[7777].binPath, "~/.local/bin/kirocrew");
  });

  it("does not migrate when remoteHosts already has entries", () => {
    const store = mockStore({ remoteHost: "old.com", remoteHosts: { 7777: { host: "existing.com" } } });
    const result = migrateRemoteHostConfig(store, 7777);
    assert.equal(result, false);
    assert.equal(store._data.remoteHost, "old.com"); // not deleted
  });

  it("does not migrate when remoteHost is empty", () => {
    const store = mockStore({ remoteHost: "", remoteHosts: {} });
    const result = migrateRemoteHostConfig(store, 7777);
    assert.equal(result, false);
  });
});

describe("getRemoteHostConfig", () => {
  it("returns config for a known port", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "a.com", binPath: "/bin/m" } } });
    assert.deepEqual(getRemoteHostConfig(store, 7778), { host: "a.com", binPath: "/bin/m" });
  });

  it("returns null for unknown port", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "a.com" } } });
    assert.equal(getRemoteHostConfig(store, 9999), null);
  });

  it("coerces numeric port to string for lookup", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "a.com" } } });
    assert.ok(getRemoteHostConfig(store, 7778));
  });
});

describe("setRemoteHostConfig", () => {
  it("sets config for a new port", () => {
    const store = mockStore({ remoteHosts: {} });
    setRemoteHostConfig(store, 7778, { host: "new.com", binPath: "~/bin/m" });
    assert.equal(store._data.remoteHosts["7778"].host, "new.com");
    assert.equal(store._data.remoteHosts["7778"].binPath, "~/bin/m");
  });

  it("preserves defaultName when clearing host", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "old.com", binPath: "/b", defaultName: "Cloud" } } });
    setRemoteHostConfig(store, 7778, { host: "" });
    assert.deepEqual(store._data.remoteHosts["7778"], { defaultName: "Cloud" });
  });

  it("deletes port entry entirely when clearing with no defaultName", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "old.com", binPath: "/b" } } });
    setRemoteHostConfig(store, 7778, { host: "" });
    assert.equal(store._data.remoteHosts["7778"], undefined);
  });

  it("preserves existing fields (like defaultName) when setting host", () => {
    const store = mockStore({ remoteHosts: { "7778": { defaultName: "Cloud" } } });
    setRemoteHostConfig(store, 7778, { host: "x.com", binPath: "/b" });
    assert.equal(store._data.remoteHosts["7778"].host, "x.com");
    assert.equal(store._data.remoteHosts["7778"].defaultName, "Cloud");
  });

  it("defaults binPath to DEFAULT_REMOTE_BIN when omitted", () => {
    const store = mockStore({ remoteHosts: {} });
    setRemoteHostConfig(store, 7777, { host: "h.com" });
    assert.equal(store._data.remoteHosts["7777"].binPath, "~/.local/bin/kirocrew");
  });
});
