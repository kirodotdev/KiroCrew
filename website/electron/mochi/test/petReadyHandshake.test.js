// The pet overlay draws nothing until the main process sends it
// set-active(true). The sends at load and 300ms later reach the page only if
// its React listener is already mounted; a renderer that mounts later used to
// stay a fully transparent window forever. The renderer's
// `mochi-pet:pet-ready` is answered with that overlay's activation, which is
// what these tests pin, together with the launch-log lines for pet failures.
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

function stubElectron(userData) {
  const created = [];
  let nextId = 1;
  class FakeWebContents {
    constructor() {
      this.id = nextId++;
      this.listeners = new Map();
      this.sent = [];
    }
    on(event, callback) {
      const list = this.listeners.get(event) ?? [];
      list.push(callback);
      this.listeners.set(event, list);
    }
    emit(event, ...args) {
      for (const cb of this.listeners.get(event) ?? []) cb(...args);
    }
    send(channel, ...args) { this.sent.push({ channel, args }); }
    isDestroyed() { return false; }
    isLoading() { return true; }
    setWindowOpenHandler() {}
  }
  class FakeWindow {
    constructor(opts) {
      this.opts = opts;
      this.webContents = new FakeWebContents();
      this.visible = false;
      created.push(this);
    }
    setFocusable() {}
    setIgnoreMouseEvents() {}
    setVisibleOnAllWorkspaces() {}
    setContentProtection() {}
    setAlwaysOnTop() {}
    setBounds() {}
    getBounds() { return { x: 0, y: 0, width: 1440, height: 900 }; }
    loadURL() {}
    isVisible() { return this.visible; }
    isDestroyed() { return false; }
    showInactive() { this.visible = true; }
    hide() { this.visible = false; }
    close() {}
    on() {}
  }
  const displays = [
    {
      id: 1,
      bounds: { x: 0, y: 0, width: 1440, height: 900 },
      workArea: { x: 0, y: 25, width: 1440, height: 875 },
    },
  ];
  const cursor = { x: 0, y: 0 };
  const ipcHandlers = new Map();
  return {
    created,
    displays,
    cursor,
    ipcHandlers,
    electron: {
      app: {
        on() {},
        setActivationPolicy() {},
        dock: { show() {} },
        getPath: () => userData,
      },
      BrowserWindow: FakeWindow,
      ipcMain: {
        on(channel, fn) { ipcHandlers.set(channel, fn); },
        handle() {},
      },
      shell: { openExternal() {}, showItemInFolder() {} },
      screen: {
        getPrimaryDisplay: () => displays[0],
        getAllDisplays: () => displays,
        getCursorScreenPoint: () => ({ ...cursor }),
        on() {},
        removeListener() {},
      },
    },
  };
}

/**
 * Load a fresh petOverlays against a throwaway userData dir, optionally seeded
 * with a saved pet position, so the position-persistence paths really run.
 */
function loadPetOverlays(t, savedPos) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "mochi-pet-ready-"));
  const posFile = path.join(userData, "mochi-pet-position.json");
  if (savedPos) fs.writeFileSync(posFile, JSON.stringify(savedPos));
  t.after(() => {
    fs.rmSync(posFile, { force: true });
    fs.rmdirSync(userData);
  });
  const stub = stubElectron(userData);
  const modPath = path.join(__dirname, "..", "petOverlays.js");
  const panelPath = path.join(__dirname, "..", "panelWindow.js");
  delete require.cache[require.resolve(modPath)];
  delete require.cache[require.resolve(panelPath)];
  const origLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "electron") return stub.electron;
    return origLoad(request, parent, isMain);
  };
  try {
    require(panelPath);
    return { mod: require(modPath), ...stub };
  } finally {
    Module._load = origLoad;
  }
}

const SECOND_DISPLAY = {
  id: 2,
  bounds: { x: 1440, y: 0, width: 1440, height: 900 },
  workArea: { x: 1440, y: 0, width: 1440, height: 900 },
};

function setActiveCalls(win) {
  return win.webContents.sent
    .filter((s) => s.channel === "mochi-pet:set-active")
    .map((s) => s.args);
}

/**
 * Load the page and let both fast-path sends fire with no listener mounted, so
 * every one of them is dropped. Returns the set-active calls that were sent.
 */
function loadWithNoListener(t, win) {
  win.webContents.emit("did-finish-load");
  t.mock.timers.tick(300);
  const fastPath = setActiveCalls(win);
  win.webContents.sent.length = 0;
  return fastPath;
}

const SAVED = { x: 200, y: 300, displayId: 1 };

test("a renderer whose listener mounts after the fast-path sends is still activated", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { mod, created, ipcHandlers } = loadPetOverlays(t, SAVED);
  try {
    mod.openPetWindow("http://localhost:6777", "tok");
    const [win] = created;
    assert.deepStrictEqual(
      loadWithNoListener(t, win),
      [[true, 200, 300, false], [true, 200, 300, false]],
      "the fast path sends at load and 300ms later",
    );

    ipcHandlers.get("mochi-pet:pet-ready")({ sender: win.webContents });

    const channels = win.webContents.sent.map((s) => s.channel);
    assert.ok(channels.includes("mochi-pet:displays-info"), "geometry is re-sent");
    assert.deepStrictEqual(
      setActiveCalls(win),
      [[true, 200, 300, false]],
      "the late listener is activated at the saved position",
    );
  } finally {
    mod.closePetWindow();
  }
});

test("an overlay that does not host the pet stays inactive when it reports ready", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { mod, created, displays, ipcHandlers } = loadPetOverlays(t, SAVED);
  displays.push(SECOND_DISPLAY);
  try {
    mod.openPetWindow("http://localhost:6777", "tok");
    assert.strictEqual(created.length, 2);
    const [, other] = created;
    assert.deepStrictEqual(loadWithNoListener(t, other), [[false], [false]]);

    ipcHandlers.get("mochi-pet:pet-ready")({ sender: other.webContents });

    assert.deepStrictEqual(setActiveCalls(other), [[false]]);
    assert.strictEqual(mod.getActiveDisplayId(), 1, "the reply moves nothing");
  } finally {
    mod.closePetWindow();
  }
});

test("an overlay that does not host the pet stays inactive during a drag", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { mod, created, displays, ipcHandlers } = loadPetOverlays(t, SAVED);
  displays.push(SECOND_DISPLAY);
  try {
    mod.openPetWindow("http://localhost:6777", "tok");
    const [active, other] = created;
    ipcHandlers.get("mochi-pet:drag-start")({ sender: active.webContents }, 10, 20);
    ipcHandlers.get("mochi-pet:pet-ready")({ sender: other.webContents });
    assert.deepStrictEqual(setActiveCalls(other), [[false]]);
  } finally {
    mod.closePetWindow();
  }
});

test("an on-demand overlay is answered by who hosts the pet when it reports ready", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { mod, created, displays, ipcHandlers } = loadPetOverlays(t, SAVED);
  try {
    mod.openPetWindow("http://localhost:6777", "tok");
    // A display connected with no overlay yet: the tool-driven move builds one.
    displays.push(SECOND_DISPLAY);
    const moved = mod.transferPetToDisplayById(2, 50, 60);
    const target = created[1];
    target.webContents.emit("did-finish-load");

    // Before the delayed transfer, display 1 still hosts the pet.
    ipcHandlers.get("mochi-pet:pet-ready")({ sender: target.webContents });
    assert.deepStrictEqual(setActiveCalls(target), [[false]]);

    t.mock.timers.tick(300);
    assert.strictEqual(await moved, true);
    target.webContents.sent.length = 0;

    // After it, a late listener on the new host is activated where the pet went.
    ipcHandlers.get("mochi-pet:pet-ready")({ sender: target.webContents });
    assert.deepStrictEqual(setActiveCalls(target), [[true, 50, 60, false]]);
    created[0].webContents.sent.length = 0;
    ipcHandlers.get("mochi-pet:pet-ready")({ sender: created[0].webContents });
    assert.deepStrictEqual(setActiveCalls(created[0]), [[false]], "the old host stays inactive");
  } finally {
    mod.closePetWindow();
  }
});

test("only the overlay that sent pet-ready is answered", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { mod, created, displays, ipcHandlers } = loadPetOverlays(t);
  displays.push(SECOND_DISPLAY);
  try {
    mod.openPetWindow("http://localhost:6777", "tok");
    const [active, other] = created;
    ipcHandlers.get("mochi-pet:pet-ready")({ sender: active.webContents });
    assert.strictEqual(other.webContents.sent.length, 0, "a sibling overlay is not touched");
    ipcHandlers.get("mochi-pet:pet-ready")({ sender: { id: -1 } });
    assert.strictEqual(active.webContents.sent.length, 2, "an unknown sender gets nothing");
  } finally {
    mod.closePetWindow();
  }
});

test("a pet-ready reply during a drag keeps the pet under the cursor", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { mod, created, cursor, ipcHandlers } = loadPetOverlays(t);
  try {
    mod.openPetWindow("http://localhost:6777", "tok");
    const [win] = created;
    loadWithNoListener(t, win);
    cursor.x = 500;
    cursor.y = 400;
    ipcHandlers.get("mochi-pet:drag-start")({ sender: win.webContents }, 10, 20);

    ipcHandlers.get("mochi-pet:pet-ready")({ sender: win.webContents });

    assert.deepStrictEqual(
      setActiveCalls(win),
      [[true, 490, 380, true]],
      "activated at the dragged position with the drag flag set",
    );
  } finally {
    mod.closePetWindow();
  }
});

test("an overlay hidden on an error page is not answered", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { mod, created, ipcHandlers } = loadPetOverlays(t);
  try {
    mod.openPetWindow("http://localhost:6777", "tok");
    const [win] = created;
    mod._handleOverlayNavigation(win, 403);
    ipcHandlers.get("mochi-pet:pet-ready")({ sender: win.webContents });
    assert.strictEqual(win.webContents.sent.length, 0);
  } finally {
    mod.closePetWindow();
  }
});

test("pet load failures, crashes and page errors reach the launch log without the token", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { mod, created } = loadPetOverlays(t);
  const lines = [];
  mod.setPetLogger((line) => lines.push(line));
  try {
    mod.openPetWindow("http://localhost:6777", "sekret-token");
    const [win] = created;

    win.webContents.emit(
      "did-fail-load",
      {},
      -102,
      "ERR_CONNECTION_REFUSED",
      "http://localhost:6777/api/apps/mochi/pet.html?token=sekret-token",
      true,
    );
    win.webContents.emit(
      "console-message",
      {},
      3,
      "Failed to load http://localhost:6777/x.js?a=1&token=sekret-token&b=2",
    );
    win.webContents.emit("console-message", {}, 1, "info line is not logged");
    win.webContents.emit("render-process-gone", {}, { reason: "crashed", exitCode: 11 });

    assert.strictEqual(lines.length, 3, lines.join("\n"));
    assert.match(lines[0], /^Mochi pet: load failed \(-102 ERR_CONNECTION_REFUSED\) for http:\/\/localhost:6777\/api\/apps\/mochi\/pet\.html$/);
    assert.match(lines[1], /^Mochi pet page error: .*token=<redacted>&b=2$/);
    assert.strictEqual(lines[2], "Mochi pet: renderer gone — reason=crashed exitCode=11");
    for (const line of lines) assert.ok(!line.includes("sekret"), `token leaked: ${line}`);
  } finally {
    mod.closePetWindow();
  }
});

test("a page error quoting the token in another form is masked too", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  const { mod, created } = loadPetOverlays(t);
  const lines = [];
  mod.setPetLogger((line) => lines.push(line));
  const token = "sekret/tok+en=";
  try {
    mod.openPetWindow("http://localhost:6777", token);
    const [win] = created;
    for (const message of [
      `fragment http://h/pet.html#token=${token}`,
      `encoded ${encodeURIComponent(token)} in a body`,
      `bare ${token} value`,
    ]) {
      win.webContents.emit("console-message", {}, 2, message);
    }
    assert.strictEqual(lines.length, 3);
    for (const line of lines) {
      assert.ok(!line.includes("sekret"), `token leaked: ${line}`);
      assert.ok(line.includes("<redacted>"), line);
    }
  } finally {
    mod.closePetWindow();
  }
});

test("a page that errors every frame is capped in the launch log", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"] });
  const { mod, created } = loadPetOverlays(t);
  const lines = [];
  mod.setPetLogger((line) => lines.push(line));
  try {
    mod.openPetWindow("http://localhost:6777", "tok");
    const [win] = created;
    for (let i = 0; i < 25; i += 1) win.webContents.emit("console-message", {}, 3, `boom ${i}`);
    assert.strictEqual(lines.length, 20, "at most 20 page-error lines a minute");

    t.mock.timers.tick(60_000);
    win.webContents.emit("console-message", {}, 3, "after the minute");
    assert.deepStrictEqual(lines.slice(20), [
      "Mochi pet page error: 5 more suppressed in the last minute",
      "Mochi pet page error: after the minute",
    ]);
  } finally {
    mod.closePetWindow();
  }
});
