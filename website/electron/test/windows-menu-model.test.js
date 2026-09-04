const { test } = require("node:test");
const assert = require("node:assert");
const { serializeMenuItems, executeMenuItem, MENU_KEYBOARD_EVENT } = require("../windows-menu-model");

test("serializes visible menu commands while preserving source indexes", () => {
  const submenu = { items: [
    { type: "normal", label: "Settings…", accelerator: "CmdOrCtrl+,", enabled: true, checked: false, visible: true },
    { type: "normal", label: "Hidden", enabled: true, checked: false, visible: false },
    { type: "separator", visible: true },
  ] };
  assert.deepStrictEqual(serializeMenuItems(submenu), [
    {
      type: "normal",
      index: 0,
      label: "Settings…",
      accelerator: "CmdOrCtrl+,",
      enabled: true,
      checked: false,
    },
    { type: "separator", index: 2 },
  ]);
});

// Electron leaves `accelerator` null on `{ role: ... }` items and exposes the
// shortcut through getDefaultRoleAccelerator(); reading only the property would
// render the whole Edit menu without its Ctrl+C/Ctrl+V hints.
test("falls back to the role accelerator when the item has no explicit one", () => {
  const submenu = { items: [
    {
      type: "normal",
      label: "Copy",
      role: "copy",
      accelerator: null,
      enabled: true,
      checked: false,
      visible: true,
    },
    {
      type: "normal",
      label: "Select All",
      role: "selectAll",
      accelerator: null,
      enabled: true,
      checked: false,
      visible: true,
    },
  ] };
  assert.deepStrictEqual(
    serializeMenuItems(submenu).map((item) => item.accelerator),
    ["Ctrl+C", "Ctrl+A"],
  );
});

test("keeps an explicit accelerator ahead of the Windows role fallback", () => {
  const submenu = { items: [{
    type: "normal",
    label: "Redo",
    role: "redo",
    accelerator: "Ctrl+Shift+Z",
    enabled: true,
    checked: false,
    visible: true,
  }] };
  assert.strictEqual(serializeMenuItems(submenu)[0].accelerator, "Ctrl+Shift+Z");
});

test("executes only enabled and visible menu commands", () => {
  const calls = [];
  const item = {
    visible: true,
    enabled: true,
    click: (...args) => calls.push(args),
  };
  const topLevelItem = { submenu: { items: [item] } };
  const win = { id: 3 };
  const senderWebContents = { id: 9 };

  assert.strictEqual(executeMenuItem(topLevelItem, 0, win, senderWebContents), true);
  assert.deepStrictEqual(calls, [[MENU_KEYBOARD_EVENT, win, senderWebContents]]);
  item.enabled = false;
  assert.strictEqual(executeMenuItem(topLevelItem, 0, win, senderWebContents), false);
  assert.strictEqual(calls.length, 1);
});

// Mirrors Electron's own delegate: click(KeyboardEvent, focusedWindow,
// focusedWebContents). A role item reaches for a WebContents method on the
// THIRD argument, so anything else there (an IpcMainEvent) throws.
test("dispatches role items through the window and webContents arguments", () => {
  const ran = [];
  const roleItem = (dispatch) => ({
    visible: true,
    enabled: true,
    // Stand-in for Electron's click wrapper around roles.execute().
    click: (_event, focusedWindow, focusedWebContents) => dispatch(focusedWindow, focusedWebContents),
  });
  const win = { minimize: () => ran.push("minimize") };
  const wc = { copy: () => ran.push("copy") };
  const topLevelItem = { submenu: { items: [
    roleItem((focusedWindow) => focusedWindow.minimize()),
    roleItem((_focusedWindow, focusedWebContents) => focusedWebContents.copy()),
  ] } };

  assert.strictEqual(executeMenuItem(topLevelItem, 0, win, wc), true);
  assert.strictEqual(executeMenuItem(topLevelItem, 1, win, wc), true);
  assert.deepStrictEqual(ran, ["minimize", "copy"]);
});
