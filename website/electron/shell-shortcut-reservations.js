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
  const listeners = new Set();

  /** Replace everything `owner` reserves with `accelerators` (falsy entries skipped). */
  function reserve(owner, accelerators) {
    const chords = new Set();
    for (const accel of accelerators || []) {
      const chord = physicalChord(accel, platform);
      if (chord) chords.add(chord);
    }
    const prev = byOwner.get(owner);
    const same = prev && prev.size === chords.size && [...chords].every((c) => prev.has(c));
    if (chords.size) byOwner.set(owner, chords);
    else byOwner.delete(owner);
    if (same || (!prev && !chords.size)) return;
    for (const listener of [...listeners]) {
      try {
        listener();
      } catch {
        /* a listener's failure never blocks the owner's own registration */
      }
    }
  }

  function isReserved(accelerator) {
    const chord = physicalChord(accelerator, platform);
    if (!chord) return false;
    for (const chords of byOwner.values()) if (chords.has(chord)) return true;
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
