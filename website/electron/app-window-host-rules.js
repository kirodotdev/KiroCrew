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
const WINDOW_KINDS = new Set(["overlay", "panel", "background"]);
const OVERLAY_DISPLAYS = new Set(["cursor", "all"]);
const PANEL_POSITIONS = new Set(["bottom-right", "top-right", "center"]);
const PANEL_WIDTH = [160, 1600];
const PANEL_HEIGHT = [120, 1200];
const PANEL_GUTTER = [0, 48];
const WINDOW_KEYS = new Set([
  "id", "entry", "kind", "openOnEnable", "width", "height", "position", "transparent",
  "gutter", "displays",
]);
const PANEL_ONLY_KEYS = ["width", "height", "position", "transparent", "gutter"];
const OVERLAY_ONLY_KEYS = ["displays"];
const SHORTCUT_KEYS = new Set(["id", "accelerator", "platformAccelerators", "window"]);
const BLOCK_KEYS = new Set(["windows", "shortcuts"]);
/** A shortcut's per-platform default overrides `accelerator`; keyed by these names. */
const PLATFORMS = Object.freeze(["macos", "windows", "linux"]);
/** What a hosted window may ask the shell for; mirrors `apps/desktop_capabilities.py`. */
const CAPABILITIES = Object.freeze(["screenCapture", "localFiles", "gatewayChoice"]);

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

/**
 * The keys *accelerator* presses on *platform* ("darwin", or anything else): each
 * modifier as the physical key it is there, then the key. "" for a chord the rule
 * above refuses.
 */
function pressedChord(accelerator, platform) {
  const chord = canonicalAccelerator(accelerator);
  if (!chord) return "";
  const parts = chord.split("+");
  const key = parts.pop();
  const table = SAME_KEY[platform === "darwin" ? 0 : 1];
  return [...new Set(parts.map((m) => table[m] || m))].sort().concat(key).join("+");
}

/**
 * Every chord, in `pressedChord` form, that one native key press spells: Electron's
 * `before-input-event` input, which page script cannot synthesize. The key is read
 * from both the physical position (`code`) and the character (`key`), so a page
 * recording either spelling of the press finds it. Empty for anything but a fresh
 * key-down of a letter, digit, function, arrow or Space key.
 */
function keyPressChords(input, platform) {
  if (!isPlainObject(input) || input.type !== "keyDown" || input.isAutoRepeat) return [];
  const mods = [];
  if (input.control) mods.push("Ctrl");
  if (input.meta) mods.push(platform === "darwin" ? "Cmd" : "Super");
  if (input.alt) mods.push("Alt");
  if (input.shift) mods.push("Shift");
  const keys = new Set();
  const code = typeof input.code === "string" ? input.code : "";
  const spelled = /^(?:Key([A-Z])|Digit([0-9])|Arrow(Up|Down|Left|Right))$/.exec(code);
  if (spelled) keys.add(spelled[1] || spelled[2] || spelled[3]);
  else if (FUNCTION_KEY_RE.test(code.toLowerCase())) keys.add(code.toUpperCase());
  else if (code === "Space") keys.add("Space");
  const key = typeof input.key === "string" ? input.key : "";
  if (/^[A-Za-z0-9]$/.test(key)) keys.add(key.toUpperCase());
  mods.sort();
  return [...keys].map((k) => [...mods, k].join("+"));
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
  if (kind !== "panel" && PANEL_ONLY_KEYS.some((k) => k in raw)) return null;
  if (kind !== "overlay" && OVERLAY_ONLY_KEYS.some((k) => k in raw)) return null;
  if (kind === "background") return win;
  if (kind === "overlay") {
    const displays = raw.displays === undefined ? "cursor" : raw.displays;
    return OVERLAY_DISPLAYS.has(displays) ? { ...win, displays } : null;
  }
  const width = raw.width === undefined ? 400 : raw.width;
  const height = raw.height === undefined ? 560 : raw.height;
  const position = raw.position === undefined ? "bottom-right" : raw.position;
  const transparent = raw.transparent === undefined ? false : raw.transparent;
  const gutter = raw.gutter === undefined ? 0 : raw.gutter;
  if (!inRange(width, PANEL_WIDTH) || !inRange(height, PANEL_HEIGHT)) return null;
  if (!PANEL_POSITIONS.has(position) || typeof transparent !== "boolean") return null;
  if (!inRange(gutter, PANEL_GUTTER)) return null;
  return { ...win, width, height, position, transparent, gutter };
}

/** The manifest's platform name for a Node `process.platform`. */
function platformName(platform) {
  if (platform === "darwin") return "macos";
  return platform === "win32" ? "windows" : "linux";
}

/**
 * One declared shortcut with its default resolved for *platform*, or null.
 *
 * `platformAccelerators` overrides `accelerator` on the platforms it names. Every
 * platform's default is checked, and no two shortcuts of the app may share one on
 * any platform (`chords` holds a Set per platform name).
 */
function parseShortcut(raw, windowIds, chords, platform) {
  if (!isPlainObject(raw) || !ownKeysWithin(raw, SHORTCUT_KEYS)) return null;
  const { id, accelerator, window } = raw;
  if (typeof id !== "string" || !ID_RE.test(id)) return null;
  if (typeof window !== "string" || !windowIds.has(window)) return null;
  const perPlatform = raw.platformAccelerators === undefined ? {} : raw.platformAccelerators;
  if (!isPlainObject(perPlatform) || !ownKeysWithin(perPlatform, new Set(PLATFORMS))) return null;
  const defaults = {};
  for (const name of PLATFORMS) {
    const accel = Object.prototype.hasOwnProperty.call(perPlatform, name) ? perPlatform[name] : accelerator;
    const chord = canonicalAccelerator(accel);
    if (!chord || chords[name].has(chord)) return null;
    chords[name].add(chord);
    defaults[name] = { accelerator: accel, chord };
  }
  // `accelerator` itself must be valid even where every platform overrides it.
  if (!canonicalAccelerator(accelerator)) return null;
  const here = defaults[platformName(platform)];
  return { id, accelerator: here.accelerator, chord: here.chord, window };
}

/**
 * Parse one app's `manifest.desktop`, or null when anything in it is refused.
 *
 * @param {unknown} raw the `desktop` value from the app's manifest.
 * @param {string} [platform] the Node platform each shortcut's default is resolved for.
 * @returns {{windows: object[], shortcuts: object[]} | null}
 */
function parseDesktopDeclaration(raw, platform = process.platform) {
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
  // One of each: the display the avatar is on, and the page outliving every display.
  if (windows.filter((w) => w.displays === "all").length > 1) return null;
  if (windows.filter((w) => w.kind === "background").length > 1) return null;
  const rawShortcuts = raw.shortcuts === undefined ? [] : raw.shortcuts;
  if (!Array.isArray(rawShortcuts) || rawShortcuts.length > MAX_SHORTCUTS) return null;
  const shortcuts = [];
  const shortcutIds = new Set();
  const chords = Object.fromEntries(PLATFORMS.map((name) => [name, new Set()]));
  for (const item of rawShortcuts) {
    const shortcut = parseShortcut(item, ids, chords, platform);
    if (!shortcut || shortcutIds.has(shortcut.id)) return null;
    shortcutIds.add(shortcut.id);
    shortcuts.push(shortcut);
  }
  return { windows, shortcuts };
}

/** The known capabilities in a list, in `CAPABILITIES` order; anything else names none. */
function knownCapabilities(value) {
  return Array.isArray(value) ? CAPABILITIES.filter((cap) => value.includes(cap)) : [];
}

/**
 * The capabilities a row both declares and has approved.
 *
 * Both halves are required: `manifest.permissions.desktopCapabilities` says what the
 * app asks for, `desktopCapabilitiesGrant.approved` what the owner approved. A
 * gateway that predates the grant publishes no `desktopCapabilitiesGrant`, so it
 * grants nothing.
 */
function approvedCapabilities(row, manifest) {
  const permissions = isPlainObject(manifest.permissions) ? manifest.permissions : {};
  const declared = knownCapabilities(permissions.desktopCapabilities);
  const grant = isPlainObject(row.desktopCapabilitiesGrant) ? row.desktopCapabilitiesGrant : {};
  const approved = knownCapabilities(grant.approved);
  return declared.filter((cap) => approved.includes(cap));
}

/**
 * The declarations the host should serve, keyed by app name, from `/api/apps`.
 *
 * Only an enabled app the gateway did not skip at boot is served. An app with no
 * `desktop` block, or one the rules above refuse, is absent from the map. Each
 * declaration carries `capabilities`: the ones the app declares AND has approved.
 *
 * @param {unknown} rows the parsed `/api/apps` body.
 * @param {(line: string) => void} [log] told once per refused declaration.
 * @param {{platform?: string}} [options] `platform`: the Node platform shortcut
 *   defaults are resolved for.
 * @returns {Map<string, {windows: object[], shortcuts: object[], capabilities: string[]}>}
 */
function desktopDeclarations(rows, log = () => {}, { platform = process.platform } = {}) {
  const out = new Map();
  const list = Array.isArray(rows) ? rows : isPlainObject(rows) && Array.isArray(rows.apps) ? rows.apps : [];
  for (const row of list) {
    if (!isPlainObject(row) || row.enabled !== true || row.unavailable) continue;
    const name = row.name;
    if (typeof name !== "string" || !APP_NAME_RE.test(name) || out.has(name)) continue;
    const manifest = isPlainObject(row.manifest) ? row.manifest : null;
    if (!manifest || manifest.desktop === undefined) continue;
    const parsed = parseDesktopDeclaration(manifest.desktop, platform);
    if (parsed) out.set(name, { ...parsed, capabilities: approvedCapabilities(row, manifest) });
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
  CAPABILITIES,
  ID_RE,
  MAX_WINDOWS,
  MAX_SHORTCUTS,
  approvedCapabilities,
  PLATFORMS,
  canonicalAccelerator,
  knownCapabilities,
  parseDesktopDeclaration,
  platformName,
  desktopDeclarations,
  isAppListBody,
  keyPressChords,
  pressedChord,
};
