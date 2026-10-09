"use strict";

/**
 * The reconcile must be able to ask the gateway with ANY credential the
 * dashboard window can sign in with, not only the local-secret mint.
 *
 * The mint deliberately refuses for a port a remote crew is configured on and
 * for a gateway this app adopted rather than spawned. With it as the only source
 * the reconcile has no credential, reads that as "unknown", and leaves an
 * enabled companion closed with nothing in any log. These tests
 * pin the three things that fix needs: a non-mint credential opens the overlay,
 * a borrowed session credential is sent as the cookie it came from (never on a
 * URL), and the no-credential case is logged instead of silent.
 */

const assert = require("node:assert/strict");
const test = require("node:test");
const Module = require("node:module");
const path = require("node:path");

const INDEX = path.join(__dirname, "..", "index.js");

/**
 * Load a fresh index.js with electron, http and the window modules stubbed.
 *
 * @param {{requests: Array<{url: string, headers: object}>, targets: Array<Array>,
 *   opened: {n: number}, statuses?: number[]}} wiring
 */
function loadCompanion(wiring) {
  const originalLoad = Module._load;
  const windowModule = {
    setOverlayTarget(...args) { wiring.targets.push(["overlay", ...args]); },
    setPanelTarget(...args) { wiring.targets.push(["panel", ...args]); },
    setGalleryTarget(...args) { wiring.targets.push(["gallery", ...args]); },
    setOverlayLogger() {},
    setPanelLogger() {},
    setGalleryLogger() {},
    registerPanelIpc() {},
    registerGalleryIpc() {},
    registerOverlayIpc() {},
    setAppearanceChangedHandler() {},
    setPanelClosedHandler() {},
    setGalleryOpenedHandler() {},
    setGalleryClosedHandler() {},
    broadcastToPets() {},
    openPetWindow() { wiring.opened.n += 1; },
    closePetWindow() {},
    closePanelWindow() {},
    closeGalleryWindow() {},
    petWindowCount: () => wiring.opened.n,
    rearmBlankedCompanionWindows: () => 0,
    stopHitboxPoll() {},
    overlayMayBeNonActivatable: () => true,
  };

  Module._load = function (request, parent, isMain) {
    if (request === "electron") {
      return {
        ipcMain: { on() {}, handle() {}, removeHandler() {} },
        BrowserWindow: { fromWebContents: () => null },
      };
    }
    if (request === "http" || request === "node:http") {
      return {
        get(url, opts, cb) {
          wiring.requests.push({ url, headers: (opts && opts.headers) || {} });
          const statusCode = (wiring.statuses && wiring.statuses.shift()) || 200;
          const res = {
            statusCode,
            on(evt, fn) {
              if (evt === "data") fn(JSON.stringify([{ name: "crew-companion", enabled: true }]));
              if (evt === "end") fn();
            },
          };
          setImmediate(() => cb(res));
          return { on() {}, destroy() {} };
        },
      };
    }
    if (
      request.startsWith("./pet") ||
      request.startsWith("./panel") ||
      request.startsWith("./gallery") ||
      request.startsWith("./page")
    ) {
      return windowModule;
    }
    return originalLoad(request, parent, isMain);
  };

  try {
    delete require.cache[require.resolve(INDEX)];
    return require(INDEX);
  } finally {
    Module._load = originalLoad;
  }
}

async function settle() {
  for (let i = 0; i < 10; i += 1) await new Promise((r) => setImmediate(r));
}

function freshWiring(extra = {}) {
  return { requests: [], targets: [], opened: { n: 0 }, ...extra };
}

test("a link token from the full chain (e.g. a remote crew's SSH token) opens the overlay", async () => {
  const wiring = freshWiring();
  const mod = loadCompanion(wiring);
  mod.initCrewCompanion({
    backendUrl: "http://localhost:5476",
    // What main.js now passes: the local mint refused, the SSH fetch answered.
    fetchGatewayAuth: async () => ({ value: "remote-tok", viaCookie: false }),
    glog: () => {},
  });
  await settle();

  assert.equal(wiring.opened.n, 1, "an enabled app must open the overlay");
  assert.equal(wiring.requests.length, 1);
  assert.equal(wiring.requests[0].url, "http://localhost:5476/api/apps?token=remote-tok");
  assert.equal(wiring.requests[0].headers.Cookie, undefined);
  assert.deepEqual(
    wiring.targets.find((t) => t[0] === "overlay"),
    ["overlay", "http://localhost:5476", "remote-tok", false],
  );
  mod.shutdownCrewCompanion();
});

test("a borrowed session credential is sent as its cookie and never put on a URL", async () => {
  const wiring = freshWiring();
  const mod = loadCompanion(wiring);
  mod.initCrewCompanion({
    backendUrl: "http://localhost:5476",
    // An adopted gateway: no mint, no remote crew, only the dashboard's session.
    fetchGatewayAuth: async () => ({ value: "session-tok", viaCookie: true }),
    glog: () => {},
  });
  await settle();

  assert.equal(wiring.opened.n, 1, "an enabled app must open the overlay");
  const req = wiring.requests[0];
  assert.equal(req.url, "http://localhost:5476/api/apps", "no ?token= for a cookie credential");
  assert.equal(req.headers.Cookie, "mc_token_5476=session-tok");
  for (const [, , pageToken, viaCookie] of wiring.targets) {
    assert.equal(pageToken, "", "window URLs must not carry the borrowed cookie value");
    if (viaCookie !== undefined) assert.equal(viaCookie, true);
  }
  mod.shutdownCrewCompanion();
});

test("a refused borrowed credential is re-resolved exactly once", async () => {
  const wiring = freshWiring({ statuses: [401, 200] });
  const calls = { n: 0 };
  const mod = loadCompanion(wiring);
  mod.initCrewCompanion({
    backendUrl: "http://localhost:5476",
    fetchGatewayAuth: async () => {
      calls.n += 1;
      return { value: `session-${calls.n}`, viaCookie: true };
    },
    glog: () => {},
  });
  await settle();

  assert.equal(calls.n, 2, "one refusal re-resolves once, and only once");
  assert.equal(wiring.requests[1].headers.Cookie, "mc_token_5476=session-2");
  assert.equal(wiring.opened.n, 1);
  mod.shutdownCrewCompanion();
});

test("no credential at all is logged once instead of failing silently", async () => {
  const wiring = freshWiring();
  const lines = [];
  const mod = loadCompanion(wiring);
  mod.initCrewCompanion({
    backendUrl: "http://localhost:5476",
    fetchGatewayAuth: async () => ({ value: "" }),
    glog: (m) => lines.push(m),
  });
  await settle();
  await mod.reconcileOnce();
  await mod.reconcileOnce();

  assert.equal(wiring.requests.length, 0, "nothing to ask with, so nothing is asked");
  assert.equal(wiring.opened.n, 0, "unknown leaves the windows as they are");
  const noted = lines.filter((m) => m.includes("no gateway credential"));
  assert.equal(noted.length, 1, "the gap is reported once, not every tick");
  mod.shutdownCrewCompanion();
});

test("main.js hands the companion the full credential chain, not the bare local mint", () => {
  const fs = require("node:fs");
  const main = fs.readFileSync(path.join(__dirname, "..", "..", "main.js"), "utf8");
  const start = main.indexOf("initCrewCompanion({");
  assert.ok(start > 0, "main.js must initialise the companion");
  const call = main.slice(start, main.indexOf("});", start));
  assert.match(call, /fetchGatewayAuth:/, "the companion must get the same chain as the dashboard");
  assert.doesNotMatch(call, /mintLocalToken:/, "the bare mint refuses for remote and adopted gateways");
});

test("the session cookie header names the stated port and declines without one", () => {
  const { sessionCookieHeader } = require("../../mochi-session-token");
  assert.equal(sessionCookieHeader("http://localhost:5476", "v"), "mc_token_5476=v");
  assert.equal(sessionCookieHeader("http://localhost", "v"), "", "no guessed default port");
  assert.equal(sessionCookieHeader("not a url", "v"), "");
  assert.equal(sessionCookieHeader("http://localhost:5476", ""), "");
});

test("an empty credential chain is not re-asked every tick", async () => {
  const wiring = freshWiring();
  const calls = { n: 0 };
  const mod = loadCompanion(wiring);
  mod.initCrewCompanion({
    backendUrl: "http://localhost:5476",
    // e.g. a remote crew whose ssh fails: each call would spawn ssh.
    fetchGatewayAuth: async () => {
      calls.n += 1;
      return { value: "" };
    },
    glog: () => {},
  });
  await settle();
  await mod.reconcileOnce();
  await mod.reconcileOnce();

  assert.equal(calls.n, 1, "inside the backoff window the chain is not asked again");
  mod.shutdownCrewCompanion();
});
