"use strict";

// Tray and close-action tests for window-lifecycle.js, split out of
// window-lifecycle.test.js to keep that file under the line cap.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const MODULE_PATH = path.join(__dirname, "..", "window-lifecycle.js");
// Normalize to LF, as window-lifecycle.test.js does, so "\n" anchors match on
// a CRLF checkout.
const SOURCE = fs.readFileSync(MODULE_PATH, "utf8").replace(/\r\n/g, "\n");
const { createWindowLifecycle } = require("../window-lifecycle");
const { validOptions } = require("./window-lifecycle-test-helpers");

// Enough of Electron for createTray, recording every Tray it makes.
function trayElectron(trays = []) {
  return {
    app: { name: "Kiro Crew", getVersion: () => "0.8.0", getPath: () => "/virtual/logs" },
    Tray: class {
      constructor() { this.destroyed = false; trays.push(this); }
      setToolTip() {}
      setContextMenu() {}
      on() {}
      destroy() { this.destroyed = true; }
    },
    Menu: { buildFromTemplate: (template) => ({ template }) },
    nativeImage: {
      createFromPath: () => ({ resize: () => ({ fake: "icon" }), setTemplateImage() {} }),
    },
    shell: {},
  };
}

// ── Close button: background vs quit (#12204) ───────────────────────────────

describe("main window close action", () => {
  function closeHandlerBody() {
    const start = SOURCE.indexOf('mainWindow.on("close", createCloseHandler({');
    assert.notEqual(start, -1, "main window close handler must be built by createCloseHandler");
    return SOURCE.slice(start, SOURCE.indexOf("\n    }));\n", start));
  }

  it("wires the close listener to the store, the tray hide, requestQuit and persist", () => {
    // The sequence itself is exercised behaviourally in close-action.test.js;
    // this pins that the main window feeds it the real effects.
    const body = closeHandlerBody();
    assert.match(body, /getAction: \(\) => getCloseAction\(store\)/);
    assert.match(body, /cancelPendingTrayHide\(mainWindow\)/);
    assert.match(body, /^\s*requestQuit,$/m);
    assert.match(body, /hideToTray\(mainWindow, \{/);
    assert.match(body, /persist\(\);/);
  });

  function facadeHarness(stored = {}) {
    const data = { ...stored };
    const writes = [];
    const store = {
      get: (key) => data[key],
      set: (key, value) => { writes.push([key, value]); data[key] = value; },
    };
    // Setting the close action re-syncs the tray icon, so Tray must exist.
    const lifecycle = createWindowLifecycle(validOptions({ store, electron: trayElectron() }));
    return { lifecycle, data, writes };
  }

  it("the facade reads and writes the stored choice, canonicalized", () => {
    const h = facadeHarness();
    assert.equal(h.lifecycle.closeAction.get(), "background", "an untouched install keeps running");
    assert.equal(h.lifecycle.closeAction.set("quit"), "quit");
    assert.equal(h.data.closeAction, "quit");
    assert.equal(h.lifecycle.closeAction.get(), "quit");
    assert.equal(h.lifecycle.closeAction.set("nonsense"), "background");
    assert.equal(h.data.closeAction, "background");
  });

  it("the facade skips a write that changes nothing", () => {
    const h = facadeHarness({ closeAction: "quit" });
    h.lifecycle.closeAction.set("quit");
    assert.deepEqual(h.writes, []);
    h.lifecycle.closeAction.set("background");
    assert.deepEqual(h.writes, [["closeAction", "background"]]);
  });

  it("the tray menu is unchanged: the setting lives only in Settings", () => {
    const menus = [];
    const lifecycle = createWindowLifecycle(validOptions({
      electron: {
        app: { name: "Kiro Crew", getVersion: () => "0.8.0", getPath: () => "/virtual/logs" },
        Tray: class {
          setToolTip() {}
          setContextMenu() {}
          on() {}
        },
        Menu: { buildFromTemplate: (template) => { menus.push(template); return { template }; } },
        nativeImage: {
          createFromPath: () => ({ resize: () => ({ fake: "icon" }), setTemplateImage() {} }),
        },
        shell: {},
      },
      store: { path: "/virtual/config.json", get: () => undefined, set() {} },
      platform: "linux",
      env: {},
    }));
    lifecycle.syncTray();
    assert.deepEqual(
      menus[0].map((item) => item.label || item.type),
      ["Show Kiro Crew", "separator", "New Connection Window…", "separator", "Open Config File", "separator", "Quit"],
    );
  });
});

// ── Show tray icon (#21924) ─────────────────────────────────────────────────

describe("tray icon setting", () => {
  function trayHarness(stored = {}, platform = "linux") {
    const data = { ...stored };
    const trays = [];
    const lifecycle = createWindowLifecycle(validOptions({
      electron: trayElectron(trays),
      store: { path: "/virtual/config.json", get: (key) => data[key], set: (key, value) => { data[key] = value; } },
      platform,
      env: {},
    }));
    const live = () => trays.filter((tray) => !tray.destroyed).length;
    return { lifecycle, data, trays, live };
  }

  it("creates the icon at startup unless the user hid it while the close button quits", () => {
    const shown = trayHarness();
    shown.lifecycle.syncTray();
    assert.equal(shown.live(), 1);

    const hidden = trayHarness({ closeAction: "quit", showTrayIcon: false });
    hidden.lifecycle.syncTray();
    assert.equal(hidden.trays.length, 0, "no Tray is created");
  });

  it("hides and shows the icon live, and brings it back when the close button hides the window again", () => {
    const h = trayHarness({ closeAction: "quit" });
    h.lifecycle.syncTray();
    assert.deepEqual(h.lifecycle.trayIcon.set(false), { show: false, canHide: true });
    assert.equal(h.live(), 0);
    assert.equal(h.lifecycle.getTray(), null);

    h.lifecycle.closeAction.set("background");
    assert.equal(h.live(), 1, "the way back to a hidden window returns with the setting");
    assert.deepEqual(h.lifecycle.trayIcon.get(), { show: true, canHide: false });

    h.lifecycle.closeAction.set("quit");
    assert.equal(h.live(), 0, "the stored choice applies again once the close button quits");
    assert.equal(h.lifecycle.trayIcon.set(true).show, true);
    assert.equal(h.live(), 1);
    assert.equal(h.trays.length, 3);
  });

  it("refuses to hide the icon while the close button hides the window off macOS", () => {
    const h = trayHarness({ closeAction: "background" });
    h.lifecycle.syncTray();
    assert.deepEqual(h.lifecycle.trayIcon.set(false), { show: true, canHide: false });
    assert.equal(h.live(), 1);
    assert.equal(h.data.showTrayIcon, undefined, "nothing is written");
  });

  it("re-syncs the icon before the close button hides the window", () => {
    // The close action is read from the store at close time, so a hand-edited
    // switch to hide must bring the icon back before the window disappears.
    const hide = SOURCE.slice(SOURCE.indexOf("close: hiding to tray"));
    const sync = hide.indexOf("syncTray();");
    assert.ok(sync !== -1 && sync < hide.indexOf("hideToTray(mainWindow"),
      "the close handler's hide path calls syncTray() before hideToTray()");
  });

  it("hides the icon on macOS whatever the close button does", () => {
    const h = trayHarness({ closeAction: "background" }, "darwin");
    h.lifecycle.syncTray();
    assert.deepEqual(h.lifecycle.trayIcon.set(false), { show: false, canHide: true });
    assert.equal(h.live(), 0);
  });

  it("offers minimize to tray on Windows while the icon shows, and never on macOS", async () => {
    const h = trayHarness({ closeAction: "quit" }, "win32");
    h.lifecycle.syncTray();
    assert.deepEqual(await h.lifecycle.minimizeToTray.set(true), { on: true, offered: true, canEnable: true });
    h.lifecycle.trayIcon.set(false);
    assert.deepEqual(await h.lifecycle.minimizeToTray.get(), { on: false, offered: true, canEnable: false });
    assert.equal(h.data.minimizeToTray, true, "the choice returns with the icon");
    assert.equal((await trayHarness({}, "darwin").lifecycle.minimizeToTray.get()).offered, false);
  });

  it("wires the main window's minimize to the tray hide, reading the live icon", () => {
    assert.match(SOURCE, /mainWindow\.on\("minimize", minimizeToTray\.minimizeHandler\(mainWindow\)\);/);
    assert.match(SOURCE, /createMinimizeToTray\(\{ store, platform, trayShown: \(\) => tray !== null, log: glog \}\)/);
  });
});
