"use strict";

// The fake Electron window that the window-lifecycle tests drive. Shared so a
// test file other than window-lifecycle.test.js can wire a real dashboard
// window through createWindowLifecycle without copying it.

const { createWindowLifecycle } = require("../window-lifecycle");
const { validOptions } = require("./window-lifecycle-test-helpers");

function recordingEmitter(label, log) {
  const handlers = new Map();
  return {
    handlers,
    on(event, handler) {
      log.push(`${label}.on:${event}`);
      if (!handlers.has(event)) handlers.set(event, []);
      handlers.get(event).push(handler);
      return this;
    },
    emit(event, ...args) {
      for (const handler of handlers.get(event) || []) handler(...args);
    },
  };
}

function dashboardWindowHarness({ platform, frameless = false, remoteHosts = null, downloadExpectations = null, dialogResponse = 1, dialogChecked = false, storedFontSize = null } = {}) {
  const log = [];
  const prompts = [];
  const sessionListeners = new Map();
  const ipcListeners = new Map();
  const firstLine = (text) => String(text).split("\n").map((line) => line.trim()).find(Boolean);
  const viewEvents = recordingEmitter("view", log);
  const viewContents = {
    ...viewEvents,
    ipc: { on(channel, listener) { ipcListeners.set(channel, listener); } },
    // A real dashboard webContents has one, and the content text size re-apply
    // on did-finish-load uses it. Without it the re-apply fails and defers,
    // which binds a focus listener and puts an entry in the wiring log that
    // production never produces. Not logged: this models an existing property
    // of the object rather than a wiring step.
    debugger: {
      isAttached: () => false,
      attach() {},
      detach() {},
      sendCommand: async () => {},
    },
    isDestroyed: () => false,
    mainFrame: { url: "http://localhost:5476/" },
    session: {
      webRequest: {
        onBeforeSendHeaders() { log.push("view.session.onBeforeSendHeaders"); },
      },
      on(event, listener) {
        log.push(`view.session.on:${event}`);
        sessionListeners.set(event, listener);
        return this;
      },
    },
    setWindowOpenHandler(handler) {
      log.push("view.setWindowOpenHandler");
      this.windowOpenHandler = handler;
    },
    insertCSS(css) { log.push(`view.insertCSS:${firstLine(css)}`); return Promise.resolve(); },
    executeJavaScript(script) {
      log.push(`view.executeJavaScript:${firstLine(script)}`);
      return Promise.resolve("");
    },
    send(channel) { log.push(`view.send:${channel}`); },
    getZoomFactor: () => 1,
    isDestroyed: () => false,
    focus() {},
    close() { log.push("view.close"); },
  };
  const winEvents = recordingEmitter("win", log);
  const win = {
    ...winEvents,
    contentView: {
      addChildView() { log.push("win.addChildView"); },
      removeChildView() {},
    },
    isDestroyed: () => false,
    isFullScreen: () => false,
    isMaximized: () => false,
    getContentBounds: () => ({ x: 0, y: 0, width: 1280, height: 860 }),
    setTitle(title) { log.push(`win.setTitle:${title}`); },
    setBackgroundColor() {},
    setWindowButtonPosition(position) { log.push(`win.setWindowButtonPosition:${JSON.stringify(position)}`); },
    setTitleBarOverlay() { log.push("win.setTitleBarOverlay"); },
  };
  const panelViews = [];
  const dashboardPrefs = [];
  const openedExternally = [];
  class WebContentsView {
    constructor(options) {
      if (options.webPreferences.partition) {
        // An embedded browser panel: its own page, never the dashboard's.
        panelViews.push(options);
        this.webContents = {
          ...recordingEmitter("panel", []),
          loads: [],
          loadURL(url) { this.loads.push(url); return Promise.resolve(); },
          setWindowOpenHandler() {},
          isDestroyed: () => false,
          getTitle: () => "",
          getURL: () => "",
          focus() {},
          close() {},
        };
        return;
      }
      log.push(`new WebContentsView:${JSON.stringify(options.webPreferences.additionalArguments || [])}`);
      // Whole options, so a construction-time preference can be asserted. The
      // log line above carries only additionalArguments on purpose: it is
      // compared literally by the wiring tests.
      dashboardPrefs.push(options.webPreferences);
      this.webContents = viewContents;
    }
    setBackgroundColor() {}
    setBounds() {}
    setVisible() {}
  }
  const lifecycle = createWindowLifecycle(validOptions({
    electron: {
      WebContentsView,
      BaseWindow: { getAllWindows: () => [win], getFocusedWindow: () => null },
      Menu: { buildFromTemplate: () => ({ popup() {} }) },
      shell: { openExternal(url) { openedExternally.push(url); return Promise.resolve(); } },
      dialog: {
        async showMessageBox(parent, options) {
          prompts.push({ parent, options });
          return { response: dialogResponse, checkboxChecked: dialogChecked };
        },
      },
      nativeTheme: { shouldUseDarkColors: false, themeSource: "system" },
      app: { getPath: () => "/virtual/logs", getVersion: () => "0.8.0", name: "Kiro Crew" },
    },
    store: { get: (key) => (key === "linuxFrameless" ? frameless : key === "remoteHosts" ? remoteHosts : key === "displayPreferences.fontSize" ? storedFontSize : null) },
    platform,
    env: {},
    downloadExpectations,
  }));
  return { lifecycle, win, viewContents, log, panelViews, dashboardPrefs, openedExternally, prompts, sessionListeners, ipcListeners };
}

async function wireDashboardWindow(t, options) {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  t.mock.method(globalThis, "fetch", async () => ({ ok: true, status: 204, json: async () => null }));
  const harness = dashboardWindowHarness(options);
  harness.lifecycle.setupWindowContents(harness.win, "http://localhost:5476");
  const wiring = harness.log.splice(0);
  // The preload asks before any load event fires, so read what exists by then.
  const ipcBeforeLoad = new Map(harness.ipcListeners);
  harness.viewContents.emit("did-finish-load");
  const onLoad = harness.log.splice(0);
  return { ...harness, wiring, onLoad, ipcBeforeLoad };
}

module.exports = { recordingEmitter, dashboardWindowHarness, wireDashboardWindow };
