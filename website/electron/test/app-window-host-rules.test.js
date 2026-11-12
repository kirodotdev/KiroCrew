/**
 * The shell's copy of the manifest `desktop` rules.
 *
 * Same cases as test/test_app_manifest_desktop.py on the gateway side: the
 * shell must refuse whatever the gateway refuses, because it re-checks every
 * declaration it reads from `/api/apps` rather than trusting a gateway of
 * another version. A refused declaration is dropped whole.
 */

const test = require("node:test");
const assert = require("node:assert");

const {
  MAX_SHORTCUTS,
  MAX_WINDOWS,
  canonicalAccelerator,
  desktopDeclarations,
  parseDesktopDeclaration,
} = require("../app-window-host-rules");

const VALID = {
  windows: [
    { id: "pet", entry: "desktop/pet.mjs", kind: "overlay", openOnEnable: true },
    { id: "panel", entry: "desktop/panel.mjs", kind: "panel", width: 420, height: 600, position: "top-right" },
  ],
  shortcuts: [{ id: "toggle-panel", accelerator: "CommandOrControl+Shift+M", window: "panel" }],
};

function withWindow(fields) {
  return { windows: [{ id: "w", entry: "w.mjs", kind: "overlay", ...fields }] };
}

test("a valid declaration parses with the panel defaults spelled out", () => {
  const parsed = parseDesktopDeclaration(VALID);
  assert.deepStrictEqual(parsed.windows, [
    { id: "pet", entry: "desktop/pet.mjs", kind: "overlay", openOnEnable: true },
    {
      id: "panel", entry: "desktop/panel.mjs", kind: "panel", openOnEnable: false,
      width: 420, height: 600, position: "top-right", transparent: false,
    },
  ]);
  assert.deepStrictEqual(parsed.shortcuts, [
    { id: "toggle-panel", accelerator: "CommandOrControl+Shift+M", chord: "CommandOrControl+Shift+M", window: "panel" },
  ]);
  const panel = parseDesktopDeclaration({ windows: [{ id: "p", entry: "p.js", kind: "panel" }] });
  assert.deepStrictEqual(
    [panel.windows[0].width, panel.windows[0].height, panel.windows[0].position],
    [400, 560, "bottom-right"],
  );
});

const REFUSED = [
  ["not an object", "yes"],
  ["no windows", {}],
  ["empty windows", { windows: [], shortcuts: [] }],
  ["unknown block key", { ...withWindow({}), menus: [] }],
  ["a kind outside the closed set", withWindow({ kind: "background" })],
  ["a capitalised id", withWindow({ id: "Pet" })],
  ["a traversing entry", withWindow({ entry: "../escape.mjs" })],
  ["an absolute entry", withWindow({ entry: "/abs.mjs" })],
  ["an html entry", withWindow({ entry: "page.html" })],
  ["a backslash entry", withWindow({ entry: "a\\b.mjs" })],
  ["panel fields on an overlay", withWindow({ width: 300 })],
  ["an undeclared window option", withWindow({ alwaysOnTop: false })],
  ["a non-boolean openOnEnable", withWindow({ openOnEnable: "yes" })],
  ["an oversized panel", { windows: [{ id: "p", entry: "p.js", kind: "panel", width: 99999 }] }],
  ["a boolean height", { windows: [{ id: "p", entry: "p.js", kind: "panel", height: true }] }],
  ["an unknown position", { windows: [{ id: "p", entry: "p.js", kind: "panel", position: "left" }] }],
  ["a duplicate window id", { windows: [withWindow({}).windows[0], withWindow({}).windows[0]] }],
  [
    "too many windows",
    { windows: Array.from({ length: MAX_WINDOWS + 1 }, (_, i) => ({ id: `w${i}`, entry: "w.mjs", kind: "overlay" })) },
  ],
  ["a shortcut naming no window", { ...withWindow({}), shortcuts: [{ id: "s", accelerator: "Ctrl+Alt+K" }] }],
  [
    "a shortcut naming another window",
    { ...withWindow({}), shortcuts: [{ id: "s", accelerator: "Ctrl+Alt+K", window: "other" }] },
  ],
  [
    "two spellings of one chord",
    {
      ...withWindow({}),
      shortcuts: [
        { id: "a", accelerator: "Ctrl+Alt+K", window: "w" },
        { id: "b", accelerator: "alt+control+k", window: "w" },
      ],
    },
  ],
  [
    "too many shortcuts",
    {
      ...withWindow({}),
      shortcuts: Array.from({ length: MAX_SHORTCUTS + 1 }, (_, i) => ({ id: `s${i}`, accelerator: `Ctrl+Alt+${i}`, window: "w" })),
    },
  ],
];

for (const [label, desktop] of REFUSED) {
  test(`refused: ${label}`, () => {
    assert.strictEqual(parseDesktopDeclaration(desktop), null);
  });
}

const BAD_CHORDS = [
  "CommandOrControl+C",
  "Ctrl+V",
  "CmdOrCtrl+Ctrl+C",
  "Command+Super+V",
  "CommandOrControl+Command+S",
  "Shift+Alt",
  "Shift+Shift+K",
  "Ctrl+Hyper+K",
  "Ctrl+Alt+Tab",
  "Ctrl+Alt+",
  "",
  `Ctrl+Alt+${"K".repeat(70)}`,
];

for (const accelerator of BAD_CHORDS) {
  test(`a chord an app may not claim: ${JSON.stringify(accelerator)}`, () => {
    assert.strictEqual(canonicalAccelerator(accelerator), "");
    assert.strictEqual(
      parseDesktopDeclaration({ ...withWindow({}), shortcuts: [{ id: "s", accelerator, window: "w" }] }),
      null,
    );
  });
}

test("chords canonicalise the way the gateway does", () => {
  assert.strictEqual(canonicalAccelerator("Shift+CmdOrCtrl+k"), "CommandOrControl+Shift+K");
  assert.strictEqual(canonicalAccelerator("alt+super+F12"), "Alt+Super+F12");
  assert.strictEqual(canonicalAccelerator("Option+Meta+space"), "Alt+Super+Space");
});

test("only enabled, available apps with a valid block are served", () => {
  const lines = [];
  const rows = [
    { name: "on", enabled: true, manifest: { desktop: VALID } },
    { name: "off", enabled: false, manifest: { desktop: VALID } },
    { name: "skipped", enabled: true, unavailable: { reason: "x" }, manifest: { desktop: VALID } },
    { name: "plain", enabled: true, manifest: {} },
    { name: "bad", enabled: true, manifest: { desktop: { windows: "no" } } },
    { name: "Bad Name", enabled: true, manifest: { desktop: VALID } },
    { name: "on", enabled: true, manifest: { desktop: withWindow({}) } },
  ];
  const served = desktopDeclarations(rows, (line) => lines.push(line));
  assert.deepStrictEqual([...served.keys()], ["on"]);
  assert.strictEqual(served.get("on").windows.length, 2);
  assert.deepStrictEqual(lines, ["app-window-host: bad: desktop declaration refused"]);
  assert.deepStrictEqual([...desktopDeclarations({ apps: rows }).keys()], ["on"]);
  assert.strictEqual(desktopDeclarations("nope").size, 0);
});
