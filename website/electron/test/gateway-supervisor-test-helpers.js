"use strict";

// Fakes and harnesses shared by the gateway-supervisor*.test.js files.
// (Not a *.test.js file, so `node --test test/*.test.js` does not run it.)

const { EventEmitter } = require("node:events");
const path = require("node:path");
const assert = require("node:assert");

const MODULE_PATH = path.join(__dirname, "..", "gateway-supervisor.js");
const { createGatewaySupervisor } = require(MODULE_PATH);

function fakeStore(initial = {}) {
  const data = { ...initial };
  return {
    data,
    get(key, fallback) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : fallback;
    },
    set(key, value) { data[key] = value; },
  };
}

function rejectingHttp(onGet = () => {}) {
  return {
    get(url) {
      onGet(url);
      const request = new EventEmitter();
      request.destroy = () => {};
      // Defer until production has attached its error listener. No socket, port,
      // timer, or host input is involved.
      queueMicrotask(() => request.emit("error", new Error("connection refused")));
      return request;
    },
  };
}

// An http fake whose answer can change mid-test: `state.status` null refuses the
// connection, a number answers with that status and `state.body`. Requests are
// recorded so a test can prove which endpoint was probed.
function switchableHttp(state) {
  const requests = [];
  return {
    requests,
    get(url, _options, callback) {
      requests.push(url);
      const request = new EventEmitter();
      request.destroy = () => {};
      queueMicrotask(() => {
        if (state.status === null) {
          request.emit("error", new Error("connection refused"));
          return;
        }
        const response = new EventEmitter();
        response.statusCode = state.status;
        response.resume = () => {};
        callback(response);
        response.emit("data", state.body || "");
        response.emit("end");
      });
      return request;
    },
  };
}

// Timers the supervisor schedules, held instead of run so a test can fire the
// one it means (by delay) or prove none is left armed.
function fakeTimers() {
  const pending = [];
  // Intervals stay armed across ticks, so they live apart from one-shots:
  // `fire` must never consume one, and a test can prove one was cleared.
  const intervals = [];
  let nextId = 1;
  return {
    pending,
    intervals,
    setTimeoutFn(fn, ms) {
      const id = nextId;
      nextId += 1;
      pending.push({ id, fn, ms });
      return id;
    },
    clearTimeoutFn(id) {
      const index = pending.findIndex((timer) => timer.id === id);
      if (index >= 0) pending.splice(index, 1);
    },
    setIntervalFn(fn, ms) {
      const id = nextId;
      nextId += 1;
      intervals.push({ id, fn, ms });
      return id;
    },
    clearIntervalFn(id) {
      const index = intervals.findIndex((timer) => timer.id === id);
      if (index >= 0) intervals.splice(index, 1);
    },
    fire(ms) {
      const index = pending.findIndex((timer) => timer.ms === ms);
      assert.ok(index >= 0, `a ${ms}ms timer is armed`);
      const [timer] = pending.splice(index, 1);
      timer.fn();
    },
    tick(ms) {
      const interval = intervals.find((timer) => timer.ms === ms);
      assert.ok(interval, `a ${ms}ms interval is armed`);
      interval.fn();
    },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function harness(overrides = {}) {
  const logs = [];
  const warnings = [];
  const errors = [];
  const spawnCalls = [];
  const store = overrides.store || fakeStore();
  const mainWindow = overrides.mainWindow || null;
  const port = overrides.port ?? 5476;
  const processRef = overrides.processRef || {
    platform: "test",
    arch: "x64",
    env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
    resourcesPath: "/virtual/resources",
    kill() { throw new Error("process kill must not run in this harness"); },
  };
  const fsMod = overrides.fsMod || {
    constants: { X_OK: 1 },
    mkdirSync() {},
    accessSync() {
      const error = new Error("not found");
      error.code = "ENOENT";
      throw error;
    },
    existsSync() { return false; },
    openSync() { return 41; },
    closeSync() {},
    readFileSync() { throw new Error("unexpected filesystem read"); },
  };

  const supervisor = createGatewaySupervisor({
    app: {
      isPackaged: false,
      getVersion: () => "0.6.0",
      quit: () => {},
      focus: () => {},
      ...(overrides.app || {}),
    },
    store,
    BrowserWindow: overrides.BrowserWindow || class {},
    nativeTheme: { shouldUseDarkColors: false },
    dialog: overrides.dialog
      || { showMessageBox: async () => ({ response: 1 }) },
    shell: { showItemInFolder: () => {} },
    ipcMain: { on: () => {}, removeListener: () => {} },
    port,
    backendUrl: overrides.backendUrl || `http://localhost:${port}`,
    home: "/virtual/kirocrew-home",
    getMainWindow: () => mainWindow,
    isQuitting: overrides.isQuitting || (() => false),
    requestQuit: overrides.requestQuit || (() => {}),
    cancelPendingTrayHide: () => {},
    exitImmersiveModes: () => {},
    log: (message) => logs.push(message),
    warn: (message) => {
      logs.push(message);
      warnings.push(message);
    },
    error: (message) => {
      logs.push(message);
      errors.push(message);
    },
    logPath: () => "/virtual/logs/gateway-launch.log",
    predictLocalPort: overrides.predictLocalPort,
    fsMod,
    osMod: { homedir: () => "/virtual/home" },
    pathMod: path.posix,
    httpMod: overrides.httpMod || rejectingHttp(),
    spawnFn: (...args) => {
      const child = new EventEmitter();
      child.pid = 1234;
      child.exitCode = null;
      child.killed = false;
      child.kill = () => { child.killed = true; };
      child.unref = () => { child.unrefed = true; };
      const call = [...args];
      call.child = child;
      spawnCalls.push(call);
      return child;
    },
    execFileFn: overrides.execFileFn
      || (() => { throw new Error("execFile must not run in this harness"); }),
    execFileSyncFn: overrides.execFileSyncFn
      || (() => { throw new Error("execFileSync must not run in this harness"); }),
    setTimeoutFn: overrides.timers ? overrides.timers.setTimeoutFn : undefined,
    clearTimeoutFn: overrides.timers ? overrides.timers.clearTimeoutFn : undefined,
    setIntervalFn: overrides.timers ? overrides.timers.setIntervalFn : undefined,
    clearIntervalFn: overrides.timers ? overrides.timers.clearIntervalFn : undefined,
    processRef,
    dirname: "/virtual/electron",
  });

  return { supervisor, store, logs, warnings, errors, spawnCalls, fsMod };
}

const APP_EXEC_PATH = "/virtual/Applications/KiroCrew.app/Contents/MacOS/KiroCrew";
const APP_ARGV = [APP_EXEC_PATH, "--some-flag"];

// The successor spawn the supervisor issues when it decides to restart the app.
function successorCall(spawnCalls) {
  return spawnCalls.find((call) => call[0] === APP_EXEC_PATH);
}

const SUCCESSOR_POLL_MS = 500;

// Drives the LISTEN-owner probe to a chosen verdict. `command` is what ps reports
// for the pid holding the port, which is the only thing that separates our own
// gateway from a tunnel occupying the same number.
function ownerProbe(command, { listening = true, ppid = 500 } = {}) {
  return (file, args, _options, callback) => {
    if (String(file).endsWith("lsof")) {
      callback(null, listening ? "4242\n" : "", "");
      return;
    }
    if (file === "/bin/ps") {
      callback(null, args.includes("ppid=") ? `${ppid}\n` : `${command}\n`, "");
      return;
    }
    callback(new Error(`unexpected command: ${file}`));
  };
}

// A bare selector, which is how classifyPortOwner recognises our own gateway
// without an absolute-path install to bind against. The trust rule itself is
// unchanged and exercised by gateway-stop's own suite.
const OWN_GATEWAY_COMMAND = "kirocrew gateway --port 5476";
const FOREIGN_COMMAND = "ssh -L 5476:localhost:5476 build-host";

const INSTALLING_PROBE_MS = 5000;
const INSTALLING_COMPLETE_LINGER_MS = 700;

// The error dialog is an ordinary BrowserWindow. This stand-in records what
// the supervisor paints into it and lets a test act as the user (click) or as
// the probe's own close(), both of which end in the `closed` handshake.
function fakeDialogWindows() {
  const windows = [];
  class FakeBrowserWindow {
    constructor(options) {
      this.options = options;
      this.handlers = {};
      this.destroyed = false;
      this.loaded = [];
      this.scripts = [];
      this.webContents = {
        executeJavaScript: (js) => {
          this.scripts.push(js);
          return Promise.resolve();
        },
      };
      windows.push(this);
    }
    setMenu() {}
    on(event, fn) {
      (this.handlers[event] ||= []).push(fn);
    }
    emit(event, ...args) {
      for (const fn of this.handlers[event] || []) fn(...args);
    }
    loadURL(url) { this.loaded.push(url); }
    isDestroyed() { return this.destroyed; }
    close() {
      if (this.destroyed) return;
      this.destroyed = true;
      this.emit("closed");
    }
    // What the page's act() does: set the title, then close the window.
    click(action) {
      this.emit("page-title-updated", {}, `mc-action:${action}`);
      this.close();
    }
    // The message text the probe last painted (the initial one is in the URL).
    // The repaint writes escaped <p> markup; strip it back to the text in one
    // pass, so an entity is never unescaped twice.
    paintedMessages() {
      const entities = { "&amp;": "&", "&lt;": "<", "&gt;": ">" };
      return this.scripts
        .filter((js) => js.startsWith('document.querySelector(".msg")'))
        .map((js) => JSON.parse(js.slice(js.indexOf("= ") + 2))
          .replace(/<\/?p>/g, "")
          .replace(/&(?:amp|lt|gt);/g, (entity) => entities[entity]));
    }
    // The completion frame's chrome repaint (title + buttons), if it happened.
    completionChrome() {
      return this.scripts.find((js) => js.startsWith('document.querySelector(".title")')) || "";
    }
  }
  return { windows, FakeBrowserWindow };
}

module.exports = {
  MODULE_PATH,
  fakeStore,
  rejectingHttp,
  switchableHttp,
  fakeTimers,
  flush,
  harness,
  APP_EXEC_PATH,
  APP_ARGV,
  successorCall,
  SUCCESSOR_POLL_MS,
  ownerProbe,
  OWN_GATEWAY_COMMAND,
  FOREIGN_COMMAND,
  INSTALLING_PROBE_MS,
  INSTALLING_COMPLETE_LINGER_MS,
  fakeDialogWindows,
};
