"use strict";

// Shared fixtures for the window-lifecycle suites: construction options and a
// fake dashboard window that records what the lifecycle wires onto it.

const { createWindowLifecycle } = require("../window-lifecycle");

function validOptions(overrides = {}) {
  return {
    electron: {},
    store: { get: () => null },
    backendUrl: "http://localhost:5476",
    port: 5476,
    mintLocalToken: async () => "",
    fetchRemoteToken: async () => ({ token: "" }),
    requestQuit: () => {},
    connectWindow: async () => {},
    // Keep construction independent of the host running the suite.
    platform: "test",
    ...overrides,
  };
}

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

function dashboardWindowHarness({
  platform,
  frameless = false,
  downloadExpectations = null,
  // A `remoteHosts` store record, so a harness window can reach a CONFIGURED
  // crew through a loopback tunnel -- the shape of the issue-#14815 client mode.
  remoteHosts = null,
  readInternalSecret,
} = {}) {
  const log = [];
  const sessionListeners = new Map();
  const firstLine = (text) => String(text).split("\n").map((line) => line.trim()).find(Boolean);
  const viewEvents = recordingEmitter("view", log);
  const viewContents = {
    ...viewEvents,
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
      nativeTheme: { shouldUseDarkColors: false, themeSource: "system" },
      app: { getPath: () => "/virtual/logs", getVersion: () => "0.8.0", name: "Kiro Crew" },
    },
    store: {
      get: (key) => {
        if (key === "linuxFrameless") return frameless;
        if (key === "remoteHosts") return remoteHosts;
        return null;
      },
    },
    ...(readInternalSecret ? { readInternalSecret } : {}),
    platform,
    env: {},
    downloadExpectations,
  }));
  return { lifecycle, win, viewContents, log, panelViews, openedExternally, sessionListeners };
}

module.exports = { dashboardWindowHarness, recordingEmitter, validOptions };
