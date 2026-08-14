/**
 * global-hotkey — the host app's system-wide summon shortcut lifecycle.
 *
 * Pins the contract the feature requires: the accelerator is registered from a
 * stored preference with fallback to the platform default, a taken or
 * malformed accelerator never throws or blocks startup, the summon handler
 * surfaces an existing dashboard window (creating one only when none exists),
 * and — critically, since the Mochi builtin also owns global shortcuts in this
 * process — teardown touches ONLY this module's own accelerator, never
 * globalShortcut.unregisterAll().
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");

function stubElectron({ failFor = new Set(), throwFor = new Set() } = {}) {
  const state = {
    registered: {}, // accel -> callback
    registerCalls: [],
    unregisterCalls: [],
    unregisterAllCalls: 0,
  };
  const electron = {
    globalShortcut: {
      register(accel, cb) {
        state.registerCalls.push(accel);
        if (throwFor.has(accel)) throw new Error(`malformed accelerator: ${accel}`);
        if (failFor.has(accel)) return false;
        state.registered[accel] = cb;
        return true;
      },
      unregister(accel) {
        state.unregisterCalls.push(accel);
        delete state.registered[accel];
      },
      unregisterAll() {
        state.unregisterAllCalls += 1;
      },
    },
  };
  return { state, electron };
}

function loadModule(stubOpts) {
  const stub = stubElectron(stubOpts);
  const modPath = path.join(__dirname, "..", "global-hotkey.js");
  delete require.cache[require.resolve(modPath)];
  const origLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "electron") return stub.electron;
    return origLoad(request, parent, isMain);
  };
  try {
    return { mod: require(modPath), ...stub };
  } finally {
    Module._load = origLoad;
  }
}

// Mirror the module's platform branch (same reasoning as mochi's test: the
// macOS default string would fail on the Linux CI runner).
const DEFAULT = process.platform === "darwin" ? "CommandOrControl+Shift+K" : "Alt+Shift+K";

// ── Registration from the stored preference ─────────────────────────────────

test("binds the platform default when nothing is stored", () => {
  const { mod, state } = loadModule();
  const out = mod.bindGlobalHotkey(null, () => {});
  assert.deepStrictEqual(out, { accelerator: DEFAULT, bound: true });
  assert.ok(DEFAULT in state.registered);
  assert.strictEqual(mod.currentGlobalHotkey(), DEFAULT);
});

test("binds a stored custom accelerator", () => {
  const { mod, state } = loadModule();
  const out = mod.bindGlobalHotkey("CommandOrControl+Shift+J", () => {});
  assert.deepStrictEqual(out, { accelerator: "CommandOrControl+Shift+J", bound: true });
  assert.ok("CommandOrControl+Shift+J" in state.registered);
});

test("a non-string stored value falls back to the default", () => {
  const { mod, state } = loadModule();
  for (const bad of [42, { accel: "x" }, true]) {
    const out = mod.bindGlobalHotkey(bad, () => {});
    assert.strictEqual(out.accelerator, DEFAULT, `stored ${JSON.stringify(bad)}`);
  }
  assert.ok(DEFAULT in state.registered);
});

test("an empty stored string means unbound — no register call, not a failure", () => {
  const { mod, state } = loadModule();
  const out = mod.bindGlobalHotkey("", () => {});
  assert.deepStrictEqual(out, { accelerator: "", bound: false });
  assert.deepStrictEqual(state.registerCalls, []);
  assert.strictEqual(mod.currentGlobalHotkey(), "");
});

test("a whitespace-only stored string is mangled, not an unbind — falls back to default", () => {
  // The deliberate unbind spelling is exactly ""; "   " is a corrupted value
  // and silently disabling the hotkey for it would be a recovery dead end.
  const { mod, state } = loadModule();
  const out = mod.bindGlobalHotkey("   ", () => {});
  assert.deepStrictEqual(out, { accelerator: DEFAULT, bound: true });
  assert.ok(DEFAULT in state.registered);
});

// ── Degraded, never fatal ───────────────────────────────────────────────────

test("a stored accelerator taken by another app falls back to the default", () => {
  const { mod, state } = loadModule({ failFor: new Set(["CommandOrControl+Shift+J"]) });
  const logs = [];
  mod.setGlobalHotkeyLogger((l) => logs.push(l));
  const out = mod.bindGlobalHotkey("CommandOrControl+Shift+J", () => {});
  assert.deepStrictEqual(out, { accelerator: DEFAULT, bound: true });
  assert.ok(DEFAULT in state.registered);
  assert.ok(logs.some((l) => /failed to register/.test(l)), logs);
  assert.ok(logs.some((l) => /falling back to default/.test(l)), logs);
});

test("a malformed stored accelerator (register throws) is caught and falls back", () => {
  const { mod, state } = loadModule({ throwFor: new Set(["NotARealChord"]) });
  const logs = [];
  mod.setGlobalHotkeyLogger((l) => logs.push(l));
  let out;
  assert.doesNotThrow(() => {
    out = mod.bindGlobalHotkey("NotARealChord", () => {});
  });
  assert.deepStrictEqual(out, { accelerator: DEFAULT, bound: true });
  assert.ok(DEFAULT in state.registered);
  assert.ok(logs.some((l) => /error registering/.test(l)), logs);
});

test("register() returning false for the default too does not throw or block startup", () => {
  const { mod } = loadModule({ failFor: new Set([DEFAULT]) });
  const logs = [];
  mod.setGlobalHotkeyLogger((l) => logs.push(l));
  let out;
  assert.doesNotThrow(() => {
    out = mod.bindGlobalHotkey(null, () => {});
  });
  assert.deepStrictEqual(out, { accelerator: "", bound: false });
  assert.strictEqual(mod.currentGlobalHotkey(), "");
  assert.ok(logs.some((l) => /failed to register/.test(l)), logs);
});

test("a refused accelerator is not remembered as live", () => {
  // Remembering it would make teardown unregister a key another app owns.
  const { mod, state } = loadModule({ failFor: new Set([DEFAULT]) });
  mod.bindGlobalHotkey(null, () => {});
  state.unregisterCalls.length = 0;
  mod.unregisterGlobalHotkey();
  assert.deepStrictEqual(state.unregisterCalls, []);
});

// ── The summon handler ──────────────────────────────────────────────────────

function fakeWindow({ minimized = false, destroyed = false } = {}) {
  const calls = [];
  return {
    calls,
    isDestroyed: () => destroyed,
    isMinimized: () => minimized,
    restore: () => calls.push("restore"),
    show: () => calls.push("show"),
    focus: () => calls.push("focus"),
  };
}

test("the handler shows + focuses an existing window and does not create one", () => {
  const { mod } = loadModule();
  const win = fakeWindow();
  let created = 0;
  let appFocused = 0;
  const handler = mod.createSummonHandler({
    getWindow: () => win,
    createWindow: () => created++,
    focusApp: () => appFocused++,
  });
  handler();
  assert.deepStrictEqual(win.calls, ["show", "focus"]);
  assert.strictEqual(created, 0);
  assert.strictEqual(appFocused, 1);
});

test("the handler restores a minimized window before focusing it", () => {
  const { mod } = loadModule();
  const win = fakeWindow({ minimized: true });
  const handler = mod.createSummonHandler({ getWindow: () => win, createWindow: () => {} });
  handler();
  assert.deepStrictEqual(win.calls, ["restore", "show", "focus"]);
});

test("the handler creates a window when none exists or it is destroyed", () => {
  const { mod } = loadModule();
  let created = 0;
  const none = mod.createSummonHandler({ getWindow: () => null, createWindow: () => created++ });
  none();
  assert.strictEqual(created, 1);
  const gone = fakeWindow({ destroyed: true });
  const dead = mod.createSummonHandler({ getWindow: () => gone, createWindow: () => created++ });
  dead();
  assert.strictEqual(created, 2);
  assert.deepStrictEqual(gone.calls, []);
});

test("a throwing summon handler is caught, not propagated to the dispatcher", () => {
  const { mod, state } = loadModule();
  const logs = [];
  mod.setGlobalHotkeyLogger((l) => logs.push(l));
  mod.bindGlobalHotkey(null, () => {
    throw new Error("boom");
  });
  assert.doesNotThrow(() => state.registered[DEFAULT]());
  assert.ok(logs.some((l) => /handler threw/.test(l) && /boom/.test(l)), logs);
});

test("the registered accelerator invokes the handler", () => {
  const { mod, state } = loadModule();
  let summoned = 0;
  mod.bindGlobalHotkey(null, () => summoned++);
  state.registered[DEFAULT]();
  assert.strictEqual(summoned, 1);
});

// ── Teardown: only our own accelerator, NEVER unregisterAll ─────────────────

test("unregister targets only this module's accelerator — never unregisterAll", () => {
  const { mod, state } = loadModule();
  mod.bindGlobalHotkey(null, () => {});
  state.unregisterCalls.length = 0;
  mod.unregisterGlobalHotkey();
  assert.deepStrictEqual(state.unregisterCalls, [DEFAULT]);
  assert.strictEqual(mod.currentGlobalHotkey(), "");
  // The invariant Mochi's shortcuts depend on: a blanket unregisterAll here
  // would drop the builtin's registrations too.
  assert.strictEqual(state.unregisterAllCalls, 0, "must NOT call globalShortcut.unregisterAll()");
});

test("a rebind releases the OLD accelerator, not the new one", () => {
  const { mod, state } = loadModule();
  mod.bindGlobalHotkey("CommandOrControl+Shift+J", () => {});
  state.unregisterCalls.length = 0;
  mod.bindGlobalHotkey("CommandOrControl+Shift+L", () => {});
  assert.ok(state.unregisterCalls.includes("CommandOrControl+Shift+J"));
  assert.deepStrictEqual(Object.keys(state.registered), ["CommandOrControl+Shift+L"]);
  assert.strictEqual(state.unregisterAllCalls, 0);
});

test("unregister is idempotent and safe before any register", () => {
  const { mod, state } = loadModule();
  assert.doesNotThrow(() => mod.unregisterGlobalHotkey());
  assert.doesNotThrow(() => mod.unregisterGlobalHotkey());
  assert.deepStrictEqual(state.unregisterCalls, []);
  assert.strictEqual(state.unregisterAllCalls, 0);
});
