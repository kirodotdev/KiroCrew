/**
 * machineStore.js — Mochi's PER-MACHINE preferences, owned by the shell.
 *
 * Two settings are properties of this computer rather than of any one gateway:
 * `petInstance` (whose Mochi the single on-screen pet shows) and the global
 * accelerators (one keyboard). Both used to live in the Mochi app's own settings
 * on the host gateway, and that placement caused three separate defects:
 *
 *  1. THE ONE-WAY DOOR. All four Mochi windows load FROM the gateway they show
 *     (see pageUrl.js), and the renderer's API seam is same-origin. So once the
 *     pet was moved to a remote, the switcher inside it wrote `petInstance` to
 *     the REMOTE while the shell kept reading the HOST — the write landed
 *     somewhere nothing reads, the UI read back its own write and looked
 *     successful, and there was no surface left that could move the pet back.
 *  2. THE POINTER DIED WITH THE APP. `/api/apps/mochi/settings` is behind
 *     `_require_enabled`, so disabling Mochi on the host made the pointer
 *     unreadable — which is why disabling it removed a pet that was showing a
 *     remote whose own Mochi was still on and still serving it perfectly.
 *  3. IT DID NOT SURVIVE A RESTART for the same reason.
 *
 * Holding them here fixes all three at once: the shell can always read them, and
 * there is exactly one copy no matter which gateway serves the window.
 *
 * PURE FUNCTIONS over an injected store, matching host-config.js — the store is
 * an electron-store instance in production, and a plain Map-like object in tests,
 * so none of this logic needs Electron to be exercised.
 */

/** Sentinel meaning "the gateway serving this window". Mirrors settings.py. */
const SELF_INSTANCE = "self";

// Namespaced so this file can share a store with unrelated shell state without
// either side having to know about the other.
const PET_INSTANCE_KEY = "mochi.petInstance";
const SHORTCUTS_KEY = "mochi.shortcuts";
const MIGRATED_KEY = "mochi.machinePrefsMigrated";
/**
 * Keys the USER has deliberately written since this store was created.
 *
 * Exists because the one-shot migration and the user can race. A non-answer from
 * the gateway must not burn the migration flag (a timeout would then lose an
 * existing choice forever), so the flag can still be false while the pet's
 * windows are already open and editable. Without this set, a settings probe that
 * succeeds LATER would import the stale gateway copy straight over the choice the
 * user just made.
 *
 * Tracked as explicit intent rather than by comparing values: the user's first
 * deliberate pick may BE the default, and a value-based guard cannot tell that
 * apart from "never touched".
 */
const USER_SET_KEY = "mochi.userSetPrefs";

/** Defaults for the electron-store instance the shell creates. */
const MACHINE_STORE_DEFAULTS = Object.freeze({
  [PET_INSTANCE_KEY]: SELF_INSTANCE,
  [SHORTCUTS_KEY]: null,
  [MIGRATED_KEY]: false,
  [USER_SET_KEY]: [],
});

/** Which prefs the user has deliberately written. Always an array of key names. */
function userSetOf(store) {
  const raw = store.get(USER_SET_KEY);
  return Array.isArray(raw) ? raw.filter((k) => typeof k === "string") : [];
}

function markUserSet(store, key) {
  const seen = userSetOf(store);
  if (!seen.includes(key)) store.set(USER_SET_KEY, [...seen, key]);
}

/**
 * The chosen instance id, or `"self"`.
 *
 * SELF IS THE FLOOR, and the value is deliberately NOT validated against the
 * live instance list — instances come and go (TTL expiry, tunnel down) and a
 * saved choice must survive one being briefly away. Resolution is where the
 * fallback happens, so "no usable instance" is never a state the pet gets stuck
 * in. Anything that is not a non-empty string reads as `self` rather than
 * throwing: a corrupted store must not cost the user their pet.
 */
function petInstanceOf(store) {
  const raw = store.get(PET_INSTANCE_KEY);
  return typeof raw === "string" && raw.trim() ? raw : SELF_INSTANCE;
}

/**
 * @param {object} store
 * @param {string} instanceId
 * @param {{ byUser?: boolean }} [opts] `byUser` records deliberate intent, so a
 *   later migration cannot import the stale gateway copy over this value.
 */
function setPetInstanceIn(store, instanceId, opts = {}) {
  const next = typeof instanceId === "string" && instanceId.trim() ? instanceId : SELF_INSTANCE;
  store.set(PET_INSTANCE_KEY, next);
  if (opts.byUser) markUserSet(store, PET_INSTANCE_KEY);
  return next;
}

/**
 * The user's accelerators, or `undefined` to let shortcuts.js use its own
 * defaults.
 *
 * UNDEFINED, NOT `{}`: an empty object reads as "the user unbound everything"
 * and would silently leave the app with no shortcuts at all. Only string values
 * survive, so one corrupted entry cannot make the whole set unusable.
 */
function shortcutsOf(store) {
  const raw = store.get(SHORTCUTS_KEY);
  if (!raw || typeof raw !== "object") return undefined;
  const out = {};
  for (const [action, accel] of Object.entries(raw)) {
    if (typeof accel === "string") out[action] = accel;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

function setShortcutsIn(store, accelerators, opts = {}) {
  if (opts.byUser) markUserSet(store, SHORTCUTS_KEY);
  if (!accelerators || typeof accelerators !== "object") {
    store.set(SHORTCUTS_KEY, null);
    return undefined;
  }
  const clean = {};
  for (const [action, accel] of Object.entries(accelerators)) {
    if (typeof accel === "string") clean[action] = accel;
  }
  store.set(SHORTCUTS_KEY, Object.keys(clean).length > 0 ? clean : null);
  return shortcutsOf(store);
}

/**
 * ONE-SHOT seed from the host gateway's Mochi settings, so an existing user's
 * choices are not silently reset to the defaults on the upgrade that moves them.
 *
 * Runs only while the host's Mochi is ENABLED, because that is the only time its
 * settings route answers. If the app is disabled at first launch after the
 * upgrade, migration simply waits — the pointer defaults to `self` meanwhile,
 * which is the same thing a disabled Mochi produced before this change.
 *
 * Guarded by a persisted flag rather than by "is the store still at its
 * default": the user's very first deliberate choice may BE the default, and a
 * value-based guard would then re-import the stale gateway copy over it forever.
 *
 * NON-DESTRUCTIVE per key. Because a non-answer deliberately does NOT burn the
 * flag, the pet's windows can already be open and editable while migration is
 * still pending — so a probe that succeeds later would otherwise import the stale
 * gateway copy straight over a choice the user just made. Any key the user has
 * written (see `USER_SET_KEY`) is therefore skipped, and the flag is still set so
 * this stops retrying.
 *
 * @param {object} store
 * @param {object|null} settings host Mochi settings, or null if unreadable
 * @returns {boolean} true when this call performed the migration
 */
function migrateMachinePrefs(store, settings) {
  if (store.get(MIGRATED_KEY) === true) return false;
  // A NON-ANSWER must not count as "nothing to migrate" — that would burn the
  // one-shot flag on a timeout and lose the user's stored choice permanently.
  if (!settings || typeof settings !== "object") return false;

  const userSet = userSetOf(store);
  if (
    !userSet.includes(PET_INSTANCE_KEY) &&
    typeof settings.petInstance === "string" &&
    settings.petInstance.trim()
  ) {
    setPetInstanceIn(store, settings.petInstance);
  }
  if (
    !userSet.includes(SHORTCUTS_KEY) &&
    settings.shortcuts &&
    typeof settings.shortcuts === "object"
  ) {
    setShortcutsIn(store, settings.shortcuts);
  }
  store.set(MIGRATED_KEY, true);
  return true;
}

/**
 * Which accelerators to persist after a rebind attempt: the ones the OS accepted,
 * with a refused action falling back to whatever was working before.
 *
 * `prev` is whatever `shortcutsOf` returned, INCLUDING `undefined` — that is its
 * documented answer for "nothing stored yet", which is the normal state until a
 * first rebind or a migration, and a migration only runs while the host's Mochi
 * is enabled. Reading a property off it directly would throw exactly in this
 * PR's headline scenario (pet on a remote, host Mochi off), and the throw would
 * be swallowed by the caller's catch: no refusal would reach the panel, which
 * would then close as though the rebind had been saved.
 *
 * An action ABSENT from `result` was never attempted (no handler, or deliberately
 * unbound) and is not a refusal. Actions with no value on either side are omitted
 * rather than stored as undefined, so shortcuts.js applies its own defaults.
 *
 * @param {Record<string, string> | undefined} prev
 * @param {Record<string, string>} desired
 * @param {Record<string, boolean>} result Per-action register() outcome.
 * @param {string[]} actions The known action names.
 */
function mergeAcceptedShortcuts(prev, desired, result, actions) {
  const before = prev && typeof prev === "object" ? prev : {};
  const req = desired && typeof desired === "object" ? desired : {};
  const outcome = result && typeof result === "object" ? result : {};
  const out = {};
  for (const action of actions) {
    const value = outcome[action] === false ? before[action] : req[action] ?? before[action];
    if (typeof value === "string") out[action] = value;
  }
  return out;
}

module.exports = {
  SELF_INSTANCE,
  MACHINE_STORE_DEFAULTS,
  PET_INSTANCE_KEY,
  SHORTCUTS_KEY,
  MIGRATED_KEY,
  USER_SET_KEY,
  petInstanceOf,
  setPetInstanceIn,
  shortcutsOf,
  setShortcutsIn,
  mergeAcceptedShortcuts,
  userSetOf,
  migrateMachinePrefs,
};
