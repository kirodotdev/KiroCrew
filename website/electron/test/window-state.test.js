const { test } = require("node:test");
const assert = require("node:assert");
const {
  sanitizeWindowState,
  captureWindowState,
  isVisibleOn,
  DEFAULT_BOUNDS,
} = require("../window-state");

const DISPLAY = { workArea: { x: 0, y: 0, width: 1920, height: 1080 } };
const OPTS = { displays: [DISPLAY], defaults: { width: 1280, height: 860 }, minSize: { width: 550, height: 600 } };

// ── sanitizeWindowState: missing / malformed ──

test("missing saved state -> default size, not fullscreen, no position", () => {
  const s = sanitizeWindowState(undefined, OPTS);
  assert.strictEqual(s.width, 1280);
  assert.strictEqual(s.height, 860);
  assert.strictEqual(s.fullScreen, false);
  assert.strictEqual(s.x, undefined);
  assert.strictEqual(s.y, undefined);
});

test("null saved state -> defaults", () => {
  const s = sanitizeWindowState(null, OPTS);
  assert.strictEqual(s.width, DEFAULT_BOUNDS.width);
  assert.strictEqual(s.fullScreen, false);
});

// ── sanitizeWindowState: the "super tiny" guard ──

test("below-minimum saved size is rejected -> defaults (kills the tiny window)", () => {
  const s = sanitizeWindowState({ x: 100, y: 100, width: 120, height: 80 }, OPTS);
  assert.strictEqual(s.width, 1280);
  assert.strictEqual(s.height, 860);
});

test("zero / NaN / non-finite size is rejected -> defaults", () => {
  for (const bad of [
    { width: 0, height: 0 },
    { width: NaN, height: 700 },
    { width: 800, height: Infinity },
    { width: "900", height: 700 },
  ]) {
    const s = sanitizeWindowState({ x: 10, y: 10, ...bad }, OPTS);
    assert.strictEqual(s.width, 1280, `bad=${JSON.stringify(bad)}`);
    assert.strictEqual(s.height, 860);
  }
});

test("size exactly at the minimum is kept", () => {
  const s = sanitizeWindowState({ x: 50, y: 50, width: 550, height: 600 }, OPTS);
  assert.strictEqual(s.width, 550);
  assert.strictEqual(s.height, 600);
});

// ── sanitizeWindowState: position visibility ──

test("valid on-screen position is preserved", () => {
  const s = sanitizeWindowState({ x: 200, y: 150, width: 1000, height: 700 }, OPTS);
  assert.strictEqual(s.x, 200);
  assert.strictEqual(s.y, 150);
  assert.strictEqual(s.width, 1000);
});

test("off-screen position (disconnected monitor) is dropped, size kept", () => {
  const s = sanitizeWindowState({ x: -5000, y: -5000, width: 1000, height: 700 }, OPTS);
  assert.strictEqual(s.x, undefined);
  assert.strictEqual(s.y, undefined);
  assert.strictEqual(s.width, 1000);
  assert.strictEqual(s.height, 700);
});

test("no display info -> position is dropped (let OS center)", () => {
  const s = sanitizeWindowState({ x: 100, y: 100, width: 1000, height: 700 }, { ...OPTS, displays: [] });
  assert.strictEqual(s.x, undefined);
  assert.strictEqual(s.y, undefined);
});

// ── sanitizeWindowState: fullscreen carry-through ──

test("fullScreen flag is carried through with normal bounds intact", () => {
  const s = sanitizeWindowState({ x: 10, y: 10, width: 1000, height: 700, fullScreen: true }, OPTS);
  assert.strictEqual(s.fullScreen, true);
  // Normal bounds are still the restore size, never a fullscreen frame.
  assert.strictEqual(s.width, 1000);
  assert.strictEqual(s.height, 700);
});

test("fullScreen is coerced to a boolean", () => {
  assert.strictEqual(sanitizeWindowState({ width: 1000, height: 700, fullScreen: 1 }, OPTS).fullScreen, true);
  assert.strictEqual(sanitizeWindowState({ width: 1000, height: 700 }, OPTS).fullScreen, false);
});

// ── isVisibleOn ──

test("isVisibleOn: window mostly on a second display is visible", () => {
  const displays = [DISPLAY, { workArea: { x: 1920, y: 0, width: 1920, height: 1080 } }];
  assert.strictEqual(isVisibleOn(displays, { x: 2000, y: 100, width: 1000, height: 700 }), true);
});

test("isVisibleOn: only a sliver on-screen is NOT visible", () => {
  // 1px of overlap, below the 80px margin.
  assert.strictEqual(isVisibleOn([DISPLAY], { x: 1919, y: 100, width: 1000, height: 700 }), false);
});

// ── captureWindowState ──

function fakeWin({ normal, fullScreen = false, destroyed = false }) {
  return {
    isDestroyed: () => destroyed,
    isFullScreen: () => fullScreen,
    getNormalBounds: () => normal,
    // getBounds intentionally returns a different (fullscreen) frame to prove
    // captureWindowState prefers getNormalBounds.
    getBounds: () => ({ x: 0, y: 0, width: 3840, height: 2160 }),
  };
}

test("captureWindowState uses getNormalBounds, not the fullscreen frame", () => {
  const win = fakeWin({ normal: { x: 12, y: 34, width: 1000, height: 700 }, fullScreen: true });
  const s = captureWindowState(win);
  assert.deepStrictEqual(s, { x: 12, y: 34, width: 1000, height: 700, fullScreen: true });
});

test("captureWindowState returns null for a destroyed window", () => {
  assert.strictEqual(captureWindowState(fakeWin({ normal: { width: 1000, height: 700 }, destroyed: true })), null);
});

test("captureWindowState returns null for null window", () => {
  assert.strictEqual(captureWindowState(null), null);
});

test("captureWindowState returns null when bounds are degenerate", () => {
  const win = { isDestroyed: () => false, isFullScreen: () => false, getNormalBounds: () => ({ x: 0, y: 0, width: NaN, height: 700 }) };
  assert.strictEqual(captureWindowState(win), null);
});

// ── round-trip: capture -> persist -> sanitize ──

test("round-trip: a fullscreen window restores fullscreen with its normal size", () => {
  const win = fakeWin({ normal: { x: 100, y: 80, width: 1100, height: 760 }, fullScreen: true });
  const saved = captureWindowState(win);
  const restored = sanitizeWindowState(saved, OPTS);
  assert.strictEqual(restored.fullScreen, true);
  assert.strictEqual(restored.width, 1100);
  assert.strictEqual(restored.height, 760);
  assert.strictEqual(restored.x, 100);
  assert.strictEqual(restored.y, 80);
});
