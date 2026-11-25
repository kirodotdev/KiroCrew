"use strict";

const { resolveThemeSource } = require("../../native-theme");
const { clampZoomFactor, stepZoomFactor } = require("../../zoom");
const { DEFAULT_TEXT_SIZE_PX, isSupportedTextSize } = require("../../text-size");
const { applyFocusModeChrome } = require("../../focus-chrome");
const { createFocusCursorWatch } = require("../../focus-cursor");
const {
  paintTitleBarOverlay,
  paintAllTitleBarOverlays,
} = require("../../windows-titlebar");

const HEADER_CSS_PX = 42;
// The one home for the content text size preference. Nothing else writes it.
const FONT_SIZE_STORE_KEY = "displayPreferences.fontSize";
const TRAFFIC_LIGHT_NATIVE_H = 12;
const TRAFFIC_LIGHT_Y_NUDGE = -4;

// The content reserve (CSS px) the Windows header must keep clear on its right
// for the native titleBarOverlay caption buttons: the 138px overlay plus 4px
// clearance. This is the single source of truth for the 142 figure; the
// `--mc-win-caption-reserve` default and fallbacks in index.css mirror it.
//
// The caption buttons are a FIXED native width (device-independent px), but a
// CSS-px padding is multiplied by page zoom when painted, so a static 142 CSS
// px shrinks to 142*zoom DIP on screen and stops clearing the buttons below
// 100% zoom (the bell slides under Minimize — the reported bug). The reserve in
// CSS px that always paints to 142 DIP is therefore 142/zoom, computed HERE
// where the zoom factor is authoritative (the same place the overlay height is
// scaled) and pushed to the renderer as a CSS variable — rather than derived in
// CSS from the Window Controls Overlay env() vars, whose presence inside a
// child WebContentsView is not something this app can verify.
const WIN_CAPTION_RESERVE_CSS_PX = 142;

// The width (CSS px) of the native caption-button overlay itself — the 138 half
// of the 142 reserve above, without the 4px clearance. The injected
// `#electron-drag-bar` clips its right edge to this so the drag region never
// covers the caption buttons (a drag rectangle over Close moves the window
// instead of closing it). Like the reserve, it is a CSS-px value that paints to
// width*zoom DIP, so a static 138 CSS px shrinks below the fixed-DIP buttons
// under 100% zoom and the drag strip creeps over them; the value that always
// paints to 138 DIP is 138/zoom, pushed from here as `--mc-win-drag-reserve`.
const WIN_CAPTION_OVERLAY_CSS_PX = 138;

/** Reserve in CSS px that paints to WIN_CAPTION_RESERVE_CSS_PX DIP at `zoom`. */
function winCaptionReserveForZoom(zoom) {
  const factor = Number.isFinite(zoom) && zoom > 0 ? zoom : 1;
  return Math.round(WIN_CAPTION_RESERVE_CSS_PX / factor);
}

/** Drag-strip inset in CSS px that paints to WIN_CAPTION_OVERLAY_CSS_PX DIP at `zoom`. */
function winDragReserveForZoom(zoom) {
  const factor = Number.isFinite(zoom) && zoom > 0 ? zoom : 1;
  return Math.round(WIN_CAPTION_OVERLAY_CSS_PX / factor);
}

function trafficLightPositionForZoom(zoomFactor) {
  const stripPx = Math.round(HEADER_CSS_PX * zoomFactor);
  return {
    x: Math.round(16 * zoomFactor),
    y: Math.max(
      4,
      Math.round((stripPx - TRAFFIC_LIGHT_NATIVE_H) / 2) + TRAFFIC_LIGHT_Y_NUDGE,
    ),
  };
}

/**
 * The native chrome around every dashboard window: macOS traffic-light
 * placement and focus-mode visibility, the Windows title-bar overlay colours,
 * the process-wide native theme source, dashboard zoom, and the off-window
 * cursor watch a focus-mode reveal needs. Every value derives from the
 * dashboard's 42px header at the current zoom, so the native controls stay
 * centred in it.
 *
 * The facade owns window lookup (windowForWebContents) and injects it, so a
 * renderer request always resolves to its sender's own window.
 */
function createWindowChrome({
  BaseWindow,
  nativeTheme,
  screen,
  store,
  log: glog,
  isMac: IS_MAC,
  isWindows: IS_WINDOWS,
  windowForWebContents,
}) {
  // Reads the MODE PREFERENCE, not only the resolved mode. Setting themeSource
  // to dark/light also overrides prefers-color-scheme in renderers; feeding the
  // resolved value back would freeze the dashboard's Auto mode.
  function syncNativeTheme(view, win) {
    if (win.isDestroyed()) return;
    view.webContents.executeJavaScript(
      `JSON.stringify({`
        + `pref: document.documentElement.dataset.modePref || "",`
        + `mode: document.documentElement.dataset.mode || ""`
        + `})`,
    ).then((raw) => {
      let pref = "";
      let mode = "";
      try {
        const parsed = JSON.parse(raw);
        pref = parsed.pref || "";
        mode = parsed.mode || "";
      } catch {
        return;
      }
      nativeTheme.themeSource = resolveThemeSource(pref, mode);
      // No mode passed: the caption buttons keep what this window's dashboard
      // last pushed (setTitlebarMode), which can be a Remote Crew's lent mode.
      if (mode === "dark" || mode === "light") updateWindowsTitleBarOverlay(win);
    }).catch(() => {});
  }

  // Each window owns its caption mode: the one its dashboard last pushed, stored
  // on the window, so a zoom or focus repaint keeps it rather than reading the
  // process-wide native theme, which never holds a lent mode.
  function updateWindowsTitleBarOverlay(win, mode) {
    if (!IS_WINDOWS) return;
    const resolvedMode = mode
      || (win && win._mcCaptionMode)
      || (nativeTheme.shouldUseDarkColors ? "dark" : "light");
    paintTitleBarOverlay(win, resolvedMode, HEADER_CSS_PX);
    pushWinCaptionReserve(win);
  }

  // Set --mc-win-caption-reserve on the dashboard document from the main
  // process's own view of the zoom factor, so the header's right reserve paints
  // to a constant 142 DIP at every zoom (see winCaptionReserveForZoom). CSS
  // alone cannot do this — the native caption width is in DIPs while a CSS-px
  // padding is zoomed — and the authoritative zoom factor already lives here,
  // next to the overlay-height scaling. The CSS carries a 142px fallback for
  // the first paint before this runs and for non-Electron contexts.
  //
  // --mc-win-drag-reserve rides the same push: the injected #electron-drag-bar
  // clips its right edge to it so the drag strip stays off the caption buttons
  // at every zoom, the same fixed-DIP problem the content reserve has.
  function pushWinCaptionReserve(win) {
    if (!IS_WINDOWS || !win || win.isDestroyed() || !win._mcView) return;
    try {
      const wc = win._mcView.webContents;
      if (!wc || wc.isDestroyed()) return;
      const zoom = wc.getZoomFactor();
      const reserve = winCaptionReserveForZoom(zoom);
      const dragReserve = winDragReserveForZoom(zoom);
      wc.executeJavaScript(
        `document.documentElement.style.setProperty(`
          + `'--mc-win-caption-reserve', '${reserve}px');`
          + `document.documentElement.style.setProperty(`
          + `'--mc-win-drag-reserve', '${dragReserve}px')`,
      ).catch(() => {});
    } catch {
      // Window is mid-teardown.
    }
  }

  function positionTrafficLights(win) {
    if (!IS_MAC || !win || win.isDestroyed()) return;
    try {
      const zoom = win._mcView ? win._mcView.webContents.getZoomFactor() : 1;
      win.setWindowButtonPosition(trafficLightPositionForZoom(zoom));
    } catch {
      // Window is mid-teardown.
    }
  }

  // Chromium applies zoom per-origin, and the header strip scales with it, so
  // the native controls are re-placed after every zoom change.
  function trackZoomChrome(win, view) {
    if (IS_MAC) {
      positionTrafficLights(win);
      view.webContents.on("zoom-changed", () => {
        setTimeout(() => positionTrafficLights(win), 0);
      });
    }
    if (IS_WINDOWS) {
      updateWindowsTitleBarOverlay(win);
      view.webContents.on("zoom-changed", () => {
        setTimeout(() => updateWindowsTitleBarOverlay(win), 0);
      });
      // A reload replaces the document, wiping the inline --mc-win-caption-reserve
      // from <html>. did-finish-load re-fires on every load AND reload, so
      // re-push the reserve here — this is the ONLY re-push not gated on the
      // theme mode resolving (syncNativeTheme only repaints when data-mode is
      // dark/light), so the header's zoom-correct reserve survives a reload even
      // before the SPA has set its mode.
      view.webContents.on("did-finish-load", () => {
        setTimeout(() => pushWinCaptionReserve(win), 0);
      });
    }
  }

  function setThemeAccent(hex) {
    if (
      typeof hex === "string"
      && /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(hex)
    ) {
      store.set("themeAccent", hex);
    }
  }

  function handleFocusMode(sender, visible) {
    if (!IS_MAC) return;
    const win = windowForWebContents(sender);
    if (!win) return;
    // AppKit drops declared drag regions when button visibility mutates;
    // applyFocusModeChrome re-declares them after changing the native chrome.
    applyFocusModeChrome(win, visible, { positionTrafficLights });
  }

  // Off-window cursor distance for a focus-mode reveal. Every platform, unlike
  // handleFocusMode's macOS-only traffic lights: the renderer stops receiving
  // mouse events the moment the pointer crosses a window edge wherever it runs,
  // so the dismissal distance can only be measured here.
  const focusCursorWatch = createFocusCursorWatch({ screen, log: glog });

  function handleWatchFocusCursor(sender, watching) {
    const win = windowForWebContents(sender);
    if (!win) return;
    focusCursorWatch.watch(win, watching);
  }

  function setThemeMode(pref, sender) {
    // themeSource is process-wide and a focused dashboard window re-syncs it on
    // focus, so a background window's push must not re-pin the focused one.
    const focused = BaseWindow.getFocusedWindow();
    if (focused && focused._mcView && focused !== windowForWebContents(sender)) return;
    if (pref === "system" || pref === "dark" || pref === "light") {
      nativeTheme.themeSource = resolveThemeSource(pref, "");
    }
  }

  function setTitlebarMode(mode, sender) {
    if (!IS_WINDOWS) return;
    const resolvedMode = mode === "dark" || mode === "light"
      ? mode
      : (nativeTheme.shouldUseDarkColors ? "dark" : "light");
    // The sender's own window only: its dashboard may be painting a Remote Crew's
    // lent mode, which no other window shares.
    const own = sender ? windowForWebContents(sender) : null;
    if (own) {
      if (own.isDestroyed()) return;
      own._mcCaptionMode = resolvedMode;
      paintTitleBarOverlay(own, resolvedMode, HEADER_CSS_PX);
      return;
    }
    // Continue past framed modal windows that cannot accept an overlay; the
    // helper catches per-window so one dialog cannot strand siblings.
    paintAllTitleBarOverlays(
      BaseWindow.getAllWindows(),
      resolvedMode,
      HEADER_CSS_PX,
    );
  }

  // Re-centre the native caption controls in the 42px header after a zoom
  // change. Both insets derive from the zoom factor — the macOS traffic-light
  // position (trafficLightPositionForZoom) and the Windows overlay HEIGHT
  // (titleBarOverlayOptions scales it by zoom) — so a zoom step that updates
  // only one of them leaves the native buttons mis-centred on the other
  // platform. The two zoom entry points (the View-menu items in
  // window-lifecycle.js and the Settings "Zoom Level" stepper via applyZoom)
  // both reconcile through here so they can never drift apart again.
  function reconcileZoomChrome(win) {
    if (!win || !win._mcView) return;
    if (IS_MAC) positionTrafficLights(win);
    if (IS_WINDOWS) updateWindowsTitleBarOverlay(win);
  }

  function applyZoom(sender, factor) {
    sender.setZoomFactor(factor);
    for (const win of BaseWindow.getAllWindows()) {
      reconcileZoomChrome(win);
    }
    return factor;
  }

  function getZoom(sender) {
    return sender.getZoomFactor();
  }

  function setZoom(sender, factor) {
    return applyZoom(sender, clampZoomFactor(factor));
  }

  function stepZoom(sender, direction) {
    return applyZoom(
      sender,
      stepZoomFactor(sender.getZoomFactor(), direction > 0 ? +1 : -1),
    );
  }

  // Content text size — Chromium's default font size for the dashboard.
  //
  // Font SIZE is the shell's; font FAMILY stays the dashboard's. The dashboard
  // must never write `html.style.fontSize`: two writers compound into scaling
  // nobody can predict, which is why the old `mc-font-scale` key was removed by
  // a migration rather than kept alongside this. The principle is the
  // one-home-per-setting rule in docs/system-specs/modules/config.md.
  //
  // Electron exposes no `webContents.setDefaultFontSize`, and
  // `webPreferences.defaultFontSize` applies at construction only, so the
  // protocol command is the only way to move a window that is already open. If
  // Chromium ever retires it the feature degrades to needing a restart, because
  // the construction seed in window-lifecycle.js is a separate durable path.
  //
  // Every dashboard window, not one. `setupWindowContents` runs for the main
  // window and for each connection window, so a single held target would be
  // whichever was built last rather than the one in front of the user. It also
  // has to be all of them to agree with the construction seed, which writes the
  // stored size into every dashboard view's webPreferences: with one target, a
  // relaunch would move windows a live pick had not.
  //
  // This state sits in the factory, not at module scope, so a test gets a clean
  // cache from a fresh createWindowChrome() rather than deleting this module
  // out of require.cache between cases.
  let fontSizeCache = null;
  // Keyed per webContents, so one blocked window defers only itself and a retry
  // can never fire against a window it was not meant for. Weak, so a closed
  // window's entry goes with it.
  const pendingFontSizes = new WeakMap();
  const fontSizeListenerBound = new WeakSet();

  function readStoredFontSize() {
    const stored = store.get(FONT_SIZE_STORE_KEY);
    // Both spellings of absent: electron-store answers undefined for a missing
    // key, and several stores in this tree answer null. Neither is a corrupt
    // value, so neither warns.
    if (stored === undefined || stored === null) return DEFAULT_TEXT_SIZE_PX;
    if (isSupportedTextSize(stored)) return stored;
    // Declined, never corrected. The operator's config is their document, and
    // silently rewriting it erases the very value somebody may be debugging.
    glog(
      `displayPreferences.fontSize is ${JSON.stringify(stored)}, which is not a`
        + ` supported size; using ${DEFAULT_TEXT_SIZE_PX}px. The stored value is left as it is.`,
    );
    return DEFAULT_TEXT_SIZE_PX;
  }

  function getFontSize() {
    if (fontSizeCache === null) fontSizeCache = readStoredFontSize();
    return fontSizeCache;
  }

  /** Every live dashboard webContents. `_mcView` is set on a dashboard window
   *  and on nothing else, which is how the rest of this facade enumerates them,
   *  so an embedded browser panel is excluded by never qualifying rather than by
   *  being skipped. */
  function dashboardWebContentsList() {
    return BaseWindow.getAllWindows()
      .filter((win) => win && !win.isDestroyed() && win._mcView)
      .map((win) => win._mcView.webContents)
      .filter((contents) => contents && !contents.isDestroyed());
  }

  /** Remember the size whose apply this window blocked, and arrange one retry
   *  for it. Exactly ONE focus listener per webContents however many applies
   *  fail there: a listener per failure would fire the apply once per failure on
   *  a single later focus. */
  function deferFontSize(contents, px) {
    // A newer pick has already landed, so this one is stale: an apply that
    // fails slowly must not queue its own size back over a pick that succeeded
    // while it was still in flight. Without this, the next focus would re-apply
    // the older size and the window would disagree with the menu and the store.
    if (px !== fontSizeCache) return;
    pendingFontSizes.set(contents, px);
    if (fontSizeListenerBound.has(contents)) return;
    // Bind first, record second. The other order leaves the set claiming a
    // listener that does not exist if the bind throws.
    contents.on("focus", () => {
      const pending = pendingFontSizes.get(contents);
      if (pending === undefined) return;
      void applyFontSizeTo(contents, pending);
    });
    fontSizeListenerBound.add(contents);
  }

  /** Push `px` to one dashboard webContents. Always resolves: the boolean says
   *  the work finished, not that Chromium accepted it, so no caller has to
   *  catch. A blocked apply leaves a pending size on that window instead. */
  async function applyFontSizeTo(contents, px) {
    // Chromium has one debugger slot per webContents and developer tools can
    // hold it. Attach only into an empty slot, and detach only the session we
    // opened: attaching over developer tools breaks Toggle Developer Tools, and
    // detaching a session we did not open closes somebody else's.
    let weAttached = false;
    try {
      // Inside the try on purpose. Outside it, a throw from isDestroyed left a
      // rejected promise that both `void` callers ignored.
      if (!contents || contents.isDestroyed()) return false;
      if (!contents.debugger.isAttached()) {
        contents.debugger.attach("1.3");
        weAttached = true;
      }
      await contents.debugger.sendCommand("Page.setFontSizes", {
        fontSizes: { standard: px, fixed: px },
      });
      pendingFontSizes.delete(contents);
      return true;
    } catch (err) {
      // The recovery is itself wrapped: a throw from the log or from binding the
      // listener would otherwise escape this catch and reject after all.
      try {
        glog(`content text size apply deferred to the next focus: ${(err && err.message) || err}`);
        deferFontSize(contents, px);
      } catch {
        // Nothing left to try; the construction seed still carries the choice.
      }
      return false;
    } finally {
      if (weAttached) {
        try {
          contents.debugger.detach();
        } catch {
          // The session went with the view; nothing left to close.
        }
      }
    }
  }

  /** Apply to every dashboard window. Each is independent, so one window that
   *  is dead or holding its debugger slot cannot stop the others. */
  async function applyFontSizeEverywhere(px) {
    // The await sits inside the try so the fan-out is safe on its own terms
    // rather than resting on applyFontSizeTo never rejecting. Both callers
    // reach this through `void`, and an unhandled rejection in the main
    // process takes every window with it.
    try {
      const targets = dashboardWebContentsList();
      await Promise.all(targets.map((contents) => applyFontSizeTo(contents, px)));
    } catch (err) {
      glog(`content text size: apply could not run: ${(err && err.message) || err}`);
    }
  }

  /** Re-push the stored size after every load on one view.
   *
   *  A renderer that is replaced — a crash recovery, a cross-site navigation —
   *  takes its default font size from the `webPreferences` captured when the
   *  window was built, which is the size at build time rather than the size the
   *  user has picked since. Without this, such a view drops to the older size
   *  while the menu and the store both still read the newer one.
   *
   *  `did-finish-load` fires on every load and reload, which is why the Windows
   *  caption reserve is re-pushed from the same event for the same reason. The
   *  re-apply costs one protocol command per load and is a no-op in effect when
   *  the size already matches. */
  function trackFontSizeChrome(view) {
    view.webContents.on("did-finish-load", () => {
      void applyFontSizeTo(view.webContents, getFontSize());
    });
  }

  function setFontSize(px) {
    if (!isSupportedTextSize(px)) return getFontSize();
    fontSizeCache = px;
    store.set(FONT_SIZE_STORE_KEY, px);
    void applyFontSizeEverywhere(px);
    return px;
  }

  return {
    syncNativeTheme,
    positionTrafficLights,
    reconcileZoomChrome,
    trackZoomChrome,
    setThemeAccent,
    handleFocusMode,
    handleWatchFocusCursor,
    setThemeMode,
    setTitlebarMode,
    getZoom,
    setZoom,
    stepZoom,
    getFontSize,
    setFontSize,
    trackFontSizeChrome,
  };
}

module.exports = {
  HEADER_CSS_PX,
  WIN_CAPTION_RESERVE_CSS_PX,
  WIN_CAPTION_OVERLAY_CSS_PX,
  winCaptionReserveForZoom,
  winDragReserveForZoom,
  trafficLightPositionForZoom,
  createWindowChrome,
};
