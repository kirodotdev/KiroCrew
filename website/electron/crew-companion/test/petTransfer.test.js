const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("path");
const os = require("os");
const Module = require("module");

// Load petOverlay.js with a fake `electron` whose screen exposes a configurable
// display arrangement. findDisplayAtPoint / findNearestDisplay / clampLocal read
// screen directly (verbatim from the reference), so the stub is how we drive them.
function loadWithDisplays(displays) {
  const electron = {
    app: { getPath: () => os.tmpdir(), on() {} },
    BrowserWindow: class {},
    ipcMain: { on() {}, handle() {} },
    screen: {
      getPrimaryDisplay: () => displays[0],
      getAllDisplays: () => displays,
      getCursorScreenPoint: () => ({ x: 0, y: 0 }),
      on() {},
      removeListener() {},
    },
  };
  const modPath = path.join(__dirname, "..", "petOverlay.js");
  delete require.cache[require.resolve(modPath)];
  const orig = Module._load;
  Module._load = (request, parent, isMain) =>
    request === "electron" ? electron : orig(request, parent, isMain);
  try {
    return require(modPath);
  } finally {
    Module._load = orig;
  }
}

// A: primary at origin; B: to its right. Gaps exist at x<0 and x>=3360.
const A = {
  id: 1,
  bounds: { x: 0, y: 0, width: 1440, height: 900 },
  workArea: { x: 0, y: 25, width: 1440, height: 875 },
};
const B = {
  id: 2,
  bounds: { x: 1440, y: 0, width: 1920, height: 1080 },
  workArea: { x: 1440, y: 0, width: 1920, height: 1040 },
};

test("findDisplayAtPoint returns the display whose bounds contain the point", () => {
  const m = loadWithDisplays([A, B]);
  assert.strictEqual(m._findDisplayAtPoint(700, 400).id, 1, "inside A");
  assert.strictEqual(m._findDisplayAtPoint(2000, 500).id, 2, "inside B");
  assert.strictEqual(m._findDisplayAtPoint(1440, 0).id, 2, "B's left edge is inclusive");
  assert.strictEqual(m._findDisplayAtPoint(1439, 0).id, 1, "one px left is still A");
});

test("findDisplayAtPoint returns null when no display contains the point", () => {
  const m = loadWithDisplays([A, B]);
  assert.strictEqual(m._findDisplayAtPoint(-50, 400), null, "gap left of A");
  assert.strictEqual(m._findDisplayAtPoint(5000, 400), null, "gap right of B");
});

test("findNearestDisplay picks the closest display for a gap point", () => {
  const m = loadWithDisplays([A, B]);
  assert.strictEqual(m._findNearestDisplay(-100, 400).id, 1, "left of A -> A");
  assert.strictEqual(m._findNearestDisplay(5000, 500).id, 2, "right of B -> B");
  assert.strictEqual(m._findNearestDisplay(1450, 4000).id, 2, "below B, inside its x -> B");
});

test("transfer decision: cross-boundary triggers a handoff, same display does not", () => {
  const m = loadWithDisplays([A, B]);
  const activeDisplayId = 1; // avatar currently on A
  // Cursor over B -> target B -> id differs -> transfer.
  const overB = m._findDisplayAtPoint(2000, 500) || m._findNearestDisplay(2000, 500);
  assert.notStrictEqual(overB.id, activeDisplayId, "over B while active A -> transfer");
  // Cursor still over A -> target A -> id matches -> no transfer.
  const overA = m._findDisplayAtPoint(700, 400) || m._findNearestDisplay(700, 400);
  assert.strictEqual(overA.id, activeDisplayId, "over A while active A -> no transfer");
  // Cursor in a gap -> nearest-display fallback still yields a valid target (never null).
  const inGap = m._findDisplayAtPoint(-100, 400) || m._findNearestDisplay(-100, 400);
  assert.strictEqual(inGap.id, 1, "gap resolves to nearest (A), never null");
});

test("clampLocal lets the avatar hang half off left/right but not top/bottom", () => {
  const m = loadWithDisplays([A, B]);
  const bounds = { width: 1000, height: 800 };
  assert.strictEqual(m._clampLocal(-500, 100, bounds).x, -m.PET_W / 2, "half off the left");
  assert.strictEqual(m._clampLocal(5000, 100, bounds).x, 1000 - m.PET_W / 2, "half off the right");
  assert.strictEqual(m._clampLocal(0, -300, bounds).y, 0, "never off the top");
  assert.strictEqual(m._clampLocal(0, 5000, bounds).y, 800 - m.PET_H, "fully on-screen at the bottom");
  assert.deepStrictEqual(m._clampLocal(400, 300, bounds), { x: 400, y: 300 }, "inside is untouched");
});

test("single-display arrangement: every point resolves to the one display", () => {
  const m = loadWithDisplays([A]);
  assert.strictEqual(m._findDisplayAtPoint(700, 400).id, 1, "inside the only display");
  assert.strictEqual(m._findNearestDisplay(-999, -999).id, 1, "off-screen still -> the only display");
});

test("main's PET box stays in sync with the renderer's exported sprite size", () => {
  const m = loadWithDisplays([A, B]);
  // The renderer owns the sprite size (src/apps/crew-companion/constants.ts); main
  // duplicates it across the process boundary for edge-clamp and hand-off math.
  // Assert cross-file equality so a change on one side that is not mirrored fails
  // HERE instead of silently mispositioning the avatar at runtime.
  const src = fs.readFileSync(
    path.join(__dirname, "..", "..", "..", "src", "apps", "crew-companion", "constants.ts"),
    "utf-8",
  );
  const rW = Number((src.match(/export const PET_W\s*=\s*(\d+)/) || [])[1]);
  const rH = Number((src.match(/export const PET_H\s*=\s*(\d+)/) || [])[1]);
  assert.strictEqual(m.PET_W, rW, "main PET_W must equal the renderer's PET_W");
  assert.strictEqual(m.PET_H, rH, "main PET_H must equal the renderer's PET_H");
});
