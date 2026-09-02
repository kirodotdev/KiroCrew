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
  const view = {
    bounds: [],
    visible: [],
    setBounds(b) { this.bounds.push(b); },
    setVisible(v) { this.visible.push(v); },
    webContents: {
      loaded: [],
      closed: false,
      handlers: {},
      openHandler: null,
      loadURL(u) { this.loaded.push(u); },
      close() { this.closed = true; },
      getTitle() { return "Example"; },
      setWindowOpenHandler(fn) { this.openHandler = fn; },
      on(name, fn) { this.handlers[name] = fn; },
    },
  };
  const attached = [];
  const events = [];
  const mgr = createBrowserViewManager({
    createView: () => view,
    getContentBounds: () => box.content,
    addView: (v) => attached.push(v),
    removeView: () => attached.pop(),
    onEvent: (n, p) => events.push([n, p]),
  });
  return { mgr, view, attached, events, box };
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
