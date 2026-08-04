/**
 * shortcuts.js — Mochi's global (system-wide) keyboard shortcuts.
 *
 * PORTED from the original src/main/shortcutManager.ts. The original registered
 * four accelerators (screenCapture, voiceInput, toggleWindow, hideAll) from its
 * AppConfig defaults. As a KiroCrew builtin only two of those have a working
 * surface today, so only those are wired here (see ACCELERATORS); the skipped
 * two are documented at the bottom of this comment so the omission is a
 * decision, not an accident.
 *
 * The composer already advertises "CMD+SHIFT+M — show/hide chat window" in its
 * rotating tips, but nothing ever called globalShortcut.register, so the tip was
 * a lie. This module makes it true.
 *
 * Lifecycle: registered when Mochi is enabled (and its windows exist),
 * unregistered on disable and on quit — the reconcile loop in main.js owns
 * those transitions. Two builtin-specific differences from the original:
 *
 *   1. Accelerator strings are FIXED here, not read from a per-app config. The
 *      builtin has no window.shortcuts settings surface yet; the originals are
 *      inlined and a settings-backed override is a follow-up.
 *   2. Unregister targets ONLY Mochi's own accelerators — NOT
 *      globalShortcut.unregisterAll(). The original was a standalone app and
 *      owned the whole global-shortcut table; inside the host app, unregisterAll
 *      would silently drop shortcuts the HOST (or another builtin) registered.
 *
 * Handlers live in main.js (mirroring the original index.ts, which passed
 * handlers into registerShortcuts) because they need the gateway origin and
 * reach across both window modules. This module only owns the register/
 * unregister lifecycle and the accelerator strings.
 */

const { globalShortcut } = require("electron");

/**
 * Default accelerators, ported from the original's shortcut defaults
 * (src/shared/config.ts):
 *   toggleWindow: 'CommandOrControl+Shift+M'  → show/hide the chat panel
 *   hideAll:      'CommandOrControl+Shift+H'  → hide/show all Mochi windows
 *
 * These are DEFAULTS only — the user can rebind both from Settings → Shortcuts,
 * and `registerMochiShortcuts` takes the accelerators it should bind. Keep this
 * table in sync with settings.py `_SHORTCUT_DEFAULTS`; it is the fallback when
 * the gateway read fails, so a divergence would mean a user's first press does
 * something different from what Settings shows.
 *
 * voiceInput 'Option+Space' is NOT bound: push-to-talk needs the OS-level
 * hotkey monitor / dictation controller, neither of which is ported, so
 * there would be nothing for the accelerator to drive.
 */
/**
 * Platform-appropriate defaults. Keep in sync with settings.py
 * `_default_shortcuts()` — this table is the fallback when the gateway read
 * fails, so a divergence means the user's first press does something different
 * from what Settings shows.
 *
 * `CommandOrControl` is portable (Cmd on macOS, Ctrl elsewhere), but the
 * MODIFIER CHOICE is not: Ctrl+Shift+<letter> is heavily used by applications on
 * Windows/Linux, and globalShortcut is EXCLUSIVE — while Mochi holds a combo the
 * focused app never sees it. Alt+Shift is used there instead (Ctrl+Alt is
 * avoided because it types a character on AltGr layouts).
 */
const ACCELERATORS =
  process.platform === "darwin"
    ? {
        toggleWindow: "CommandOrControl+Shift+M",
        hideAll: "CommandOrControl+Shift+H",
        screenCapture: "CommandOrControl+Shift+X",
      }
    : {
        toggleWindow: "Alt+Shift+M",
        hideAll: "Alt+Shift+H",
        screenCapture: "Alt+Shift+X",
      };

/**
 * action -> handler key. ONE list so adding an accelerator is a data change;
 * previously the action names were repeated in three places and the copies drifted.
 */
const MOCHI_SHORTCUT_ACTIONS = [
  ["toggleWindow", "onToggleWindow"],
  ["hideAll", "onHideAll"],
  ["screenCapture", "onScreenCapture"],
];

/**
 * The accelerator strings THIS module actually handed to Electron, per action.
 *
 * Load-bearing for rebinding: unregister must target what was registered, not
 * what the config says now. Walking the current config instead would leave the
 * OLD accelerator bound after a rebind — a live binding pointing at a handler
 * the user can no longer see or change, surviving until the process exits.
 */
let liveAccelerators = {};

let registered = false;

/**
 * Gateway log sink, injected by the shell so a failed registration (accelerator
 * already taken by another app) is visible in gateway-launch.log. Defaults to
 * console.warn so the module works before the shell wires it.
 */
let logFn = (line) => console.warn(line);
function setShortcutLogger(fn) {
  if (typeof fn === "function") logFn = fn;
}

/**
 * Register Mochi's global shortcuts.
 *
 * @param {{onToggleWindow?: () => void, onHideAll?: () => void}} handlers
 * @param {{toggleWindow?: string, hideAll?: string}} [accelerators]
 *   What to bind. Falls back to ACCELERATORS per action; an empty string means
 *   the user unbound that action, and it is skipped without being an error.
 * @returns {{toggleWindow?: boolean, hideAll?: boolean}}
 *   Per-action outcome. `false` means the OS refused the combination — almost
 *   always because another app owns it. Returned rather than only logged so the
 *   Settings UI can tell the user their new key is taken; silently accepting a
 *   failed bind is exactly the "the tip was a lie" problem this module fixed.
 *
 * Each accelerator is wrapped so a throwing handler cannot escape into the
 * global-shortcut dispatcher (which would surface as an unhandled exception in
 * the main process). A registration that fails is logged and skipped, never
 * thrown, so a single taken key never blocks the others or the enable path.
 */
function registerMochiShortcuts(handlers, accelerators) {
  // Re-register cleanly: drop any prior Mochi bindings first so a stale handler
  // (e.g. bound to a window that has since been recreated) is never left live.
  unregisterMochiShortcuts();

  const bindings = MOCHI_SHORTCUT_ACTIONS.map(([action, handlerKey]) => [
    action,
    pickAccelerator(accelerators, action),
    handlers && handlers[handlerKey],
  ]);

  /** @type {Record<string, boolean>} */
  const results = {};
  for (const [action, accelerator, handler] of bindings) {
    if (typeof handler !== "function") continue;
    // Unbound by the user: not a failure, and nothing to report as one.
    if (!accelerator) continue;
    let ok = false;
    try {
      ok = globalShortcut.register(accelerator, () => {
        try {
          handler();
        } catch (err) {
          logFn(`Mochi shortcut ${accelerator} handler threw: ${err && err.message}`);
        }
      });
      if (!ok) {
        logFn(`Mochi shortcut: failed to register ${accelerator} (already in use?)`);
      }
    } catch (err) {
      logFn(`Mochi shortcut: error registering ${accelerator}: ${err && err.message}`);
      ok = false;
    }
    results[action] = !!ok;
    // Record only what Electron accepted — unregistering a string that never
    // registered is harmless, but remembering a rejected one would let a later
    // rebind think it owns a key another app holds.
    if (ok) liveAccelerators[action] = accelerator;
  }
  registered = true;
  return results;
}

/** The configured accelerator for an action, or its default. */
function pickAccelerator(accelerators, action) {
  const value = accelerators && accelerators[action];
  return typeof value === "string" ? canonicalize(value) : ACCELERATORS[action];
}

/** Canonical modifier order, applied on READ. */
// EVERY Electron modifier token, including the aliases (Cmd/Command/Meta,
// Ctrl/Control, Alt/Option, CmdOrCtrl/CommandOrControl). A token missing from
// this list would be treated as the main KEY and reordered after the modifiers,
// corrupting the accelerator.
const MODIFIER_ORDER = [
  "CommandOrControl", "CmdOrCtrl",
  "Command", "Cmd", "Meta", "Super",
  "Control", "Ctrl",
  "Alt", "Option",
  "AltGr",
  "Shift",
];

/**
 * Reorder an accelerator's modifiers.
 *
 * The Settings recorder used to emit them in KEYPRESS order, so a value stored
 * before that was fixed can read e.g. "Shift+CommandOrControl+A". Normalizing
 * here repairs existing config without a migration, and keeps the drift check
 * (which compares the configured string against what was registered) from
 * seeing two spellings of one chord as a permanent change.
 */
function canonicalize(accelerator) {
  if (typeof accelerator !== "string" || accelerator === "") return accelerator;
  const parts = accelerator.split("+");
  const mods = parts.filter((k) => MODIFIER_ORDER.includes(k));
  const rest = parts.filter((k) => !MODIFIER_ORDER.includes(k));
  mods.sort((a, b) => MODIFIER_ORDER.indexOf(a) - MODIFIER_ORDER.indexOf(b));
  return [...mods, ...rest].join("+");
}

/**
 * Unregister ONLY Mochi's accelerators. Deliberately not
 * globalShortcut.unregisterAll(): in the host app that would clobber shortcuts
 * owned by the host or other builtins. Idempotent.
 *
 * Targets `liveAccelerators` — the strings actually registered — NOT the current
 * config, so a rebind releases the key it really bound (see that variable).
 */
function unregisterMochiShortcuts() {
  for (const accelerator of Object.values(liveAccelerators)) {
    try {
      globalShortcut.unregister(accelerator);
    } catch {
      /* not registered / accelerator string rejected — nothing to undo */
    }
  }
  liveAccelerators = {};
  registered = false;
}

/** What is bound right now, per action. Used by the reconcile drift check. */
function currentMochiShortcuts() {
  return { ...liveAccelerators };
}

function areMochiShortcutsRegistered() {
  return registered;
}

module.exports = {
  MOCHI_SHORTCUT_ACTIONS,
  registerMochiShortcuts,
  unregisterMochiShortcuts,
  areMochiShortcutsRegistered,
  currentMochiShortcuts,
  setShortcutLogger,
  ACCELERATORS,
};
