"use strict";

/**
 * The off-window cursor poll behind focus mode's distance-based dismissal.
 *
 * What is worth pinning here is the DISTANCE contract, not the plumbing: the
 * poller must stay quiet while the cursor loiters just outside the window (that
 * is the gesture that summoned the overlay), report exactly once when it has
 * genuinely travelled away, report re-entry rather than leaving the renderer to
 * guess it, and stop polling in every one of those cases.
 */

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  createFocusCursorWatch,
  cursorDistanceOutside,
  CURSOR_AWAY_CHANNEL,
  CURSOR_AWAY_PX,
} = require("../focus-cursor");

const BOUNDS = { x: 100, y: 100, width: 800, height: 600 };

/** A BrowserWindow stub that records what was sent to its renderer. */
function fakeWindow(bounds = BOUNDS) {
  const sent = [];
  return {
    sent,
    destroyed: false,
    isDestroyed() {
      return this.destroyed;
    },
    getBounds() {
      return bounds;
    },
    webContents: {
      send(channel, ...args) {
        sent.push([channel, ...args]);
      },
    },
  };
}

/** A `screen` stub whose cursor the test moves by assignment. */
function fakeScreen(x, y) {
  const point = { x, y };
  return {
    point,
    move(nx, ny) {
      point.x = nx;
      point.y = ny;
    },
    getCursorScreenPoint() {
      return { x: point.x, y: point.y };
    },
  };
}

describe("cursorDistanceOutside", () => {
  it("is zero anywhere inside the window, including its edges", () => {
    assert.equal(cursorDistanceOutside(BOUNDS, { x: 500, y: 400 }), 0);
    assert.equal(cursorDistanceOutside(BOUNDS, { x: 100, y: 100 }), 0);
    assert.equal(cursorDistanceOutside(BOUNDS, { x: 900, y: 700 }), 0);
  });

  it("measures the gap to the nearest edge on each axis", () => {
    assert.equal(cursorDistanceOutside(BOUNDS, { x: 60, y: 400 }), 40);
    assert.equal(cursorDistanceOutside(BOUNDS, { x: 500, y: 70 }), 30);
    assert.equal(cursorDistanceOutside(BOUNDS, { x: 930, y: 400 }), 30);
    assert.equal(cursorDistanceOutside(BOUNDS, { x: 500, y: 750 }), 50);
  });

  it("measures a corner exit on both axes", () => {
    // Straight-line distance, not the larger axis: leaving diagonally past a
    // corner covers real ground on both, and 30/40/50 makes that checkable.
    assert.equal(cursorDistanceOutside(BOUNDS, { x: 70, y: 60 }), 50);
  });

  it("reads unusable geometry as still-inside rather than as far away", () => {
    // Fail toward keeping the surface: a dismissal nobody asked for is worse
    // than one that waits for the next readable sample.
    assert.equal(cursorDistanceOutside(null, { x: 0, y: 0 }), 0);
    assert.equal(cursorDistanceOutside(BOUNDS, null), 0);
    assert.equal(cursorDistanceOutside(BOUNDS, { x: NaN, y: 0 }), 0);
    assert.equal(cursorDistanceOutside({ x: 0, y: 0 }, { x: 5, y: 5 }), 0);
  });
});

describe("createFocusCursorWatch", () => {
  it("reports away exactly once past the threshold, then stops polling", () => {
    // 4px outside: the overshoot that OPENED the overlay. Dismissing here is the
    // bug — the pointer is on its way back to the surface it just summoned.
    const screen = fakeScreen(96, 400);
    const win = fakeWindow();
    const watch = createFocusCursorWatch({ screen });

    watch.watch(win, true);
    assert.equal(watch.isPolling(), true);
    watch.pollOnce();
    assert.deepEqual(win.sent, []);

    // Still short of the threshold — hovering just off the edge keeps it.
    screen.move(100 - (CURSOR_AWAY_PX - 1), 400);
    watch.pollOnce();
    assert.deepEqual(win.sent, []);

    screen.move(100 - CURSOR_AWAY_PX, 400);
    watch.pollOnce();
    assert.deepEqual(win.sent, [[CURSOR_AWAY_CHANNEL, true]]);
    assert.equal(watch.watchedCount(), 0);
    assert.equal(watch.isPolling(), false);

    // One transition per watch: further travel reports nothing.
    screen.move(-500, -500);
    watch.pollOnce();
    assert.equal(win.sent.length, 1);
  });

  it("reports re-entry instead of leaving the renderer to notice it", () => {
    // The band the pointer comes back through can be a window-drag region, which
    // the compositor resolves before hit-testing — so the page may never see the
    // mouseover that would cancel the dismissal.
    const screen = fakeScreen(80, 400);
    const win = fakeWindow();
    const watch = createFocusCursorWatch({ screen });

    watch.watch(win, true);
    watch.pollOnce();
    screen.move(400, 300);
    watch.pollOnce();

    assert.deepEqual(win.sent, [[CURSOR_AWAY_CHANNEL, false]]);
    assert.equal(watch.isPolling(), false);
  });

  it("does not call a cursor still inside the window a re-entry", () => {
    // The renderer arms this from `mouseout`, when the cursor is a pixel from the
    // boundary and routinely still within getBounds() (which includes the
    // titlebar the viewport does not). Answering "back inside" on the first tick
    // would cancel every dismissal the instant it was armed.
    const screen = fakeScreen(400, 100);
    const win = fakeWindow();
    const watch = createFocusCursorWatch({ screen });

    watch.watch(win, true);
    watch.pollOnce();
    watch.pollOnce();
    assert.deepEqual(win.sent, []);
    assert.equal(watch.isPolling(), true);

    // Now leave for real, then come back: THAT is a re-entry.
    screen.move(400, 90);
    watch.pollOnce();
    screen.move(400, 300);
    watch.pollOnce();
    assert.deepEqual(win.sent, [[CURSOR_AWAY_CHANNEL, false]]);
  });

  it("stops polling when the renderer disarms the watch", () => {
    const screen = fakeScreen(0, 0);
    const win = fakeWindow();
    const watch = createFocusCursorWatch({ screen });

    watch.watch(win, true);
    watch.watch(win, false);
    assert.equal(watch.isPolling(), false);
    watch.pollOnce();
    assert.deepEqual(win.sent, []);
  });

  it("drops a destroyed window without sending to it", () => {
    const screen = fakeScreen(0, 0);
    const win = fakeWindow();
    const watch = createFocusCursorWatch({ screen });

    watch.watch(win, true);
    win.destroyed = true;
    watch.pollOnce();
    assert.deepEqual(win.sent, []);
    assert.equal(watch.watchedCount(), 0);
    assert.equal(watch.isPolling(), false);
  });

  it("reports nothing when the cursor cannot be read", () => {
    // A locked screen or a headless run: keep the watch armed for the next tick
    // rather than dismissing on missing evidence.
    const win = fakeWindow();
    const watch = createFocusCursorWatch({
      screen: {
        getCursorScreenPoint() {
          throw new Error("no cursor");
        },
      },
    });

    watch.watch(win, true);
    watch.pollOnce();
    assert.deepEqual(win.sent, []);
    assert.equal(watch.watchedCount(), 1);
  });

  it("watches several windows independently", () => {
    // Two dashboard windows can each hold a reveal; one going away must not
    // dismiss the other's.
    const screen = fakeScreen(100 - CURSOR_AWAY_PX, 400);
    const near = fakeWindow(BOUNDS);
    const far = fakeWindow({ x: 2000, y: 100, width: 800, height: 600 });
    const watch = createFocusCursorWatch({ screen });

    watch.watch(near, true);
    watch.watch(far, true);
    watch.pollOnce();

    assert.deepEqual(near.sent, [[CURSOR_AWAY_CHANNEL, true]]);
    assert.deepEqual(far.sent, [[CURSOR_AWAY_CHANNEL, true]]);
    assert.equal(watch.isPolling(), false);

    // And the near window alone when the cursor sits inside the far one.
    const screen2 = fakeScreen(2400, 400);
    const near2 = fakeWindow(BOUNDS);
    const far2 = fakeWindow({ x: 2000, y: 100, width: 800, height: 600 });
    const watch2 = createFocusCursorWatch({ screen: screen2 });
    watch2.watch(near2, true);
    watch2.watch(far2, true);
    watch2.pollOnce();
    assert.deepEqual(near2.sent, [[CURSOR_AWAY_CHANNEL, true]]);
    assert.deepEqual(far2.sent, []);
    assert.equal(watch2.isPolling(), true);
  });

  it("stopAll drops every watch", () => {
    const screen = fakeScreen(400, 300);
    const win = fakeWindow();
    const watch = createFocusCursorWatch({ screen });
    watch.watch(win, true);
    watch.stopAll();
    assert.equal(watch.watchedCount(), 0);
    assert.equal(watch.isPolling(), false);
  });

  it("really polls on its interval, not only when driven by hand", () => {
    // Everything above calls pollOnce directly; this is the one check that the
    // timer is wired to it at all.
    const screen = fakeScreen(100 - CURSOR_AWAY_PX, 400);
    const win = fakeWindow();
    const watch = createFocusCursorWatch({ screen, pollMs: 1 });
    watch.watch(win, true);
    return new Promise((resolve) => {
      setTimeout(() => {
        assert.deepEqual(win.sent, [[CURSOR_AWAY_CHANNEL, true]]);
        resolve();
      }, 30);
    });
  });
});
