const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  isSelectablePort,
  migrateRemoteHostConfig,
  remoteHostPort,
  getRemoteHostConfig,
  setRemoteHostConfig,
} = require("../host-config");

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

// #6138: with "Run a local gateway" off, the launch has to aim at the remote
// crew the user configured instead of the local default nothing will bind.
describe("remoteHostPort", () => {
  it("returns null when nothing is configured", () => {
    assert.equal(remoteHostPort(mockStore()), null);
    assert.equal(remoteHostPort(mockStore({ remoteHosts: {} })), null);
  });

  it("returns the port of the only configured remote host", () => {
    const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
    assert.equal(remoteHostPort(store), 7778);
  });

  it("picks the lowest port, whatever order the keys were written in", () => {
    const store = mockStore({
      remoteHosts: {
        "9001": { host: "c.example.com" },
        "5477": { host: "a.example.com" },
        "7778": { host: "b.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 5477);
  });

  it("skips entries that carry only a window name", () => {
    const store = mockStore({
      remoteHosts: {
        "5477": { defaultName: "Laptop" },
        "7778": { host: "a.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 7778);
  });

  it("skips an entry whose host was cleared", () => {
    const store = mockStore({
      remoteHosts: {
        "5477": { host: "", binPath: "~/.local/bin/kirocrew" },
        "7778": { host: "a.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 7778);
  });

  it("skips keys that are not usable port numbers", () => {
    const store = mockStore({
      remoteHosts: {
        "0": { host: "a.example.com" },
        "70000": { host: "b.example.com" },
        "not-a-port": { host: "c.example.com" },
        "7778": { host: "d.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 7778);
  });

  it("skips a key that only STARTS with digits", () => {
    // parseInt would read "5477-old" as 5477 and dial a port whose own entry
    // does not exist, so the launch would carry no host for that port.
    const store = mockStore({
      remoteHosts: {
        "5477-old": { host: "a.example.com" },
        "7778": { host: "b.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 7778);
  });

  it("skips non-canonical spellings of a port number", () => {
    for (const key of ["05477", " 5477", "5477 ", "+5477", "5477.0", "0x1565"]) {
      const store = mockStore({ remoteHosts: { [key]: { host: "a.example.com" } } });
      assert.equal(remoteHostPort(store), null, key);
    }
  });

  it("returns null when every entry is unusable", () => {
    const store = mockStore({
      remoteHosts: { "5477": { defaultName: "Laptop" }, "70000": { host: "a.example.com" } },
    });
    assert.equal(remoteHostPort(store), null);
  });

  it("tolerates a malformed entry instead of throwing", () => {
    const store = mockStore({ remoteHosts: { "5477": null, "7778": { host: "a.example.com" } } });
    assert.equal(remoteHostPort(store), 7778);
  });

  // Security: `new URL("http://localhost:80").port` is "", so a target of 80
  // defeats every per-port lookup keyed off that URL -- including the
  // host-presence classifier, which would then read a tunnelled crew as local
  // and send this machine's internal secret over the tunnel.
  it("never selects port 80, even as the only configured crew", () => {
    const store = mockStore({ remoteHosts: { "80": { host: "a.example.com" } } });
    assert.equal(remoteHostPort(store), null);
  });

  it("skips port 80 and takes the next selectable crew", () => {
    const store = mockStore({
      remoteHosts: {
        "80": { host: "a.example.com" },
        "7778": { host: "b.example.com" },
      },
    });
    assert.equal(remoteHostPort(store), 7778);
  });
});

describe("isSelectablePort", () => {
  it("refuses port 80 and accepts its neighbours", () => {
    assert.equal(isSelectablePort(80), false);
    assert.equal(isSelectablePort(79), true);
    assert.equal(isSelectablePort(81), true);
  });

  it("refuses anything that is not a port number in range", () => {
    for (const value of [0, -1, 65536, 1.5, NaN, null, undefined, "5476"]) {
      assert.equal(isSelectablePort(value), false, String(value));
    }
  });

  it("accepts the ordinary gateway ports", () => {
    for (const value of [1, 443, 5476, 7778, 65535]) {
      assert.equal(isSelectablePort(value), true, String(value));
    }
  });
});
