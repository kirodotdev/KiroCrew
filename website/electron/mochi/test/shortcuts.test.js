/**
 * shortcuts — Mochi's global-shortcut register/unregister lifecycle.
 *
 * Pins the behaviour Problem 3 required: the ported toggleWindow + hideAll
 * accelerators are registered when Mochi is enabled and torn down on
 * disable/quit, a taken accelerator or a throwing handler never propagates, and
 * — critically for a builtin — teardown touches ONLY Mochi's own accelerators,
 * never globalShortcut.unregisterAll() (which would clobber the host app's).
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");

function stubElectron(failFor = new Set()) {
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

function loadModule(failFor) {
  const stub = stubElectron(failFor);
  const modPath = path.join(__dirname, "..", "shortcuts.js");
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

// shortcuts.js picks platform-specific DEFAULT accelerators (Cmd⇧ on macOS,
// Alt+Shift on Windows/Linux, because Cmd/Ctrl+Shift combos are taken there).
// Mirror that exact branch so the default-relying tests pass on every OS — the
// hardcoded macOS strings failed on the Linux CI runner (registered[TOGGLE]
// was undefined because it registered Alt+Shift+M).
const IS_MAC = process.platform === "darwin";
const TOGGLE = IS_MAC ? "CommandOrControl+Shift+M" : "Alt+Shift+M";
const HIDEALL = IS_MAC ? "CommandOrControl+Shift+H" : "Alt+Shift+H";
const SCREENCAP = IS_MAC ? "CommandOrControl+Shift+X" : "Alt+Shift+X";

test("registers exactly the ported toggleWindow + hideAll accelerators", () => {
  const { mod, state } = loadModule();
  mod.registerMochiShortcuts({ onToggleWindow() {}, onHideAll() {} });

  assert.deepStrictEqual(Object.keys(state.registered).sort(), [HIDEALL, TOGGLE].sort());
  // The two skipped originals must NOT be registered.
  assert.ok(!state.registerCalls.includes(SCREENCAP), "screenCapture must be skipped");
  assert.ok(!state.registerCalls.includes("Option+Space"), "voiceInput must be skipped");
  assert.strictEqual(mod.areMochiShortcutsRegistered(), true);
});

test("the registered accelerators invoke their handlers", () => {
  const { mod, state } = loadModule();
  let toggled = 0;
  let hidden = 0;
  mod.registerMochiShortcuts({ onToggleWindow: () => toggled++, onHideAll: () => hidden++ });

  state.registered[TOGGLE]();
  state.registered[HIDEALL]();
  assert.strictEqual(toggled, 1);
  assert.strictEqual(hidden, 1);
});

test("a throwing handler is caught, not propagated to the dispatcher", () => {
  const { mod, state } = loadModule();
  const logs = [];
  mod.setShortcutLogger((l) => logs.push(l));
  mod.registerMochiShortcuts({
    onToggleWindow: () => { throw new Error("boom"); },
    onHideAll() {},
  });
  assert.doesNotThrow(() => state.registered[TOGGLE]());
  assert.ok(logs.some((l) => /handler threw/.test(l) && /boom/.test(l)), logs);
});

test("a taken accelerator is logged and skipped, the rest still register", () => {
  const { mod, state } = loadModule(new Set([TOGGLE]));
  const logs = [];
  mod.setShortcutLogger((l) => logs.push(l));
  mod.registerMochiShortcuts({ onToggleWindow() {}, onHideAll() {} });

  assert.ok(!(TOGGLE in state.registered), "the taken accelerator is not bound");
  assert.ok(HIDEALL in state.registered, "the other accelerator still registers");
  assert.ok(logs.some((l) => /failed to register/.test(l) && l.includes(TOGGLE)), logs);
});

test("a handler passed as undefined is simply skipped (no crash)", () => {
  const { mod, state } = loadModule();
  mod.registerMochiShortcuts({ onToggleWindow() {} }); // no onHideAll
  assert.ok(TOGGLE in state.registered);
  assert.ok(!(HIDEALL in state.registered));
});

test("unregister targets only Mochi's accelerators — never unregisterAll", () => {
  const { mod, state } = loadModule();
  mod.registerMochiShortcuts({ onToggleWindow() {}, onHideAll() {} });
  state.unregisterCalls.length = 0; // ignore the cleanup unregister register() does first
  mod.unregisterMochiShortcuts();

  assert.strictEqual(mod.areMochiShortcutsRegistered(), false);
  assert.deepStrictEqual(state.unregisterCalls.sort(), [HIDEALL, TOGGLE].sort());
  // Every accelerator ever unregistered is one of Mochi's own — never a
  // blanket unregisterAll that would drop the host app's shortcuts.
  assert.strictEqual(state.unregisterAllCalls, 0, "must NOT call globalShortcut.unregisterAll()");
});

test("re-register cleans up prior bindings first (no stale/stacked handlers)", () => {
  const { mod, state } = loadModule();
  mod.registerMochiShortcuts({ onToggleWindow() {}, onHideAll() {} });
  mod.registerMochiShortcuts({ onToggleWindow() {}, onHideAll() {} });
  // Each register begins with an unregister of both accelerators.
  assert.ok(state.unregisterCalls.filter((a) => a === TOGGLE).length >= 1);
  assert.ok(state.unregisterCalls.filter((a) => a === HIDEALL).length >= 1);
  assert.strictEqual(state.unregisterAllCalls, 0);
});

test("unregister is idempotent and safe before any register", () => {
  const { mod } = loadModule();
  assert.doesNotThrow(() => mod.unregisterMochiShortcuts());
  assert.strictEqual(mod.areMochiShortcutsRegistered(), false);
});

// ── Rebinding (user-editable accelerators) ─────────────────────────────────

test("registers the accelerators it is given, not the defaults", () => {
  const { mod, state } = loadModule();
  mod.registerMochiShortcuts(
    { onToggleWindow() {}, onHideAll() {} },
    { toggleWindow: "CommandOrControl+Shift+K", hideAll: "Option+Shift+J" },
  );
  assert.deepStrictEqual(state.registerCalls, [
    "CommandOrControl+Shift+K",
    "Option+Shift+J",
  ]);
});

test("falls back to the default for an action with no configured accelerator", () => {
  const { mod, state } = loadModule();
  mod.registerMochiShortcuts({ onToggleWindow() {}, onHideAll() {} }, { toggleWindow: undefined });
  assert.deepStrictEqual(state.registerCalls, [
    mod.ACCELERATORS.toggleWindow,
    mod.ACCELERATORS.hideAll,
  ]);
});

test("a rebind releases the OLD accelerator, not the new one", () => {
  // THE trap this feature has: unregistering by walking the CURRENT config
  // leaves the previous accelerator bound to a handler the user can no longer
  // see or change, alive until the process exits.
  const { mod, state } = loadModule();
  const handlers = { onToggleWindow() {}, onHideAll() {} };
  mod.registerMochiShortcuts(handlers, { toggleWindow: "CommandOrControl+Shift+M", hideAll: "" });
  state.unregisterCalls.length = 0;

  mod.registerMochiShortcuts(handlers, { toggleWindow: "CommandOrControl+Shift+K", hideAll: "" });
  assert.ok(
    state.unregisterCalls.includes("CommandOrControl+Shift+M"),
    "the previously-bound accelerator must be released",
  );
  assert.deepStrictEqual(Object.keys(state.registered), ["CommandOrControl+Shift+K"]);
});

test("an empty accelerator means unbound — registered, not reported as a failure", () => {
  const { mod, state } = loadModule();
  const results = mod.registerMochiShortcuts(
    { onToggleWindow() {}, onHideAll() {} },
    { toggleWindow: "CommandOrControl+Shift+M", hideAll: "" },
  );
  assert.deepStrictEqual(state.registerCalls, ["CommandOrControl+Shift+M"]);
  assert.strictEqual(results.toggleWindow, true);
  assert.ok(!("hideAll" in results), "an unbound action is not a failed one");
});

test("reports a taken accelerator so the UI can say so", () => {
  // Without this the user rebinds onto a key another app owns, sees no error, and
  // the shortcut simply never fires — the same class of lie as advertising a
  // shortcut that was never registered.
  const { mod } = loadModule(new Set(["CommandOrControl+Shift+K"]));
  const results = mod.registerMochiShortcuts(
    { onToggleWindow() {}, onHideAll() {} },
    { toggleWindow: "CommandOrControl+Shift+K", hideAll: "Option+Shift+J" },
  );
  assert.strictEqual(results.toggleWindow, false);
  assert.strictEqual(results.hideAll, true);
});

test("a refused accelerator is not remembered as live", () => {
  // Remembering it would make a later rebind unregister a key Mochi never owned
  // — i.e. yank it out from under whichever app actually holds it.
  const { mod, state } = loadModule(new Set(["CommandOrControl+Shift+K"]));
  mod.registerMochiShortcuts({ onToggleWindow() {} }, { toggleWindow: "CommandOrControl+Shift+K" });
  assert.deepStrictEqual(mod.currentMochiShortcuts(), {});
  state.unregisterCalls.length = 0;
  mod.unregisterMochiShortcuts();
  assert.deepStrictEqual(state.unregisterCalls, []);
});

test("currentMochiShortcuts reports what is live, for the reconcile drift check", () => {
  const { mod } = loadModule();
  mod.registerMochiShortcuts(
    { onToggleWindow() {}, onHideAll() {} },
    { toggleWindow: "CommandOrControl+Shift+K", hideAll: "Option+Shift+J" },
  );
  assert.deepStrictEqual(mod.currentMochiShortcuts(), {
    toggleWindow: "CommandOrControl+Shift+K",
    hideAll: "Option+Shift+J",
  });
  mod.unregisterMochiShortcuts();
  assert.deepStrictEqual(mod.currentMochiShortcuts(), {});
});

test("canonicalizes a stored accelerator's modifier order on read", () => {
  // The recorder once emitted modifiers in KEYPRESS order, so stored values can
  // read "Shift+CommandOrControl+A". Reordering on read repairs them without a
  // migration and keeps the reconcile drift check from seeing two spellings of
  // one chord as a permanent change.
  const { mod, state } = loadModule();
  mod.registerMochiShortcuts(
    { onToggleWindow() {}, onHideAll() {} },
    { toggleWindow: "Shift+CommandOrControl+A", hideAll: "Shift+Alt+B" },
  );
  assert.deepStrictEqual(state.registerCalls, [
    "CommandOrControl+Shift+A",
    "Alt+Shift+B",
  ]);
});

test("canonicalization treats every Electron modifier ALIAS as a modifier", () => {
  // A token missing from the modifier table would be read as the main KEY and
  // reordered behind the modifiers, corrupting the accelerator.
  const { mod, state } = loadModule();
  mod.registerMochiShortcuts(
    { onToggleWindow() {}, onHideAll() {} },
    { toggleWindow: "Option+Shift+J", hideAll: "Shift+CmdOrCtrl+K" },
  );
  assert.deepStrictEqual(state.registerCalls, [
    "Option+Shift+J",
    "CmdOrCtrl+Shift+K",
  ]);
});
