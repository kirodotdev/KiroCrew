"use strict";

/**
 * Every request the companion sends is addressed to its CONFIGURED backend URL.
 *
 * The companion caches a token for hours, and the gateway it belongs to is reached
 * by a name (`localhost`) that identifies both loopback families. The protection
 * is not a second spelling to address: it is that the gateway holds every family
 * the name reaches, checked before the secret ever leaves (see
 * `listenerSecretsFor`). So the companion keeps the token and nothing else, and no
 * request it builds may name an origin other than the configured one -- rewriting
 * it would move the document's storage bucket and split the settings of every
 * window this addresses.
 *
 * These tests drive `reconcileOnce` directly (it is exported for exactly that) and
 * record every URL the companion produces: the `/api/apps` probe and the three
 * window targets.
 */

const assert = require("node:assert/strict");
const test = require("node:test");
const Module = require("node:module");
const path = require("node:path");

const INDEX = path.join(__dirname, "..", "index.js");

/**
 * Load index.js with electron, http and the sibling window modules stubbed,
 * recording every URL that carries a credential.
 *
 * @param {{status: number[]}} wiring
 * @returns {{mod: object, probes: string[], targets: Array<{url: string, token: string}>}}
 */
function loadCompanion(wiring) {
  const originalLoad = Module._load;
  const probes = [];
  const targets = [];
  const recordTarget = (url, token) => targets.push({ url, token });
  const windowModule = {
    setOverlayTarget: recordTarget,
    setPanelTarget: recordTarget,
    setGalleryTarget: recordTarget,
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
    openPetWindow() {},
    closePetWindow() {},
    closePanelWindow() {},
    closeGalleryWindow() {},
    petWindowCount: () => 0,
    rearmBlankedCompanionWindows: () => 0,
    startHitboxPoll() {},
    stopHitboxPoll() {},
  };

  Module._load = function (request, parent, isMain) {
    if (request === "electron") {
      return {
        ipcMain: { on() {}, handle() {}, removeHandler() {} },
        BrowserWindow: { fromWebContents: () => null },
      };
    }
    if (request === "node:http" || request === "http") {
      return {
        get(url, _opts, cb) {
          probes.push(String(url));
          const statusCode = wiring.status.shift();
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
      request.startsWith("./pet")
      || request.startsWith("./panel")
      || request.startsWith("./gallery")
      || request.startsWith("./page")
    ) {
      return windowModule;
    }
    return originalLoad(request, parent, isMain);
  };

  try {
    delete require.cache[require.resolve(INDEX)];
    return { mod: require(INDEX), probes, targets };
  } finally {
    Module._load = originalLoad;
  }
}

/** Let the reconcile that `initCrewCompanion` fires itself settle. */
async function settle() {
  for (let i = 0; i < 10; i += 1) await new Promise((r) => setImmediate(r));
}

test("every token-bearing URL is addressed at the configured backend URL", async () => {
  const { mod, probes, targets } = loadCompanion({ status: [200] });
  mod.initCrewCompanion({
    backendUrl: "http://localhost:5476",
    mintLocalToken: async () => "tok",
  });
  await settle();

  assert.equal(probes.length, 1, "one probe from init's own reconcile");
  assert.equal(probes[0], "http://localhost:5476/api/apps?token=tok");
  assert.equal(targets.length, 3, "the overlay, panel and gallery targets are all set");
  for (const { url, token } of targets) {
    assert.equal(url, "http://localhost:5476", "a window target names the configured URL");
    assert.equal(token, "tok");
  }
  for (const seen of [...probes, ...targets.map((t) => t.url)]) {
    assert.equal(
      seen.includes("127.0.0.1"),
      false,
      `${seen} must not rewrite the configured host to a literal`,
    );
  }
});

test("a refused mint addresses nothing at all", async () => {
  // The predicate refuses when the gateway does not hold every family the name
  // reaches, and the mint then returns "". No token means no probe and no target:
  // the companion waits rather than asking unauthenticated.
  const { mod, probes, targets } = loadCompanion({ status: [] });
  mod.initCrewCompanion({
    backendUrl: "http://localhost:5476",
    mintLocalToken: async () => "",
  });
  await settle();

  assert.deepEqual(probes, []);
  assert.deepEqual(targets, []);
});

test("the re-mint after a refusal re-addresses the targets too", async () => {
  // Independent of any origin question: a cached token the gateway has stopped
  // accepting is re-minted exactly once, and the windows must be handed the NEW
  // token rather than keeping the refused one.
  const mints = { n: 0 };
  const { mod, probes, targets } = loadCompanion({ status: [401, 200] });
  mod.initCrewCompanion({
    backendUrl: "http://localhost:5476",
    mintLocalToken: async () => {
      mints.n += 1;
      return `tok${mints.n}`;
    },
  });
  await settle();

  assert.equal(mints.n, 2, "an auth refusal re-mints once");
  assert.deepEqual(probes, [
    "http://localhost:5476/api/apps?token=tok1",
    "http://localhost:5476/api/apps?token=tok2",
  ]);
  assert.deepEqual(
    targets.map((t) => `${t.url}|${t.token}`),
    [
      "http://localhost:5476|tok1",
      "http://localhost:5476|tok1",
      "http://localhost:5476|tok1",
      "http://localhost:5476|tok2",
      "http://localhost:5476|tok2",
      "http://localhost:5476|tok2",
    ],
  );
});
