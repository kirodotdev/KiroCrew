"use strict";

/**
 * app-window-host-rules.js — what an app may ask the desktop window host for.
 *
 * The shell's copy of the manifest `desktop` rules in
 * `src/kiro_crew/apps/desktop_manifest.py`. The gateway refuses an install that
 * breaks them; the shell checks again because it must not trust a gateway of
 * another version, nor a manifest that reached `/api/apps` some other way. A
 * declaration that fails here is dropped whole: the shell hosts nothing for that
 * app rather than the part it understood.
 *
 * Pure (no Electron), so `test/app-window-host-rules.test.js` pins every rule.
 */

const APP_NAME_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
const ID_RE = /^[a-z][a-z0-9-]{0,31}$/;
const ENTRY_RE = /^(?:[A-Za-z0-9_-][A-Za-z0-9._-]*\/)*[A-Za-z0-9_-][A-Za-z0-9._-]*\.m?js$/;
const MAX_ENTRY_LEN = 200;
const MAX_WINDOWS = 4;
const MAX_SHORTCUTS = 4;
const WINDOW_KINDS = new Set(["overlay", "panel"]);
const PANEL_POSITIONS = new Set(["bottom-right", "top-right", "center"]);
const PANEL_WIDTH = [160, 1600];
const PANEL_HEIGHT = [120, 1200];
const WINDOW_KEYS = new Set([
  "id", "entry", "kind", "openOnEnable", "width", "height", "position", "transparent",
]);
const PANEL_ONLY_KEYS = ["width", "height", "position", "transparent"];
const SHORTCUT_KEYS = new Set(["id", "accelerator", "window"]);
const BLOCK_KEYS = new Set(["windows", "shortcuts"]);

const MODIFIERS = {
  commandorcontrol: "CommandOrControl",
  cmdorctrl: "CommandOrControl",
  command: "Command",
  cmd: "Command",
  control: "Control",
  ctrl: "Control",
  alt: "Alt",
  option: "Alt",
  altgr: "AltGr",
  shift: "Shift",
  super: "Super",
  meta: "Super",
};
/** The modifier each spelling presses on macOS, then on Windows and Linux. */
const SAME_KEY = [
  { CommandOrControl: "Cmd", Command: "Cmd", Super: "Cmd", Control: "Ctrl" },
  { CommandOrControl: "Ctrl", Control: "Ctrl", Command: "Cmd", Super: "Super" },
];
const NAMED_KEYS = { space: "Space", up: "Up", down: "Down", left: "Left", right: "Right" };
const FUNCTION_KEY_RE = /^f([1-9]|1[0-9]|2[0-4])$/;

/**
 * The canonical chord for an accelerator an app may claim, or "".
 *
 * A global shortcut is exclusive while the shell holds it, so an app gets only a
 * chord of two different modifier keys on every platform (so one is not Shift)
 * and one letter, digit, function, arrow or Space key: no single-modifier chord
 * such as copy or paste.
 */
function canonicalAccelerator(accelerator) {
  if (typeof accelerator !== "string" || !accelerator || accelerator.length > 64) return "";
  const parts = accelerator.split("+").map((p) => p.trim());
  if (parts.some((p) => !p)) return "";
  const keyRaw = parts.pop();
  const mods = [];
  for (const raw of parts) {
    const mod = MODIFIERS[raw.toLowerCase()];
    if (!mod || mods.includes(mod)) return "";
    mods.push(mod);
  }
  // Counted per platform: on macOS CommandOrControl, Command and Super are all
  // Cmd, elsewhere CommandOrControl is Ctrl. Two spellings of one key are one.
  if (SAME_KEY.some((table) => new Set(mods.map((m) => table[m] || m)).size < 2)) return "";
  const low = keyRaw.toLowerCase();
  let key;
  if (/^[A-Za-z0-9]$/.test(keyRaw)) key = keyRaw.toUpperCase();
  else if (FUNCTION_KEY_RE.test(low)) key = low.toUpperCase();
  else if (Object.prototype.hasOwnProperty.call(NAMED_KEYS, low)) key = NAMED_KEYS[low];
  else return "";
  return [...mods.sort(), key].join("+");
}

function isPlainObject(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function ownKeysWithin(obj, allowed) {
  return Object.keys(obj).every((k) => allowed.has(k));
}

function inRange(value, [low, high]) {
  return Number.isInteger(value) && value >= low && value <= high;
}

function parseWindow(raw) {
  if (!isPlainObject(raw) || !ownKeysWithin(raw, WINDOW_KEYS)) return null;
  const { id, entry, kind } = raw;
  if (typeof id !== "string" || !ID_RE.test(id)) return null;
  if (typeof entry !== "string" || entry.length > MAX_ENTRY_LEN || !ENTRY_RE.test(entry)) {
    return null;
  }
  if (!WINDOW_KINDS.has(kind)) return null;
  const openOnEnable = raw.openOnEnable === undefined ? false : raw.openOnEnable;
  if (typeof openOnEnable !== "boolean") return null;
  const win = { id, entry, kind, openOnEnable };
  if (kind === "overlay") {
    return PANEL_ONLY_KEYS.some((k) => k in raw) ? null : win;
  }
  const width = raw.width === undefined ? 400 : raw.width;
  const height = raw.height === undefined ? 560 : raw.height;
  const position = raw.position === undefined ? "bottom-right" : raw.position;
  const transparent = raw.transparent === undefined ? false : raw.transparent;
  if (!inRange(width, PANEL_WIDTH) || !inRange(height, PANEL_HEIGHT)) return null;
  if (!PANEL_POSITIONS.has(position) || typeof transparent !== "boolean") return null;
  return { ...win, width, height, position, transparent };
}

function parseShortcut(raw, windowIds, chords) {
  if (!isPlainObject(raw) || !ownKeysWithin(raw, SHORTCUT_KEYS)) return null;
  const { id, accelerator, window } = raw;
  if (typeof id !== "string" || !ID_RE.test(id)) return null;
  if (typeof window !== "string" || !windowIds.has(window)) return null;
  const chord = canonicalAccelerator(accelerator);
  if (!chord || chords.has(chord)) return null;
  chords.add(chord);
  return { id, accelerator, chord, window };
}

/**
 * Parse one app's `manifest.desktop`, or null when anything in it is refused.
 *
 * @param {unknown} raw the `desktop` value from the app's manifest.
 * @returns {{windows: object[], shortcuts: object[]} | null}
 */
function parseDesktopDeclaration(raw) {
  if (!isPlainObject(raw) || !ownKeysWithin(raw, BLOCK_KEYS)) return null;
  const rawWindows = raw.windows;
  if (!Array.isArray(rawWindows) || rawWindows.length < 1 || rawWindows.length > MAX_WINDOWS) {
    return null;
  }
  const windows = [];
  const ids = new Set();
  for (const item of rawWindows) {
    const win = parseWindow(item);
    if (!win || ids.has(win.id)) return null;
    ids.add(win.id);
    windows.push(win);
  }
  const rawShortcuts = raw.shortcuts === undefined ? [] : raw.shortcuts;
  if (!Array.isArray(rawShortcuts) || rawShortcuts.length > MAX_SHORTCUTS) return null;
  const shortcuts = [];
  const shortcutIds = new Set();
  const chords = new Set();
  for (const item of rawShortcuts) {
    const shortcut = parseShortcut(item, ids, chords);
    if (!shortcut || shortcutIds.has(shortcut.id)) return null;
    shortcutIds.add(shortcut.id);
    shortcuts.push(shortcut);
  }
  return { windows, shortcuts };
}

/**
 * The declarations the host should serve, keyed by app name, from `/api/apps`.
 *
 * Only an enabled app the gateway did not skip at boot is served. An app with no
 * `desktop` block, or one the rules above refuse, is absent from the map.
 *
 * @param {unknown} rows the parsed `/api/apps` body.
 * @param {(line: string) => void} [log] told once per refused declaration.
 * @returns {Map<string, {windows: object[], shortcuts: object[]}>}
 */
function desktopDeclarations(rows, log = () => {}) {
  const out = new Map();
  const list = Array.isArray(rows) ? rows : isPlainObject(rows) && Array.isArray(rows.apps) ? rows.apps : [];
  for (const row of list) {
    if (!isPlainObject(row) || row.enabled !== true || row.unavailable) continue;
    const name = row.name;
    if (typeof name !== "string" || !APP_NAME_RE.test(name) || out.has(name)) continue;
    const manifest = isPlainObject(row.manifest) ? row.manifest : null;
    if (!manifest || manifest.desktop === undefined) continue;
    const parsed = parseDesktopDeclaration(manifest.desktop);
    if (parsed) out.set(name, parsed);
    else log(`app-window-host: ${name}: desktop declaration refused`);
  }
  return out;
}

/** Whether the list body is one the host can read at all (vs. an unexpected shape). */
function isAppListBody(body) {
  return Array.isArray(body) || (isPlainObject(body) && Array.isArray(body.apps));
}

module.exports = {
  APP_NAME_RE,
  ID_RE,
  MAX_WINDOWS,
  MAX_SHORTCUTS,
  canonicalAccelerator,
  parseDesktopDeclaration,
  desktopDeclarations,
  isAppListBody,
};
