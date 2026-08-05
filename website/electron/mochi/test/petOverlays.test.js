const { test } = require("node:test");
const assert = require("node:assert");
const {
  _inRect,
  isPetWindowOpen,
  closePetWindow,
  hidePetWindow,
  showPetWindow,
  POLL_MS,
} = require("../petOverlays");

// The pet overlay covers an ENTIRE display and is click-through by default.
// Whether a click reaches the desktop below or the pet is decided purely by
// _inRect against the renderer-reported hitbox, so these bounds are the whole
// contract: get them wrong and the user either cannot click the pet, or cannot
// click anything else on their screen.

test("_inRect accepts points inside the rectangle", () => {
  const r = { x: 10, y: 20, w: 100, h: 50 };
  assert.equal(_inRect(50, 40, r), true);
  assert.equal(_inRect(10, 20, r), true, "top-left corner is inside");
  assert.equal(_inRect(110, 70, r), true, "bottom-right corner is inside");
});

test("_inRect rejects points outside the rectangle", () => {
  const r = { x: 10, y: 20, w: 100, h: 50 };
  assert.equal(_inRect(9, 40, r), false, "one px left");
  assert.equal(_inRect(111, 40, r), false, "one px right");
  assert.equal(_inRect(50, 19, r), false, "one px above");
  assert.equal(_inRect(50, 71, r), false, "one px below");
});

test("_inRect treats a null hitbox as a miss", () => {
  // This is the safety net: no known hitbox must mean "let the click through",
  // never "capture everything".
  assert.equal(_inRect(0, 0, null), false);
  assert.equal(_inRect(500, 500, null), false);
});

test("_inRect handles a zero-size rectangle", () => {
  // A pet mid-fade can report w/h 0; only the exact point may match, and it
  // must not throw or match broadly.
  const r = { x: 5, y: 5, w: 0, h: 0 };
  assert.equal(_inRect(5, 5, r), true);
  assert.equal(_inRect(6, 5, r), false);
});

test("no pet window is open before one is created", () => {
  assert.equal(isPetWindowOpen(), false);
});

test("closePetWindow is safe when nothing was opened", () => {
  // before-quit calls this unconditionally.
  assert.doesNotThrow(() => closePetWindow());
  assert.equal(isPetWindowOpen(), false);
});

test("hideAll primitives are safe no-ops when no pet exists", () => {
  // The hideAll hotkey (main.js mochiToggleHideAll) calls these unconditionally;
  // with no pet window they must not throw. hidePetWindow reports 'was not
  // visible' so the caller does not try to restore a window that never existed.
  assert.doesNotThrow(() => showPetWindow());
  assert.strictEqual(hidePetWindow(), false);
});

test("the cursor poll runs at ~60fps", () => {
  // Slower than this and the pet visibly swallows or drops clicks at its edge
  // while the cursor moves; the original settled on 16ms.
  assert.equal(POLL_MS, 16);
});

// The context menu is drawn INSIDE the click-through overlay, so it needs its
// own hitbox or every click on a row is forwarded to whatever sits behind the
// pet. This was the P0: the renderer reported a menu rect that nothing consumed.
const { _shouldIgnoreAt } = require("../petOverlays");

test("a point inside the open menu is NOT ignored", () => {
  const boxes = {
    pet: { x: 0, y: 0, w: 100, h: 100 },
    bubble: null,
    menu: { x: 200, y: 200, w: 160, h: 120 },
  };
  assert.equal(_shouldIgnoreAt(250, 250, boxes), false, "menu row must receive the click");
  assert.equal(_shouldIgnoreAt(50, 50, boxes), false, "pet still receives clicks");
  assert.equal(_shouldIgnoreAt(500, 500, boxes), true, "empty desktop still clicks through");
});

test("with no menu open the decision is pet/bubble only", () => {
  const boxes = { pet: { x: 0, y: 0, w: 10, h: 10 }, bubble: null, menu: null };
  assert.equal(_shouldIgnoreAt(5, 5, boxes), false);
  assert.equal(_shouldIgnoreAt(50, 50, boxes), true);
});

// ── Drag clamp geometry ───────────────────────────────────────────────────
// The pet may hang HALF off the left/right edge (that is how edge-peek reads as
// tucking behind the screen border) but never off the top or bottom. The
// hand-written predecessor used PET_W/PET_H = 120 here while the renderer's
// shared constants say 128, so every clamp was 8px off — a discrepancy nothing
// would report at runtime.
const { _clampLocal, PET_W, PET_H } = require("../petOverlays");

test("pet box matches the renderer's shared constants", () => {
  assert.strictEqual(PET_W, 128);
  assert.strictEqual(PET_H, 128);
});

test("clamp allows half the pet past the left and right edges", () => {
  const bounds = { width: 1000, height: 800 };
  assert.strictEqual(_clampLocal(-500, 100, bounds).x, -PET_W / 2);
  assert.strictEqual(_clampLocal(5000, 100, bounds).x, 1000 - PET_W / 2);
});

test("clamp keeps the pet fully on screen vertically", () => {
  const bounds = { width: 1000, height: 800 };
  assert.strictEqual(_clampLocal(0, -300, bounds).y, 0);
  // Bottom limit leaves the whole sprite visible, unlike the horizontal case.
  assert.strictEqual(_clampLocal(0, 5000, bounds).y, 800 - PET_H);
});

test("a position already inside the display is untouched", () => {
  const r = _clampLocal(400, 300, { width: 1000, height: 800 });
  assert.deepStrictEqual(r, { x: 400, y: 300 });
});

// Regression: a pet-instance switch registers a replacement overlay for the
// same display before the old window's async `closed` fires. The cleanup must
// be identity-checked or it evicts the live replacement, leaking an
// unreachable always-on-top full-screen window.
test("a stale closed handler does not evict the replacement overlay", () => {
  const { _registerOverlay, _getOverlays } = require("../petOverlays");
  const mk = () => {
    let closedCb = null;
    return { on: (ev, cb) => { if (ev === "closed") closedCb = cb; }, fireClosed: () => closedCb && closedCb() };
  };
  const DID = 99123; // unlikely to collide with other tests' display ids
  const oldWin = mk();
  const newWin = mk();
  _registerOverlay(DID, oldWin);
  _registerOverlay(DID, newWin); // replacement for the same display
  oldWin.fireClosed(); // old window's async close fires AFTER the swap
  try {
    assert.equal(_getOverlays().get(DID), newWin, "replacement must survive a stale close");
  } finally {
    _getOverlays().delete(DID);
  }
});
