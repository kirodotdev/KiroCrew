// A focused showMainWindow answers a user gesture that reaches the app while
// another app is frontmost: relaunching it (Spotlight, the Dock's Open, a
// launcher re-exec, all of which arrive as `second-instance`), the tray's Show
// item, or clicking the update notification. On macOS 14+ `win.focus()` orders
// the window in without activating the app, so the window must be followed by
// `app.focus({ steal: true })`, the activation the summon hotkey and the
// gateway-error reveal already use. Activation stays off every other platform,
// off a show without `focus`, and off every path that is not a user gesture.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ELECTRON_DIR = path.join(__dirname, "..");
const WINDOW_LIFECYCLE_JS = fs
  .readFileSync(path.join(ELECTRON_DIR, "window-lifecycle.js"), "utf8")
  .replace(/\r\n/g, "\n");

// showMainWindow and the helpers it calls sit between unhideApp and
// activateMainWindow inside the factory. Evaluating that region as written,
// with its closure names supplied, makes the assertions about behaviour on a
// fake window and app rather than about source text.
function showMainWindowFor({ platform, win, app, calls = win.calls }) {
  const start = WINDOW_LIFECYCLE_JS.indexOf("  function unhideApp()");
  const end = WINDOW_LIFECYCLE_JS.indexOf("  function activateMainWindow()", start);
  assert.notEqual(start, -1, "could not locate unhideApp");
  assert.notEqual(end, -1, "could not bound showMainWindow");
  const context = {
    mainWindow: win,
    app,
    IS_MAC: platform === "darwin",
    cancelPendingTrayHide: () => calls.push("cancelPendingTrayHide"),
  };
  vm.runInNewContext(
    `${WINDOW_LIFECYCLE_JS.slice(start, end)}; this.show = showMainWindow;`,
    context,
  );
  return context.show;
}

function fakeWindow({ minimized = false } = {}) {
  const calls = [];
  return {
    calls,
    isDestroyed: () => false,
    isMinimized: () => minimized,
    restore: () => calls.push("window.restore"),
    show: () => calls.push("window.show"),
    focus: () => calls.push("window.focus"),
  };
}

function fakeApp(calls, { focusThrows = false } = {}) {
  return {
    show: () => calls.push("app.show"),
    focus: (options) => {
      calls.push(`app.focus ${JSON.stringify(options)}`);
      if (focusThrows) throw new Error("activation refused");
    },
  };
}

describe("showMainWindow app activation", () => {
  it("darwin: a focused show activates the app after focusing the window", () => {
    const win = fakeWindow({ minimized: true });
    const show = showMainWindowFor({ platform: "darwin", win, app: fakeApp(win.calls) });
    assert.equal(show({ focus: true }), true);
    assert.deepEqual(win.calls, [
      "cancelPendingTrayHide",
      "app.show",
      "window.restore",
      "window.show",
      "window.focus",
      'app.focus {"steal":true}',
    ]);
  });

  it("darwin: a show without focus never activates the app", () => {
    const win = fakeWindow();
    const show = showMainWindowFor({ platform: "darwin", win, app: fakeApp(win.calls) });
    assert.equal(show(), true);
    assert.equal(show({ focus: false }), true);
    assert.equal(
      win.calls.some((call) => call.startsWith("app.focus") || call === "window.focus"),
      false,
      `unexpected focus: ${win.calls.join(", ")}`,
    );
  });

  for (const platform of ["linux", "win32"]) {
    it(`${platform}: a focused show focuses the window and leaves app activation alone`, () => {
      const win = fakeWindow();
      const show = showMainWindowFor({ platform, win, app: fakeApp(win.calls) });
      assert.equal(show({ focus: true }), true);
      assert.deepEqual(win.calls, ["cancelPendingTrayHide", "window.show", "window.focus"]);
    });
  }

  it("darwin: a refused activation still reports the window as shown", () => {
    const win = fakeWindow();
    const show = showMainWindowFor({
      platform: "darwin",
      win,
      app: fakeApp(win.calls, { focusThrows: true }),
    });
    assert.equal(show({ focus: true }), true);
    assert.equal(win.calls.at(-1), 'app.focus {"steal":true}');
  });

  it("darwin: no window means no activation", () => {
    const calls = [];
    const show = showMainWindowFor({ platform: "darwin", win: null, app: fakeApp(calls), calls });
    assert.equal(show({ focus: true }), false);
    assert.deepEqual(calls, []);
  });
});

describe("a focused showMainWindow is reached only from user gestures", () => {
  // Activation takes the foreground from whatever the user is doing, so a
  // focused show belongs only to paths the user triggered. Gateway restarts,
  // update relaunches and liveness recovery surface windows without it. Every
  // focused call site in the shell is listed here, so a new one has to be
  // judged a user gesture before this passes.
  function shellSources(dir, out = []) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (["node_modules", "test", "dist", "out"].includes(entry.name)) continue;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) shellSources(full, out);
      else if (entry.name.endsWith(".js")) out.push(full);
    }
    return out;
  }

  it("lists every focused call site", () => {
    const sites = {};
    for (const file of shellSources(ELECTRON_DIR)) {
      const text = fs.readFileSync(file, "utf8");
      const count = (text.match(/showMainWindow\(\{ focus: true \}\)/g) || []).length;
      if (count) sites[path.relative(ELECTRON_DIR, file).split(path.sep).join("/")] = count;
    }
    assert.deepEqual(sites, {
      "ipc-registrar.js": 1,
      "main.js": 1,
      "window-lifecycle.js": 1,
    });
  });

  it("each focused call site is a user gesture handler", () => {
    const read = (name) => fs
      .readFileSync(path.join(ELECTRON_DIR, name), "utf8")
      .replace(/\r\n/g, "\n");
    assert.match(
      read("main.js"),
      /app\.on\("second-instance", \(\) => \{[^}]*?windows\?\.showMainWindow\(\{ focus: true \}\);\s*\}\);/,
      "relaunching the app",
    );
    assert.match(
      WINDOW_LIFECYCLE_JS,
      /const showFromTray = \(\) => \{\s*showMainWindow\(\{ focus: true \}\);\s*\};/,
      "the tray's Show item and icon click",
    );
    assert.match(
      read("ipc-registrar.js"),
      /notification\.on\("click", \(\) => \{[^}]*?windows\.showMainWindow\(\{ focus: true \}\);\s*\}\);/,
      "clicking the update notification",
    );
  });
});
