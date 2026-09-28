const { test } = require("node:test");
const assert = require("node:assert");
const { ZOOM_LADDER, ZOOM_MIN, ZOOM_MAX, clampZoomFactor, stepZoomFactor } = require("../zoom");

// ── ladder shape ──

test("ladder is strictly ascending and includes 100%", () => {
  for (let i = 1; i < ZOOM_LADDER.length; i++) {
    assert.ok(ZOOM_LADDER[i] > ZOOM_LADDER[i - 1], `ladder[${i}] ascends`);
  }
  assert.ok(ZOOM_LADDER.includes(1));
  assert.strictEqual(ZOOM_MIN, ZOOM_LADDER[0]);
  assert.strictEqual(ZOOM_MAX, ZOOM_LADDER[ZOOM_LADDER.length - 1]);
});

// ── clampZoomFactor ──

test("clamp: non-finite and non-number inputs reset to 1", () => {
  assert.strictEqual(clampZoomFactor(NaN), 1);
  assert.strictEqual(clampZoomFactor(Infinity), 1);
  assert.strictEqual(clampZoomFactor(-Infinity), 1);
  assert.strictEqual(clampZoomFactor("1.5"), 1);
  assert.strictEqual(clampZoomFactor(undefined), 1);
  assert.strictEqual(clampZoomFactor(null), 1);
});

test("clamp: out-of-range values saturate", () => {
  assert.strictEqual(clampZoomFactor(0.1), ZOOM_MIN);
  assert.strictEqual(clampZoomFactor(9), ZOOM_MAX);
  assert.strictEqual(clampZoomFactor(-2), ZOOM_MIN);
});

test("clamp: in-range values pass through", () => {
  assert.strictEqual(clampZoomFactor(1), 1);
  assert.strictEqual(clampZoomFactor(1.37), 1.37);
});

// ── stepZoomFactor: on-ladder stepping ──

test("step up from 100% -> 110%; down -> 90%", () => {
  assert.strictEqual(stepZoomFactor(1, +1), 1.1);
  assert.strictEqual(stepZoomFactor(1, -1), 0.9);
});

test("stepping walks the whole ladder in both directions", () => {
  let f = ZOOM_MIN;
  for (let i = 1; i < ZOOM_LADDER.length; i++) {
    f = stepZoomFactor(f, +1);
    assert.strictEqual(f, ZOOM_LADDER[i]);
  }
  for (let i = ZOOM_LADDER.length - 2; i >= 0; i--) {
    f = stepZoomFactor(f, -1);
    assert.strictEqual(f, ZOOM_LADDER[i]);
  }
});

test("ends saturate: up from max stays max, down from min stays min", () => {
  assert.strictEqual(stepZoomFactor(ZOOM_MAX, +1), ZOOM_MAX);
  assert.strictEqual(stepZoomFactor(ZOOM_MIN, -1), ZOOM_MIN);
});

// ── stepZoomFactor: off-ladder values snap ──

test("off-ladder value snaps to the next stop in the step direction", () => {
  // e.g. a factor restored from an old setZoomLevel(±0.5) session: 1.2^0.5 ≈ 1.0954
  assert.strictEqual(stepZoomFactor(1.0954, +1), 1.1);
  assert.strictEqual(stepZoomFactor(1.0954, -1), 1);
  assert.strictEqual(stepZoomFactor(0.55, -1), 0.5);
  assert.strictEqual(stepZoomFactor(2.9, +1), 3);
});

test("float noise within tolerance does not double-step", () => {
  assert.strictEqual(stepZoomFactor(1.1000000000000002, +1), 1.25);
  assert.strictEqual(stepZoomFactor(0.9999999, +1), 1.1);
});

test("garbage current factor steps from the reset value 1", () => {
  assert.strictEqual(stepZoomFactor(NaN, +1), 1.1);
  assert.strictEqual(stepZoomFactor(undefined, -1), 0.9);
});
