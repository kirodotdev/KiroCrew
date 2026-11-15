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
  CAPABILITIES,
  MAX_SHORTCUTS,
  MAX_WINDOWS,
  approvedCapabilities,
  canonicalAccelerator,
  desktopDeclarations,
  keyPressChords,
  parseDesktopDeclaration,
  pressedChord,
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
    { id: "pet", entry: "desktop/pet.mjs", kind: "overlay", openOnEnable: true, displays: "cursor" },
    {
      id: "panel", entry: "desktop/panel.mjs", kind: "panel", openOnEnable: false,
      width: 420, height: 600, position: "top-right", transparent: false, gutter: 0,
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

test("a spanning overlay, a background window and a panel gutter parse", () => {
  const parsed = parseDesktopDeclaration({
    windows: [
      { id: "pet", entry: "pet.mjs", kind: "overlay", displays: "all", openOnEnable: true },
      { id: "brain", entry: "brain.mjs", kind: "background", openOnEnable: true },
      { id: "panel", entry: "panel.mjs", kind: "panel", transparent: true, gutter: 48 },
      { id: "other", entry: "other.mjs", kind: "overlay" },
    ],
  });
  assert.deepStrictEqual(parsed.windows, [
    { id: "pet", entry: "pet.mjs", kind: "overlay", openOnEnable: true, displays: "all" },
    { id: "brain", entry: "brain.mjs", kind: "background", openOnEnable: true },
    {
      id: "panel", entry: "panel.mjs", kind: "panel", openOnEnable: false,
      width: 400, height: 560, position: "bottom-right", transparent: true, gutter: 48,
    },
    { id: "other", entry: "other.mjs", kind: "overlay", openOnEnable: false, displays: "cursor" },
  ]);
});

const REFUSED = [
  ["not an object", "yes"],
  ["no windows", {}],
  ["empty windows", { windows: [], shortcuts: [] }],
  ["unknown block key", { ...withWindow({}), menus: [] }],
  ["a kind outside the closed set", withWindow({ kind: "toast" })],
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
  ["an unknown display span", withWindow({ displays: "everywhere" })],
  ["a display span on a panel", { windows: [{ id: "p", entry: "p.js", kind: "panel", displays: "all" }] }],
  ["a gutter on an overlay", withWindow({ gutter: 8 })],
  ["a gutter wider than the cap", { windows: [{ id: "p", entry: "p.js", kind: "panel", gutter: 49 }] }],
  ["a boolean gutter", { windows: [{ id: "p", entry: "p.js", kind: "panel", gutter: true }] }],
  ["panel fields on a background window", { windows: [{ id: "b", entry: "b.js", kind: "background", width: 400 }] }],
  ["a display span on a background window", { windows: [{ id: "b", entry: "b.js", kind: "background", displays: "all" }] }],
  [
    "two overlays spanning every display",
    {
      windows: [
        { id: "a", entry: "a.js", kind: "overlay", displays: "all" },
        { id: "b", entry: "b.js", kind: "overlay", displays: "all" },
      ],
    },
  ],
  [
    "two background windows",
    {
      windows: [
        { id: "a", entry: "a.js", kind: "background" },
        { id: "b", entry: "b.js", kind: "background" },
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

test("a capability is served only when the manifest declares it AND the row approves it", () => {
  const row = (declared, grant) => ({
    name: "cap-app",
    enabled: true,
    manifest: { desktop: { windows: [{ id: "w", entry: "w.mjs", kind: "panel" }] }, permissions: { desktopCapabilities: declared } },
    ...(grant === undefined ? {} : { desktopCapabilitiesGrant: grant }),
  });
  const caps = (r) => desktopDeclarations([r]).get("cap-app").capabilities;
  assert.deepStrictEqual(CAPABILITIES, ["screenCapture", "localFiles", "gatewayChoice"]);
  assert.deepStrictEqual(caps(row([...CAPABILITIES], { approved: [...CAPABILITIES] })), [...CAPABILITIES]);
  assert.deepStrictEqual(caps(row(["localFiles", "screenCapture"], { approved: ["screenCapture"] })), ["screenCapture"]);
  // Approved but no longer declared, unknown names, a gateway with no grant row.
  assert.deepStrictEqual(caps(row([], { approved: ["localFiles"] })), []);
  assert.deepStrictEqual(caps(row(["everything"], { approved: ["everything"] })), []);
  assert.deepStrictEqual(caps(row(["localFiles"])), []);
  assert.deepStrictEqual(caps(row("localFiles", { approved: "localFiles" })), []);
  assert.deepStrictEqual(approvedCapabilities({}, {}), []);
});

test("a shortcut's per-platform default overrides its accelerator on the platforms it names", () => {
  const raw = {
    windows: [{ id: "pet", entry: "pet.mjs", kind: "overlay" }],
    shortcuts: [{
      id: "toggle",
      accelerator: "Alt+Shift+M",
      platformAccelerators: { macos: "CommandOrControl+Shift+M" },
      window: "pet",
    }],
  };
  const on = (platform) => parseDesktopDeclaration(raw, platform).shortcuts[0];
  assert.deepStrictEqual(on("darwin"), {
    id: "toggle", accelerator: "CommandOrControl+Shift+M", chord: "CommandOrControl+Shift+M", window: "pet",
  });
  assert.deepStrictEqual(on("linux"), { id: "toggle", accelerator: "Alt+Shift+M", chord: "Alt+Shift+M", window: "pet" });
  assert.strictEqual(on("win32").accelerator, "Alt+Shift+M");
  assert.strictEqual(desktopDeclarations([{ name: "a", enabled: true, manifest: { desktop: raw } }], () => {}, { platform: "darwin" })
    .get("a").shortcuts[0].accelerator, "CommandOrControl+Shift+M");
});

test("a per-platform default is held to every shortcut rule, on every platform", () => {
  const base = (shortcuts) => ({ windows: [{ id: "pet", entry: "pet.mjs", kind: "overlay" }], shortcuts });
  const refused = [
    // A single-modifier chord, an unknown platform, a non-object.
    [{ id: "a", accelerator: "Alt+Shift+M", platformAccelerators: { macos: "CommandOrControl+C" }, window: "pet" }],
    [{ id: "a", accelerator: "Alt+Shift+M", platformAccelerators: { darwin: "Alt+Shift+N" }, window: "pet" }],
    [{ id: "a", accelerator: "Alt+Shift+M", platformAccelerators: ["Alt+Shift+N"], window: "pet" }],
    // The fallback itself must be valid even when every platform overrides it.
    [{
      id: "a", accelerator: "M",
      platformAccelerators: { macos: "Alt+Shift+M", windows: "Alt+Shift+M", linux: "Alt+Shift+M" },
      window: "pet",
    }],
    // Two shortcuts on one chord on any one platform.
    [
      { id: "a", accelerator: "Alt+Shift+M", platformAccelerators: { linux: "Alt+Shift+N" }, window: "pet" },
      { id: "b", accelerator: "Alt+Shift+N", platformAccelerators: { linux: "Alt+Shift+O" }, window: "pet" },
      { id: "c", accelerator: "Alt+Shift+P", platformAccelerators: { linux: "Shift+Alt+O" }, window: "pet" },
    ],
  ];
  for (const shortcuts of refused) {
    for (const platform of ["darwin", "linux", "win32"]) {
      assert.strictEqual(parseDesktopDeclaration(base(shortcuts), platform), null, JSON.stringify(shortcuts));
    }
  }
});

test("NEGATIVE: a desktop block asks the shell for nothing beyond its windows and shortcuts", () => {
  assert.strictEqual(parseDesktopDeclaration({ ...VALID, legacyMachinePrefs: { gatewayChoice: "petInstance" } }), null);
});

test("a chord is the keys it presses on the platform, whatever its spelling", () => {
  assert.strictEqual(pressedChord("CommandOrControl+Shift+M", "darwin"), "Cmd+Shift+M");
  assert.strictEqual(pressedChord("Shift+Super+M", "darwin"), "Cmd+Shift+M");
  assert.strictEqual(pressedChord("CommandOrControl+Shift+M", "linux"), "Ctrl+Shift+M");
  assert.strictEqual(pressedChord("control+shift+m", "win32"), "Ctrl+Shift+M");
  assert.strictEqual(pressedChord("Super+Alt+F5", "win32"), "Alt+Super+F5");
  assert.strictEqual(pressedChord("CommandOrControl+C", "linux"), "", "a chord the rule refuses presses nothing");
});

test("a native key press spells its chord from the key's position and its character", () => {
  const down = (fields) => ({ type: "keyDown", key: "", code: "", ...fields });
  // macOS: Option changes the character, so the position names the key.
  assert.deepStrictEqual(
    keyPressChords(down({ code: "KeyJ", key: "\u00d4", meta: true, alt: true, shift: true }), "darwin"),
    ["Alt+Cmd+Shift+J"],
  );
  assert.deepStrictEqual(keyPressChords(down({ code: "KeyJ", key: "j", meta: true, alt: true }), "linux"), ["Alt+Super+J"]);
  // Another layout: the position and the character both count.
  assert.deepStrictEqual(keyPressChords(down({ code: "KeyQ", key: "a", control: true, alt: true }), "win32"), [
    "Alt+Ctrl+Q", "Alt+Ctrl+A",
  ]);
  assert.deepStrictEqual(keyPressChords(down({ code: "Digit7", key: "&", control: true, shift: true }), "linux"), ["Ctrl+Shift+7"]);
  assert.deepStrictEqual(keyPressChords(down({ code: "F12", key: "F12", control: true, alt: true }), "linux"), ["Alt+Ctrl+F12"]);
  assert.deepStrictEqual(keyPressChords(down({ code: "ArrowLeft", key: "ArrowLeft", alt: true, shift: true }), "linux"), ["Alt+Shift+Left"]);
  assert.deepStrictEqual(keyPressChords(down({ code: "Space", key: " ", control: true, alt: true }), "linux"), ["Alt+Ctrl+Space"]);
  for (const input of [
    down({ code: "ShiftLeft", key: "Shift", shift: true, alt: true }),
    down({ code: "KeyJ", key: "j", alt: true, shift: true, isAutoRepeat: true }),
    { type: "keyUp", code: "KeyJ", key: "j", alt: true, shift: true },
    { type: "rawKeyDown", code: "KeyJ", key: "j", alt: true, shift: true },
    null,
  ]) {
    assert.deepStrictEqual(keyPressChords(input, "linux"), [], JSON.stringify(input));
  }
  // What a press spells is compared with what a request presses.
  const [chord] = keyPressChords(down({ code: "KeyM", key: "M", meta: true, shift: true }), "darwin");
  assert.strictEqual(chord, pressedChord("Shift+CmdOrCtrl+M", "darwin"));
});
