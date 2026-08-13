// The "installing" lifecycle state contract.
//
// Dispatching an install stops the bundled Python gateway ON PURPOSE, and the
// window can stay open for a long stretch while the gateway winds down and the
// platform installer stages the bundle. Without a state that says an install
// is underway, the renderer reads that silence as an outage (offline pill,
// failed requests) and a successful install looks like a failure.
//
// These tests pin two things: the emit ORDER — "installing" must reach the
// renderer BEFORE stopGateway is awaited, so the overlay is up before the
// surfaces beneath it go dark — and that a refused install (nothing staged)
// never claims to be installing.
const { test } = require("node:test");
const assert = require("node:assert");

const { initAutoUpdate } = require("../auto-update");

function installHarness() {
  // One interleaved timeline of renderer emits and gateway calls, in the
  // exact order the module produced them — the order IS the assertion.
  const timeline = [];
  const payloads = [];
  const handlers = {};
  let quitHook = null;
  const deps = {
    app: {
      isPackaged: true,
      getVersion: () => "1.0.0",
      once: (ev, fn) => { if (ev === "before-quit") quitHook = fn; },
      removeListener: () => {},
      exit: () => {},
    },
    autoUpdater: {
      setFeedURL: () => {},
      checkForUpdates: async () => {},
      downloadUpdate: async () => {},
      quitAndInstall: () => { timeline.push("quitAndInstall"); },
      on: (ev, fn) => { handlers[ev] = fn; },
    },
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function () { return { show: () => {} }; },
    getFlavor: () => "stable",
    stopGateway: async () => { timeline.push("stopGateway"); },
    osPlatform: "darwin",
    feedBase: "https://cdn.example.dev/feed",
    nativeAutoUpdater: { once: () => {} },
    onUpdateState: (p) => { timeline.push(`state:${p.state}`); payloads.push(p); },
    log: { info: () => {}, warn: () => {}, error: () => {} },
  };
  return {
    updater: initAutoUpdate(deps),
    timeline,
    payloads,
    fire: (ev, p) => handlers[ev] && handlers[ev](p),
    quit: () => quitHook && quitHook({ preventDefault: () => {} }),
  };
}

test("install() with a staged update emits 'installing' BEFORE the gateway stops", async () => {
  const h = installHarness();
  h.fire("update-downloaded", { version: "1.1.0" });
  await h.updater.install();

  const installingAt = h.timeline.indexOf("state:installing");
  const stopAt = h.timeline.indexOf("stopGateway");
  assert.notStrictEqual(installingAt, -1, "'installing' must be emitted on dispatch");
  assert.notStrictEqual(stopAt, -1, "the gateway must be stopped");
  assert.ok(
    installingAt < stopAt,
    `'installing' must reach the renderer before the gateway goes silent (got ${h.timeline.join(" -> ")})`,
  );
});

test("the 'installing' payload carries the STAGED version, not the running one", async () => {
  const h = installHarness();
  h.fire("update-downloaded", { version: "1.1.0" });
  await h.updater.install();

  const p = h.payloads.find((x) => x.state === "installing");
  assert.ok(p, "'installing' must be emitted");
  assert.strictEqual(p.version, "1.1.0");
});

test("install() with nothing staged does NOT emit 'installing'", async () => {
  const h = installHarness();
  await h.updater.install();

  assert.ok(
    !h.timeline.includes("state:installing"),
    `a refused install must not claim to be installing (got ${h.timeline.join(" -> ")})`,
  );
  assert.ok(!h.timeline.includes("stopGateway"), "a refused install must not stop the gateway");
});

test("the deferred install on quit emits 'installing' before the gateway stops", async () => {
  const h = installHarness();
  h.fire("update-downloaded", { version: "1.1.0" });
  h.quit();
  // The quit handler's body is async; give it a tick to run through the
  // awaited gateway stop.
  await new Promise((resolve) => setImmediate(resolve));

  const installingAt = h.timeline.indexOf("state:installing");
  const stopAt = h.timeline.indexOf("stopGateway");
  assert.notStrictEqual(installingAt, -1, "'installing' must be emitted on the quit path too");
  assert.ok(
    installingAt < stopAt,
    `'installing' must precede the gateway stop on the quit path (got ${h.timeline.join(" -> ")})`,
  );
});
