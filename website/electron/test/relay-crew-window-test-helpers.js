// Shared by relay-crew-window.test.js and relay-crew-pane-header.test.js. (Not a
// *.test.js file, so `node --test test/*.test.js` does not run it.)
//
// The fake Electron the relay Crew window and pane are built on (a session, a
// BaseWindow, a WebContentsView), the one Crew every test opens, and the pane
// opener.

"use strict";

const { EventEmitter } = require("node:events");
const { openRelayCrewPane, PANE_HEADER_READ } = require("../relay-crew-window");

const ENDPOINT = "abcdefghij0123456789";
const DOMAIN = "relay.example.test";
const CREW_URL = `https://${ENDPOINT}.${DOMAIN}`;
const ORIGIN = CREW_URL;
const TOKEN = "tok-SECRET-0123456789abcdef";

function fakeSession() {
  const ses = {
    partitions: [],
    headerRule: null,
    completedRule: null,
    errorRule: null,
    permissionRequest: null,
    permissionCheck: null,
    cleared: 0,
    webRequest: {
      onBeforeSendHeaders(...args) {
        const listener = args[args.length - 1];
        ses.headerRule = listener === null ? null : { args, listener };
      },
      onCompleted(filter, listener) {
        ses.completedRule = filter === null ? null : { filter, listener };
      },
      onErrorOccurred(filter, listener) {
        ses.errorRule = filter === null ? null : { filter, listener };
      },
    },
    userAgent: "Mozilla/5.0 Electron/44",
    getUserAgent() { return ses.userAgent; },
    setUserAgent(ua) { ses.userAgent = ua; },
    setPermissionRequestHandler(fn) { ses.permissionRequest = fn; },
    setPermissionCheckHandler(fn) { ses.permissionCheck = fn; },
    clearStorageData() { ses.cleared += 1; return Promise.resolve(); },
    clearCache() { return Promise.resolve(); },
  };
  return ses;
}

function fakeDeps() {
  const ses = fakeSession();
  const logs = [];
  const opened = [];
  const views = [];
  const windows = [];
  class WebContentsView {
    constructor(options) {
      this.options = options;
      this.webContents = new EventEmitter();
      this.webContents.loaded = [];
      this.webContents.reloads = 0;
      this.webContents.loadURL = (url) => { this.webContents.loaded.push(url); return Promise.resolve(); };
      this.webContents.files = [];
      this.webContents.loadFile = (file, opts) => { this.webContents.files.push({ file, opts }); return Promise.resolve(); };
      this.webContents.closed = false;
      this.webContents.ipc = new EventEmitter();
      this.webContents.reload = () => { this.webContents.reloads += 1; };
      this.webContents.close = () => { this.webContents.closed = true; };
      this.webContents.setWindowOpenHandler = (fn) => { this.webContents.openHandler = fn; };
      this.webContents.setUserAgent = (ua) => { this.webContents.userAgent = ua; };
      this.webContents.hasFocus = false;
      this.webContents.isFocused = () => this.webContents.hasFocus;
      this.webContents.focus = () => { this.webContents.hasFocus = true; };
      this.webContents.isDestroyed = () => this.webContents.closed;
      // What the page answers the root probe: "" until its root has text and its theme is on.
      this.webContents.rootDrawn = "";
      this.webContents.probes = 0;
      // What the page answers the header read (PANE_HEADER_READ), counted apart from the probes.
      this.webContents.headerAnswer = false;
      this.webContents.headerReads = 0;
      this.webContents.executeJavaScript = (src) => {
        if (src === PANE_HEADER_READ) { this.webContents.headerReads += 1; return Promise.resolve(this.webContents.headerAnswer); }
        this.webContents.probes += 1;
        return Promise.resolve(this.webContents.rootDrawn);
      };
      // `visible` is what the person sees: a view put under the dashboard (index 0) is
      // on screen for its compositor only.
      this.set = true;
      this.under = false;
      Object.defineProperty(this, "visible", { get: () => this.set && !this.under });
      this.setVisible = (v) => { this.set = v; };
      this.setBounds = (b) => { this.bounds = b; };
      views.push(this);
    }
  }
  class BaseWindow extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.destroyed = false;
      this.focused = 0;
      this.minimized = false;
      this.calls = [];
      this.children = [];
      this.contentView = {
        addChildView: (v, index) => {
          this.child = v;
          this.children = this.children.filter((c) => c !== v);
          if (index === 0) this.children.unshift(v); else this.children.push(v);
          v.under = index === 0;
        },
        removeChildView: (v) => { this.children = this.children.filter((c) => c !== v); },
      };
      windows.push(this);
    }
    isDestroyed() { return this.destroyed; }
    isFocused() { return this.windowFocused !== false; }
    getContentBounds() { return { width: 1000, height: 700 }; }
    isMinimized() { return this.minimized; }
    restore() { this.calls.push("restore"); this.minimized = false; this.emit("restore"); }
    show() { this.calls.push("show"); this.emit("show"); }
    focus() { this.calls.push("focus"); this.focused += 1; }
    close() { this.destroyed = true; this.emit("closed"); }
  }
  return {
    ses, logs, opened, views, windows,
    deps: {
      WebContentsView,
      BaseWindow,
      session: { fromPartition: (name) => { ses.partitions.push(name); return ses; } },
      shell: { openExternal: (url) => { opened.push(url); return Promise.resolve(); } },
      log: (m) => logs.push(m),
    },
  };
}

const PANE_RECT = { x: 0, y: 42, width: 1000, height: 658 };

function openPane(f, extra = {}) {
  const parent = extra.parent || new f.deps.BaseWindow({});
  const dashboard = extra.dashboard || {
    webContents: Object.assign(new EventEmitter(), { focused: 0, focus() { this.focused += 1; }, isDestroyed: () => false }),
  };
  const handle = openRelayCrewPane({
    endpointId: ENDPOINT, crewUrl: CREW_URL, relayDomain: DOMAIN, getToken: () => TOKEN,
    parent, dashboard, bounds: PANE_RECT, platform: "linux",
    // What a dashboard that keeps to its tab strip above the pane asks for (#42922).
    ownHeader: extra.ownHeader,
  }, f.deps);
  const view = f.views[f.views.length - 1];
  // Past the load gate, so the pane may show (a failed load is the one synchronous way
  // through it); `unloaded` keeps it before that.
  if (!extra.unloaded) view.webContents.emit("did-fail-load");
  return { parent, dashboard, handle, view };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

module.exports = { ENDPOINT, DOMAIN, CREW_URL, ORIGIN, TOKEN, PANE_RECT, fakeSession, fakeDeps, openPane, flush };
