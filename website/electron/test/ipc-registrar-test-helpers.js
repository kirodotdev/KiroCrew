// Shared by ipc-registrar.test.js and its ipc-registrar.*.test.js parts. (Not a
// *.test.js file, so `node --test test/*.test.js` does not run it.)
//
// Holds the registrar load/harness scaffolding every ipc-registrar suite needs:
// the module sources read once at load, the channel inventories, the fake store
// and hotkey stubs, loadRegistrar, harness, and the small per-channel helpers.

"use strict";

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
  "app-menu:execute",
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
  "close-action:get",
  "close-action:set",
  "crash-reports:get",
  "crash-reports:reveal",
  "dashboard:can-pick-folder",
  "dashboard:open-file",
  "dashboard:pick-folder",
  "external-terminal:detect",
  "external-terminal:open",
  "first-run:can-use-remote-crew",
  "first-run:use-remote-crew",
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
  "badge:set",
  "dev-mode-changed",
  "focus-mode-chrome",
  "focus-mode-watch-cursor",
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
  isCurrentBundleGateway = true,
  // What the supervisor answers for first-run setup's remote-crew exit.
  canSwitchToRemoteCrew = true,
  switchOutcome = "switching",
  crashScan,
  revealThrows = false,
  openPathResult = "",
  openPathThrows = false,
  // Called synchronously INSIDE the openPath mock, so a test can observe
  // process state that only holds for the duration of the native launch (the
  // narrowed launcher PATH openPathHardened installs and then restores).
  onOpenPath = null,
  // What the fake native folder dialog answers: a value, or a function of the
  // call's arguments so a test can hold the dialog open.
  dialogResult = { canceled: true, filePaths: [] },
  externalTerminal = undefined,
  internalSecret = "",
  getRequiredChannel = undefined,
  detectWsl = async () => ({
    available: true,
    distros: [],
    defaultDistro: null,
  }),
} = {}) {
  const order = [];
  const shellCalls = [];
  const crashScans = [];
  const secretReads = [];
  const windowCalls = [];
  const gatewayCalls = [];
  const appCalls = [];
  const notifications = [];
  const sentUpdates = [];
  const wslDetections = [];
  const dialogCalls = [];
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
    dialog: {
      fake: "dialog",
      showOpenDialog: async (...args) => {
        dialogCalls.push(args);
        return typeof dialogResult === "function" ? dialogResult(...args) : dialogResult;
      },
    },
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
      watchFocusCursor: recordWindow("chrome.watchFocusCursor"),
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
    closeAction: {
      get: recordWindow("closeAction.get", "background"),
      set: (...args) => {
        windowCalls.push(["closeAction.set", ...args]);
        return args[0] === "quit" ? "quit" : "background";
      },
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
    stopForUpdate: recordGateway("stopForUpdate", Promise.resolve("stopped")),
    onInstallDispatched: recordGateway("onInstallDispatched"),
    onInstallFailed: recordGateway("onInstallFailed"),
    probePrimaryPortOwner: recordGateway(
      "probePrimaryPortOwner",
      Promise.resolve(primaryPortOwner),
    ),
    isCurrentBundleGateway: recordGateway(
      "isCurrentBundleGateway",
      Promise.resolve(isCurrentBundleGateway),
    ),
    canSwitchToRemoteCrew: recordGateway("canSwitchToRemoteCrew", canSwitchToRemoteCrew),
    switchToRemoteCrew: recordGateway("switchToRemoteCrew", Promise.resolve(switchOutcome)),
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
    ...(externalTerminal ? { externalTerminal } : {}),
    ...(getRequiredChannel ? { getRequiredChannel } : {}),
    // Like main.js's readInternalSecret: the secret belongs to one gateway
    // URL, so a read that names no URL gets nothing.
    readInternalSecret: (gatewayUrl) => {
      secretReads.push(gatewayUrl);
      return gatewayUrl === backendUrl ? internalSecret : "";
    },
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
    secretReads,
    dialogCalls,
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

module.exports = {
  ROOT,
  MODULE_PATH,
  SOURCE,
  MAIN_SOURCE,
  GATEWAY_SOURCE,
  SHELL_HANDLES,
  SHELL_LISTENERS,
  UPDATE_HANDLES,
  fakeStore,
  makeHotkeyStub,
  loadRegistrar,
  harness,
  updaterHandle,
  lastCall,
  WSL_RESTRICTED_ERROR,
  wslEvent,
  assertWslRejected,
};
