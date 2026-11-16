/**
 * hostHandOff.js: hand Mochi's per-machine choices to the desktop window host.
 *
 * The built-in keeps the crew the pet shows and the user's rebinds in its own
 * `mochi-machine.json` (machineStore.js). Once Mochi's own repository copy is
 * installed, the window host serves it: the crew its windows load from is the
 * host's gateway choice for `mochi` and its shortcuts are the host's bindings for
 * the ids that copy declares. With nothing there, an adopted Mochi would show this
 * computer's crew and fixed default keys.
 *
 * So at every start, before main.js creates the host (which reads both stores once),
 * each choice is copied into the host's store and the value copied is recorded
 * under HAND_OFF_KEY in Mochi's own store. A copy is redone while the host still
 * holds the value last copied: before adoption the built-in is what the user
 * changes, so a later change reaches the host on the next start. Once the host
 * holds something else, the user chose it through the host (a reset, "self", a
 * new chord) and it is never overwritten. With no record yet, a host that already
 * holds a value of its own keeps it. After adoption the built-in no longer writes
 * its store, so a copy can only repeat what the host has.
 *
 * The host reads these entries only for an installed app named `mochi` that
 * declares the shortcut and, for the crew, holds the approved `gatewayChoice`
 * capability, so writing them ahead of time changes nothing for anyone else. Lives
 * in Mochi's shell directory and leaves with it.
 */

const { createGatewayChoices, SELF_GATEWAY } = require("../app-window-gateways");
const { canonicalAccelerator } = require("../app-window-host-rules");
const { createMachinePrefs } = require("../app-window-prefs");
const { petInstanceOf, shortcutsOf } = require("./machineStore");

const APP_ID = "mochi";
const HAND_OFF_KEY = "mochi.hostHandOff";
/** Mochi's shortcut actions as the ids its repository's manifest declares. */
const HOST_SHORTCUT_IDS = Object.freeze({
  toggleWindow: "toggle-window",
  hideAll: "hide-all",
  screenCapture: "screen-capture",
});

function isPlainObject(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function hasOwn(obj, key) {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

/**
 * Whether the host's *current* value may be replaced: it still holds what was last
 * copied, or, before any copy, it holds nothing of its own.
 */
function hostStillSeeded(current, record, key, emptyValue) {
  return hasOwn(record, key) ? current === record[key] : current === emptyValue;
}

/**
 * The host binding the built-in's *accelerator* for *action* means: null for its
 * default (no binding), "" for unbound, else the chord. A chord the host's rules
 * refuse (a single-modifier one) is null, so the host's default applies.
 */
function bindingFor(action, accelerator, defaults, log) {
  if (typeof accelerator !== "string") return null;
  if (accelerator === "") return "";
  const chord = canonicalAccelerator(accelerator);
  if (!chord) {
    log(`mochi: host hand-off: ${action} binding ${accelerator} refused by the host's rules; its default applies`);
    return null;
  }
  return chord === canonicalAccelerator(defaults[action] || "") ? null : accelerator;
}

/**
 * Seed the host's gateway choice and shortcut bindings for Mochi from the built-in's.
 *
 * @param {{machineStore: object, gatewayStore?: object, prefsStore?: object,
 *          defaults: Record<string, string>, log?: (line: string) => void}} deps
 *   `defaults`: the built-in's accelerator per action on this platform.
 * @returns {string[]} what was written to the host: "gatewayChoice" and shortcut ids.
 */
function seedHostMachinePrefs(deps) {
  const { machineStore, gatewayStore, prefsStore, defaults } = deps;
  const log = deps.log || (() => {});
  const raw = machineStore.get(HAND_OFF_KEY);
  const record = isPlainObject(raw) ? raw : {};
  const recordedShortcuts = isPlainObject(record.shortcuts) ? record.shortcuts : {};
  const next = { ...record, shortcuts: { ...recordedShortcuts } };
  const written = [];

  if (gatewayStore) {
    const choices = createGatewayChoices(gatewayStore);
    const current = choices.get(APP_ID);
    const legacy = petInstanceOf(machineStore);
    if (hostStillSeeded(current, record, "gatewayChoice", SELF_GATEWAY)) {
      if (current !== legacy) {
        choices.set(APP_ID, legacy);
        written.push("gatewayChoice");
      }
      next.gatewayChoice = legacy;
    }
  }

  if (prefsStore) {
    const bindings = createMachinePrefs(prefsStore);
    const legacy = shortcutsOf(machineStore) || {};
    for (const [action, id] of Object.entries(HOST_SHORTCUT_IDS)) {
      const have = bindings.bindingsOf(APP_ID);
      const current = hasOwn(have, id) ? have[id] : null;
      const wanted = bindingFor(action, legacy[action], defaults, log);
      if (!hostStillSeeded(current, recordedShortcuts, id, null)) continue;
      if (current !== wanted) {
        bindings.setBinding(APP_ID, id, wanted);
        written.push(id);
      }
      next.shortcuts[id] = wanted;
    }
  }

  if (JSON.stringify(next) !== JSON.stringify({ ...record, shortcuts: recordedShortcuts })) {
    machineStore.set(HAND_OFF_KEY, next);
  }
  return written;
}

module.exports = { APP_ID, HAND_OFF_KEY, HOST_SHORTCUT_IDS, seedHostMachinePrefs };
