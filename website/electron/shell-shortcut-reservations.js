"use strict";

/**
 * shell-shortcut-reservations.js — the global chords the shell's own owners keep.
 *
 * `globalShortcut` is first-come: `isRegistered()` sees only what this process
 * holds at that instant. A shell owner that lets a chord go for a while (one
 * disabled, one rebinding: unregister, then register again) would lose it to
 * whichever registrant asked in between. So every shell owner records here the
 * chords it is CONFIGURED to hold, held right now or not, and the desktop
 * window host never gives an app a reserved chord. Reserving a chord an app
 * holds takes it back synchronously, so the owner's register() that follows
 * finds it free.
 *
 * An owner that is the shell code of an app (`{ app: true }`, its owner name
 * being that app's id) hands its reservation over to the host: the host serving
 * that same app may bind the chords, because they are that app's own. Every
 * other app is still refused them, and an owner that is not an app (the summon
 * hotkey) is never handed over, whatever an app is named.
 *
 * Pure (no Electron); one shared registry per process.
 */

/** Each spelling of a modifier, as the key it presses on macOS, then elsewhere. */
const MODIFIER_KEYS = {
  commandorcontrol: ["Cmd", "Ctrl"],
  cmdorctrl: ["Cmd", "Ctrl"],
  command: ["Cmd", "Super"],
  cmd: ["Cmd", "Super"],
  super: ["Cmd", "Super"],
  meta: ["Cmd", "Super"],
  control: ["Ctrl", "Ctrl"],
  ctrl: ["Ctrl", "Ctrl"],
  alt: ["Alt", "Alt"],
  option: ["Alt", "Alt"],
  altgr: ["AltGr", "AltGr"],
  shift: ["Shift", "Shift"],
};

/**
 * The physical chord an accelerator presses on `platform`, or "" when it names
 * nothing. Two spellings of one chord answer the same string.
 */
function physicalChord(accelerator, platform = process.platform) {
  if (typeof accelerator !== "string" || !accelerator.trim()) return "";
  const slot = platform === "darwin" ? 0 : 1;
  const parts = accelerator.split("+").map((p) => p.trim().toLowerCase());
  const key = parts.pop();
  if (!key) return "";
  const mods = new Set();
  for (const part of parts) {
    const mapped = MODIFIER_KEYS[part];
    if (!mapped) return "";
    mods.add(mapped[slot]);
  }
  return [...[...mods].sort(), key].join("+");
}

function createShortcutReservations({ platform = process.platform } = {}) {
  /** owner -> Set of physical chords. */
  const byOwner = new Map();
  /** Owners that are an app's own shell code, named by that app's id. */
  const appOwners = new Set();
  const listeners = new Set();

  /**
   * Replace everything `owner` reserves with `accelerators` (falsy entries skipped).
   * `{ app: true }` marks `owner` as the id of the app this shell code serves.
   */
  function reserve(owner, accelerators, { app = false } = {}) {
    const chords = new Set();
    for (const accel of accelerators || []) {
      const chord = physicalChord(accel, platform);
      if (chord) chords.add(chord);
    }
    const prev = byOwner.get(owner);
    const same = prev && prev.size === chords.size && [...chords].every((c) => prev.has(c));
    const handedOver = appOwners.has(owner);
    if (app) appOwners.add(owner);
    else appOwners.delete(owner);
    if (chords.size) byOwner.set(owner, chords);
    else byOwner.delete(owner);
    if ((same && handedOver === Boolean(app)) || (!prev && !chords.size)) return;
    for (const listener of [...listeners]) {
      try {
        listener();
      } catch {
        /* a listener's failure never blocks the owner's own registration */
      }
    }
  }

  /**
   * Whether a shell owner keeps `accelerator` from the host. `forApp` names the
   * app the host would bind it for: a chord only that app's own shell code
   * reserves is not kept from it.
   */
  function isReserved(accelerator, forApp = "") {
    const chord = physicalChord(accelerator, platform);
    if (!chord) return false;
    for (const [owner, chords] of byOwner) {
      if (!chords.has(chord)) continue;
      if (forApp && owner === forApp && appOwners.has(owner)) continue;
      return true;
    }
    return false;
  }

  /** Call `listener` after any reservation changes; answers an unsubscribe. */
  function onChange(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  return { reserve, isReserved, onChange };
}

const shellShortcutReservations = createShortcutReservations();

module.exports = {
  createShortcutReservations,
  physicalChord,
  shellShortcutReservations,
};
