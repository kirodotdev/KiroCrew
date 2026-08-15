/**
 * global-hotkey.js — the host app's system-wide summon shortcut.
 *
 * Registers ONE global accelerator that shows and focuses the dashboard
 * window from anywhere (creating it when none exists). Follows the same
 * lifecycle contract as mochi/shortcuts.js, the other globalShortcut owner in
 * this process:
 *
 *   - Teardown targets ONLY the accelerator THIS module registered — NEVER
 *     globalShortcut.unregisterAll(), which would silently drop the shortcuts
 *     the Mochi builtin (or any other module) holds. Mochi's test asserts that
 *     invariant for its side; this module honours it symmetrically, and its
 *     test asserts it too.
 *   - A registration that fails (accelerator taken by another application, or
 *     a malformed accelerator string that makes register() throw) is logged
 *     and degraded, never thrown: a hotkey that cannot be bound is a missing
 *     convenience, not a startup failure.
 *
 * The binding is configurable: main.js persists it in the same electron-store
 * that holds the other desktop preferences (key `globalHotkey`; see the store
 * defaults in main.js) and hands the stored value to bindGlobalHotkey(). An
 * invalid or unbindable stored value falls back to the platform default.
 */

const { globalShortcut } = require("electron");

/**
 * Platform-appropriate default, mirroring the modifier rationale documented in
 * mochi/shortcuts.js: `CommandOrControl` is portable, but the MODIFIER CHOICE
 * is not. globalShortcut is EXCLUSIVE — while this app holds a combo, the
 * focused application never sees it — and Ctrl+Shift+<letter> chords are
 * heavily used by applications on Windows/Linux (Ctrl+Shift+K opens the web
 * console in Firefox), so claiming one system-wide would steal it from every
 * other program. Alt+Shift is used there instead; macOS gets Cmd+Shift+K.
 */
const DEFAULT_GLOBAL_HOTKEY =
  process.platform === "darwin" ? "CommandOrControl+Shift+K" : "Alt+Shift+K";

/**
 * The accelerator string this module actually handed to Electron, or "" when
 * nothing is bound. Unregister must target what was REGISTERED, not what the
 * store says now — after a rebind, walking the current config would leave the
 * old accelerator live until the process exits (see mochi/shortcuts.js for the
 * same invariant).
 */
let liveAccelerator = "";

/**
 * Log sink, injected by main.js so a failed registration is visible in
 * gateway-launch.log. Defaults to console.warn so the module works standalone.
 */
let logFn = (line) => console.warn(line);
function setGlobalHotkeyLogger(fn) {
  if (typeof fn === "function") logFn = fn;
}

/**
 * Resolve the accelerator a stored preference asks for. Pure.
 *
 * @param {*} saved  Raw value read from persistent storage.
 * @returns {string} "" ONLY when the user explicitly unbound the hotkey (a
 *   stored empty string); the stored accelerator when it is a plausible
 *   non-empty string; the platform default otherwise — unset, a non-string
 *   value from a corrupted store, or a whitespace-only string (mangled, not a
 *   deliberate unbind — the deliberate spelling is exactly ""). Whether the
 *   string is a VALID accelerator is decided by Electron at register time —
 *   bindGlobalHotkey() falls back on failure.
 */
function resolveGlobalHotkey(saved) {
  if (saved === "") return "";
  if (typeof saved !== "string") return DEFAULT_GLOBAL_HOTKEY;
  const trimmed = saved.trim();
  return trimmed === "" ? DEFAULT_GLOBAL_HOTKEY : trimmed;
}

/**
 * Build the summon handler: surface an existing dashboard window (restoring it
 * first when minimized), or create one when none exists. Window discovery and
 * creation are injected so main.js keeps ownership of its window-management
 * helpers and this logic stays unit-testable without a live Electron app.
 *
 * @param {{
 *   getWindow: () => ({isDestroyed?: () => boolean, isMinimized?: () => boolean,
 *                      restore?: () => void, show: () => void, focus: () => void}|null|undefined),
 *   createWindow: () => void,
 *   focusApp?: () => void,
 * }} deps  `focusApp` runs after the window is surfaced — on macOS the app
 *   must also steal application focus or the window rises behind the frontmost
 *   app without keyboard focus.
 */
function createSummonHandler({ getWindow, createWindow, focusApp }) {
  return () => {
    const win = typeof getWindow === "function" ? getWindow() : null;
    if (win && !(typeof win.isDestroyed === "function" && win.isDestroyed())) {
      if (typeof win.isMinimized === "function" && win.isMinimized() && win.restore) {
        win.restore();
      }
      win.show();
      win.focus();
    } else if (typeof createWindow === "function") {
      createWindow();
    }
    if (typeof focusApp === "function") focusApp();
  };
}

/**
 * Register one accelerator with a crash-proofed handler. A throwing handler is
 * caught so it cannot escape into the global-shortcut dispatcher (an unhandled
 * exception in the main process); a failed or throwing register() is logged
 * and reported as false, never propagated.
 */
function tryRegister(accelerator, handler) {
  let ok = false;
  try {
    ok = globalShortcut.register(accelerator, () => {
      try {
        handler();
      } catch (err) {
        logFn(`global hotkey ${accelerator} handler threw: ${err && err.message}`);
      }
    });
    if (!ok) {
      logFn(`global hotkey: failed to register ${accelerator} (already in use?)`);
    }
  } catch (err) {
    logFn(`global hotkey: error registering ${accelerator}: ${err && err.message}`);
    ok = false;
  }
  return !!ok;
}

/**
 * Bind the summon hotkey from a stored preference, with fallback.
 *
 * Order: the stored accelerator first; when it cannot be bound (taken by
 * another app, or malformed so register() throws) the platform default is
 * tried instead, so a bad stored value degrades to the documented default
 * rather than to nothing. When even the default is taken, the app starts with
 * no hotkey — logged, never fatal. An explicitly unbound preference (stored
 * "") skips registration entirely and is not an error.
 *
 * @returns {{accelerator: string, bound: boolean}}  What is live now:
 *   `accelerator` is "" when nothing is bound.
 */
function bindGlobalHotkey(saved, handler) {
  // Re-register cleanly so a rebind releases the key it really bound.
  unregisterGlobalHotkey();
  const wanted = resolveGlobalHotkey(saved);
  if (!wanted) return { accelerator: "", bound: false };
  if (tryRegister(wanted, handler)) {
    liveAccelerator = wanted;
    return { accelerator: wanted, bound: true };
  }
  if (wanted !== DEFAULT_GLOBAL_HOTKEY && tryRegister(DEFAULT_GLOBAL_HOTKEY, handler)) {
    logFn(
      `global hotkey: falling back to default ${DEFAULT_GLOBAL_HOTKEY} ` +
        `(stored value ${JSON.stringify(wanted)} could not be bound)`,
    );
    liveAccelerator = DEFAULT_GLOBAL_HOTKEY;
    return { accelerator: DEFAULT_GLOBAL_HOTKEY, bound: true };
  }
  return { accelerator: "", bound: false };
}

/**
 * Unregister ONLY this module's accelerator. Deliberately not
 * globalShortcut.unregisterAll(): that would clobber the registrations other
 * modules in this process hold (Mochi's shortcuts). Idempotent, and safe to
 * call before any register.
 */
function unregisterGlobalHotkey() {
  if (liveAccelerator) {
    try {
      globalShortcut.unregister(liveAccelerator);
    } catch {
      /* not registered / accelerator string rejected — nothing to undo */
    }
  }
  liveAccelerator = "";
}

/** What is bound right now ("" when nothing is). Read by the shortcuts UI IPC. */
function currentGlobalHotkey() {
  return liveAccelerator;
}

module.exports = {
  DEFAULT_GLOBAL_HOTKEY,
  resolveGlobalHotkey,
  createSummonHandler,
  bindGlobalHotkey,
  unregisterGlobalHotkey,
  currentGlobalHotkey,
  setGlobalHotkeyLogger,
};
