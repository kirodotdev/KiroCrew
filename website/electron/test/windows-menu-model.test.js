const { test } = require("node:test");
const assert = require("node:assert");
const {
  serializeMenuItems,
  executeMenuItem,
  MENU_KEYBOARD_EVENT,
  LOCAL_ONLY_ROLES,
  LOCAL_ONLY_MENU_IDS,
  LOCAL_ONLY_ITEM_IDS,
} = require("../windows-menu-model");
const { buildMenuTemplate } = require("../app-menu");
const { WINDOWS_TITLEBAR_MENU_IDS } = require("../window-lifecycle");

test("serializes visible menu commands while preserving source indexes", () => {
  const topLevelItem = { id: "file-menu", submenu: { items: [
    { type: "normal", label: "Settings…", accelerator: "CmdOrCtrl+,", enabled: true, checked: false, visible: true },
    { type: "normal", label: "Hidden", enabled: true, checked: false, visible: false },
    { type: "separator", visible: true },
  ] } };
  assert.deepStrictEqual(serializeMenuItems(topLevelItem, /*senderIsLocal*/ true), [
    {
      type: "normal",
      index: 0,
      label: "Settings…",
      accelerator: "CmdOrCtrl+,",
      enabled: true,
      gated: false,
      acceleratorRegistered: true,
      checked: false,
    },
    { type: "separator", index: 2 },
  ]);
});

// Electron's `registerAccelerator: false` is a Linux/Windows option that shows
// the accelerator label but does NOT route the chord to the menu. Settings on
// Windows uses it (Alt+,) because the same chord belongs to the renderer's own
// shortcut registry. For the renderer's "row off, shortcut on" glyph, an
// unregistered chord is a false claim — the OS never fires the menu, so in a
// connection window the remote gateway's SPA can freely intercept the key.
// `serializeMenuItems` therefore threads a positive `acceleratorRegistered`
// flag through so the renderer can gate the glyph on it. Default is `true`
// (Electron's own default); explicit `false` is the only way to turn it off.
test("serializeMenuItems flags display-only accelerators via acceleratorRegistered", () => {
  const topLevelItem = { id: "file-menu", submenu: { items: [
    // Settings on Windows: caption "Alt+," but registerAccelerator: false, so
    // the OS never routes the chord to this menu.
    { type: "normal", label: "Settings…", id: "settings", accelerator: "Alt+,", registerAccelerator: false, enabled: true, checked: false, visible: true },
    // Real OS-registered accelerator (default behaviour, no registerAccelerator field).
    { type: "normal", label: "Quit", role: "quit", accelerator: "Ctrl+Q", enabled: true, checked: false, visible: true },
  ] } };
  const items = serializeMenuItems(topLevelItem, /*senderIsLocal*/ false);
  assert.strictEqual(items[0].label, "Settings…");
  assert.strictEqual(items[0].acceleratorRegistered, false, "registerAccelerator:false must surface as acceleratorRegistered:false");
  assert.strictEqual(items[0].accelerator, "Alt+,", "the caption itself is still shown — the renderer only suppresses the glyph");
  assert.strictEqual(items[1].label, "Quit");
  assert.strictEqual(items[1].acceleratorRegistered, true, "default (undefined registerAccelerator) surfaces as acceleratorRegistered:true");
});

// The renderer's disabled-menu footer + "shortcut still works here" glyph
// must scope to gate-caused disables only. A natively-disabled row (Electron
// disabled it for its own runtime reasons — undo with no history, paste
// with no selection focus, etc.) has NO live accelerator, and painting the
// footer over such a menu would be a false claim. The distinction lives in
// `serializeMenuItems`'s output shape: `gated` is TRUE only when
// `senderIsLocal !== true && isLocalOnlyItem`, FALSE otherwise (including on
// a row that Electron already disabled). Pinned here so a future edit that
// collapses `gated` onto `!enabled` cannot pass silently.
test("serializeMenuItems distinguishes gate-caused disables from native ones via `gated`", () => {
  const topLevelItem = { id: "edit-menu", submenu: { items: [
    // Gate-caused: paste role + remote sender.
    { type: "normal", label: "Paste", role: "paste", accelerator: null, enabled: true, checked: false, visible: true },
    // Natively disabled: Electron flagged it so; the gate does not add
    // anything (undo/redo/copy off-selection have this shape in practice).
    { type: "normal", label: "Redo", role: "redo", accelerator: null, enabled: false, checked: false, visible: true },
    // Enabled and non-LOCAL_ONLY: gated must stay false.
    { type: "normal", label: "Select All", role: "selectall", accelerator: null, enabled: true, checked: false, visible: true },
  ] } };
  const remote = serializeMenuItems(topLevelItem, /*senderIsLocal*/ false);
  assert.strictEqual(remote[0].label, "Paste");
  assert.strictEqual(remote[0].enabled, false, "gate-caused row is enabled:false");
  assert.strictEqual(remote[0].gated, true, "…and gated:true so the renderer knows the reason");
  assert.strictEqual(remote[1].label, "Redo");
  assert.strictEqual(remote[1].enabled, false, "natively-disabled row stays enabled:false");
  assert.strictEqual(remote[1].gated, false, "…but gated:false — the gate did not fence it");
  assert.strictEqual(remote[2].label, "Select All");
  assert.strictEqual(remote[2].enabled, true);
  assert.strictEqual(remote[2].gated, false);

  // Local sender: nothing is gated, even the LOCAL_ONLY leaf.
  const local = serializeMenuItems(topLevelItem, /*senderIsLocal*/ true);
  assert.strictEqual(local[0].gated, false, "local sender: gate does not fire on paste");
  assert.strictEqual(local[1].gated, false, "local sender: native disable is still not gate-caused");
  assert.strictEqual(local[2].gated, false);
});

// Electron leaves `accelerator` null on `{ role: ... }` items and exposes the
// shortcut through getDefaultRoleAccelerator(); reading only the property would
// render the whole Edit menu without its Ctrl+C/Ctrl+V hints.
test("falls back to the role accelerator when the item has no explicit one", () => {
  const topLevelItem = { id: "edit-menu", submenu: { items: [
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
  ] } };
  assert.deepStrictEqual(
    serializeMenuItems(topLevelItem, /*senderIsLocal*/ true).map((item) => item.accelerator),
    ["Ctrl+C", "Ctrl+A"],
  );
});

test("keeps an explicit accelerator ahead of the Windows role fallback", () => {
  const topLevelItem = { id: "edit-menu", submenu: { items: [{
    type: "normal",
    label: "Redo",
    role: "redo",
    accelerator: "Ctrl+Shift+Z",
    enabled: true,
    checked: false,
    visible: true,
  }] } };
  assert.strictEqual(
    serializeMenuItems(topLevelItem, /*senderIsLocal*/ true)[0].accelerator,
    "Ctrl+Shift+Z",
  );
});

// A REMOTE-origin sender must not be handed an `enabled: true` LOCAL_ONLY
// row on the items surface — the row would look clickable and then silently
// no-op inside executeMenuItem's fence. Reflect the gate in the enabled
// column so the renderer paints a greyed label. Labels, accelerators and
// checked state remain unchanged: the row still exists so a connection
// window's menu keeps shape parity with a local one. Sibling to the
// execute-side rejection tests below.
test("serializeMenuItems marks LOCAL_ONLY leaves disabled for a remote sender", () => {
  // Paste, Copy, Cut are all LOCAL_ONLY_ROLES (copy/cut close the clipboard-
  // WRITE mirror of the paste READ threat: sender.copy() bypasses Chromium's
  // navigator.clipboard.writeText permission model). "Select All" is
  // sender-scoped and stays enabled — no shared OS state.
  const topLevelItem = { id: "edit-menu", submenu: { items: [
    { type: "normal", label: "Paste", role: "paste", accelerator: null, enabled: true, checked: false, visible: true },
    { type: "normal", label: "Copy", role: "copy", accelerator: null, enabled: true, checked: false, visible: true },
    { type: "normal", label: "Cut", role: "cut", accelerator: null, enabled: true, checked: false, visible: true },
    { type: "normal", label: "Select All", role: "selectall", accelerator: null, enabled: true, checked: false, visible: true },
  ] } };
  const remote = serializeMenuItems(topLevelItem, /*senderIsLocal*/ false);
  assert.strictEqual(remote[0].label, "Paste");
  assert.strictEqual(remote[0].enabled, false, "paste must be greyed for remote sender");
  assert.strictEqual(remote[1].label, "Copy");
  assert.strictEqual(remote[1].enabled, false, "copy overwrites the shared OS clipboard; must be greyed");
  assert.strictEqual(remote[2].label, "Cut");
  assert.strictEqual(remote[2].enabled, false, "cut is copy plus source removal; same clipboard threat");
  assert.strictEqual(remote[3].label, "Select All");
  assert.strictEqual(remote[3].enabled, true, "select-all is sender-scoped, stays enabled");

  // Same input, sender=local: all four enabled again.
  const local = serializeMenuItems(topLevelItem, /*senderIsLocal*/ true);
  assert.strictEqual(local[0].enabled, true);
  assert.strictEqual(local[1].enabled, true);
  assert.strictEqual(local[2].enabled, true);
});

// A LOCAL_ONLY_ITEM_ID under a submenu that is otherwise remote-safe (View)
// must reflect the gate as `enabled: false` on the items surface, not just
// refuse on execute. Devtools-toggle is the clearest case: its click hands
// full renderer code execution to the local dashboard's origin.
test("serializeMenuItems disables LOCAL_ONLY_ITEM_IDS on the View menu for remote senders", () => {
  // Every listed item is LOCAL_ONLY: reload/zoom-* are focus-scoped custom-
  // click items that resolve to the local dashboard, devtools-toggle hands
  // full renderer code execution to the local origin, keep-on-top mutates
  // OS compositor Z-order across every window. Local senders keep them all.
  const topLevelItem = { id: "view-menu", submenu: { items: [
    { type: "normal", label: "Reload", id: "reload", accelerator: "Ctrl+R", enabled: true, checked: false, visible: true },
    { type: "normal", label: "Zoom In", id: "zoom-in", accelerator: "Ctrl+=", enabled: true, checked: false, visible: true },
    { type: "normal", label: "Toggle DevTools", id: "devtools-toggle", enabled: true, checked: false, visible: true },
    { type: "checkbox", label: "Keep on Top", id: "keep-on-top", enabled: true, checked: true, visible: true },
  ] } };
  const remote = serializeMenuItems(topLevelItem, /*senderIsLocal*/ false);
  assert.strictEqual(remote[0].enabled, false, "reload resolves to the LOCAL dashboard, must be greyed");
  assert.strictEqual(remote[1].enabled, false, "zoom-in resolves to the LOCAL dashboard, must be greyed");
  assert.strictEqual(remote[2].enabled, false, "devtools-toggle is LOCAL_ONLY");
  assert.strictEqual(remote[3].enabled, false, "keep-on-top is LOCAL_ONLY");

  const local = serializeMenuItems(topLevelItem, /*senderIsLocal*/ true);
  assert.strictEqual(local[0].enabled, true, "local sender: reload stays enabled");
  assert.strictEqual(local[1].enabled, true, "local sender: zoom-in stays enabled");
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

// A REMOTE-origin sender in a connection window shares this preload with the
// local dashboard. Menu roles that reach state OUTSIDE the sender's own
// WebContents — starting with paste, which reads the local OS clipboard —
// MUST be refused when the sender is not local. See the LOCAL_ONLY_* sets in
// windows-menu-model.js and the per-action gate in ipc-registrar.js's
// app-menu:execute handler.
test("executeMenuItem should reject paste role when sender is remote", () => {
  const calls = [];
  const topLevelItem = { id: "edit-menu", submenu: { items: [{
    role: "paste",
    visible: true,
    enabled: true,
    click: (...args) => calls.push(args),
  }] } };
  assert.strictEqual(
    executeMenuItem(topLevelItem, 0, {}, {}, /*senderIsLocal*/ false),
    false,
  );
  assert.strictEqual(calls.length, 0);
});

// Beyond paste, every menu role that reaches OUT of the sender's own
// WebContents must be gated the same way: `quit` kills the local app,
// `close`/`minimize`/`zoom` mutate the OS-level state of the window the
// remote is rendered in, `togglefullscreen` covers the whole desktop, and
// `pasteAndMatchStyle` is a clipboard read like `paste`. `copy`/`cut`
// WRITE the sender's selection into the shared OS clipboard, bypassing
// Chromium's `navigator.clipboard.writeText` permission model — symmetric
// with paste's READ threat, so they are in this set too. `undo`/`redo`
// and `selectall` are sender-scoped (they operate on the sender's own
// document) and stay OUT of this set.
test("executeMenuItem should reject every LOCAL_ONLY role when sender is remote", () => {
  const roles = [
    "paste", "pasteandmatchstyle", "copy", "cut", "quit",
    "close", "minimize", "zoom", "togglefullscreen",
  ];
  for (const role of roles) {
    const calls = [];
    const topLevelItem = { id: "edit-menu", submenu: { items: [{
      role,
      visible: true,
      enabled: true,
      click: (...args) => calls.push(args),
    }] } };
    assert.strictEqual(
      executeMenuItem(topLevelItem, 0, {}, {}, /*senderIsLocal*/ false),
      false,
      `role=${role} must be rejected for a remote sender`,
    );
    assert.strictEqual(calls.length, 0, `role=${role} must not dispatch`);
  }
});

// The Connection submenu bundles items with custom click handlers that reach
// local state (config file, SSH auth, spawning windows). None of them carry
// an Electron role, so the role-based fence does not catch them — the whole
// submenu id is fenced instead. This is coarser than per-item but the
// submenu is homogenous: every entry is a local-only action.
test("executeMenuItem should reject items under connection-menu when sender is remote", () => {
  const calls = [];
  const topLevelItem = { id: "connection-menu", submenu: { items: [{
    label: "Set Remote Host…",
    visible: true,
    enabled: true,
    click: (...args) => calls.push(args),
  }] } };
  assert.strictEqual(
    executeMenuItem(topLevelItem, 0, {}, {}, /*senderIsLocal*/ false),
    false,
  );
  assert.strictEqual(calls.length, 0);
});

// Keep on Top is a per-item outlier: its parent (view-menu) is mostly safe
// (reload, zoom, DevTools all belong to the sender's own page), so a menu-id
// fence would be too wide. It's the ONLY item under view-menu that mutates
// OS-level desktop state (compositor Z-order across every window in the
// session), so it's fenced by its own id.
test("executeMenuItem should reject the keep-on-top item when sender is remote", () => {
  const calls = [];
  const topLevelItem = { id: "view-menu", submenu: { items: [{
    id: "keep-on-top",
    type: "checkbox",
    visible: true,
    enabled: true,
    click: (...args) => calls.push(args),
  }] } };
  assert.strictEqual(
    executeMenuItem(topLevelItem, 0, {}, {}, /*senderIsLocal*/ false),
    false,
  );
  assert.strictEqual(calls.length, 0);
});

// Drift pin — set membership. Adding a new role or menu id to Electron or
// to this app's menu template must be an EXPLICIT categorization decision,
// not a silent widening. If Electron introduces a new sender-scoped role
// (e.g. a future zoom variant), an author adding it here must consciously
// decide whether remote callers can reach it. The set pin catches a change
// to any of the THREE classification sets so the review round can attach.
// A separate "empty REMOTE_SAFE allowlist" set was removed in c2d85562b's
// follow-up per First Principles Review (Item 8): it had zero consumers
// outside the drift pin, and the pinned shipped reality is "every
// reachable leaf is LOCAL_ONLY-classified" — that is the drift pin below.
test("LOCAL_ONLY sets pin the exact membership", () => {
  assert.deepStrictEqual([...LOCAL_ONLY_ROLES].sort(), [
    "close", "copy", "cut", "minimize", "paste", "pasteandmatchstyle",
    "quit", "togglefullscreen", "zoom",
  ]);
  assert.deepStrictEqual([...LOCAL_ONLY_MENU_IDS].sort(), ["connection-menu"]);
  assert.deepStrictEqual([...LOCAL_ONLY_ITEM_IDS].sort(), [
    "about", "devtools-toggle", "force-reload", "keep-on-top",
    "reload", "settings", "zoom-actual", "zoom-in", "zoom-out",
  ]);
});

// Drift pin — TEMPLATE WALK. The set-membership pin above catches changes
// to the sets themselves, but leaves the OTHER direction wide open: a new
// custom-click item added to `app-menu.js` (or a new role in an existing
// submenu) that is classified in NEITHER LOCAL_ONLY nor REMOTE_SAFE would
// silently inherit remote-safe on execute. Spock TEST-01 documented this
// gap by adding `devtools-toggle` without a classification decision and
// noting the set pin stayed green.
//
// This test walks the actual template output of `buildMenuTemplate` and
// asserts every reachable leaf is classified as LOCAL_ONLY in one of:
//   - top-level id is in LOCAL_ONLY_MENU_IDS (whole submenu fenced), OR
//   - item.role is in LOCAL_ONLY_ROLES, OR
//   - item.id is in LOCAL_ONLY_ITEM_IDS.
// The shipped policy classifies every reachable leaf as LOCAL_ONLY — no
// item is truly "remote-safe" once you look at what its click handler
// reaches (see First Principles Review comment on the removed
// REMOTE_SAFE_ITEM_IDS empty allowlist). A role-populated top-level
// (`role: "editMenu"` / `"windowMenu"`) is SKIPPED because Electron owns
// its children — the LOCAL_ONLY_ROLES fence catches anything sensitive
// from those items at execute time.
function walkLeaves(nodes, parentId, out) {
  for (const node of nodes) {
    if (!node || node.type === "separator") continue;
    if (Array.isArray(node.submenu)) {
      walkLeaves(node.submenu, node.id, out);
      continue;
    }
    out.push({ parentId, item: node });
  }
}
test("every reachable menu leaf is classified in one direction", () => {
  const noop = () => {};
  // Walk BOTH macOS and non-macOS branches: the drift-pin's job is to catch
  // an item added to either without a classification decision.
  for (const isMac of [false, true]) {
    const template = buildMenuTemplate({
      isMac,
      appName: "Kiro Crew",
      openSettings: noop,
      openAbout: noop,
      reload: noop,
      forceReload: noop,
      toggleDevTools: noop,
      zoomActualSize: noop,
      zoomIn: noop,
      zoomOut: noop,
      alwaysOnTop: false,
      toggleAlwaysOnTop: noop,
      openNewSessionWindow: noop,
      openNewConnectionWindow: noop,
      renameCurrentWindow: noop,
      promptRemoteHost: noop,
      refreshToken: noop,
      openConfigFile: noop,
    });
    const unclassified = [];
    for (const top of template) {
      // Reachability filter: `app-menu:execute` only dispatches to top-level
      // ids in WINDOWS_TITLEBAR_MENU_IDS, so a submenu outside that set is
      // by construction unreachable through this channel (macOS's app-menu
      // {services, hide, hideOthers, unhide} is not classified because
      // Electron owns it and it cannot be invoked from the Windows titlebar
      // menu surface). If a future author adds a NEW id to the reachable
      // set without classifying its items, this filter opens up and the
      // pin catches the drift.
      if (!WINDOWS_TITLEBAR_MENU_IDS.has(top.id)) continue;
      // Role-populated top levels: Electron owns the items, LOCAL_ONLY_ROLES
      // is the runtime fence for anything sensitive inside them.
      if (top.role === "editMenu" || top.role === "windowMenu") continue;
      // A whole submenu is fenced by its top-level id.
      if (LOCAL_ONLY_MENU_IDS.has(top.id)) continue;
      if (!Array.isArray(top.submenu)) continue;
      const leaves = [];
      walkLeaves(top.submenu, top.id, leaves);
      for (const { item } of leaves) {
        const role = String(item.role || "").toLowerCase();
        if (LOCAL_ONLY_ROLES.has(role)) continue;
        if (item.id && LOCAL_ONLY_ITEM_IDS.has(item.id)) continue;
        unclassified.push({
          parentId: top.id,
          isMac,
          label: item.label,
          id: item.id,
          role,
        });
      }
    }
    assert.deepStrictEqual(
      unclassified,
      [],
      `unclassified menu leaves on isMac=${isMac}: `
      + `every item must be in LOCAL_ONLY_ROLES, LOCAL_ONLY_ITEM_IDS, `
      + `or under a LOCAL_ONLY_MENU_IDS submenu.`,
    );
  }
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
