const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  isSelectablePort,
  migrateRemoteHostConfig,
  remoteHostPort,
  getRemoteHostConfig,
  getRemoteHostConfigForUrl,
  retireLegacyEmptyPortHost,
  setRemoteHostConfig,
} = require("../host-config");
const { saveRemoteCrewConfig } = require("../remote-crew-setup");

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

describe("getRemoteHostConfigForUrl", () => {
  it("resolves the scheme default and prefers a record under that key", () => {
    const store = mockStore({ remoteHosts: { "80": { host: "eighty.example.test" }, "443": { host: "four43.example.test" } } });
    assert.equal(getRemoteHostConfigForUrl(store, "http://localhost/")?.host, "eighty.example.test");
    assert.equal(getRemoteHostConfigForUrl(store, "http://localhost:80/")?.host, "eighty.example.test");
    assert.equal(getRemoteHostConfigForUrl(store, "https://localhost/")?.host, "four43.example.test");
  });

  it("falls back to a host under the empty key only for a scheme-default port", () => {
    // That record can only have come from a URL whose port the URL API erased,
    // so it is honoured for exactly that shape and for nothing else. Answering
    // "no crew" would classify a tunnelled crew as this machine's own gateway.
    const store = mockStore({ remoteHosts: { "": { host: "legacy.example.test" } } });
    assert.equal(getRemoteHostConfigForUrl(store, "http://localhost/")?.host, "legacy.example.test");
    assert.equal(getRemoteHostConfigForUrl(store, "https://localhost/")?.host, "legacy.example.test");
    assert.equal(getRemoteHostConfigForUrl(store, "http://localhost:80/")?.host, "legacy.example.test");
    assert.equal(getRemoteHostConfigForUrl(store, "http://localhost:5476/"), null);
    assert.equal(getRemoteHostConfigForUrl(store, "http://localhost:7778/"), null);
  });

  it("does not read a defaultName-only empty-key entry as a crew", () => {
    // Returns null rather than the entry. Asserting only that `.host` is absent
    // would pass either way, and handing a hostless entry back would let a future
    // consumer that checks truthiness read "a crew is configured here" -- the
    // same shape of latent misread this whole normalization exists to remove.
    const store = mockStore({ remoteHosts: { "": { defaultName: "Pinned" } } });
    assert.equal(getRemoteHostConfigForUrl(store, "http://localhost/"), null);
  });

  it("lets a resolved-key record win over the legacy one", () => {
    const store = mockStore({ remoteHosts: { "": { host: "legacy.example.test" }, "80": { host: "current.example.test" } } });
    assert.equal(getRemoteHostConfigForUrl(store, "http://localhost/")?.host, "current.example.test");
  });

  it("answers null for an unparseable URL rather than reaching the empty key", () => {
    const store = mockStore({ remoteHosts: { "": { host: "legacy.example.test" } } });
    assert.equal(getRemoteHostConfigForUrl(store, "not a url"), null);
  });
});

describe("retireLegacyEmptyPortHost", () => {
  it("removes the whole empty-key record, window name included", () => {
    // Nothing reads a window name under that key -- every name lookup is keyed by
    // a resolved port -- so preserving one would keep a field no path can reach.
    const store = mockStore({ remoteHosts: { "": { host: "legacy.example.test" }, "80": { host: "current.example.test" } } });
    assert.equal(retireLegacyEmptyPortHost(store), true);
    assert.deepEqual(store._data.remoteHosts, { "80": { host: "current.example.test" } });

    const named = mockStore({ remoteHosts: { "": { host: "legacy.example.test", defaultName: "Pinned" } } });
    assert.equal(retireLegacyEmptyPortHost(named), true);
    assert.deepEqual(named._data.remoteHosts, {});
  });

  it("keeps the record when the replacement write is refused", () => {
    // The sequence that makes the ORDER load-bearing. A legacy record on an http
    // window resolving to :80, the user re-states the crew, and the save is
    // refused because 80 is unselectable. Retiring before that write would leave
    // no record at all, so the crew would read as this machine's own gateway --
    // the exposure this change exists to close -- with no way back, since no
    // later save on that port can ever succeed either.
    const store = mockStore({ remoteHosts: { "": { host: "legacy.example.test" } } });
    const fields = { host: "legacy.example.test", binPath: "~/.local/bin/kirocrew", remotePort: "", remotePath: "" };

    const { saved } = saveRemoteCrewConfig(store, "80", fields);
    assert.equal(saved, false, "80 is unselectable, so the replacement cannot be written");
    // Retirement is gated on that write, so it has not run.
    assert.equal(
      getRemoteHostConfigForUrl(store, "http://localhost/")?.host,
      "legacy.example.test",
      "the only record marking this crew remote must survive a refused save",
    );

    // On :443 the same statement IS durable, so retirement is correct there.
    const ok = mockStore({ remoteHosts: { "": { host: "legacy.example.test" } } });
    assert.equal(saveRemoteCrewConfig(ok, "443", fields).saved, true);
    retireLegacyEmptyPortHost(ok);
    assert.deepEqual(Object.keys(ok._data.remoteHosts), ["443"]);
    assert.equal(getRemoteHostConfigForUrl(ok, "https://localhost/")?.host, "legacy.example.test");
  });

  it("is a no-op when there is nothing to retire", () => {
    const empty = mockStore({ remoteHosts: {} });
    assert.equal(retireLegacyEmptyPortHost(empty), false);
    const named = mockStore({ remoteHosts: { "": { defaultName: "Pinned" } } });
    assert.equal(retireLegacyEmptyPortHost(named), false);
    assert.deepEqual(named._data.remoteHosts, { "": { defaultName: "Pinned" } });
    const blank = mockStore({ remoteHosts: { "": { host: "" } } });
    assert.equal(retireLegacyEmptyPortHost(blank), false);
  });

  it("closes the clear loop: after a clear the URL no longer reads as remote", () => {
    // The sequence that made this necessary. A legacy record on :80, the user
    // clears the crew, and without retirement the resolver keeps falling back to
    // the record the clear was meant to remove -- remote for ever.
    const store = mockStore({ remoteHosts: { "": { host: "legacy.example.test" } } });
    assert.equal(getRemoteHostConfigForUrl(store, "http://localhost/")?.host, "legacy.example.test");
    retireLegacyEmptyPortHost(store);
    setRemoteHostConfig(store, "80", {});
    assert.equal(getRemoteHostConfigForUrl(store, "http://localhost/"), null);
  });
});
