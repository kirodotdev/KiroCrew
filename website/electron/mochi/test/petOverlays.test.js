const { test } = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");
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

// The overlay covers a whole display and, when its hitbox says so, receives
// real clicks. A click on a NORMAL window activates the app, and the shell's
// app.on("activate") re-shows a deliberately-hidden dashboard -- so petting the
// cat would resurface the whole app. NSWindowStyleMaskNonactivatingPanel
// (type: "panel") is the fix, and dropping it regresses that SILENTLY, so the
// shipped BrowserWindow option is what this pins.
function stubElectronForOpen() {
  const created = [];
  class FakeWebContents {
    on() {}
    send() {}
    isDestroyed() { return false; }
    isLoading() { return true; }
    setWindowOpenHandler() {}
  }
  class FakeWindow {
    constructor(opts) {
      this.opts = opts;
      this.webContents = new FakeWebContents();
      created.push(this);
    }
    setFocusable() {}
    setAcceptFirstMouse() {}
    setIgnoreMouseEvents() {}
    setVisibleOnAllWorkspaces() {}
    // #3125 started calling setContentProtection to keep the overlay out of
    // screen captures, but this double was not taught the method, so every
    // createOverlayForDisplay threw "win.setContentProtection is not a
    // function" and Electron Shell Tests went red on main. Recording the
    // argument rather than swallowing it, so the behaviour #3125 shipped is
    // actually asserted instead of merely not crashing.
    setContentProtection(on) { this.contentProtection = on; }
    setAlwaysOnTop() {}
    loadURL() {}
    isVisible() { return false; }
    isDestroyed() { return false; }
    showInactive() {}
    close() {}
    on() {}
  }
  const DISPLAY = { id: 1, bounds: { x: 0, y: 0, width: 1440, height: 900 } };
  return {
    created,
    electron: {
      app: { on() {}, setActivationPolicy() {}, dock: { show() {} } },
      BrowserWindow: FakeWindow,
      ipcMain: { on() {}, handle() {} },
      shell: { openExternal() {}, showItemInFolder() {} },
      screen: {
        getPrimaryDisplay: () => DISPLAY,
        getAllDisplays: () => [DISPLAY],
        getCursorScreenPoint: () => ({ x: 0, y: 0 }),
        on() {},
      },
    },
  };
}

function loadPetOverlays() {
  const stub = stubElectronForOpen();
  const modPath = path.join(__dirname, "..", "petOverlays.js");
  const panelPath = path.join(__dirname, "..", "panelWindow.js");
  delete require.cache[require.resolve(modPath)];
  delete require.cache[require.resolve(panelPath)]; // bindIpc requires it fresh
  const origLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "electron") return stub.electron;
    return origLoad(request, parent, isMain);
  };
  try {
    // petOverlays requires ./panelWindow LAZILY (inside openPetWindow). Load it
    // now, while the stub is active, so it caches with the fake electron rather
    // than resolving the real one after the override is torn down.
    require(panelPath);
    return { mod: require(modPath), ...stub };
  } finally {
    Module._load = origLoad;
  }
}

test("the pet overlay is a non-activating panel on macOS only", () => {
  const { mod, created } = loadPetOverlays();
  try {
    mod.openPetWindow("http://localhost:6777", "tok");
    assert.strictEqual(created.length, 1, "one overlay for the single display");
    // #3125: the overlay must be excluded from screen captures on every
    // platform, otherwise a region capture bakes the pet into the image and the
    // macOS screenshot picker offers the overlay instead of the app underneath.
    assert.strictEqual(created[0].contentProtection, true, "content protection on");
    const { type } = created[0].opts;
    if (process.platform === "darwin") {
      assert.strictEqual(type, "panel");
    } else {
      // "panel" is not a legal `type` off macOS; the option must be omitted.
      assert.strictEqual(type, undefined);
    }
  } finally {
    mod.closePetWindow();
  }
});

// ── Overlay error-page recovery ────────────────────────────────────────────
// A pet overlay covers a whole frameless, click-through display, so a gateway
// error page (401/403/4xx/5xx) rendered in it would trap the user behind an
// uncloseable full-screen page (the reported "403 after sleep" bug). An error
// page is a COMPLETED navigation, so the status code is the only signal; the
// handler only hides+latches, and recovery is owned by the host's reconcile tick.
const fs = require("node:fs");
const {
  _isOverlayErrorPage,
  _handleOverlayNavigation,
  hasBlankedOverlay,
  rearmBlankedOverlays,
  _registerOverlay,
  _getOverlays,
} = require("../petOverlays");

// A fake overlay window: identity for the WeakSet, plus the methods the handler
// and re-arm touch. Registered in the real overlays map so hasBlankedOverlay /
// rearmBlankedOverlays see it, then removed in a finally.
function fakeWin() {
  return {
    destroyed: false,
    hidden: false,
    loadedUrl: null,
    loadOptions: null,
    isDestroyed() { return this.destroyed; },
    hide() { this.hidden = true; },
    showInactive() { this.hidden = false; },
    isVisible() { return !this.hidden; },
    loadURL(u, options) { this.loadedUrl = u; this.loadOptions = options; },
    on() {},
  };
}

test("any >=400 status is an error page; success and redirects are not", () => {
  // The reported harm is "an opaque page blankets the display" — a 404/500 body
  // traps the user identically to a 401/403, so hiding must cover all of them.
  for (const code of [400, 401, 403, 404, 500, 503]) {
    assert.equal(_isOverlayErrorPage(code), true, `${code} is an error page`);
  }
  for (const code of [200, 301, 304, undefined, null, "500"]) {
    assert.equal(_isOverlayErrorPage(code), false, `${code} is not an error page`);
  }
});

test("handleOverlayNavigation hides+latches an error page and clears on a good load", () => {
  const DID = 99311;
  const win = fakeWin();
  _registerOverlay(DID, win);
  try {
    _handleOverlayNavigation(win, 403);
    assert.equal(win.hidden, true, "an error page must hide the overlay");
    assert.equal(hasBlankedOverlay(), true, "the overlay is latched as blanked");

    _handleOverlayNavigation(win, 200); // a healed reload lands on 200
    assert.equal(hasBlankedOverlay(), false, "a good load clears the latch");

    _handleOverlayNavigation(win, 500); // a 5xx also latches, not only auth codes
    assert.equal(hasBlankedOverlay(), true, "a 5xx error page also latches");

    win.destroyed = true; // a destroyed window is a no-op, never throws
    assert.doesNotThrow(() => _handleOverlayNavigation(win, 200));
  } finally {
    _getOverlays().delete(DID);
  }
});

test("rearmBlankedOverlays reloads only blanked windows with a minted query token", () => {
  const BLANK = 99312;
  const OK = 99313;
  const blanked = fakeWin();
  const healthy = fakeWin();
  _registerOverlay(BLANK, blanked);
  _registerOverlay(OK, healthy);
  try {
    _handleOverlayNavigation(blanked, 403); // latch the blanked one; healthy never errored
    rearmBlankedOverlays("http://localhost:5476", "tok123");
    assert.match(blanked.loadedUrl || "", /token=tok123/, "blanked overlay reloads with the host's token");
    assert.equal(blanked.loadOptions, undefined, "a minted token needs no extra request headers");
    assert.equal(healthy.loadedUrl, null, "a non-blanked overlay is left untouched");
  } finally {
    _getOverlays().delete(BLANK);
    _getOverlays().delete(OK);
  }
});

test("rearmBlankedOverlays reloads a borrowed session through the default session", () => {
  const DID = 99315;
  const win = fakeWin();
  _registerOverlay(DID, win);
  try {
    _handleOverlayNavigation(win, 403);
    rearmBlankedOverlays("http://localhost:5476", "borrowed-session", true);
    assert.doesNotMatch(win.loadedUrl || "", /[?&]token=/, "a borrowed session must not be stringified into a query token");
    assert.equal(win.loadOptions, undefined, "the BrowserWindow default session supplies the borrowed cookie");
  } finally {
    _getOverlays().delete(DID);
  }
});

test("rearmBlankedOverlays refuses to reload with an empty/missing token", () => {
  const DID = 99314;
  const win = fakeWin();
  _registerOverlay(DID, win);
  try {
    _handleOverlayNavigation(win, 403);
    rearmBlankedOverlays("http://localhost:5476", ""); // no usable credential
    assert.equal(win.loadedUrl, null, "an empty token must not trigger a reload (it would 403 again)");
    rearmBlankedOverlays("", "tok"); // no base url
    assert.equal(win.loadedUrl, null, "a missing base url must not trigger a reload");
  } finally {
    _getOverlays().delete(DID);
  }
});

// Source guards for invariants that cannot be exercised without Electron.
const fsSrc = fs.readFileSync(path.join(__dirname, "..", "petOverlays.js"), "utf8");
const idxSrc = fs.readFileSync(path.join(__dirname, "..", "index.js"), "utf8");

test("did-navigate delegates to the single navigation handler (no inline retry path)", () => {
  assert.match(fsSrc, /did-navigate/, "must hook did-navigate (an error page is not a did-fail-load)");
  assert.match(fsSrc, /handleOverlayNavigation\(win, httpResponseCode\)/,
    "did-navigate must delegate to handleOverlayNavigation");
  // The inline fast-retry machinery is gone (Fable: one reconcile-owned path).
  for (const gone of ["setPetReauthProvider", "fastReauthOverlay", "reloadOverlayForCurrentTarget", "overlayReauthAttempts", "isRegisteredOverlay"]) {
    assert.ok(!fsSrc.includes(gone), `${gone} must be removed (single reconcile-owned recovery path)`);
  }
  assert.ok(!idxSrc.includes("setPetReauthProvider"), "index.js must not wire a reauth provider anymore");
});

test("every showInactive reveal is guarded by the blanked latch", () => {
  const reveals = fsSrc.match(/win\.showInactive\(\)/g) || [];
  const guarded = fsSrc.match(/!overlayBlanked\.has\(win\)[\s\S]{0,80}win\.showInactive\(\)/g) || [];
  assert.ok(reveals.length >= 3, "expected the three overlay reveal sites (handshake, showPetWindow, transfer)");
  assert.equal(guarded.length, reveals.length, "every showInactive reveal must be latch-guarded");
});

test("reconcile re-arms with scalar credential values and delivery mode", () => {
  // The host passes the credential it ALREADY resolved this tick; for self it
  // preserves the probe-maintained delivery mode (including a borrowed-cookie
  // credential), only when something is actually blanked — and NEVER resolves
  // repeatedly here, or a persistent non-auth error would churn session tokens.
  const callAt = idxSrc.indexOf("rearmBlankedOverlays(mochiPetBaseUrl");
  assert.ok(callAt >= 0, "reconcile must call rearmBlankedOverlays with resolved auth");
  const region = idxSrc.slice(idxSrc.indexOf("if (hasBlankedOverlay())"), callAt + 90);
  assert.match(region, /hasBlankedOverlay\(\)/, "only re-arm when an overlay is actually blanked");
  assert.match(region, /SELF_INSTANCE/, "self resolves its gateway credential");
  assert.match(region, /const auth = await gatewayToken\(\)/, "self obtains the gateway auth record once");
  assert.match(region, /rearmToken = auth\.value/, "only the credential value reaches rearm");
  assert.match(region, /rearmViaCookie = auth\.viaCookie/, "rearm retains cookie delivery mode");
  assert.match(region, /rearmBlankedOverlays\(mochiPetBaseUrl, rearmToken, rearmViaCookie\)/,
    "rearm receives scalar token and delivery mode, never the auth record");
  assert.ok(!region.includes('cachedGatewayAuth = { value: "" }'),
    "must NOT clear the credential cache in the rearm path — probe-driven invalidation only, or a persistent non-auth error mints every tick");
});
