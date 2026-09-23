"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  STORE_KEY,
  FALLBACK_REASONS,
  RASTERIZER_DISABLE_SWITCH,
  softwareRenderingSwitches,
  readArmedFallback,
  applySoftwareRendering,
  createGpuCrashFallback,
  isDashboardDocument,
  initGpuCrashFallback,
} = require("../gpu-crash-fallback.js");
const { gpuDisableSwitches } = require("../disable-gpu.js");

const VERSION = "0.8.0";
const BACKEND = "http://localhost:5476";

/** In-memory electron-store double. */
function fakeStore(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    path: "/fake/config.json",
    get: (k) => data.get(k),
    set: (k, v) => data.set(k, v),
    _data: data,
  };
}

function gpuCrash(overrides = {}) {
  return { type: "GPU", reason: "crashed", exitCode: -2147483645, ...overrides };
}

/** A coordinator with recording hooks; every knob overridable. */
function makeFallback(overrides = {}) {
  const store = overrides.store || fakeStore();
  const relaunches = [];
  const logs = [];
  const fb = createGpuCrashFallback({
    store,
    version: VERSION,
    backendUrl: BACKEND,
    platform: "win32",
    relaunch: () => relaunches.push(true),
    log: (m) => logs.push(m),
    now: () => "2026-09-18T00:00:00.000Z",
    ...overrides,
  });
  return { fb, store, relaunches, logs };
}

// ---------------------------------------------------------------------------
// The switch set
// ---------------------------------------------------------------------------

test("softwareRenderingSwitches: the reporter's tested set, exact Chromium spelling", () => {
  assert.deepEqual(softwareRenderingSwitches(), [
    ["disable-gpu"],
    ["disable-gpu-compositing"],
    ["in-process-gpu"],
    ["use-angle", "swiftshader"],
    ["use-gl", "angle"],
  ]);
});

test("softwareRenderingSwitches: never carries a sandbox switch", () => {
  // Dropping the Chromium sandbox is a security-posture change, not a
  // rendering fallback. Pin the absence so a future edit cannot slip it in.
  for (const [name] of softwareRenderingSwitches()) {
    assert.doesNotMatch(name, /sandbox/, `--${name} must not be in the fallback set`);
  }
});

test("applySoftwareRendering: appends every switch, values as separate args", () => {
  const seen = [];
  const applied = applySoftwareRendering({
    appendSwitch: (n, v) => seen.push(v === undefined ? [n] : [n, v]),
  });
  assert.deepEqual(seen, softwareRenderingSwitches());
  assert.deepEqual(applied, [
    "disable-gpu",
    "disable-gpu-compositing",
    "in-process-gpu",
    "use-angle=swiftshader",
    "use-gl=angle",
  ]);
});

test("applySoftwareRendering: a throwing appendSwitch does not abort the rest", () => {
  const seen = [];
  const logs = [];
  const applied = applySoftwareRendering({
    appendSwitch: (n) => {
      if (n === "in-process-gpu") throw new Error("boom");
      seen.push(n);
    },
    log: (m) => logs.push(m),
  });
  assert.deepEqual(seen, ["disable-gpu", "disable-gpu-compositing", "use-angle", "use-gl"]);
  assert.equal(applied.includes("in-process-gpu"), false);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /in-process-gpu.*boom/);
});

test("RASTERIZER_DISABLE_SWITCH: is the switch the disable-gpu opt-in appends", () => {
  // The opt-in forbids the SwiftShader fallback with this exact switch; if its
  // spelling ever drifts there, the removal here would silently miss it.
  assert.ok(gpuDisableSwitches().includes(RASTERIZER_DISABLE_SWITCH));
  assert.equal(RASTERIZER_DISABLE_SWITCH, "disable-software-rasterizer");
});

test("applySoftwareRendering: removes disable-software-rasterizer before requesting SwiftShader", () => {
  // KIROCREW_DISABLE_GPU=1 survives app.relaunch(), so the opt-in re-appends
  // --disable-software-rasterizer on the software-mode boot. Left in place it
  // vetoes --use-angle=swiftshader and the app exits with no window again.
  const order = [];
  applySoftwareRendering({
    appendSwitch: (n) => order.push(`append:${n}`),
    removeSwitch: (n) => order.push(`remove:${n}`),
  });
  assert.equal(order[0], `remove:${RASTERIZER_DISABLE_SWITCH}`);
  assert.equal(order.filter((o) => o.startsWith("remove:")).length, 1);
  assert.equal(order.filter((o) => o.startsWith("append:")).length, softwareRenderingSwitches().length);
});

test("applySoftwareRendering: a throwing removeSwitch is logged and the switches still apply", () => {
  const logs = [];
  const applied = applySoftwareRendering({
    appendSwitch: () => {},
    removeSwitch: () => { throw new Error("no such switch"); },
    log: (m) => logs.push(m),
  });
  assert.equal(applied.length, softwareRenderingSwitches().length);
  assert.equal(logs.length, 1);
  assert.match(logs[0], /disable-software-rasterizer.*no such switch/);
});

test("applySoftwareRendering: no removeSwitch dependency is tolerated", () => {
  const applied = applySoftwareRendering({ appendSwitch: () => {} });
  assert.equal(applied.length, softwareRenderingSwitches().length);
});

// ---------------------------------------------------------------------------
// Reading the persisted decision at boot
// ---------------------------------------------------------------------------

test("readArmedFallback: no record reads as not armed", () => {
  assert.equal(readArmedFallback({ store: fakeStore(), version: VERSION, platform: "win32" }), null);
  assert.equal(
    readArmedFallback({ store: fakeStore({ [STORE_KEY]: null }), version: VERSION, platform: "win32" }),
    null,
  );
});

test("readArmedFallback: a record for THIS version is armed", () => {
  const record = { version: VERSION, at: "x", reason: "crashed", exitCode: 1 };
  const store = fakeStore({ [STORE_KEY]: record });
  assert.deepEqual(readArmedFallback({ store, version: VERSION, platform: "win32" }), record);
});

test("readArmedFallback: a record from another app version is NOT armed (one hardware attempt per build)", () => {
  const store = fakeStore({ [STORE_KEY]: { version: "0.7.9", at: "x", reason: "crashed" } });
  assert.equal(readArmedFallback({ store, version: VERSION, platform: "win32" }), null);
});

test("readArmedFallback: malformed records read as not armed", () => {
  for (const bad of ["armed", 1, true, [], {}, { version: 42 }, { at: "x" }]) {
    const store = fakeStore({ [STORE_KEY]: bad });
    assert.equal(
      readArmedFallback({ store, version: VERSION, platform: "win32" }),
      null,
      `expected ${JSON.stringify(bad)} to read as not armed`,
    );
  }
});

test("readArmedFallback: a throwing store reads as not armed rather than breaking boot", () => {
  const store = { get: () => { throw new Error("corrupt"); } };
  assert.equal(readArmedFallback({ store, version: VERSION, platform: "win32" }), null);
});

test("readArmedFallback: ignored off Windows even when a record exists", () => {
  const store = fakeStore({ [STORE_KEY]: { version: VERSION, at: "x", reason: "crashed" } });
  for (const platform of ["darwin", "linux"]) {
    assert.equal(readArmedFallback({ store, version: VERSION, platform }), null, platform);
  }
});

// ---------------------------------------------------------------------------
// The crash-time decision
// ---------------------------------------------------------------------------

test("a GPU crash before the dashboard loads persists the decision, then relaunches once", () => {
  const { fb, store, relaunches, logs } = makeFallback();
  assert.equal(fb.handleChildGone(gpuCrash()), "relaunched");
  assert.deepEqual(relaunches, [true]);
  assert.deepEqual(store.get(STORE_KEY), {
    version: VERSION,
    at: "2026-09-18T00:00:00.000Z",
    reason: "crashed",
    exitCode: -2147483645,
  });
  assert.equal(logs.length, 1);
  assert.match(logs[0], /relaunching once with software rendering/);
  assert.match(logs[0], /exitCode=-2147483645/);
});

test("the record persisted at crash time is the one readArmedFallback arms on the next boot", () => {
  const { fb, store } = makeFallback();
  fb.handleChildGone(gpuCrash());
  const armed = readArmedFallback({ store, version: VERSION, platform: "win32" });
  assert.ok(armed, "next boot must see the decision");
  assert.equal(armed.reason, "crashed");
});

test("every recoverable reason arms the fallback", () => {
  for (const reason of FALLBACK_REASONS) {
    const { fb, relaunches } = makeFallback();
    assert.equal(fb.handleChildGone(gpuCrash({ reason })), "relaunched", reason);
    assert.equal(relaunches.length, 1, reason);
  }
});

test("a second GPU death in the same run does not relaunch twice", () => {
  const { fb, relaunches } = makeFallback();
  assert.equal(fb.handleChildGone(gpuCrash()), "relaunched");
  assert.equal(fb.handleChildGone(gpuCrash()), "ignored-already-relaunching");
  assert.equal(relaunches.length, 1);
});

test("a non-GPU child dying is not the fallback's business", () => {
  const { fb, store, relaunches } = makeFallback();
  for (const type of ["Utility", "Zygote", "Sandbox helper", "Unknown", undefined]) {
    assert.equal(fb.handleChildGone({ type, reason: "crashed", exitCode: 1 }), "ignored-not-gpu", String(type));
  }
  assert.equal(relaunches.length, 0);
  assert.equal(store.get(STORE_KEY), undefined);
});

test("clean-exit, killed, oom and integrity-failure do not arm the fallback", () => {
  for (const reason of ["clean-exit", "killed", "oom", "integrity-failure", "", undefined]) {
    const { fb, store, relaunches } = makeFallback();
    assert.equal(fb.handleChildGone(gpuCrash({ reason })), "ignored-reason", String(reason));
    assert.equal(relaunches.length, 0, String(reason));
    assert.equal(store.get(STORE_KEY), undefined, String(reason));
  }
});

test("off Windows a GPU crash is logged and left to Chromium", () => {
  for (const platform of ["darwin", "linux"]) {
    const { fb, store, relaunches, logs } = makeFallback({ platform });
    assert.equal(fb.handleChildGone(gpuCrash()), "ignored-platform", platform);
    assert.equal(relaunches.length, 0);
    assert.equal(store.get(STORE_KEY), undefined);
    assert.match(logs[0], /Windows-only/);
  }
});

test("a GPU death while quitting is the quit, not a crash", () => {
  const { fb, store, relaunches } = makeFallback({ isQuitting: () => true });
  assert.equal(fb.handleChildGone(gpuCrash()), "ignored-quitting");
  assert.equal(relaunches.length, 0);
  assert.equal(store.get(STORE_KEY), undefined);
});

test("a GPU crash AFTER the dashboard loaded never relaunches (a working host's mid-session hiccup)", () => {
  const { fb, store, relaunches, logs } = makeFallback();
  fb.noteDocumentLoaded(`${BACKEND}/?token=abc`);
  assert.equal(fb.dashboardLoaded, true);
  assert.equal(fb.handleChildGone(gpuCrash()), "ignored-after-dashboard");
  assert.equal(relaunches.length, 0);
  assert.equal(store.get(STORE_KEY), undefined, "no decision is persisted either");
  assert.match(logs[0], /after the dashboard loaded/);
});

test("the boot splash and other origins do NOT end the startup phase", () => {
  const { fb, relaunches } = makeFallback();
  fb.noteDocumentLoaded("file:///app/loading.html");
  fb.noteDocumentLoaded("http://localhost:54760/"); // prefix of the port, different origin
  fb.noteDocumentLoaded("http://remote-crew.example:5476/");
  fb.noteDocumentLoaded("not a url");
  fb.noteDocumentLoaded(undefined);
  assert.equal(fb.dashboardLoaded, false);
  assert.equal(fb.handleChildGone(gpuCrash()), "relaunched");
  assert.equal(relaunches.length, 1);
});

test("a GPU crash with software rendering already active has nothing further to try", () => {
  // This is the bound: the record is already on disk, so re-persisting and
  // relaunching would only loop. The existing recovery paths take it from here.
  const store = fakeStore({ [STORE_KEY]: { version: VERSION, at: "x", reason: "crashed", exitCode: 1 } });
  const { fb, relaunches, logs } = makeFallback({ store, softwareActive: true });
  assert.equal(fb.handleChildGone(gpuCrash()), "ignored-software-active");
  assert.equal(relaunches.length, 0);
  assert.match(logs[0], /software rendering already active/);
  // The record is untouched, not overwritten with a new timestamp.
  assert.equal(store.get(STORE_KEY).at, "x");
});

test("a decision that cannot be persisted does not relaunch (a relaunch without it would loop)", () => {
  const throwing = { path: "/x", get: () => undefined, set: () => { throw new Error("EROFS"); } };
  const { fb, relaunches, logs } = makeFallback({ store: throwing });
  assert.equal(fb.handleChildGone(gpuCrash()), "persist-failed");
  assert.equal(relaunches.length, 0);
  assert.match(logs.join("\n"), /EROFS/);
  assert.match(logs.join("\n"), /could not be persisted — not relaunching/);
  // The failure does not latch: a later crash may find the store writable.
  const { fb: fb2, relaunches: r2 } = makeFallback();
  assert.equal(fb2.handleChildGone(gpuCrash()), "relaunched");
  assert.equal(r2.length, 1);
});

test("a store whose set() silently drops the write is treated as not persisted", () => {
  const silent = { path: "/x", get: () => undefined, set: () => {} };
  const { fb, relaunches } = makeFallback({ store: silent });
  assert.equal(fb.handleChildGone(gpuCrash()), "persist-failed");
  assert.equal(relaunches.length, 0);
});

test("a throwing relaunch is logged, never escapes, and the record stays for the next manual launch", () => {
  const { fb, store, logs } = makeFallback({ relaunch: () => { throw new Error("no exec"); } });
  assert.equal(fb.handleChildGone(gpuCrash()), "relaunched");
  assert.ok(store.get(STORE_KEY), "record persisted before the relaunch attempt");
  assert.match(logs.join("\n"), /relaunch failed: no exec/);
});

test("isDashboardDocument: origin equality, not prefix", () => {
  assert.equal(isDashboardDocument("http://localhost:5476/?token=t", "http://localhost:5476"), true);
  assert.equal(isDashboardDocument("http://localhost:5476/sessions", "http://localhost:5476"), true);
  assert.equal(isDashboardDocument("http://localhost:54760/", "http://localhost:5476"), false);
  assert.equal(isDashboardDocument("https://localhost:5476/", "http://localhost:5476"), false);
  assert.equal(isDashboardDocument("file:///loading.html", "http://localhost:5476"), false);
  assert.equal(isDashboardDocument("", "http://localhost:5476"), false);
});

// ---------------------------------------------------------------------------
// Wiring into a live app
// ---------------------------------------------------------------------------

/** Minimal Electron `app` double: records switches, listeners and relaunches. */
function fakeApp(version = VERSION) {
  const listeners = new Map();
  const switches = [];
  const removed = [];
  const app = {
    relaunches: 0,
    getVersion: () => version,
    commandLine: {
      appendSwitch: (n, v) => switches.push(v === undefined ? n : `${n}=${v}`),
      removeSwitch: (n) => removed.push(n),
    },
    on: (event, fn) => listeners.set(event, fn),
    relaunch: () => { app.relaunches += 1; },
    emit: (event, ...args) => listeners.get(event)(...args),
    switches,
    removed,
    listeners,
  };
  return app;
}

/** A webContents double that reports `url` and fires did-finish-load on demand. */
function fakeContents(url) {
  let onLoad = null;
  return {
    getURL: () => url,
    on: (event, fn) => { if (event === "did-finish-load") onLoad = fn; },
    finishLoad: () => onLoad(),
  };
}

test("initGpuCrashFallback: a cold boot applies nothing and registers both listeners", () => {
  const app = fakeApp();
  const res = initGpuCrashFallback({
    app, store: fakeStore(), backendUrl: BACKEND, isQuitting: () => false, requestQuit: () => {},
  });
  assert.equal(res.softwareActive, false);
  assert.deepEqual(app.switches, []);
  assert.deepEqual(app.removed, []);
  assert.ok(app.listeners.has("child-process-gone"));
  assert.ok(app.listeners.has("web-contents-created"));
});

test("initGpuCrashFallback: an armed boot applies the software switches before ready and says how to undo it", () => {
  const app = fakeApp();
  const store = fakeStore({ [STORE_KEY]: { version: VERSION, at: "2026-09-18T00:00:00Z", reason: "crashed", exitCode: -2147483645 } });
  const logs = [];
  const res = initGpuCrashFallback({
    app, store, backendUrl: BACKEND, isQuitting: () => false, requestQuit: () => {}, log: (m) => logs.push(m),
    platform: "win32",
  });
  assert.equal(res.softwareActive, true);
  assert.deepEqual(app.switches, [
    "disable-gpu",
    "disable-gpu-compositing",
    "in-process-gpu",
    "use-angle=swiftshader",
    "use-gl=angle",
  ]);
  // The disable-gpu opt-in (env survives relaunch) must not veto SwiftShader.
  assert.deepEqual(app.removed, [RASTERIZER_DISABLE_SWITCH]);
  assert.match(logs[0], /software rendering ACTIVE/);
  assert.match(logs[0], new RegExp(`remove "${STORE_KEY}" from /fake/config.json`));
});

test("initGpuCrashFallback: a startup GPU crash relaunches through the app's own quit path", () => {
  const app = fakeApp();
  const store = fakeStore();
  let quits = 0;
  initGpuCrashFallback({
    app, store, backendUrl: BACKEND, isQuitting: () => false, requestQuit: () => { quits += 1; },
    platform: "win32",
  });
  // The splash finishing its load does not end the startup phase.
  const splash = fakeContents("file:///app/loading.html");
  app.emit("web-contents-created", {}, splash);
  splash.finishLoad();

  app.emit("child-process-gone", {}, gpuCrash());
  assert.equal(app.relaunches, 1, "app.relaunch() scheduled");
  assert.equal(quits, 1, "then the normal quit, so the gateway is stopped cleanly");
  assert.equal(store.get(STORE_KEY).version, VERSION);
});

test("initGpuCrashFallback: once the dashboard has loaded, a GPU crash does not relaunch", () => {
  const app = fakeApp();
  const store = fakeStore();
  let quits = 0;
  initGpuCrashFallback({
    app, store, backendUrl: BACKEND, isQuitting: () => false, requestQuit: () => { quits += 1; },
    platform: "win32",
  });
  const dashboard = fakeContents(`${BACKEND}/?token=abc`);
  app.emit("web-contents-created", {}, dashboard);
  dashboard.finishLoad();

  app.emit("child-process-gone", {}, gpuCrash());
  assert.equal(app.relaunches, 0);
  assert.equal(quits, 0);
  assert.equal(store.get(STORE_KEY), undefined);
});

test("initGpuCrashFallback: a destroyed webContents (getURL throws) does not break the listener", () => {
  const app = fakeApp();
  initGpuCrashFallback({
    app, store: fakeStore(), backendUrl: BACKEND, isQuitting: () => false, requestQuit: () => {},
  });
  const dead = { getURL: () => { throw new Error("destroyed"); }, on: (_e, fn) => { dead.fire = fn; } };
  app.emit("web-contents-created", {}, dead);
  assert.doesNotThrow(() => dead.fire());
});

test("initGpuCrashFallback: a null details payload is tolerated", () => {
  const app = fakeApp();
  initGpuCrashFallback({
    app, store: fakeStore(), backendUrl: BACKEND, isQuitting: () => false, requestQuit: () => {},
  });
  assert.doesNotThrow(() => app.emit("child-process-gone", {}, null));
  assert.equal(app.relaunches, 0);
});

// ---------------------------------------------------------------------------
// main.js wiring
// ---------------------------------------------------------------------------

test("main wires the fallback in the lock winner, after the opt-in policy, before ready", () => {
  // Chromium consumes the switches during initialization, and the listener has
  // to exist before the GPU process can die. Pin the call site so a refactor
  // cannot leave a fully tested module disconnected.
  const source = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  const lock = source.indexOf("if (!app.requestSingleInstanceLock())");
  const winner = source.indexOf("} else {", lock);
  const gpuPolicy = source.indexOf("initGpuPolicy({", winner);
  const fallback = source.indexOf("initGpuCrashFallback({", gpuPolicy);
  const secondInstance = source.indexOf('app.on("second-instance"', fallback);
  const ready = source.indexOf("app.whenReady().then", fallback);

  assert.ok(lock >= 0 && winner > lock, "expected the single-instance winner branch");
  assert.ok(gpuPolicy > winner, "opt-in GPU policy must be inside the lock winner");
  assert.ok(fallback > gpuPolicy, "the fallback must follow the opt-in policy");
  assert.ok(secondInstance > fallback, "the fallback must precede second-instance registration");
  assert.ok(ready > fallback, "the fallback must be armed before app ready");
  assert.match(source, /gpuSoftwareFallback: null/, "the store must declare the key's default");
  assert.match(source, /requestQuit,\s*\n\s*log: glog,\s*\n\s*\}\);/, "the fallback relaunches through requestQuit");
});

test("main never passes --no-sandbox on any path", () => {
  const source = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  const module_ = fs.readFileSync(path.join(__dirname, "..", "gpu-crash-fallback.js"), "utf8");
  for (const [name, text] of [["main.js", source], ["gpu-crash-fallback.js", module_]]) {
    // Code only: strip line comments, which are allowed to NAME the switch.
    const code = text.replace(/^\s*\/\/.*$/gm, "");
    assert.doesNotMatch(code, /appendSwitch\(\s*["']no-sandbox/, `${name} must not append no-sandbox`);
    assert.doesNotMatch(code, /["']no-sandbox["']/, `${name} must not carry a no-sandbox literal`);
  }
});
