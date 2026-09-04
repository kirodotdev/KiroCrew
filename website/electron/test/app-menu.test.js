const { test } = require("node:test");
const assert = require("node:assert");
const { buildMenuTemplate } = require("../app-menu");

// Every action callback stubbed with a call recorder.
function makeDeps(overrides = {}) {
  const calls = [];
  const record = (name) => () => calls.push(name);
  const deps = {
    isMac: true,
    appName: "Kiro Crew",
    openSettings: record("openSettings"),
    openAbout: record("openAbout"),
    reload: record("reload"),
    forceReload: record("forceReload"),
    toggleDevTools: record("toggleDevTools"),
    zoomActualSize: record("zoomActualSize"),
    zoomIn: record("zoomIn"),
    zoomOut: record("zoomOut"),
    alwaysOnTop: false,
    toggleAlwaysOnTop: record("toggleAlwaysOnTop"),
    openNewSessionWindow: record("openNewSessionWindow"),
    openNewConnectionWindow: record("openNewConnectionWindow"),
    renameCurrentWindow: record("renameCurrentWindow"),
    promptRemoteHost: record("promptRemoteHost"),
    refreshToken: record("refreshToken"),
    openConfigFile: record("openConfigFile"),
    ...overrides,
  };
  return { deps, calls };
}

// Depth-first search over the template for an item matching pred.
function findItem(template, pred) {
  for (const item of template) {
    if (pred(item)) return item;
    if (Array.isArray(item.submenu)) {
      const hit = findItem(item.submenu, pred);
      if (hit) return hit;
    }
  }
  return null;
}

const topLabels = (template) => template.map((i) => i.label || i.role);

// ── macOS shape ──

test("mac: app menu is first, labeled with the app name", () => {
  const { deps } = makeDeps({ isMac: true });
  const template = buildMenuTemplate(deps);
  assert.strictEqual(template[0].label, "Kiro Crew");
  assert.ok(Array.isArray(template[0].submenu));
});

test("mac: app menu carries About, Settings…, and the standard roles", () => {
  const { deps } = makeDeps({ isMac: true });
  const appMenu = buildMenuTemplate(deps)[0].submenu;
  assert.strictEqual(appMenu[0].label, "About Kiro Crew");
  const settings = appMenu.find((i) => i.label === "Settings…");
  assert.ok(settings, "Settings… present in app menu");
  assert.strictEqual(settings.accelerator, "CmdOrCtrl+,");
  for (const role of ["services", "hide", "hideOthers", "unhide", "quit"]) {
    assert.ok(appMenu.some((i) => i.role === role), `role ${role} present`);
  }
});

test("mac: no File or Help menus (their items live in the app menu)", () => {
  const { deps } = makeDeps({ isMac: true });
  const labels = topLabels(buildMenuTemplate(deps));
  assert.ok(!labels.includes("File"));
  assert.ok(!labels.includes("Help"));
});

test("mac: Connection exposes New Window on Cmd+Shift+N", () => {
  const { deps, calls } = makeDeps({ isMac: true });
  const item = findItem(buildMenuTemplate(deps), (i) => i.label === "New Window");
  assert.ok(item, "New Window present on macOS");
  assert.strictEqual(item.accelerator, "Cmd+Shift+N");
  item.click();
  assert.deepStrictEqual(calls, ["openNewSessionWindow"]);
});

// ── Windows/Linux shape ──

test("win/linux: File menu is first with Settings… and quit", () => {
  const { deps } = makeDeps({ isMac: false });
  const template = buildMenuTemplate(deps);
  assert.strictEqual(template[0].label, "File");
  const [settings, sep, quit] = template[0].submenu;
  assert.strictEqual(settings.label, "Settings…");
  assert.strictEqual(settings.accelerator, "CmdOrCtrl+,");
  assert.strictEqual(sep.type, "separator");
  assert.strictEqual(quit.role, "quit");
});

test("win/linux: Help menu is last with About", () => {
  const { deps } = makeDeps({ isMac: false });
  const template = buildMenuTemplate(deps);
  const help = template[template.length - 1];
  assert.strictEqual(help.label, "Help");
  assert.strictEqual(help.submenu[0].label, "About Kiro Crew");
});

test("win/linux: every custom-titlebar menu has a stable native menu id", () => {
  const { deps } = makeDeps({ isMac: false });
  const template = buildMenuTemplate(deps);
  assert.deepStrictEqual(
    template.map((item) => item.id),
    ["file-menu", "edit-menu", "view-menu", "connection-menu", "window-menu", "help-menu"],
  );
});

test("win/linux: no macOS-only roles anywhere in the template", () => {
  const { deps } = makeDeps({ isMac: false });
  const template = buildMenuTemplate(deps);
  for (const role of ["appMenu", "services", "hide", "hideOthers", "unhide"]) {
    assert.strictEqual(findItem(template, (i) => i.role === role), null, `no ${role} off darwin`);
  }
});

test("win/linux: New Window stays macOS-only", () => {
  const { deps } = makeDeps({ isMac: false });
  assert.strictEqual(
    findItem(buildMenuTemplate(deps), (i) => i.label === "New Window"),
    null,
  );
});

test("win/linux: Window menu is written out with Close on Ctrl+Shift+W", () => {
  // The stock `windowMenu` role puts Close on Ctrl+W, which the renderer uses for
  // "close session"; the window close takes the VS Code / Chrome chord instead.
  const { deps } = makeDeps({ isMac: false });
  const win = buildMenuTemplate(deps).find((i) => i.id === "window-menu");
  assert.strictEqual(win.label, "Window");
  assert.strictEqual(win.role, undefined);
  assert.deepStrictEqual(
    win.submenu.map((i) => i.role),
    ["minimize", "zoom", "close"],
  );
  assert.strictEqual(win.submenu[2].accelerator, "Ctrl+Shift+W");
});

test("mac: Window menu keeps the stock role (no Close entry, so Cmd+W reaches the page)", () => {
  const { deps } = makeDeps({ isMac: true });
  const win = buildMenuTemplate(deps).find((i) => i.id === "window-menu");
  assert.strictEqual(win.role, "windowMenu");
});

// ── shared structure (both platforms) ──

for (const isMac of [true, false]) {
  const os = isMac ? "mac" : "win/linux";

  test(`${os}: Edit, View, Connection, Window menus survive the extraction`, () => {
    const { deps } = makeDeps({ isMac });
    const labels = topLabels(buildMenuTemplate(deps));
    // macOS keeps the stock role; Windows/Linux write the Window menu out (see
    // the Ctrl+W test below), so it surfaces by label there.
    for (const expected of ["editMenu", "View", "Connection", isMac ? "windowMenu" : "Window"]) {
      assert.ok(labels.includes(expected), `${expected} present`);
    }
  });

  // Cmd/Ctrl+N is "new session" and Cmd/Ctrl+W "close session" in the renderer
  // (src/lib/shortcutRegistry.ts, #4608). A menu accelerator on either would take
  // the keystroke before the page saw it, so the menu must leave both unclaimed.
  test(`${os}: no menu item claims CmdOrCtrl+N or CmdOrCtrl+W`, () => {
    const { deps } = makeDeps({ isMac });
    const template = buildMenuTemplate(deps);
    for (const acc of ["CmdOrCtrl+N", "Cmd+N", "Ctrl+N", "CmdOrCtrl+W", "Cmd+W", "Ctrl+W"]) {
      assert.strictEqual(findItem(template, (i) => i.accelerator === acc), null, `${acc} unclaimed`);
    }
  });

  test(`${os}: New Connection Window… moved to CmdOrCtrl+Alt+N`, () => {
    const { deps } = makeDeps({ isMac });
    const item = findItem(buildMenuTemplate(deps), (i) => i.label === "New Connection Window…");
    assert.ok(item, "New Connection Window… present");
    assert.strictEqual(item.accelerator, "CmdOrCtrl+Alt+N");
  });

  test(`${os}: devtools item keeps its id and starts hidden`, () => {
    const { deps } = makeDeps({ isMac });
    const item = findItem(buildMenuTemplate(deps), (i) => i.id === "devtools-toggle");
    assert.ok(item, "devtools-toggle present");
    assert.strictEqual(item.visible, false);
    assert.strictEqual(item.accelerator, "CmdOrCtrl+Shift+I");
  });

  test(`${os}: View has a Keep on Top checkbox whose checked mirrors the injected value`, () => {
    for (const alwaysOnTop of [true, false]) {
      const { deps } = makeDeps({ isMac, alwaysOnTop });
      const view = buildMenuTemplate(deps).find((i) => i.label === "View");
      const item = view.submenu.find((i) => i.label === "Keep on Top");
      assert.ok(item, "Keep on Top present in the View submenu");
      assert.strictEqual(item.type, "checkbox");
      assert.strictEqual(item.id, "keep-on-top");
      assert.strictEqual(item.checked, alwaysOnTop, `checked follows alwaysOnTop=${alwaysOnTop}`);
      assert.strictEqual(item.accelerator, undefined, "deliberately ships no accelerator");
    }
  });

  test(`${os}: Keep on Top click invokes the injected toggle`, () => {
    const { deps, calls } = makeDeps({ isMac });
    findItem(buildMenuTemplate(deps), (i) => i.label === "Keep on Top").click();
    assert.deepStrictEqual(calls, ["toggleAlwaysOnTop"]);
  });

  test(`${os}: Settings… and About clicks invoke the injected actions`, () => {
    const { deps, calls } = makeDeps({ isMac });
    const template = buildMenuTemplate(deps);
    findItem(template, (i) => i.label === "Settings…").click();
    findItem(template, (i) => i.label === "About Kiro Crew").click();
    assert.deepStrictEqual(calls, ["openSettings", "openAbout"]);
  });

  test(`${os}: View and Connection clicks invoke the injected actions`, () => {
    const { deps, calls } = makeDeps({ isMac });
    const template = buildMenuTemplate(deps);
    for (const label of [
      "Reload",
      "Force Reload",
      "Actual Size",
      "Zoom In",
      "Zoom Out",
      "Keep on Top",
      "Toggle Developer Tools",
      "New Connection Window…",
      "Rename Window…",
      "Set Remote Host…",
      "Refresh Token",
      "Open Config File",
    ]) {
      findItem(template, (i) => i.label === label).click();
    }
    assert.deepStrictEqual(calls, [
      "reload",
      "forceReload",
      "zoomActualSize",
      "zoomIn",
      "zoomOut",
      "toggleAlwaysOnTop",
      "toggleDevTools",
      "openNewConnectionWindow",
      "renameCurrentWindow",
      "promptRemoteHost",
      "refreshToken",
      "openConfigFile",
    ]);
  });
}
