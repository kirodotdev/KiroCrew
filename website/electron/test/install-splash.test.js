"use strict";
//
// The window a start-time update apply shows while its command runs
// (runtime/update/install-splash.js). It opens before the main window, the tray
// and the IPC bridges exist, so it carries no preload, and closing it must never
// quit the app or cancel the update command.

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const vm = require("node:vm");

const { createInstallSplash } = require("../runtime/update/install-splash");

const PAGE = path.join(__dirname, "..", "loading.html");
const RAISED = "[update] a launch arrived during the start-time apply";

function fakeBrowserWindow() {
  const created = [];
  class FakeWindow extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.calls = [];
      this.destroyed = false;
      this.minimized = false;
      created.push(this);
    }
    removeMenu() { this.calls.push("removeMenu"); }
    loadFile(file, opts) { this.loaded = { file, opts }; }
    hide() { this.calls.push("hide"); }
    show() { this.calls.push("show"); }
    focus() { this.calls.push("focus"); }
    restore() { this.calls.push("restore"); this.minimized = false; }
    isMinimized() { return this.minimized; }
    isDestroyed() { return this.destroyed; }
    destroy() { this.calls.push("destroy"); this.destroyed = true; }
    // What Electron does on a user close: emit `close`, then close unless prevented.
    userClose() {
      let prevented = false;
      this.emit("close", { preventDefault: () => { prevented = true; } });
      return prevented;
    }
  }
  return { FakeWindow, created };
}

function splashFixture() {
  const { FakeWindow, created } = fakeBrowserWindow();
  const logs = [];
  const splash = createInstallSplash({ BrowserWindow: FakeWindow, page: PAGE, log: (m) => logs.push(m) });
  return { splash, created, logs };
}

test("show opens one window with no preload that loads loading.html with the version's status", () => {
  const f = splashFixture();
  f.splash.show("2.0.0");
  assert.equal(f.created.length, 1);
  const [win] = f.created;
  assert.equal(win.options.title, "Kiro Crew");
  assert.equal(win.options.resizable, false);
  assert.equal(win.options.webPreferences.preload, undefined, "preload.js asks a WebContents that does not exist yet");
  assert.deepEqual(win.options.webPreferences, { contextIsolation: true, nodeIntegration: false, sandbox: true });
  assert.ok(win.calls.includes("removeMenu"));
  assert.equal(win.loaded.file, PAGE);
  assert.deepEqual(win.loaded.opts, { query: { status: "Installing update 2.0.0. Kiro Crew opens when it is done." } });
});

test("a close during the apply only hides the window; after the take-down it closes", () => {
  const f = splashFixture();
  const takeDown = f.splash.show("2.0.0");
  const [win] = f.created;
  assert.equal(win.userClose(), true, "prevented while the command runs");
  assert.deepEqual(win.calls.slice(-1), ["hide"]);
  const before = win.calls.length;
  takeDown();
  assert.deepEqual(win.calls.slice(before), ["hide"], "the take-down hides the window");
  assert.equal(win.userClose(), false, "no longer prevented");
});

test("a close that is part of an app quit goes through, even during the apply", () => {
  // before-quit has already run its teardown by the time the windows close, so
  // a prevented close here would cancel the quit with the app half stopped.
  const { FakeWindow, created } = fakeBrowserWindow();
  let quitting = false;
  const splash = createInstallSplash({ BrowserWindow: FakeWindow, page: PAGE, log: () => {}, isQuitting: () => quitting });
  splash.show("2.0.0");
  quitting = true;
  assert.equal(created[0].userClose(), false);
  assert.deepEqual(created[0].calls.filter((c) => c === "hide"), []);
});

test("main.js wires the quit flag and raises the window on a macOS reopen as well as a second launch", () => {
  const main = require("node:fs").readFileSync(path.join(__dirname, "..", "main.js"), "utf8").replace(/\r\n/g, "\n");
  const factory = main.match(/const installSplash = createInstallSplash\(\{([\s\S]*?)\}\);/);
  assert.ok(factory, "could not locate the install window factory");
  assert.match(factory[1], /isQuitting: \(\) => isQuitting/);
  // A running app reopened from the Dock or Finder gets `activate`, not
  // `second-instance`, and the boot's own activate handler comes after the apply.
  const reopen = main.indexOf('app.on("activate", () => installSplash.raise());');
  assert.ok(reopen !== -1, "the reopen must raise the install window");
  assert.ok(reopen < main.indexOf("app.whenReady().then("), "registered before the apply can run");
  const relaunch = main.match(/app\.on\("second-instance", \([^)]*\) => \{([\s\S]*?)\n {2}\}\);/);
  assert.ok(relaunch, "could not locate the second-instance handler");
  assert.match(relaunch[1], /^\s*(\/\/.*\n\s*)*installSplash\.raise\(\);/, "a second launch raises the install window first");
});

test("raise brings the window forward and logs one line only while the apply runs", () => {
  const f = splashFixture();
  assert.equal(f.splash.raise(), false, "before show");
  assert.deepEqual(f.logs, []);
  const takeDown = f.splash.show("2.0.0");
  const [win] = f.created;
  win.minimized = true;
  assert.equal(f.splash.raise(), true);
  assert.deepEqual(win.calls.slice(-3), ["restore", "show", "focus"]);
  assert.deepEqual(f.logs, [RAISED]);
  takeDown();
  assert.equal(f.splash.raise(), false, "after the take-down");
  assert.deepEqual(f.logs, [RAISED]);
});

test("close destroys the window and is idempotent", () => {
  const f = splashFixture();
  f.splash.close();
  f.splash.show("2.0.0")();
  f.splash.close();
  f.splash.close();
  assert.deepEqual(f.created[0].calls.filter((c) => c === "destroy"), ["destroy"]);
});

test("a window that cannot be created is logged once and the apply still gets a take-down", () => {
  const logs = [];
  const splash = createInstallSplash({
    BrowserWindow: class { constructor() { throw new Error("no display"); } },
    page: PAGE,
    log: (m) => logs.push(m),
  });
  const takeDown = splash.show("2.0.0");
  assert.equal(typeof takeDown, "function");
  assert.equal(logs.length, 1);
  assert.match(logs[0], /no display/);
  takeDown();
  splash.close();
});

// loading.html's script run whole in a vm realm with no Electron bridge, on a
// virtual clock the test advances, so the preview's auto-ready delay passes
// without real time.
function runSplashPage(search) {
  const html = require("node:fs").readFileSync(PAGE, "utf8");
  const open = html.indexOf("<script>") + "<script>".length;
  const script = html.slice(open, html.indexOf("</script>", open));
  let now = 0;
  const timers = [];
  const element = () => ({
    style: { setProperty() {} },
    classList: { added: new Set(), add(...c) { c.forEach((x) => this.added.add(x)); }, remove() {} },
    addEventListener() {},
    querySelector: () => element(),
    appendChild() {},
    setAttribute() {},
    hidden: false,
    textContent: "",
  });
  const elements = new Map();
  const byId = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const context = {
    document: { documentElement: element(), getElementById: byId, createElement: () => element() },
    location: { search },
    URLSearchParams,
    URL,
    performance: { now: () => now },
    setTimeout: (fn, ms = 0) => timers.push({ at: now + ms, fn }),
    requestAnimationFrame: (fn) => timers.push({ at: now, fn }),
    matchMedia: () => ({ matches: true }),
    innerWidth: 640,
    innerHeight: 440,
    addEventListener() {},
  };
  context.window = context;
  vm.runInNewContext(script, context);
  const advance = (ms) => {
    const until = now + ms;
    for (;;) {
      timers.sort((a, b) => a.at - b.at);
      if (!timers.length || timers[0].at > until) break;
      const next = timers.shift();
      now = next.at;
      next.fn();
    }
    now = until;
  };
  return { advance, statusText: () => byId("statusText").textContent, gone: () => byId("stage").classList.added.has("gone") };
}

test("loading.html with a fixed status shows it and never reports Connected or fades out", () => {
  const page = runSplashPage("?status=" + encodeURIComponent("Installing update 2.0.0. Kiro Crew opens when it is done."));
  assert.equal(page.statusText(), "Installing update 2.0.0. Kiro Crew opens when it is done.");
  page.advance(60_000);
  assert.equal(page.statusText(), "Installing update 2.0.0. Kiro Crew opens when it is done.");
  assert.equal(page.gone(), false);
});

test("loading.html without a status still plays the browser preview through to Connected", () => {
  const page = runSplashPage("");
  page.advance(60_000);
  assert.equal(page.statusText(), "Connected ✓");
  assert.equal(page.gone(), true);
});
