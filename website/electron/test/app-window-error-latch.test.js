"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  createAppWindowErrorLatch,
  isAppWindowErrorResponse,
} = require("../app-window-error-latch");

function fakeWindow() {
  return {
    destroyed: false,
    isDestroyed() { return this.destroyed; },
  };
}

test("4xx and 5xx responses are error documents; success and redirects are not", () => {
  for (const code of [400, 401, 403, 404, 500, 503]) {
    assert.equal(isAppWindowErrorResponse(code), true, `${code} is an error response`);
  }
  for (const code of [200, 301, 304, undefined, null, "500"]) {
    assert.equal(isAppWindowErrorResponse(code), false, `${code} is not an error response`);
  }
});

test("navigation latches an error document and a healthy navigation clears it", () => {
  const blanked = [];
  const latch = createAppWindowErrorLatch({ onBlank: (win) => blanked.push(win) });
  const win = fakeWindow();

  latch.handleNavigation(win, 403);
  assert.deepEqual(blanked, [win]);
  assert.equal(latch.isBlanked(win), true);

  latch.handleNavigation(win, 200);
  assert.equal(latch.isBlanked(win), false);

  win.destroyed = true;
  assert.doesNotThrow(() => latch.handleNavigation(win, 500));
  assert.equal(blanked.length, 1, "a destroyed window is ignored");
});

test("only a main-frame transport failure that was not superseded is latched", () => {
  const latch = createAppWindowErrorLatch();
  const subframe = fakeWindow();
  const superseded = fakeWindow();
  const failed = fakeWindow();

  latch.handleLoadFailure(subframe, -106, false);
  latch.handleLoadFailure(superseded, -3, true);
  latch.handleLoadFailure(failed, -102, true);

  assert.equal(latch.isBlanked(subframe), false);
  assert.equal(latch.isBlanked(superseded), false);
  assert.equal(latch.isBlanked(failed), true);
});

test("rearm visits only live blanked windows and keeps them latched until success", () => {
  const latch = createAppWindowErrorLatch();
  const failed = fakeWindow();
  const healthy = fakeWindow();
  const destroyed = fakeWindow();
  latch.handleNavigation(failed, 500);
  latch.handleNavigation(destroyed, 500);
  destroyed.destroyed = true;

  const reloaded = [];
  assert.equal(
    latch.rearm([failed, healthy, destroyed], (win) => reloaded.push(win)),
    1,
  );
  assert.deepEqual(reloaded, [failed]);
  assert.equal(latch.isBlanked(failed), true, "reload alone does not prove the document is healthy");
  assert.equal(latch.hasBlanked([healthy, failed]), true);

  latch.handleNavigation(failed, 200);
  assert.equal(latch.hasBlanked([healthy, failed]), false);
});
