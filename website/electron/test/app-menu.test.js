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

test("win/linux: no macOS-only roles anywhere in the template", () => {
  const { deps } = makeDeps({ isMac: false });
  const template = buildMenuTemplate(deps);
  for (const role of ["appMenu", "services", "hide", "hideOthers", "unhide"]) {
    assert.strictEqual(findItem(template, (i) => i.role === role), null, `no ${role} off darwin`);
  }
});

// ── shared structure (both platforms) ──

for (const isMac of [true, false]) {
  const os = isMac ? "mac" : "win/linux";

  test(`${os}: Edit, View, Connection, Window menus survive the extraction`, () => {
    const { deps } = makeDeps({ isMac });
    const labels = topLabels(buildMenuTemplate(deps));
    for (const expected of ["editMenu", "View", "Connection", "windowMenu"]) {
      assert.ok(labels.includes(expected), `${expected} present`);
    }
  });

  test(`${os}: devtools item keeps its id and starts hidden`, () => {
    const { deps } = makeDeps({ isMac });
    const item = findItem(buildMenuTemplate(deps), (i) => i.id === "devtools-toggle");
    assert.ok(item, "devtools-toggle present");
    assert.strictEqual(item.visible, false);
    assert.strictEqual(item.accelerator, "CmdOrCtrl+Shift+I");
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
      "toggleDevTools",
      "openNewConnectionWindow",
      "renameCurrentWindow",
      "promptRemoteHost",
      "refreshToken",
      "openConfigFile",
    ]);
  });
}
