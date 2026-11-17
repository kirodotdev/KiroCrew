"use strict";

/**
 * Once the app's own repository copy is installed (the gateway's row is no longer
 * `origin: "builtin"`), the desktop window host draws the avatar from that copy's
 * manifest. The built-in windows must then close and stay closed, or the user gets
 * two avatars and every reminder fires twice. A row without `origin` (a gateway
 * older than the field) keeps today's behaviour.
 *
 * The second half pins the display hand-off: the host opens the copy's avatar on
 * the display the built-in last used, not on the one under the cursor, and keeps
 * a display it chose itself.
 */

const assert = require("node:assert/strict");
const test = require("node:test");
const Module = require("node:module");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const INDEX = path.join(__dirname, "..", "index.js");
const HAND_OFF = path.join(__dirname, "..", "displayHandOff.js");

function loadCompanion(wiring) {
  const originalLoad = Module._load;
  const calls = { open: 0, closePet: 0, closePanel: 0, closeGallery: 0 };
  let windowOpen = false;
  const noopWindowModule = {
    setOverlayTarget() {}, setPanelTarget() {}, setGalleryTarget() {},
    setOverlayLogger() {}, setPanelLogger() {}, setGalleryLogger() {},
    registerPanelIpc() {}, registerGalleryIpc() {}, registerOverlayIpc() {},
    setAppearanceChangedHandler() {}, setPanelClosedHandler() {},
    setGalleryOpenedHandler() {}, setGalleryClosedHandler() {},
    broadcastToPets() {},
    openPetWindow() { windowOpen = true; calls.open += 1; },
    closePetWindow() { windowOpen = false; calls.closePet += 1; },
    closePanelWindow() { calls.closePanel += 1; },
    closeGalleryWindow() { calls.closeGallery += 1; },
    petWindowCount: () => (windowOpen ? 1 : 0),
    rearmBlankedCompanionWindows: () => 0,
    startHitboxPoll() {}, stopHitboxPoll() {},
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
        get(_url, _opts, cb) {
          const rows = wiring.rows.shift();
          const res = {
            statusCode: 200,
            on(evt, fn) {
              if (evt === "data") fn(JSON.stringify(rows));
              if (evt === "end") fn();
            },
          };
          setImmediate(() => cb(res));
          return { on() {}, destroy() {} };
        },
      };
    }
    if (
      request.startsWith("./pet") || request.startsWith("./panel") ||
      request.startsWith("./gallery") || request.startsWith("./page")
    ) {
      return noopWindowModule;
    }
    return originalLoad(request, parent, isMain);
  };
  try {
    delete require.cache[require.resolve(INDEX)];
    const mod = require(INDEX);
    mod.__calls = calls;
    return mod;
  } finally {
    Module._load = originalLoad;
  }
}

async function settle() {
  for (let i = 0; i < 10; i += 1) await new Promise((r) => setImmediate(r));
}

function init(mod, logs) {
  mod.initCrewCompanion({
    backendUrl: "http://127.0.0.1:5476",
    fetchGatewayAuth: async () => ({ value: "tok" }),
    glog: (m) => logs.push(m),
  });
}

const BUILTIN = { name: "crew-companion", enabled: true, origin: "builtin" };
const ADOPTED = { name: "crew-companion", enabled: true, origin: "registry" };

test("an adopted row closes the built-in windows and keeps them closed", async () => {
  const logs = [];
  const mod = loadCompanion({ rows: [[BUILTIN], [ADOPTED], [ADOPTED]] });
  init(mod, logs);
  await settle();
  assert.equal(mod.__calls.open, 1, "the built-in row opens its windows");

  await mod.reconcileOnce();
  assert.equal(mod.__calls.closePet, 1, "the adopted row closes the avatar");
  assert.equal(mod.__calls.closePanel, 1, "and the panel");
  assert.equal(mod.__calls.closeGallery, 1, "and the gallery");
  assert.ok(logs.some((m) => m.includes("own copy is installed")), logs.join("\n"));

  await mod.reconcileOnce();
  assert.equal(mod.__calls.open, 1, "an enabled adopted row never reopens them");
  mod.shutdownCrewCompanion();
});

test("an adopted row opens nothing on a fresh start", async () => {
  const mod = loadCompanion({ rows: [[ADOPTED]] });
  init(mod, []);
  await settle();
  assert.equal(mod.__calls.open, 0);
  mod.shutdownCrewCompanion();
});

test("a row without origin is the built-in's, as before", async () => {
  const mod = loadCompanion({ rows: [[{ name: "crew-companion", enabled: true }]] });
  init(mod, []);
  await settle();
  assert.equal(mod.__calls.open, 1);
  mod.shutdownCrewCompanion();
});

test("probeEnabled answers released for a non-builtin row whatever its enabled flag", async () => {
  for (const enabled of [true, false]) {
    const mod = loadCompanion({ rows: [[{ ...ADOPTED, enabled }]] });
    const state = await mod.probeEnabled({ value: "tok", viaCookie: false });
    assert.equal(state, "released");
  }
});

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cc-handoff-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function handOff() {
  delete require.cache[require.resolve(HAND_OFF)];
  return require(HAND_OFF);
}

test("the built-in's display seeds the host's memory for the avatar window", (t) => {
  const dir = tempDir(t);
  const { seedHostDisplayMemory, BUILTIN_POSITION_FILE, HOST_KEY } = handOff();
  fs.writeFileSync(path.join(dir, BUILTIN_POSITION_FILE), JSON.stringify({ displayId: 42 }));
  const hostFile = path.join(dir, "app-window-displays.json");
  fs.writeFileSync(hostFile, JSON.stringify({ "other-app/overlay": 7 }));

  assert.equal(seedHostDisplayMemory(dir, hostFile), true);
  assert.deepEqual(JSON.parse(fs.readFileSync(hostFile, "utf-8")), { "other-app/overlay": 7, [HOST_KEY]: 42 });
});

test("a host file the hand-off creates is owner-only", (t) => {
  const dir = tempDir(t);
  const { seedHostDisplayMemory, BUILTIN_POSITION_FILE, HOST_KEY } = handOff();
  fs.writeFileSync(path.join(dir, BUILTIN_POSITION_FILE), JSON.stringify({ displayId: 9 }));
  const hostFile = path.join(dir, "app-window-displays.json");

  assert.equal(seedHostDisplayMemory(dir, hostFile), true);
  assert.deepEqual(JSON.parse(fs.readFileSync(hostFile, "utf-8")), { [HOST_KEY]: 9 });
  if (process.platform !== "win32") assert.equal(fs.statSync(hostFile).mode & 0o777, 0o600);
});

test("a display the host chose itself is never overwritten", (t) => {
  const dir = tempDir(t);
  const { seedHostDisplayMemory, BUILTIN_POSITION_FILE, HOST_KEY } = handOff();
  fs.writeFileSync(path.join(dir, BUILTIN_POSITION_FILE), JSON.stringify({ displayId: 42 }));
  const hostFile = path.join(dir, "app-window-displays.json");
  fs.writeFileSync(hostFile, JSON.stringify({ [HOST_KEY]: 3 }));

  assert.equal(seedHostDisplayMemory(dir, hostFile), false);
  assert.deepEqual(JSON.parse(fs.readFileSync(hostFile, "utf-8")), { [HOST_KEY]: 3 });
});

test("a move of the built-in avatar after the first start reaches the host on the next", (t) => {
  // Before adoption the built-in is the avatar the user moves; the host's entry
  // still holds what was copied, so the newer display replaces it.
  const dir = tempDir(t);
  const { seedHostDisplayMemory, BUILTIN_POSITION_FILE, HOST_KEY } = handOff();
  const position = path.join(dir, BUILTIN_POSITION_FILE);
  const hostFile = path.join(dir, "app-window-displays.json");
  fs.writeFileSync(position, JSON.stringify({ displayId: 1 }));
  assert.equal(seedHostDisplayMemory(dir, hostFile), true);

  fs.writeFileSync(position, JSON.stringify({ displayId: 2 }));
  assert.equal(seedHostDisplayMemory(dir, hostFile), true);
  assert.equal(JSON.parse(fs.readFileSync(hostFile, "utf-8"))[HOST_KEY], 2);
  assert.equal(seedHostDisplayMemory(dir, hostFile), false, "an unchanged display writes nothing");
});

test("after adoption the host's own move survives every later start", (t) => {
  const dir = tempDir(t);
  const { seedHostDisplayMemory, BUILTIN_POSITION_FILE, HOST_KEY } = handOff();
  const hostFile = path.join(dir, "app-window-displays.json");
  fs.writeFileSync(path.join(dir, BUILTIN_POSITION_FILE), JSON.stringify({ displayId: 1 }));
  assert.equal(seedHostDisplayMemory(dir, hostFile), true);

  // The adopted avatar was dragged to display 7; the host recorded it.
  fs.writeFileSync(hostFile, JSON.stringify({ [HOST_KEY]: 7 }));
  assert.equal(seedHostDisplayMemory(dir, hostFile), false);
  assert.equal(seedHostDisplayMemory(dir, hostFile), false);
  assert.deepEqual(JSON.parse(fs.readFileSync(hostFile, "utf-8")), { [HOST_KEY]: 7 });
});

test("no built-in display, a bad one, or an unreadable host file writes nothing", (t) => {
  const dir = tempDir(t);
  const { seedHostDisplayMemory, BUILTIN_POSITION_FILE } = handOff();
  const hostFile = path.join(dir, "app-window-displays.json");

  assert.equal(seedHostDisplayMemory(dir, hostFile), false);
  assert.equal(fs.existsSync(hostFile), false);

  fs.writeFileSync(path.join(dir, BUILTIN_POSITION_FILE), JSON.stringify({ displayId: "1" }));
  assert.equal(seedHostDisplayMemory(dir, hostFile), false);
  assert.equal(fs.existsSync(hostFile), false);

  fs.writeFileSync(path.join(dir, BUILTIN_POSITION_FILE), JSON.stringify({ displayId: 5 }));
  fs.writeFileSync(hostFile, "{ broken");
  assert.equal(seedHostDisplayMemory(dir, hostFile), false);
  assert.equal(fs.readFileSync(hostFile, "utf-8"), "{ broken");
});

test("initCrewCompanion runs the hand-off itself, so main.js calls nothing of it", async (t) => {
  const dir = tempDir(t);
  const { BUILTIN_POSITION_FILE, HOST_KEY } = handOff();
  fs.writeFileSync(path.join(dir, BUILTIN_POSITION_FILE), JSON.stringify({ displayId: 4 }));
  const hostFile = path.join(dir, "app-window-displays.json");
  const mod = loadCompanion({ rows: [[BUILTIN]] });
  mod.initCrewCompanion({
    backendUrl: "http://127.0.0.1:5476",
    fetchGatewayAuth: async () => ({ value: "tok" }),
    userDataDir: dir,
    displayMemoryPath: hostFile,
  });
  // Synchronous: the file is in place before main.js goes on to start the host.
  assert.deepEqual(JSON.parse(fs.readFileSync(hostFile, "utf-8")), { [HOST_KEY]: 4 });
  await settle();
  mod.shutdownCrewCompanion();

  const main = fs.readFileSync(path.join(__dirname, "..", "..", "main.js"), "utf8");
  assert.doesNotMatch(main, /seedHostDisplayMemory|displayHandOff/);
});
