const { test } = require("node:test");
const assert = require("node:assert");
const {
  BLANK_URL,
  isUntrustedContents,
  normalizeUrl,
  normalizeBounds,
  boundsEqual,
  deriveScale,
  scaleRect,
  computeVisible,
  viewHoldsFocus,
  focusHandBackMessage,
  createBrowserViewManager,
} = require("../browser-view");

// ── normalizeUrl ──

test("url: http and https pass through", () => {
  assert.strictEqual(normalizeUrl("https://example.com/a?b=1"), "https://example.com/a?b=1");
  assert.strictEqual(normalizeUrl("http://127.0.0.1:8080/x"), "http://127.0.0.1:8080/x");
});

test("url: bare hosts are upgraded to https", () => {
  assert.strictEqual(normalizeUrl("example.com"), "https://example.com/");
  assert.strictEqual(normalizeUrl("  example.com/docs  "), "https://example.com/docs");
});

test("url: localhost with a port is treated as a host, not a scheme", () => {
  // `new URL("localhost:5173")` parses protocol "localhost:" — must not leak through.
  assert.strictEqual(normalizeUrl("localhost:5173"), "https://localhost:5173/");
});

test("url: about:blank is allowed", () => {
  assert.strictEqual(normalizeUrl(BLANK_URL), BLANK_URL);
});

test("url: non-web schemes are refused", () => {
  for (const bad of [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "data:text/html,<h1>x</h1>",
    "chrome://settings",
    "devtools://devtools/bundled/x.html",
    "ftp://example.com",
  ]) {
    assert.strictEqual(normalizeUrl(bad), null, `${bad} must be refused`);
  }
});

test("url: empty and non-string input is refused", () => {
  for (const bad of ["", "   ", null, undefined, 42, {}, []]) {
    assert.strictEqual(normalizeUrl(bad), null);
  }
});

// ── normalizeBounds ──

const CONTENT = { width: 1280, height: 860 };

test("bounds: an in-window rect passes through, rounded", () => {
  assert.deepStrictEqual(
    normalizeBounds({ x: 820.4, y: 60.6, width: 459.5, height: 799.2 }, CONTENT),
    { x: 820, y: 61, width: 460, height: 799 }
  );
});

test("bounds: a rect overflowing the window is clamped to the content area", () => {
  assert.deepStrictEqual(
    normalizeBounds({ x: 1000, y: 100, width: 900, height: 900 }, CONTENT),
    { x: 1000, y: 100, width: 280, height: 760 }
  );
});

test("bounds: negative origin is clamped to zero", () => {
  assert.deepStrictEqual(
    normalizeBounds({ x: -50, y: -10, width: 300, height: 200 }, CONTENT),
    { x: 0, y: 0, width: 300, height: 200 }
  );
});

test("bounds: malformed or zero-area rects yield null (treated as hide)", () => {
  assert.strictEqual(normalizeBounds({ x: NaN, y: 0, width: 10, height: 10 }, CONTENT), null);
  assert.strictEqual(normalizeBounds({ x: 0, y: 0, width: 0, height: 500 }, CONTENT), null);
  assert.strictEqual(normalizeBounds({ x: 0, y: 0, width: 10, height: -5 }, CONTENT), null);
  assert.strictEqual(normalizeBounds(null, CONTENT), null);
  assert.strictEqual(normalizeBounds("nope", CONTENT), null);
});

test("bounds: a collapsed window (pre-layout) yields null", () => {
  assert.strictEqual(normalizeBounds({ x: 0, y: 0, width: 10, height: 10 }, { width: 0, height: 0 }), null);
  assert.strictEqual(normalizeBounds({ x: 0, y: 0, width: 10, height: 10 }, undefined), null);
});

// ── deriveScale / scaleRect (CSS px -> DIP) ──

test("scale: viewport matching the content area means zoom 1", () => {
  assert.strictEqual(deriveScale({ width: 1280 }, { width: 1280 }), 1);
});

test("scale: a zoomed dashboard reports a smaller viewport, giving scale > 1", () => {
  // Zoom 1.25 -> 1280 DIP of content reports as 1024 CSS px.
  assert.strictEqual(deriveScale({ width: 1024 }, { width: 1280 }), 1.25);
});

test("scale: unusable or implausible inputs fall back to 1", () => {
  assert.strictEqual(deriveScale(null, { width: 1280 }), 1);
  assert.strictEqual(deriveScale({ width: 0 }, { width: 1280 }), 1);
  assert.strictEqual(deriveScale({ width: NaN }, { width: 1280 }), 1);
  assert.strictEqual(deriveScale({ width: 1280 }, undefined), 1);
  assert.strictEqual(deriveScale({ width: 10 }, { width: 1280 }), 1); // 128x — refused
  assert.strictEqual(deriveScale({ width: 9000 }, { width: 1280 }), 1); // 0.14x — refused
});

test("scaleRect: multiplies every field, and is identity at scale 1", () => {
  const r = { x: 100, y: 50, width: 200, height: 400 };
  assert.deepStrictEqual(scaleRect(r, 1.5), { x: 150, y: 75, width: 300, height: 600 });
  assert.strictEqual(scaleRect(r, 1), r);
});

// ── boundsEqual ──

test("boundsEqual: identical vs differing rects", () => {
  const a = { x: 1, y: 2, width: 3, height: 4 };
  assert.ok(boundsEqual(a, { ...a }));
  assert.ok(!boundsEqual(a, { ...a, width: 9 }));
  assert.ok(!boundsEqual(a, null));
  assert.ok(boundsEqual(null, null));
});

// ── computeVisible ──

const B = { x: 0, y: 0, width: 100, height: 100 };

test("visible only when open, bounded, and no overlay is up", () => {
  assert.strictEqual(computeVisible({ open: true, overlayActive: false, bounds: B }), true);
  assert.strictEqual(computeVisible({ open: true, overlayActive: true, bounds: B }), false);
  assert.strictEqual(computeVisible({ open: false, overlayActive: false, bounds: B }), false);
  assert.strictEqual(computeVisible({ open: true, overlayActive: false, bounds: null }), false);
  assert.strictEqual(computeVisible(undefined), false);
});

test("an INACTIVE panel is hidden (its tab is not the visible one)", () => {
  assert.strictEqual(computeVisible({ open: true, inactive: true, bounds: B }), false);
  assert.strictEqual(computeVisible({ open: true, inactive: false, bounds: B }), true);
});

// ── manager ──

function harness(content = { width: 1280, height: 860 }) {
  const box = { content };
  // Ordered trace of every side effect, so tests can assert sequence (the
  // focus hand-back must land AFTER the hide/detach, not before).
  const log = [];
  const view = {
    bounds: [],
    visible: [],
    setBounds(b) { this.bounds.push(b); },
    setVisible(v) { this.visible.push(v); log.push(["setVisible", v]); },
    webContents: {
      loaded: [],
      closed: false,
      focused: false,
      handlers: {},
      openHandler: null,
      loadURL(u) { this.loaded.push(u); },
      close() { this.closed = true; log.push(["close"]); },
      isFocused() { return this.focused; },
      getTitle() { return "Example"; },
      setWindowOpenHandler(fn) { this.openHandler = fn; },
      on(name, fn) { this.handlers[name] = fn; },
    },
  };
  const attached = [];
  const events = [];
  const focusHost = [];
  const logLines = [];
  const mgr = createBrowserViewManager({
    createView: () => view,
    getContentBounds: () => box.content,
    addView: (v) => attached.push(v),
    removeView: () => { attached.pop(); log.push(["removeView"]); },
    onEvent: (n, p) => events.push([n, p]),
    focusHost: () => { focusHost.push(Date.now()); log.push(["focusHost"]); },
    log: (m) => logLines.push(m),
  });
  return { mgr, view, attached, events, box, focusHost, log, logLines };
}

test("manager: view is created lazily on first open", () => {
  const h = harness();
  assert.strictEqual(h.mgr._view(), null);
  assert.strictEqual(h.attached.length, 0);
  h.mgr.open("example.com");
  assert.ok(h.mgr._view());
  assert.strictEqual(h.attached.length, 1);
  assert.deepStrictEqual(h.view.webContents.loaded, ["https://example.com/"]);
});

test("manager: bounds apply only once the panel rect is known", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  // No rect reported yet -> nothing drawable, so not visible.
  assert.strictEqual(h.mgr.getState().visible, false);
  h.mgr.setPanelBounds({ x: 820, y: 60, width: 460, height: 800 });
  assert.strictEqual(h.mgr.getState().visible, true);
  assert.deepStrictEqual(h.view.bounds.at(-1), { x: 820, y: 60, width: 460, height: 800 });
});

test("manager: repeated identical bounds do not re-apply (resize churn)", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  const rect = { x: 820, y: 60, width: 460, height: 800 };
  h.mgr.setPanelBounds(rect);
  h.mgr.setPanelBounds({ ...rect });
  h.mgr.setPanelBounds({ ...rect });
  assert.strictEqual(h.view.bounds.length, 1);
});

test("manager: an overlay hides the native view, and clearing it restores", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds({ x: 0, y: 0, width: 400, height: 400 });
  h.mgr.setOverlayActive(true);
  assert.strictEqual(h.mgr.getState().visible, false);
  assert.strictEqual(h.view.visible.at(-1), false);
  h.mgr.setOverlayActive(false);
  assert.strictEqual(h.mgr.getState().visible, true);
  assert.strictEqual(h.view.visible.at(-1), true);
});

test("manager: navigate refuses a disallowed URL without touching the page", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  const before = h.view.webContents.loaded.length;
  const res = h.mgr.navigate("file:///etc/passwd");
  assert.strictEqual(res.refused, true);
  assert.strictEqual(h.view.webContents.loaded.length, before);
  assert.strictEqual(h.mgr.getState().url, "https://example.com/");
});

test("manager: refreshBounds re-clamps when the window shrinks", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds({ x: 820, y: 60, width: 460, height: 800 });
  h.box.content = { width: 900, height: 500 };
  h.mgr.refreshBounds();
  assert.deepStrictEqual(h.view.bounds.at(-1), { x: 820, y: 60, width: 80, height: 440 });
});

test("manager: a CSS-px rect from a zoomed dashboard is scaled to DIP", () => {
  const h = harness({ width: 1280, height: 860 });
  h.mgr.open("https://example.com/");
  // Dashboard at zoom 1.25: the same content area reports as 1024x688 CSS px,
  // and the panel measures 368 CSS px wide (= 460 DIP).
  h.mgr.setPanelBounds(
    { x: 656, y: 48, width: 368, height: 640 },
    { width: 1024, height: 688 }
  );
  assert.deepStrictEqual(h.view.bounds.at(-1), { x: 820, y: 60, width: 460, height: 800 });
});

test("manager: bounds re-derive from the last report when zoom changes", () => {
  const h = harness({ width: 1280, height: 860 });
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds({ x: 820, y: 60, width: 460, height: 800 }, { width: 1280, height: 860 });
  assert.deepStrictEqual(h.view.bounds.at(-1), { x: 820, y: 60, width: 460, height: 800 });
  // Same rect, but the report is stale after the user zooms — refreshBounds
  // re-derives rather than reusing the previously applied DIP bounds.
  h.mgr.setPanelBounds({ x: 656, y: 48, width: 368, height: 640 }, { width: 1024, height: 688 });
  assert.deepStrictEqual(h.view.bounds.at(-1), { x: 820, y: 60, width: 460, height: 800 });
});

test("manager: setInactive hides the view but KEEPS the page alive", () => {
  // Regression: the renderer used to close() when its tab went inactive, which
  // destroyed the WebContents and lost unsaved form input / scroll / history.
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds({ x: 0, y: 0, width: 400, height: 400 });
  assert.strictEqual(h.mgr.getState().visible, true);

  h.mgr.setInactive(true);
  assert.strictEqual(h.mgr.getState().visible, false, "hidden while inactive");
  assert.strictEqual(h.mgr.getState().open, true, "still open");
  assert.ok(h.mgr._view(), "view NOT destroyed");
  assert.strictEqual(h.view.webContents.closed, false, "webContents NOT closed");

  h.mgr.setInactive(false);
  assert.strictEqual(h.mgr.getState().visible, true, "restored on reactivate");
});

test("manager: close releases the view and resets state", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds({ x: 0, y: 0, width: 400, height: 400 });
  h.mgr.close();
  assert.strictEqual(h.mgr._view(), null);
  assert.strictEqual(h.view.webContents.closed, true);
  assert.strictEqual(h.mgr.getState().open, false);
  assert.strictEqual(h.mgr.getState().visible, false);
});

test("manager: page-driven popups are denied and reported to the host", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  const decision = h.view.webContents.openHandler({ url: "https://popup.example/x" });
  assert.deepStrictEqual(decision, { action: "deny" });
  assert.deepStrictEqual(h.events.at(-1), ["open-external", { url: "https://popup.example/x" }]);
});

test("manager: non-web navigation attempts are vetoed", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  let prevented = false;
  h.view.webContents.handlers["will-navigate"]({ preventDefault: () => { prevented = true; } }, "file:///etc/passwd");
  assert.strictEqual(prevented, true);

  prevented = false;
  h.view.webContents.handlers["will-navigate"]({ preventDefault: () => { prevented = true; } }, "https://ok.example/");
  assert.strictEqual(prevented, false);
});

// ── untrusted registration (permission gating) ──

test("the embedded view's webContents is registered as untrusted", () => {
  const h = harness();
  assert.strictEqual(isUntrustedContents(h.view.webContents), false, "not before creation");
  h.mgr.open("https://example.com/");
  assert.strictEqual(isUntrustedContents(h.view.webContents), true, "registered on creation");
});

test("isUntrustedContents is false for anything else (the dashboard view)", () => {
  assert.strictEqual(isUntrustedContents({ getURL: () => "http://localhost:6777/" }), false);
  assert.strictEqual(isUntrustedContents(null), false);
  assert.strictEqual(isUntrustedContents(undefined), false);
});

test("onCreate fires once with the view so the host can attach chrome", () => {
  const created = [];
  const view = {
    setBounds() {}, setVisible() {},
    webContents: { loadURL() {}, close() {}, getTitle: () => "", setWindowOpenHandler() {}, on() {} },
  };
  const mgr = createBrowserViewManager({
    createView: () => view,
    getContentBounds: () => ({ width: 1280, height: 860 }),
    addView: () => {},
    removeView: () => {},
    onCreate: (v) => created.push(v),
  });
  mgr.open("https://example.com/");
  mgr.navigate("https://example.com/other");
  assert.strictEqual(created.length, 1, "only on creation, not per navigation");
  assert.strictEqual(created[0], view);
});

// ── keyboard focus hand-back ──
//
// A BaseWindow routes keystrokes to exactly one child view. Hiding or detaching
// the embedded view while it holds focus leaves every dashboard text input
// deaf (pointer events still hit-test onto the dashboard, so it LOOKS alive).
// Every path that takes the view off screen must hand focus to the host when,
// and only when, the view held it.

const RECT = { x: 820, y: 60, width: 460, height: 800 };

/** An open, visible panel whose page holds keyboard focus (the user clicked
 *  or typed in it). */
function focusedPanel() {
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds(RECT);
  assert.strictEqual(h.mgr.getState().visible, true, "precondition: visible");
  h.view.webContents.focused = true;
  h.log.length = 0;
  return h;
}

test("focus: viewHoldsFocus answers only a real boolean true from isFocused", () => {
  assert.strictEqual(viewHoldsFocus(null), false);
  assert.strictEqual(viewHoldsFocus({}), false, "no webContents");
  assert.strictEqual(viewHoldsFocus({ webContents: {} }), false, "no isFocused (older Electron)");
  assert.strictEqual(viewHoldsFocus({ webContents: { isFocused: () => { throw new Error("destroyed"); } } }), false);
  assert.strictEqual(viewHoldsFocus({ webContents: { isFocused: () => 1 } }), false, "truthy but not boolean");
  assert.strictEqual(viewHoldsFocus({ webContents: { isFocused: () => false } }), false);
  assert.strictEqual(viewHoldsFocus({ webContents: { isFocused: () => true } }), true);
});

test("focus: an overlay hiding a focused view hands focus to the host, after the hide", () => {
  const h = focusedPanel();
  h.mgr.setOverlayActive(true);
  assert.strictEqual(h.mgr.getState().visible, false);
  assert.strictEqual(h.focusHost.length, 1, "handed back exactly once");
  assert.deepStrictEqual(h.log, [["setVisible", false], ["focusHost"]], "hide first, then hand back");
});

test("focus: the tab going inactive with a focused view hands focus to the host", () => {
  const h = focusedPanel();
  h.mgr.setInactive(true);
  assert.strictEqual(h.mgr.getState().visible, false);
  assert.strictEqual(h.mgr.getState().open, true, "page kept alive");
  assert.strictEqual(h.focusHost.length, 1);
  assert.deepStrictEqual(h.log, [["setVisible", false], ["focusHost"]]);
});

test("focus: the panel collapsing (no drawable rect) with a focused view hands focus back", () => {
  const h = focusedPanel();
  h.mgr.setPanelBounds({ x: 820, y: 60, width: 0, height: 800 });
  assert.strictEqual(h.mgr.getState().visible, false);
  assert.strictEqual(h.focusHost.length, 1);
});

test("focus: a window resize that leaves no drawable rect hands focus back", () => {
  const h = focusedPanel();
  h.box.content = { width: 800, height: 600 }; // the panel's x=820 is now off-window
  h.mgr.refreshBounds();
  assert.strictEqual(h.mgr.getState().visible, false);
  assert.strictEqual(h.focusHost.length, 1);
});

test("focus: close() with a focused view hands focus back, after detach and teardown", () => {
  const h = focusedPanel();
  h.mgr.close();
  assert.strictEqual(h.mgr._view(), null);
  assert.strictEqual(h.focusHost.length, 1);
  assert.deepStrictEqual(h.log, [["removeView"], ["close"], ["focusHost"]]);
});

test("focus: on an Electron without setVisible, the detach path hands focus back too", () => {
  const h = focusedPanel();
  delete h.view.setVisible;
  h.mgr.setOverlayActive(true);
  assert.strictEqual(h.attached.length, 0, "detached");
  assert.deepStrictEqual(h.log, [["removeView"], ["focusHost"]]);
});

test("focus: NOT handed back when the view did not hold focus", () => {
  // Focusing the host when the page did not have focus could yank focus off a
  // modal prompt window the user is typing into.
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds(RECT);
  assert.strictEqual(h.view.webContents.focused, false, "precondition: unfocused");
  h.mgr.setOverlayActive(true);
  h.mgr.setOverlayActive(false);
  h.mgr.setInactive(true);
  h.mgr.setInactive(false);
  h.mgr.setPanelBounds({ x: 0, y: 0, width: 0, height: 0 });
  h.mgr.setPanelBounds(RECT);
  h.mgr.close();
  assert.strictEqual(h.focusHost.length, 0);
});

test("focus: NOT handed back while the view stays visible", () => {
  const h = focusedPanel();
  h.mgr.setPanelBounds({ ...RECT, width: 400 }); // bounds churn, still visible
  h.mgr.setOverlayActive(false); // already clear
  h.mgr.setInactive(false); // already active
  h.mgr.refreshBounds();
  h.mgr.navigate("https://example.com/other");
  assert.strictEqual(h.mgr.getState().visible, true);
  assert.strictEqual(h.focusHost.length, 0, "the user is in the page; leave focus there");
});

test("focus: NOT handed back on first open before any rect is known", () => {
  // A freshly created view is hidden until the renderer reports a rect; it
  // cannot have been clicked yet, and isFocused says so.
  const h = harness();
  h.mgr.open("https://example.com/");
  assert.strictEqual(h.mgr.getState().visible, false);
  assert.strictEqual(h.focusHost.length, 0);
});

test("focus: a view whose webContents lacks isFocused never triggers a hand-back and never throws", () => {
  const h = harness();
  delete h.view.webContents.isFocused;
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds(RECT);
  assert.doesNotThrow(() => {
    h.mgr.setOverlayActive(true);
    h.mgr.setInactive(true);
    h.mgr.close();
  });
  assert.strictEqual(h.focusHost.length, 0);
});

test("focus: a hand-back that throws does not undo the hide", () => {
  const h = focusedPanel();
  const view = h.view;
  const mgr = createBrowserViewManager({
    createView: () => view,
    getContentBounds: () => h.box.content,
    addView: () => {},
    removeView: () => {},
    focusHost: () => { throw new Error("host tearing down"); },
  });
  mgr.open("https://example.com/");
  mgr.setPanelBounds(RECT);
  assert.doesNotThrow(() => mgr.setOverlayActive(true));
  assert.strictEqual(mgr.getState().visible, false);
  assert.strictEqual(view.visible.at(-1), false, "the view is hidden regardless");
  assert.doesNotThrow(() => mgr.close());
  assert.strictEqual(mgr._view(), null);
});

test("focus: the hand-back is asked on EVERY hide, so a view found hidden-yet-focused later is healed", () => {
  // The platform may hand focus back to a hidden view on its own (window
  // re-activation); the next bounds report while still hidden must heal it
  // rather than only reacting on the visible→hidden edge.
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds(RECT);
  h.mgr.setInactive(true); // hidden, unfocused: no hand-back
  assert.strictEqual(h.focusHost.length, 0);
  h.view.webContents.focused = true; // focus landed on the hidden view
  h.mgr.setPanelBounds({ ...RECT, width: 420 }); // any later report while hidden
  assert.strictEqual(h.focusHost.length, 1);
});

test("focus: the hand-back also runs when the hide order is inactive → overlay (still one per hide)", () => {
  const h = focusedPanel();
  h.mgr.setInactive(true);
  assert.strictEqual(h.focusHost.length, 1);
  h.view.webContents.focused = false; // the host took focus as asked
  h.mgr.setOverlayActive(true); // a second reason to stay hidden
  assert.strictEqual(h.focusHost.length, 1, "nothing to reclaim any more");
});

// reclaimFocus(): the window-activation belt-and-braces.

test("focus: reclaimFocus heals a hidden view that holds focus and reports it", () => {
  const h = focusedPanel();
  h.view.webContents.focused = false;
  h.mgr.setOverlayActive(true); // hidden, unfocused at this point
  assert.strictEqual(h.focusHost.length, 0);
  h.view.webContents.focused = true; // re-activation picked the hidden view
  assert.strictEqual(h.mgr.reclaimFocus(), true);
  assert.strictEqual(h.focusHost.length, 1);
});

test("focus: reclaimFocus leaves a VISIBLE focused view alone", () => {
  const h = focusedPanel();
  assert.strictEqual(h.mgr.reclaimFocus(), false);
  assert.strictEqual(h.focusHost.length, 0, "the user is in the page");
});

test("focus: reclaimFocus leaves a hidden UNFOCUSED view alone", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds(RECT);
  h.mgr.setInactive(true);
  assert.strictEqual(h.mgr.reclaimFocus(), false);
  assert.strictEqual(h.focusHost.length, 0);
});

test("focus: reclaimFocus is a no-op with no view (never opened, or closed)", () => {
  const h = harness();
  assert.strictEqual(h.mgr.reclaimFocus(), false);
  h.mgr.open("https://example.com/");
  h.mgr.close();
  h.focusHost.length = 0;
  assert.strictEqual(h.mgr.reclaimFocus(), false);
  assert.strictEqual(h.focusHost.length, 0);
});

test("focus: a manager built without focusHost still hides and closes", () => {
  const view = {
    visible: [],
    setBounds() {}, setVisible(v) { this.visible.push(v); },
    webContents: {
      loadURL() {}, close() {}, getTitle: () => "", setWindowOpenHandler() {}, on() {},
      isFocused: () => true,
    },
  };
  const mgr = createBrowserViewManager({
    createView: () => view,
    getContentBounds: () => ({ width: 1280, height: 860 }),
    addView: () => {},
    removeView: () => {},
  });
  mgr.open("https://example.com/");
  mgr.setPanelBounds(RECT);
  assert.doesNotThrow(() => mgr.setOverlayActive(true));
  assert.strictEqual(view.visible.at(-1), false);
  assert.doesNotThrow(() => mgr.close());
});

// Focus arriving on a view that is ALREADY hidden: no hide follows, so the
// view's own focus event (and a finished load) must trigger the hand-back.

/** Fire one of the embedded webContents' registered events. */
function fire(h, name, ...args) {
  const fn = h.view.webContents.handlers[name];
  assert.strictEqual(typeof fn, "function", `${name} handler registered`);
  fn(...args);
}

/** The focus hand-back is deferred one tick (setImmediate); let it run. */
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("focus: a HIDDEN view gaining focus hands it back to the host", async () => {
  const h = harness();
  h.mgr.open("https://example.com/"); // no rect yet: added hidden (agent bootstrap shape)
  assert.strictEqual(h.mgr.getState().visible, false);
  h.view.webContents.focused = true;
  fire(h, "focus");
  // Not from inside the event: in a real Electron window the native focus
  // change still in progress overrides a synchronous hand-back.
  assert.strictEqual(h.focusHost.length, 0, "deferred past the focus event");
  await tick();
  assert.strictEqual(h.focusHost.length, 1);
});

test("focus: a view hidden by an inactive tab gaining focus hands it back, even when isFocused reads false", async () => {
  // The event itself is the signal; isFocused can lag on a hidden view.
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds(RECT);
  h.mgr.setInactive(true);
  assert.strictEqual(h.focusHost.length, 0);
  fire(h, "focus");
  await tick();
  assert.strictEqual(h.focusHost.length, 1);
});

test("focus: a VISIBLE view gaining focus keeps it (unchanged behaviour)", async () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds(RECT);
  h.view.webContents.focused = true;
  fire(h, "focus");
  await tick();
  fire(h, "did-stop-loading");
  assert.strictEqual(h.mgr.getState().visible, true);
  assert.strictEqual(h.focusHost.length, 0, "the user is in the page");
  assert.deepStrictEqual(h.logLines, []);
});

test("focus: a load finishing in a hidden view hands focus back only when the view holds it", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  fire(h, "did-stop-loading");
  assert.strictEqual(h.focusHost.length, 0, "unfocused: left alone");
  h.view.webContents.focused = true;
  fire(h, "did-stop-loading");
  assert.strictEqual(h.focusHost.length, 1);
});

test("focus: events from a released view never hand focus back", async () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.close();
  h.view.webContents.focused = true;
  fire(h, "focus");
  await tick();
  fire(h, "did-stop-loading");
  assert.strictEqual(h.focusHost.length, 0);
});

test("focus: a hand-back logs one line naming the focus owner and trigger", async () => {
  const h = harness();
  h.mgr.open("https://example.com/secret-path?q=private");
  fire(h, "focus");
  await tick();
  assert.strictEqual(h.logLines.length, 1);
  assert.match(h.logLines[0], /embedded browser view while off screen/);
  assert.match(h.logLines[0], /dashboard view/);
  assert.match(h.logLines[0], /trigger=hidden-view-focus/);
  assert.doesNotMatch(h.logLines[0], /example\.com|secret|private|Example/, "no page URL or title");
});

test("focus: every hand-back path logs its own trigger", () => {
  const hide = focusedPanel();
  hide.mgr.setOverlayActive(true);
  assert.deepStrictEqual(hide.logLines, [focusHandBackMessage("hide")]);

  const close = focusedPanel();
  close.mgr.close();
  assert.deepStrictEqual(close.logLines, [focusHandBackMessage("close")]);

  const win = focusedPanel();
  win.view.webContents.focused = false;
  win.mgr.setInactive(true);
  win.view.webContents.focused = true;
  win.mgr.reclaimFocus();
  assert.deepStrictEqual(win.logLines, [focusHandBackMessage("window-focus")]);

  const load = harness();
  load.mgr.open("https://example.com/");
  load.view.webContents.focused = true;
  fire(load, "did-stop-loading");
  assert.deepStrictEqual(load.logLines, [focusHandBackMessage("hidden-view-load")]);
});

test("focus: no log line when nothing is handed back", () => {
  const h = harness();
  h.mgr.open("https://example.com/");
  h.mgr.setPanelBounds(RECT);
  h.mgr.setOverlayActive(true);
  h.mgr.close();
  assert.strictEqual(h.focusHost.length, 0);
  assert.deepStrictEqual(h.logLines, []);
});

test("focus: focusHandBackMessage never echoes an unknown trigger", () => {
  assert.match(focusHandBackMessage("https://example.com/x"), /trigger=unknown\)$/);
  assert.match(focusHandBackMessage(undefined), /trigger=unknown\)$/);
});

test("focus: a log sink that throws does not stop the hand-back", async () => {
  const h = harness();
  const view = h.view;
  const handed = [];
  const mgr = createBrowserViewManager({
    createView: () => view,
    getContentBounds: () => h.box.content,
    addView: () => {},
    removeView: () => {},
    focusHost: () => handed.push(1),
    log: () => { throw new Error("log closed"); },
  });
  mgr.open("https://example.com/");
  assert.doesNotThrow(() => fire(h, "focus"));
  await tick();
  assert.strictEqual(handed.length, 1);
});

test("focus: a hidden view gaining focus while the host window is NOT active is left for reclaimFocus", async () => {
  // Focusing the host from the background could bring the window forward.
  const h = harness();
  const view = h.view;
  const handed = [];
  const active = { value: false };
  const mgr = createBrowserViewManager({
    createView: () => view,
    getContentBounds: () => h.box.content,
    addView: () => {},
    removeView: () => {},
    focusHost: () => handed.push(1),
    hostActive: () => active.value,
  });
  mgr.open("https://example.com/");
  view.webContents.focused = true;
  fire(h, "focus");
  await tick();
  fire(h, "did-stop-loading");
  assert.strictEqual(handed.length, 0, "background window: no hand-back");
  active.value = true; // the user comes back: the window-focus heal runs
  assert.strictEqual(mgr.reclaimFocus(), true);
  assert.strictEqual(handed.length, 1);
});

test("focus: a hostActive probe that throws reads as inactive", async () => {
  const h = harness();
  const view = h.view;
  const handed = [];
  const mgr = createBrowserViewManager({
    createView: () => view,
    getContentBounds: () => h.box.content,
    addView: () => {},
    removeView: () => {},
    focusHost: () => handed.push(1),
    hostActive: () => { throw new Error("window destroyed"); },
  });
  mgr.open("https://example.com/");
  assert.doesNotThrow(() => fire(h, "focus"));
  await tick();
  assert.strictEqual(handed.length, 0);
});

test("focus: events from a previous view's webContents are ignored after a reopen", async () => {
  const make = () => {
    const wc = {
      handlers: {}, focused: false,
      loadURL() {}, close() {}, isFocused() { return this.focused; },
      getTitle: () => "", setWindowOpenHandler() {}, on(n, fn) { this.handlers[n] = fn; },
    };
    return { setBounds() {}, setVisible() {}, webContents: wc };
  };
  const views = [make(), make()];
  let next = 0;
  const handed = [];
  const mgr = createBrowserViewManager({
    createView: () => views[next++],
    getContentBounds: () => ({ width: 1280, height: 860 }),
    addView: () => {},
    removeView: () => {},
    focusHost: () => handed.push(1),
  });
  mgr.open("https://example.com/");
  mgr.close();
  mgr.open("https://example.com/"); // a fresh, hidden view
  assert.strictEqual(mgr._view(), views[1]);
  assert.strictEqual(handed.length, 0, "precondition: nothing handed back yet");
  views[0].webContents.focused = true;
  views[0].webContents.handlers.focus();
  await tick();
  views[0].webContents.handlers["did-stop-loading"]();
  assert.strictEqual(handed.length, 0, "the released view is never acted on");
  views[1].webContents.handlers.focus();
  await tick();
  assert.strictEqual(handed.length, 1, "the live view still is");
});
