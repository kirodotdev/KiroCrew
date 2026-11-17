"use strict";

const { canonicalAccelerator } = require("./app-window-host-rules");

/**
 * app-window-prefs.js — the shortcut bindings the desktop window host keeps per app
 * on this computer.
 *
 * Per declared shortcut id, the chord the user chose ("" when they unbound it). It
 * belongs to the computer (one keyboard), never to whichever gateway served a
 * window, so the shell keeps it. The manifest's per-platform default applies to
 * every shortcut without one. Names no app; pure apart from the injected store.
 */

const BINDINGS_KEY = "appWindowShortcutBindings";

function isPlainObject(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function hasOwn(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

/**
 * Per-app bindings over a key-value store (electron-store in the shell, a Map in
 * tests). Every read re-validates, so a hand-edited or corrupted file costs at most
 * the entries it broke. The key is read from the store once and then kept in memory
 * (one instance is the store's only writer while the host runs), so a page's reads
 * cost no disk.
 */
function createMachinePrefs(store) {
  let held;
  let loaded = false;
  const readAll = () => {
    if (!loaded) {
      held = store.get(BINDINGS_KEY);
      loaded = true;
    }
    return isPlainObject(held) ? held : {};
  };

  /** shortcut id -> the chord the user bound it to ("" = unbound). */
  function bindingsOf(app) {
    const raw = readAll()[app];
    const out = {};
    if (!isPlainObject(raw)) return out;
    for (const [id, accel] of Object.entries(raw)) if (typeof accel === "string") out[id] = accel;
    return out;
  }

  /** Bind *id* to *accelerator* ("" unbinds it); null returns it to the declared default. */
  function setBinding(app, id, accelerator) {
    const mine = bindingsOf(app);
    if (accelerator === null || accelerator === undefined) delete mine[id];
    else mine[id] = String(accelerator);
    const all = { ...readAll() };
    if (Object.keys(mine).length) all[app] = mine;
    else delete all[app];
    store.set(BINDINGS_KEY, all);
    held = all;
  }

  return { bindingsOf, setBinding };
}

/**
 * The shortcuts to register for an app: each declared one at the user's binding, or
 * at its declared default. An unbound one is left out, as is a binding the shortcut
 * rules refuse (its default applies). When a binding lands on a chord another of the
 * app's shortcuts has by default, the user's binding keeps it and the other is left
 * out until the user moves one of them.
 *
 * @param {{id: string, accelerator: string, chord: string, window: string}[]} declared
 * @param {Record<string, string>} bindings
 */
function boundShortcuts(declared, bindings) {
  const picked = declared.map((s) => {
    const accel = hasOwn(bindings, s.id) ? bindings[s.id] : undefined;
    if (accel === "") return null;
    const chord = typeof accel === "string" ? canonicalAccelerator(accel) : "";
    return chord ? { shortcut: { ...s, accelerator: accel, chord }, user: true } : { shortcut: { ...s }, user: false };
  });
  const used = new Set();
  const kept = new Set();
  for (const user of [true, false]) {
    for (const p of picked) {
      if (!p || p.user !== user || used.has(p.shortcut.chord)) continue;
      used.add(p.shortcut.chord);
      kept.add(p);
    }
  }
  return picked.filter((p) => kept.has(p)).map((p) => p.shortcut);
}

module.exports = {
  BINDINGS_KEY,
  boundShortcuts,
  createMachinePrefs,
};
