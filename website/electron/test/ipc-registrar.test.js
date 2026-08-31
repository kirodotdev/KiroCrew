"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
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
  "global-hotkey:get",
  "local-gateway:get",
  "local-gateway:set",
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
  detectWsl = async () => ({
    available: true,
    distros: [],
    defaultDistro: null,
  }),
} = {}) {
  const order = [];
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
  assert.equal(h.handlers.size + h.listeners.size, 30);

  // boot-complete is the 31st non-update host channel, but it is deliberately
  // gateway-owned and scoped to a single connecting WebContents. Registering it
  // globally here would weaken its sender check and leak listeners.
  assert.match(GATEWAY_SOURCE, /ipcMain\.on\("boot-complete", onComplete\)/);
  assert.equal(h.handlers.size + h.listeners.size + 1, 31);
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
