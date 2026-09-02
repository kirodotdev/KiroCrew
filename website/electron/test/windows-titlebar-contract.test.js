const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { buildMenuTemplate } = require("../app-menu");
const { serializeMenuItems } = require("../windows-menu-model");

// The Windows titlebar menu's top-level list is maintained in THREE places that
// must agree, and every disagreement fails silently rather than loudly:
//
//   1. `app-menu.js`                       — the native template's menu ids
//   2. `window-lifecycle.js` WINDOWS_TITLEBAR_MENU_IDS — the IPC allowlist; an id missing
//      here makes `app-menu:items` return [] , so the menu opens EMPTY
//   3. `WindowsTitlebarMenu.tsx` WINDOWS_MENUS — the rendered label row; an id
//      missing here means the menu is never offered at all
//
// So a contributor adding a seventh top-level menu gets a menu that either does
// not appear or opens blank, with nothing failing. These tests turn each of
// those into a build failure instead.
//
// Read as TEXT for (2) and (3) on purpose: the allowlist is intentionally kept
// literal for review and the renderer is TSX, so neither is required here.

const ELECTRON_DIR = path.join(__dirname, "..");
const WINDOW_LIFECYCLE_JS = path.join(ELECTRON_DIR, "window-lifecycle.js");
const RENDERER_TSX = path.join(
  ELECTRON_DIR, "..", "src", "components", "WindowsTitlebarMenu.tsx",
);

/** Windows/Linux template — the Windows titlebar menu is not a macOS surface. */
function windowsTemplate() {
  const record = () => () => {};
  return buildMenuTemplate({
    isMac: false,
    appName: "Kiro Crew",
    openSettings: record(), openAbout: record(), reload: record(),
    forceReload: record(), toggleDevTools: record(), zoomActualSize: record(),
    zoomIn: record(), zoomOut: record(), alwaysOnTop: false,
    toggleAlwaysOnTop: record(), openNewConnectionWindow: record(),
    renameCurrentWindow: record(), promptRemoteHost: record(),
    refreshToken: record(), openConfigFile: record(),
  });
}

/**
 * Ids from the ALLOWLIST literal in window-lifecycle.js (the `-menu` suffixed strings).
 *
 * Both this and `rendererIds` hard-code the `-menu` suffix (and the renderer's
 * quote style), so a future top-level id that breaks that convention is seen by
 * the template side (which is REQUIRED, not text-parsed) and missed here. That
 * fails loudly rather than silently — but it reports as "the lists disagree",
 * which points at the Set/renderer instead of at these regexes. If a
 * disagreement looks wrong, check the id naming convention before hunting for a
 * missing entry.
 */
function allowlistIds() {
  const src = fs.readFileSync(WINDOW_LIFECYCLE_JS, "utf8");
  const block = /WINDOWS_TITLEBAR_MENU_IDS\s*=\s*new Set\(\[([\s\S]*?)\]\)/.exec(src);
  assert.ok(block, "WINDOWS_TITLEBAR_MENU_IDS literal not found in window-lifecycle.js");
  return [...block[1].matchAll(/"([a-z-]+-menu)"/g)].map((m) => m[1]);
}

/** Ids from the WINDOWS_MENUS literal in the renderer. */
function rendererIds() {
  const src = fs.readFileSync(RENDERER_TSX, "utf8");
  const block = /WINDOWS_MENUS\s*=\s*\[([\s\S]*?)\]\s*as const/.exec(src);
  assert.ok(block, "WINDOWS_MENUS literal not found in WindowsTitlebarMenu.tsx");
  return [...block[1].matchAll(/id:\s*'([a-z-]+-menu)'/g)].map((m) => m[1]);
}

test("the native template's top-level menu ids are all allowlisted for IPC", () => {
  const templateIds = windowsTemplate().map((m) => m.id).filter(Boolean);
  assert.deepStrictEqual(
    [...templateIds].sort(), [...allowlistIds()].sort(),
    "app-menu.js top-level ids and window-lifecycle.js WINDOWS_TITLEBAR_MENU_IDS disagree — "
    + "an id only in the template opens an EMPTY menu (the handler returns [])",
  );
});

test("the renderer offers exactly the menus the template defines", () => {
  const templateIds = windowsTemplate().map((m) => m.id).filter(Boolean);
  assert.deepStrictEqual(
    [...templateIds].sort(), [...rendererIds()].sort(),
    "app-menu.js top-level ids and WindowsTitlebarMenu.tsx WINDOWS_MENUS disagree — "
    + "an id only in the template is never rendered, so the menu is unreachable",
  );
});

test("the renderer's menu ORDER matches the native template", () => {
  // Order is user-visible (it is the left-to-right label row) and it also drives
  // ArrowLeft/ArrowRight traversal, so a reorder in one place is a real defect
  // rather than a cosmetic drift.
  const templateIds = windowsTemplate().map((m) => m.id).filter(Boolean);
  assert.deepStrictEqual(templateIds, rendererIds());
});

test("every titlebar menu is single-level, which is all the popup can render", () => {
  // serializeMenuItems flattens ONE level: a nested submenu would serialize as an
  // ordinary item, and the renderer would draw a row that looks live and does
  // nothing when clicked. Nested submenus are simply not supported here, so this
  // asserts the template never grows one rather than letting it fail silently.
  for (const menu of windowsTemplate()) {
    if (!menu.id || !Array.isArray(menu.submenu)) continue;
    for (const item of menu.submenu) {
      assert.ok(
        !item.submenu,
        `${menu.id} > "${item.label}" carries a nested submenu, which the Windows `
        + "titlebar popup cannot render — it would appear as a dead item. Flatten "
        + "it, or teach serializeMenuItems + the renderer about nesting.",
      );
    }
  }
});

test("serializeMenuItems does not silently pass a submenu off as a command", () => {
  // Guards the model itself, independently of today's template: if nesting ever
  // reaches it, the serialized item must NOT claim to be a plain "normal"
  // command, which is what makes a dead row indistinguishable from a live one.
  const nested = {
    items: [{
      visible: true, enabled: true, label: "Recent", type: "submenu",
      submenu: { items: [{ visible: true, enabled: true, label: "Deep", type: "normal" }] },
    }],
  };
  const [entry] = serializeMenuItems(nested);
  assert.notStrictEqual(
    entry.type, "normal",
    "a submenu item serialized as type 'normal' renders as a live-looking dead row",
  );
});
