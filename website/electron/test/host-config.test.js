const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  fallbackLocalPort,
  isSelectablePort,
  legacyMigrationPort,
  migrateRemoteHostConfig,
  remoteHostPort,
  getRemoteHostConfig,
  selectLaunchPort,
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

describe("legacyMigrationPort", () => {
  // A legacy entry names a host and no port, so this key has to be the port the
  // launch will actually bind. Keying it anywhere else leaves the launch running
  // against a port whose remote host is recorded somewhere else.
  it("takes an explicit KIROCREW_PORT over the configured port", () => {
    // The override short-circuits selection, so that value is bound whether or
    // not this module would have picked it.
    const store = mockStore({ remoteHosts: {} });
    assert.equal(legacyMigrationPort({ store, envPort: 9100, configuredPort: 7778 }), 9100);
  });

  it("refuses to key a legacy crew under an unselectable override", () => {
    // remoteHosts["80"] is the entry whose missed lookup makes a tunnelled crew
    // read as local, because the shell's own URL drops the :80. The launch cannot
    // bind 80 either, so keying the crew there would also break this helper's one
    // invariant: the key is the port the launch targets.
    const store = mockStore({ remoteHosts: {} });
    assert.notEqual(legacyMigrationPort({ store, envPort: 80, configuredPort: 7778 }), 80);
    assert.equal(legacyMigrationPort({ store, envPort: 80, configuredPort: 7778 }), 7778);
  });

  it("falls back to the configured port when no override is set", () => {
    const store = mockStore({ remoteHosts: {} });
    assert.equal(legacyMigrationPort({ store, envPort: 0, configuredPort: 7778 }), 7778);
  });

  it("refuses an unselectable configured port and uses the fallback", () => {
    const store = mockStore({ remoteHosts: {} });
    assert.equal(legacyMigrationPort({ store, envPort: 0, configuredPort: 80 }), 5476);
    assert.equal(legacyMigrationPort({ store, envPort: 0, configuredPort: null }), 5476);
  });

  it("agrees with selection on the store it is given", () => {
    // What this helper can promise is about its OWN inputs: read the same
    // pre-migration store selection would read, and the two answers match. It
    // is deliberately not a claim about the launch, which reads the store again
    // after the migration has written to it -- see the next test.
    for (const configured of [7778, 5476, 9999, 80]) {
      const store = mockStore({ remoteHosts: {} });
      const selected = selectLaunchPort({
        store: mockStore({ remoteHosts: {} }),
        configuredPort: configured,
        localGatewayEnabled: true,
      });
      assert.equal(
        legacyMigrationPort({ store, envPort: 0, configuredPort: configured }),
        selected,
        String(configured),
      );
    }
  });

  it("keys the legacy crew under the port the launch then targets", () => {
    // The real sequence on ONE store: key the legacy crew, write it, then let
    // the launch select. The write is what makes that port name a configured
    // crew, so this is the only test that can see whether selection still agrees
    // with the key AFTER the store has changed under it -- the previous test
    // reads a pre-migration store and cannot.
    //
    // Agreement is the helper's one invariant: a legacy entry names a host and
    // no port, so the key has to be the port the launch reaches the crew on. It
    // held for a while by luck and then stopped: selection used to step off a
    // port that named a crew, which meant the migration's own write pushed the
    // launch away from the entry it had just created, in all three shapes. What
    // restores it is selection targeting the crew's port, with the refusal to
    // BIND that port moved to bind time, where the port's actual holder is known.
    const cases = [
      { configured: null, expected: 5476 },
      { configured: 7778, expected: 7778 },
      { configured: 5476, expected: 5476 },
    ];
    for (const { configured, expected } of cases) {
      const store = mockStore({ remoteHost: "legacy.example.com", remoteHosts: {} });
      const keyed = legacyMigrationPort({ store, envPort: 0, configuredPort: configured });
      assert.equal(keyed, expected, `keyed port for configured=${configured}`);
      assert.equal(migrateRemoteHostConfig(store, keyed), true, "the migration writes");
      const targetedNow = selectLaunchPort({
        store,
        configuredPort: configured,
        localGatewayEnabled: true,
      });
      assert.equal(
        targetedNow,
        keyed,
        `the launch targets the port the crew was keyed under (configured=${configured})`,
      );
      assert.equal(
        getRemoteHostConfig(store, targetedNow)?.host,
        "legacy.example.com",
        "so the crew entry names the port the launch targets",
      );
    }
  });

  it("keys it there even when selection is asked again with the setting off", () => {
    // Same store, both settings. A remote-only machine is exactly where a legacy
    // entry is found, so the key must name the crew's port on that path too --
    // and that path reaches it by a different branch, the one that honours a
    // record naming a configured crew.
    for (const configured of [null, 7778]) {
      const store = mockStore({ remoteHost: "legacy.example.com", remoteHosts: {} });
      const keyed = legacyMigrationPort({ store, envPort: 0, configuredPort: configured });
      migrateRemoteHostConfig(store, keyed);
      assert.equal(
        selectLaunchPort({ store, configuredPort: configured, localGatewayEnabled: false }),
        keyed,
        `configured=${configured}`,
      );
    }
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

describe("fallbackLocalPort", () => {
  // Where the crew-skipping walk lives now. It moved off selection because the
  // two answer different questions: selection names the port a launch TARGETS,
  // which must be the crew's when one is configured, and this names a port a NEW
  // gateway may BIND, where a crew's port is the wrong answer -- binding it makes
  // every per-port lookup read our own gateway as that crew. Only a process that
  // has not fixed its port can act on the answer, so the caller is the
  // successor's port choice.
  function fallback(store) {
    const logged = [];
    const port = fallbackLocalPort(store, (message) => logged.push(message));
    return { port, logged: logged.join("\n") };
  }

  it("returns the default when no crew claims it", () => {
    // The control the walk is measured against: without it, a walk test cannot
    // tell "walks when claimed" from "always walks".
    const store = mockStore({ remoteHosts: { "9999": { host: "a.example.com" } } });
    const { port, logged } = fallback(store);
    assert.equal(port, 5476);
    assert.equal(logged, "", "nothing to announce when the default is free");
  });

  it("walks past a crew-claimed default to the next free port", () => {
    const store = mockStore({ remoteHosts: { "5476": { host: "b.example.com" } } });
    const { port, logged } = fallback(store);
    assert.equal(port, 5477);
    assert.match(logged, /remote crew is configured on 5476/);
    assert.match(logged, /using 5477/);
  });

  it("skips a run of crew-claimed ports to reach a free one", () => {
    const remoteHosts = {};
    for (let p = 5476; p <= 5479; p += 1) remoteHosts[String(p)] = { host: "c.example.com" };
    const store = mockStore({ remoteHosts });
    assert.equal(fallback(store).port, 5480);
  });

  it("keeps the default and leaves the collision visible when the window is full", () => {
    // Sixty-four consecutive crews is a configuration to report, not one to
    // out-guess: a defined answer beats one that depends on the search width.
    const remoteHosts = {};
    for (let p = 5476; p < 5476 + 64; p += 1) remoteHosts[String(p)] = { host: "d.example.com" };
    const store = mockStore({ remoteHosts });
    const { port, logged } = fallback(store);
    assert.equal(port, 5476);
    assert.match(logged, /names a configured crew/);
  });

  it("never answers with a port a launch could not bind", () => {
    // The walk crosses no unselectable port in this window, but the guard is
    // what keeps that true if the window or the rule ever moves: an unbindable
    // answer here would be pinned into the successor's environment.
    for (const claimed of [[], ["5476"], ["5476", "5477", "5478"]]) {
      const remoteHosts = {};
      for (const key of claimed) remoteHosts[key] = { host: "e.example.com" };
      assert.equal(isSelectablePort(fallbackLocalPort(mockStore({ remoteHosts }))), true);
    }
  });
});

describe("selectLaunchPort", () => {
  function select(store, { configuredPort = null, localGatewayEnabled = true } = {}) {
    const logged = [];
    const port = selectLaunchPort({
      store,
      configuredPort,
      localGatewayEnabled,
      log: (message) => logged.push(message),
    });
    return { port, logged: logged.join("\n") };
  }

  describe("with the local gateway off", () => {
    it("honours a dashboard record that names a configured crew", () => {
      const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: 7778, localGatewayEnabled: false }).port, 7778);
    });

    it("ignores a dashboard record no host can serve and takes the crew", () => {
      const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: 5476, localGatewayEnabled: false }).port, 7778);
    });

    it("takes the configured crew when there is no dashboard record", () => {
      const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: null, localGatewayEnabled: false }).port, 7778);
    });

    it("keeps the dashboard record when no crew is configured at all", () => {
      const store = mockStore({ remoteHosts: {} });
      assert.equal(select(store, { configuredPort: 9999, localGatewayEnabled: false }).port, 9999);
    });

    it("refuses a record on port 80 whose only crew sits there too", () => {
      // The crew scan skips 80, so no remote target is found and the record
      // reaches the shared exit. Honouring it would hand the launch the one port
      // whose URL drops it, which is what makes a remote link read as local.
      const store = mockStore({ remoteHosts: { "80": { host: "a.example.com" } } });
      assert.equal(
        select(store, { configuredPort: 80, localGatewayEnabled: false }).port,
        5476,
      );
    });

    it("falls back to the default with neither a record nor a crew", () => {
      const store = mockStore({ remoteHosts: {} });
      assert.equal(select(store, { configuredPort: null, localGatewayEnabled: false }).port, 5476);
    });
  });

  describe("with the local gateway on", () => {
    it("targets a dashboard record whose port has a configured crew", () => {
      // The record is the local end of a link to that crew, and the crew may be
      // answering on it right now through a tunnel. Targeting it is what lets
      // that tunnel be found and adopted; stepping off it meant a live tunnel on
      // a crew-named port was never even probed.
      //
      // Whether a gateway may BIND this port is a different question, and it is
      // not answerable here: nothing in these arguments says who holds the port.
      // It is answered at bind time, where the holder is known -- see the
      // supervisor's refusal to start a gateway on a crew's port.
      const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
      const { port, logged } = select(store, { configuredPort: 7778 });
      assert.equal(port, 7778);
      assert.doesNotMatch(
        logged,
        /remote crew is configured/,
        "no avoidance happens here any more, so nothing announces one",
      );
    });

    it("declines it on a port it would not select either", () => {
      const store = mockStore({ remoteHosts: { "80": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: 80 }).port, 5476);
    });

    it("keeps a dashboard record whose own port has no crew", () => {
      // A crew configured somewhere else is not a reason to move a local launch.
      const store = mockStore({ remoteHosts: { "9999": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: 7778 }).port, 7778);
    });

    it("keeps a record whose entry is only a window-title setting", () => {
      const store = mockStore({ remoteHosts: { "7778": { defaultName: "Staging" } } });
      assert.equal(select(store, { configuredPort: 7778 }).port, 7778);
    });

    it("falls back to the default when a crew is configured and no record exists", () => {
      const store = mockStore({ remoteHosts: { "7778": { host: "a.example.com" } } });
      assert.equal(select(store, { configuredPort: null }).port, 5476);
    });

    it("keeps a record whose entry carries an empty or non-string host", () => {
      // Neither shape names a machine to reach, so neither is a crew to shadow.
      for (const entry of [{ host: "" }, { host: 5 }, {}]) {
        const store = mockStore({ remoteHosts: { "7778": entry } });
        assert.equal(select(store, { configuredPort: 7778 }).port, 7778);
      }
    });

    // The product default is also the likeliest local end of a tunnel, so a crew
    // configured there is the commonest shape of all -- and the one where
    // stepping aside used to cost the most.
    it("targets the record's port even when the default names a crew too", () => {
      const store = mockStore({
        remoteHosts: {
          "7778": { host: "a.example.com" },
          "5476": { host: "b.example.com" },
        },
      });
      assert.equal(select(store, { configuredPort: 7778 }).port, 7778);
    });

    it("targets the default with no record, even when a crew is configured there", () => {
      // This is the shape the legacy migration lands in: the crew is keyed under
      // the default and there is no dashboard.url yet. Walking off the default
      // here is what sent that first launch to a port no crew entry named.
      const store = mockStore({ remoteHosts: { "5476": { host: "c.example.com" } } });
      const { port, logged } = select(store, { configuredPort: null });
      assert.equal(port, 5476);
      assert.doesNotMatch(logged, /using 5477/, "no walk happens here");
    });

    it("targets the default over a run of crew-claimed ports, rather than walking past them", () => {
      // Four consecutive crews used to make selection walk to the first free
      // port. Where a NEW gateway may bind still walks -- that is
      // fallbackLocalPort, tested on its own -- but a launch's target does not:
      // one of those four may be serving.
      const remoteHosts = {};
      for (let p = 5476; p <= 5479; p += 1) remoteHosts[String(p)] = { host: "c.example.com" };
      const store = mockStore({ remoteHosts });
      assert.equal(select(store, { configuredPort: null }).port, 5476);
    });

    // A dashboard record on a scheme's default port is refused whether or not a
    // crew is configured there. Every per-port lookup keyed off the window URL
    // reads "" for 80, so a launch that lands there cannot be told apart from a
    // link to a remote crew, and the heartbeat sends the internal secret on that
    // answer. Which port is selected is the part this module controls.
    it("refuses a crew-free dashboard record on port 80", () => {
      const store = mockStore({ remoteHosts: {} });
      const { port, logged } = select(store, { configuredPort: 80 });
      assert.equal(port, 5476);
      assert.match(logged, /No usable dashboard\.url port/);
      assert.match(logged, /targeting 5476/);
    });
  });
});
