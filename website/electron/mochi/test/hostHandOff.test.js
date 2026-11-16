/**
 * Mochi's per-machine choices survive a move onto the desktop window host.
 *
 * The shell's `mochi-machine.json` (machineStore.js) holds the crew the pet shows
 * and the user's rebinds. At every start Mochi's own shell code copies them into
 * the host's gateway-choice and binding stores (hostHandOff.js), so an adopted
 * Mochi the host serves keeps both. The NEGATIVE half pins that a choice the user
 * made through the host (a reset, "self", a new chord) is never copied over.
 */

const test = require("node:test");
const assert = require("node:assert");

const { setPetInstanceIn, setShortcutsIn } = require("../machineStore");
const { createGatewayChoices } = require("../../app-window-gateways");
const { parseDesktopDeclaration } = require("../../app-window-host-rules");
const { BINDINGS_KEY, boundShortcuts, createMachinePrefs } = require("../../app-window-prefs");
const { HAND_OFF_KEY, HOST_SHORTCUT_IDS, seedHostMachinePrefs } = require("../hostHandOff");

function mapStore(initial = {}) {
  const map = new Map(Object.entries(initial));
  return { get: (k) => map.get(k), set: (k, v) => map.set(k, v), map };
}

/** The built-in's defaults on macOS, as shortcuts.js holds them there. */
const MAC_DEFAULTS = {
  toggleWindow: "CommandOrControl+Shift+M",
  hideAll: "CommandOrControl+Shift+H",
  screenCapture: "CommandOrControl+Shift+X",
};

/** The `desktop` block an adopted Mochi declares: the built-in's defaults, per platform. */
const ADOPTED_DESKTOP = {
  windows: [
    { id: "pet", entry: "desktop/pet.mjs", kind: "overlay", displays: "all", openOnEnable: true },
    { id: "panel", entry: "desktop/panel.mjs", kind: "panel" },
  ],
  shortcuts: [
    { id: "toggle-window", accelerator: "Alt+Shift+M", platformAccelerators: { macos: "CommandOrControl+Shift+M" }, window: "panel" },
    { id: "hide-all", accelerator: "Alt+Shift+H", platformAccelerators: { macos: "CommandOrControl+Shift+H" }, window: "pet" },
    { id: "screen-capture", accelerator: "Alt+Shift+X", platformAccelerators: { macos: "CommandOrControl+Shift+X" }, window: "pet" },
  ],
};

function stores() {
  return { machineStore: mapStore(), gatewayStore: mapStore(), prefsStore: mapStore() };
}

function seed(s, log) {
  return seedHostMachinePrefs({ ...s, defaults: MAC_DEFAULTS, log });
}

const hostGateway = (s) => createGatewayChoices(s.gatewayStore).get("mochi");
const hostBindings = (s) => createMachinePrefs(s.prefsStore).bindingsOf("mochi");

test("the adopted declaration's defaults and ids are the built-in's, on this platform", () => {
  const { ACCELERATORS, MOCHI_SHORTCUT_ACTIONS } = require("../shortcuts");
  const decl = parseDesktopDeclaration(ADOPTED_DESKTOP, process.platform);
  assert.deepStrictEqual(
    decl.shortcuts.map((s) => s.accelerator),
    MOCHI_SHORTCUT_ACTIONS.map(([action]) => ACCELERATORS[action]),
  );
  assert.deepStrictEqual(
    decl.shortcuts.map((s) => s.id),
    MOCHI_SHORTCUT_ACTIONS.map(([action]) => HOST_SHORTCUT_IDS[action]),
  );
});

test("the crew the pet shows and the user's rebinds reach the host's stores", () => {
  const s = stores();
  setPetInstanceIn(s.machineStore, "crew-3", { byUser: true });
  setShortcutsIn(s.machineStore, {
    toggleWindow: "Alt+Shift+CommandOrControl+J", hideAll: "", screenCapture: MAC_DEFAULTS.screenCapture,
  }, { byUser: true });
  assert.deepStrictEqual(seed(s), ["gatewayChoice", "toggle-window", "hide-all"]);
  assert.strictEqual(hostGateway(s), "crew-3");
  // The built-in's default is no binding: the adopted manifest's default applies.
  assert.deepStrictEqual(hostBindings(s), { "toggle-window": "Alt+Shift+CommandOrControl+J", "hide-all": "" });
  const decl = parseDesktopDeclaration(ADOPTED_DESKTOP, "darwin");
  assert.deepStrictEqual(
    boundShortcuts(decl.shortcuts, hostBindings(s)).map((x) => [x.id, x.accelerator]),
    [["toggle-window", "Alt+Shift+CommandOrControl+J"], ["screen-capture", "CommandOrControl+Shift+X"]],
  );
  // A second start with nothing new writes nothing.
  assert.deepStrictEqual(seed(s), []);
});

test("a change in the built-in before adoption reaches the host on the next start", () => {
  const s = stores();
  setPetInstanceIn(s.machineStore, "crew-3");
  seed(s);
  setPetInstanceIn(s.machineStore, "self");
  setShortcutsIn(s.machineStore, { toggleWindow: "Alt+Shift+CommandOrControl+K" });
  assert.deepStrictEqual(seed(s), ["gatewayChoice", "toggle-window"]);
  assert.strictEqual(hostGateway(s), "self");
  assert.deepStrictEqual(hostBindings(s), { "toggle-window": "Alt+Shift+CommandOrControl+K" });
});

test("NEGATIVE: a choice the user made through the host is never copied over", () => {
  const s = stores();
  setPetInstanceIn(s.machineStore, "crew-3");
  setShortcutsIn(s.machineStore, { toggleWindow: "Alt+Shift+CommandOrControl+J", hideAll: "" });
  seed(s);
  // Through the host: "self", a reset to the default, a new chord.
  createGatewayChoices(s.gatewayStore).set("mochi", "self");
  const host = createMachinePrefs(s.prefsStore);
  host.setBinding("mochi", "toggle-window", null);
  host.setBinding("mochi", "hide-all", "Alt+Shift+CommandOrControl+H");
  assert.deepStrictEqual(seed(s), []);
  assert.strictEqual(hostGateway(s), "self");
  assert.deepStrictEqual(hostBindings(s), { "hide-all": "Alt+Shift+CommandOrControl+H" });
  // Not even when the built-in's own value moves later.
  setPetInstanceIn(s.machineStore, "crew-9");
  setShortcutsIn(s.machineStore, { toggleWindow: "Alt+Shift+CommandOrControl+L" });
  assert.deepStrictEqual(seed(s), []);
  assert.strictEqual(hostGateway(s), "self");
});

test("NEGATIVE: before any copy, a host holding a value of its own keeps it", () => {
  const s = stores();
  createGatewayChoices(s.gatewayStore).set("mochi", "crew-1");
  createMachinePrefs(s.prefsStore).setBinding("mochi", "toggle-window", "Alt+Shift+CommandOrControl+K");
  setPetInstanceIn(s.machineStore, "crew-3");
  setShortcutsIn(s.machineStore, { toggleWindow: "Alt+Shift+CommandOrControl+J", hideAll: "" });
  assert.deepStrictEqual(seed(s), ["hide-all"]);
  assert.strictEqual(hostGateway(s), "crew-1");
  assert.deepStrictEqual(hostBindings(s), { "toggle-window": "Alt+Shift+CommandOrControl+K", "hide-all": "" });
});

test("NEGATIVE: a built-in rebind the host's rules refuse leaves the host's default", () => {
  const s = stores();
  const lines = [];
  setShortcutsIn(s.machineStore, { toggleWindow: "CommandOrControl+M" });
  assert.deepStrictEqual(seed(s, (line) => lines.push(line)), []);
  assert.deepStrictEqual(hostBindings(s), {});
  assert.ok(lines.some((l) => l.includes("toggleWindow binding CommandOrControl+M refused")));
});

test("only Mochi's own entries are written, and a missing host store is skipped", () => {
  const s = stores();
  s.gatewayStore.set("appWindowGatewayChoices", { other: "crew-1" });
  s.prefsStore.set(BINDINGS_KEY, { other: { go: "" } });
  setPetInstanceIn(s.machineStore, "crew-3");
  setShortcutsIn(s.machineStore, { hideAll: "" });
  seed(s);
  assert.deepStrictEqual(s.gatewayStore.get("appWindowGatewayChoices"), { other: "crew-1", mochi: "crew-3" });
  assert.deepStrictEqual(s.prefsStore.get(BINDINGS_KEY), { other: { go: "" }, mochi: { "hide-all": "" } });
  assert.ok(s.machineStore.get(HAND_OFF_KEY));

  const bare = { machineStore: mapStore() };
  setPetInstanceIn(bare.machineStore, "crew-3");
  assert.deepStrictEqual(seedHostMachinePrefs({ ...bare, defaults: MAC_DEFAULTS }), []);
});
