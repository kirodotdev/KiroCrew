/**
 * machineStore.test.js — the per-MACHINE prefs the SHELL owns.
 *
 * These cover the three defects that moving `petInstance` and the accelerators out
 * of the host gateway's Mochi settings is meant to fix, so each test names the
 * behaviour rather than the function:
 *
 *  - the pointer stays readable when the host's Mochi is disabled (no gateway is
 *    consulted at all here — that IS the fix),
 *  - an existing user's choice is migrated exactly once, and never burned on a
 *    failed read,
 *  - a corrupted store degrades to defaults instead of costing the user their pet.
 */

const test = require("node:test");
const assert = require("node:assert");

const {
  SELF_INSTANCE,
  MACHINE_STORE_DEFAULTS,
  MIGRATED_KEY,
  petInstanceOf,
  setPetInstanceIn,
  shortcutsOf,
  setShortcutsIn,
  mergeAcceptedShortcuts,
  userSetOf,
  migrateMachinePrefs,
} = require("../machineStore");

/** Minimal electron-store stand-in: get/set over a plain Map, with defaults. */
function fakeStore(overrides = {}) {
  const data = new Map(Object.entries({ ...MACHINE_STORE_DEFAULTS, ...overrides }));
  return {
    get: (k) => data.get(k),
    set: (k, v) => data.set(k, v),
    _data: data,
  };
}

test("petInstance defaults to self, and round-trips a chosen id", () => {
  const store = fakeStore();
  assert.strictEqual(petInstanceOf(store), SELF_INSTANCE);

  setPetInstanceIn(store, "crew-abc");
  assert.strictEqual(petInstanceOf(store), "crew-abc");

  // Store the identifier the resolver keys on, so a re-read lands on the same
  // target rather than on something derived from it.
  setPetInstanceIn(store, SELF_INSTANCE);
  assert.strictEqual(petInstanceOf(store), SELF_INSTANCE);
});

test("a corrupted or blank pointer reads as self rather than throwing", () => {
  // A pet that vanishes because a JSON file got mangled is worse than one that
  // falls back to this computer.
  for (const bad of [null, undefined, 42, {}, [], "", "   "]) {
    assert.strictEqual(petInstanceOf(fakeStore({ "mochi.petInstance": bad })), SELF_INSTANCE, `${JSON.stringify(bad)}`);
  }
});

test("shortcuts are undefined when unset — never {}", () => {
  // `{}` reads downstream as "the user unbound everything", which would silently
  // leave the app with no accelerators at all.
  assert.strictEqual(shortcutsOf(fakeStore()), undefined);
  assert.strictEqual(shortcutsOf(fakeStore({ "mochi.shortcuts": {} })), undefined);
  assert.strictEqual(shortcutsOf(fakeStore({ "mochi.shortcuts": "nope" })), undefined);
});

test("shortcuts keep only string values, and clearing returns to defaults", () => {
  const store = fakeStore();
  setShortcutsIn(store, { toggleWindow: "Alt+Shift+M", hideAll: 7, screenCapture: null });
  assert.deepStrictEqual(shortcutsOf(store), { toggleWindow: "Alt+Shift+M" });

  assert.strictEqual(setShortcutsIn(store, null), undefined);
  assert.strictEqual(shortcutsOf(store), undefined);
});

test("migration seeds both prefs from the host's settings, exactly once", () => {
  const store = fakeStore();
  const migrated = migrateMachinePrefs(store, {
    petInstance: "crew-remote",
    shortcuts: { hideAll: "Alt+Shift+H" },
  });

  assert.strictEqual(migrated, true);
  assert.strictEqual(petInstanceOf(store), "crew-remote");
  assert.deepStrictEqual(shortcutsOf(store), { hideAll: "Alt+Shift+H" });
  assert.strictEqual(store.get(MIGRATED_KEY), true);

  // A later choice must survive: re-importing the gateway's stale copy over the
  // user's own pick is the failure a value-based guard would have caused.
  setPetInstanceIn(store, SELF_INSTANCE);
  assert.strictEqual(migrateMachinePrefs(store, { petInstance: "crew-remote" }), false);
  assert.strictEqual(petInstanceOf(store), SELF_INSTANCE);
});

test("a NON-ANSWER never burns the one-shot migration flag", () => {
  // `mochiSettings()` returns null for a timeout, a 403 and malformed JSON alike.
  // Treating that as "nothing to migrate" would mark the migration done and lose
  // the user's stored choice permanently, on one slow tick.
  for (const nonAnswer of [null, undefined, "", 0, "{}"]) {
    const store = fakeStore();
    assert.strictEqual(migrateMachinePrefs(store, nonAnswer), false);
    assert.strictEqual(store.get(MIGRATED_KEY), false);
  }

  // ...and the real answer still lands afterwards.
  const store = fakeStore();
  migrateMachinePrefs(store, null);
  assert.strictEqual(migrateMachinePrefs(store, { petInstance: "crew-x" }), true);
  assert.strictEqual(petInstanceOf(store), "crew-x");
});

test("an answer with neither pref still completes the migration", () => {
  // A user who never set either one has nothing to import; leaving the flag unset
  // would re-probe the gateway on every tick forever.
  const store = fakeStore();
  assert.strictEqual(migrateMachinePrefs(store, { mode: "quiet" }), true);
  assert.strictEqual(petInstanceOf(store), SELF_INSTANCE);
  assert.strictEqual(store.get(MIGRATED_KEY), true);
});

// ── migration must never clobber a deliberate choice ───────────────────────────
//
// THE RACE: a non-answer deliberately does NOT burn the one-shot flag (otherwise a
// single timeout would lose an existing choice forever), so the flag can still be
// false while the pet's windows are already open and editable. A probe that
// succeeds LATER must not import the stale gateway copy over what the user just
// picked.

test("a later migration does NOT overwrite a user-set pointer", () => {
  const store = fakeStore();
  // Tick 1: the settings probe times out, so migration stays pending.
  assert.strictEqual(migrateMachinePrefs(store, null), false);
  assert.strictEqual(store.get(MIGRATED_KEY), false);

  // The user picks an instance through the IPC while migration is still pending.
  setPetInstanceIn(store, "crew-user-picked", { byUser: true });

  // Tick N: the probe finally answers, carrying the STALE gateway value.
  assert.strictEqual(migrateMachinePrefs(store, { petInstance: "crew-stale" }), true);

  assert.strictEqual(
    petInstanceOf(store),
    "crew-user-picked",
    "a delayed migration reverted the user's own choice",
  );
  // ...and it still stops retrying.
  assert.strictEqual(store.get(MIGRATED_KEY), true);
});

test("a later migration does NOT overwrite user-set shortcuts", () => {
  const store = fakeStore();
  migrateMachinePrefs(store, null);
  setShortcutsIn(store, { hideAll: "Alt+Shift+J" }, { byUser: true });

  migrateMachinePrefs(store, { shortcuts: { hideAll: "Alt+Shift+H" } });
  assert.deepStrictEqual(shortcutsOf(store), { hideAll: "Alt+Shift+J" });
});

test("migration still seeds the key the user did NOT touch", () => {
  // Per-key, not all-or-nothing: touching one pref must not forfeit the other's
  // migration.
  const store = fakeStore();
  migrateMachinePrefs(store, null);
  setPetInstanceIn(store, "crew-user-picked", { byUser: true });

  migrateMachinePrefs(store, {
    petInstance: "crew-stale",
    shortcuts: { hideAll: "Alt+Shift+H" },
  });

  assert.strictEqual(petInstanceOf(store), "crew-user-picked");
  assert.deepStrictEqual(shortcutsOf(store), { hideAll: "Alt+Shift+H" });
});

test("a write WITHOUT byUser stays migratable — only intent protects a key", () => {
  // The migration itself writes through the same setters; if those writes marked
  // the keys as user-set, nothing would distinguish "imported" from "chosen".
  const store = fakeStore();
  setPetInstanceIn(store, "crew-imported");
  assert.deepStrictEqual(userSetOf(store), []);
  assert.strictEqual(migrateMachinePrefs(store, { petInstance: "crew-from-gateway" }), true);
  assert.strictEqual(petInstanceOf(store), "crew-from-gateway");
});

test("no gateway is consulted to read the pointer", () => {
  // The whole point of the move: this module has no HTTP surface, so a disabled
  // host Mochi (whose /api/apps/mochi/settings 403s) cannot make the pointer
  // unreadable. Asserted structurally because the failure mode was architectural —
  // a future edit that reached back over HTTP would restore the original bug while
  // every behavioural test above still passed.
  const source = require("node:fs").readFileSync(require.resolve("../machineStore"), "utf-8");
  assert.ok(!/\bfetch\s*\(/.test(source), "machineStore must not fetch");
  assert.ok(!/require\(["']https?["']\)/.test(source), "machineStore must not require http");
});

// ── mergeAcceptedShortcuts ───────────────────────────────────────────────────
//
// What to persist after a rebind attempt. The `prev = undefined` cases are the
// load-bearing ones: that is `shortcutsOf`'s documented answer for "nothing
// stored yet", which holds until a first rebind or a migration — and the
// migration only runs while the host's Mochi is enabled, so it is the NORMAL
// state in this PR's headline scenario (pet on a remote, host Mochi off).

const ACTIONS = ["toggleWindow", "screenCapture", "hideAll"];

test("an unset store does not throw — undefined prev is a normal state", () => {
  const keep = mergeAcceptedShortcuts(
    undefined,
    { toggleWindow: "CommandOrControl+Shift+M" },
    { toggleWindow: true },
    ACTIONS,
  );
  assert.deepStrictEqual(keep, { toggleWindow: "CommandOrControl+Shift+M" });
});

test("a REFUSED action with no previous value is omitted, not stored as undefined", () => {
  // Storing `undefined` would defeat shortcuts.js's own defaults, and reading
  // `prev[action]` off an unset store is what used to throw here.
  const keep = mergeAcceptedShortcuts(
    undefined,
    { toggleWindow: "CommandOrControl+Shift+M" },
    { toggleWindow: false },
    ACTIONS,
  );
  assert.deepStrictEqual(keep, {});
  assert.ok(!("toggleWindow" in keep), "an unstorable action must be absent, not undefined");
});

test("a refused action keeps the accelerator that was already working", () => {
  const keep = mergeAcceptedShortcuts(
    { toggleWindow: "CommandOrControl+Shift+M", hideAll: "CommandOrControl+Shift+H" },
    { toggleWindow: "CommandOrControl+Shift+T" },
    { toggleWindow: false },
    ACTIONS,
  );
  assert.strictEqual(keep.toggleWindow, "CommandOrControl+Shift+M", "the refusal must not stick");
  assert.strictEqual(keep.hideAll, "CommandOrControl+Shift+H", "untouched actions survive");
});

test("an accepted action replaces the previous accelerator", () => {
  const keep = mergeAcceptedShortcuts(
    { toggleWindow: "CommandOrControl+Shift+M" },
    { toggleWindow: "CommandOrControl+Shift+T" },
    { toggleWindow: true },
    ACTIONS,
  );
  assert.strictEqual(keep.toggleWindow, "CommandOrControl+Shift+T");
});

test("an action ABSENT from the result was not attempted, so it is not a refusal", () => {
  // register() skips actions with no handler or no accelerator. Treating that
  // absence as a refusal would revert a key the OS never rejected.
  const keep = mergeAcceptedShortcuts(
    { hideAll: "CommandOrControl+Shift+H" },
    { toggleWindow: "CommandOrControl+Shift+T" },
    {},
    ACTIONS,
  );
  assert.strictEqual(keep.toggleWindow, "CommandOrControl+Shift+T");
  assert.strictEqual(keep.hideAll, "CommandOrControl+Shift+H");
});

test("a non-object result or desired is tolerated rather than thrown on", () => {
  assert.deepStrictEqual(mergeAcceptedShortcuts(undefined, undefined, undefined, ACTIONS), {});
  assert.deepStrictEqual(
    mergeAcceptedShortcuts({ hideAll: "CommandOrControl+Shift+H" }, null, null, ACTIONS),
    { hideAll: "CommandOrControl+Shift+H" },
  );
});
