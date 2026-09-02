const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// `leave-full-screen` (and its enter twin) fires before the window finishes
// reflowing, so the handler's synchronous updateViewBounds() can read a
// pre-reflow content rect and leave the WebContentsView mis-sized — on Linux
// ~28px taller than the window, clipping the sidebar footer and composer until
// some other event resizes the window. The fix keeps the synchronous recompute
// and follows it with bounded deferred settle passes whose timers are cleared
// on window close.
//
// Read the window owner as TEXT because the checks below intentionally pin the
// listener order and teardown wiring, not just the helper's behavior. Each
// assertion covers one piece a refactor could silently drop while every other
// test stays green.
describe("window lifecycle fullscreen bounds-settle wiring", () => {
  const WINDOW_LIFECYCLE_JS = fs.readFileSync(
    path.join(__dirname, "..", "window-lifecycle.js"),
    "utf8",
  );

  // The whole defect is WHEN bounds are read: the settle passes are worthless
  // if the immediate recompute is dropped (platforms that reflow synchronously
  // would regress to a 250ms flash of wrong bounds), and vice versa.
  it("both fullscreen handlers keep the synchronous recompute AND schedule the settle", () => {
    for (const event of ["enter-full-screen", "leave-full-screen"]) {
      const handler = WINDOW_LIFECYCLE_JS.match(
        new RegExp(`win\\.on\\("${event}", \\(\\) => \\{([\\s\\S]*?)\\}\\);`),
      );
      assert.ok(handler, `could not locate the ${event} handler`);
      const body = handler[1];
      assert.match(body, /updateViewBounds\(\)/, `${event} must keep the synchronous recompute`);
      assert.match(body, /scheduleFullscreenSettle\(\)/, `${event} must schedule the settle pass`);
      assert.ok(
        body.indexOf("updateViewBounds") < body.indexOf("scheduleFullscreenSettle"),
        `${event}: the synchronous recompute runs first; the settle is the backstop`,
      );
    }
  });

  it("the settle schedules bounded deferred recomputes of the view bounds", () => {
    const helper = WINDOW_LIFECYCLE_JS.match(
      /const scheduleFullscreenSettle = \(\) => \{([\s\S]*?)\};/,
    );
    assert.ok(helper, "could not locate scheduleFullscreenSettle");
    const body = helper[1];
    // A re-trigger (e.g. F11 twice) must not leak the previous passes: the
    // helper clears before it re-arms, so at most one settle set is pending.
    assert.match(body, /clearTimeout/, "must clear pending timers before re-arming");
    assert.match(body, /FULLSCREEN_SETTLE_MS\.map/, "must arm every configured settle pass");
    assert.match(body, /setTimeout\(updateViewBounds/, "the deferred pass must recompute bounds");
    // Pin the delay set itself: the LATE pass is the half that closes #5333 on
    // a slow window manager (the quick pass only covers fast reflow). Without
    // this, collapsing to one short timer keeps the suite green while the
    // Linux clipped-composer regression returns.
    const delaySet = WINDOW_LIFECYCLE_JS.match(
      /const FULLSCREEN_SETTLE_MS = \[([^\]]+)\]/,
    );
    assert.ok(delaySet, "could not locate the bounded fullscreen settle delays");
    const delays = [...delaySet[1].matchAll(/(\d+)/g)].map((m) => Number(m[1]));
    assert.ok(delays.length >= 2, "must schedule at least two settle passes");
    assert.ok(Math.min(...delays) <= 500, "must keep a quick pass for the common fast reflow");
    assert.ok(Math.max(...delays) >= 1000, "must keep a late backstop for slow window managers");
  });

  // A timer that outlives the window would call into a destroyed BrowserWindow.
  // updateViewBounds() itself guards isDestroyed(), but the timers must also be
  // cleared on close so nothing stays queued into teardown.
  it("clears the pending settle timers when the window closes", () => {
    // Match loosely like the other assertions: any `closed` handler whose body
    // clears the settle timers counts, so a rename/reformat of the cleanup
    // line does not read as a missing teardown.
    const closedBodies = [
      ...WINDOW_LIFECYCLE_JS.matchAll(/win\.on\("closed", \(\) => \{([\s\S]*?)\}\);/g),
    ]
      .map((m) => m[1]);
    assert.ok(
      closedBodies.some((b) => b.includes("fullscreenSettleTimers") && b.includes("clearTimeout")),
      "a closed handler must clear the pending settle timers",
    );
  });

  // The deferred pass is only safe because updateViewBounds no-ops on a
  // destroyed window; pin that guard so a refactor cannot drop it while the
  // timers still fire.
  it("updateViewBounds guards against a destroyed window", () => {
    const fn = WINDOW_LIFECYCLE_JS.match(/function updateViewBounds\(\) \{([\s\S]*?)\n    \}/);
    assert.ok(fn, "could not locate updateViewBounds");
    assert.match(fn[1], /if \(win\.isDestroyed\(\)\) return;/);
  });
});
