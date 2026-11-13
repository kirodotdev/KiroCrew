/**
 * Where the companion's panel opens.
 *
 * `placePanelNearPet` is pure geometry, so these tests call it directly. They pin
 * the side rule (the companion's centre against the work area's centre), the
 * top-aligned slot, and the clamp to the work area, including displays with
 * negative origins.
 */

const test = require("node:test");
const assert = require("node:assert");

const {
  PANEL_PLACEMENT_DEFAULTS,
  clamp,
  idealLeft,
  placePanelNearPet,
} = require("../panelPlacement");

const WORK = { x: 0, y: 0, width: 1920, height: 1080 };
const PANEL = { width: 400, height: 600 };
const PET = { width: 120, height: 120 };

test("a companion on the left half gets the panel on its right", () => {
  const pet = { ...PET, x: 100, y: 200 };
  const placed = placePanelNearPet(pet, PANEL, WORK);
  assert.strictEqual(placed.side, "right");
  assert.deepStrictEqual(placed.rect, {
    x: 100 + 120 + PANEL_PLACEMENT_DEFAULTS.gap,
    y: 200,
    width: 400,
    height: 600,
  });
  assert.strictEqual(placed.clamped, false);
});

test("a companion on the right half gets the panel on its left, even with room to its right", () => {
  // Centre at 1060, just right of the 960 midline, with 740px free to the right:
  // enough for the 400px panel, and the panel still opens on the left.
  const pet = { ...PET, x: 1000, y: 200 };
  const placed = placePanelNearPet(pet, PANEL, WORK);
  assert.strictEqual(placed.side, "left");
  assert.strictEqual(placed.rect.x, 1000 - PANEL_PLACEMENT_DEFAULTS.gap - 400);
  assert.strictEqual(placed.clamped, false);
});

test("a companion centred exactly on the midline counts as the left half", () => {
  const pet = { ...PET, x: 960 - 60, y: 200 };
  assert.strictEqual(placePanelNearPet(pet, PANEL, WORK).side, "right");
});

test("the panel is clamped inside the work area and reports it", () => {
  // Near the bottom-right corner: the left-side slot fits horizontally, but the
  // top-aligned slot would run past the bottom edge.
  const pet = { ...PET, x: 1780, y: 900 };
  const placed = placePanelNearPet(pet, PANEL, WORK);
  const margin = PANEL_PLACEMENT_DEFAULTS.margin;
  assert.strictEqual(placed.side, "left");
  assert.strictEqual(placed.rect.y, 1080 - 600 - margin);
  assert.strictEqual(placed.clamped, true);
});

test("a companion at the left edge keeps the panel off the screen edge", () => {
  const pet = { ...PET, x: -50, y: -40 };
  const placed = placePanelNearPet(pet, PANEL, WORK);
  assert.strictEqual(placed.side, "right");
  assert.strictEqual(placed.rect.y, PANEL_PLACEMENT_DEFAULTS.margin);
  assert.strictEqual(placed.clamped, true);
});

test("a display left of and above the primary keeps its negative origin", () => {
  const work = { x: -1920, y: -1080, width: 1920, height: 1080 };
  const pet = { ...PET, x: -1700, y: -900 };
  const placed = placePanelNearPet(pet, PANEL, work);
  assert.strictEqual(placed.side, "right");
  assert.deepStrictEqual(
    { x: placed.rect.x, y: placed.rect.y },
    { x: -1700 + 120 + PANEL_PLACEMENT_DEFAULTS.gap, y: -900 },
  );
  assert.strictEqual(placed.clamped, false);
});

test("options override the gap, margin and vertical offset", () => {
  const pet = { ...PET, x: 100, y: 200 };
  const placed = placePanelNearPet(pet, PANEL, WORK, { gap: 20, margin: 0, slotOffsetY: 50 });
  assert.deepStrictEqual({ x: placed.rect.x, y: placed.rect.y }, { x: 240, y: 150 });
});

test("a panel wider than the work area pins to the left margin", () => {
  const work = { x: 0, y: 0, width: 300, height: 1080 };
  const pet = { ...PET, x: 10, y: 10 };
  const placed = placePanelNearPet(pet, PANEL, work);
  assert.strictEqual(placed.rect.x, PANEL_PLACEMENT_DEFAULTS.margin);
  assert.strictEqual(placed.clamped, true);
});

test("clamp returns min when the range is inverted", () => {
  assert.strictEqual(clamp(5, 0, 10), 5);
  assert.strictEqual(clamp(-5, 0, 10), 0);
  assert.strictEqual(clamp(50, 0, 10), 10);
  assert.strictEqual(clamp(5, 10, 0), 10);
});

test("idealLeft puts the panel one gap beside the companion", () => {
  const pet = { x: 100, y: 0, width: 120, height: 120 };
  assert.strictEqual(idealLeft(pet, 400, "right", 12), 232);
  assert.strictEqual(idealLeft(pet, 400, "left", 12), -312);
});
