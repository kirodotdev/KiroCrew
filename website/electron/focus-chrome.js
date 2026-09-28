"use strict";

/**
 * Focus-mode chrome for the macOS shell: hide/show the native traffic lights
 * with the dashboard header, WITHOUT losing the window's draggable regions.
 *
 * setWindowButtonVisibility mutates the native window's titlebar (styleMask) on
 * a titleBarStyle:"hidden" BaseWindow, and that mutation drops the draggable
 * regions the renderer had declared. The ordering makes this bite every time:
 * the renderer mounts its drag surface (regions declared), then its effect sends
 * the focus-mode-chrome IPC, so the styleMask change lands AFTER the declaration
 * and wipes it. Symptom: -webkit-app-region:drag surfaces that are provably in
 * the DOM select text instead of moving the window — renderer-side variants
 * (marking the header, re-expanding the injected drag bar, mounting a fresh
 * strip) all fail identically because the regions are discarded window-side.
 *
 * The fix is to make the renderer RE-DECLARE its regions after the native
 * change: Chromium re-sends the window's draggable-region set whenever the
 * computed set changes, so briefly adding (and next frame removing) a 1px
 * drag-region element forces two re-sends whose final state is the true set.
 * There is no main-process API to re-apply regions directly.
 */

// Kept as a plain string: this runs in the page, and building it here keeps the
// ordering guarantee (executeJavaScript is issued after setWindowButtonVisibility
// returns) in one auditable place.
const REDECLARE_DRAG_REGIONS_JS = [
  "(() => {",
  "  const el = document.createElement('div');",
  "  el.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;" +
    "pointer-events:none;-webkit-app-region:drag;';",
  "  document.body.appendChild(el);",
  "  void el.offsetWidth;", // force style+layout so the region set actually changes
  "  requestAnimationFrame(() => el.remove());",
  "})()",
].join("\n");

/**
 * Apply the focus-mode chrome state to one window. Returns true when the state
 * was applied, false when the window was unusable (gone, or fullscreen — where
 * macOS owns the buttons and there is no titlebar to mutate).
 *
 * `helpers.positionTrafficLights` is injected rather than imported: it lives in
 * main.js next to the zoom bookkeeping it needs, and injecting it keeps this
 * module requireable from plain node tests.
 */
function applyFocusModeChrome(win, visible, helpers) {
  if (!win || win.isDestroyed() || win.isFullScreen()) return false;
  try {
    win.setWindowButtonVisibility(!!visible);
    // A visibility round-trip can drop the custom traffic-light inset.
    if (visible && helpers && helpers.positionTrafficLights) {
      helpers.positionTrafficLights(win);
    }
    // AFTER the native mutation, on BOTH transitions: hide also changes the
    // styleMask, and the hidden state's remaining regions (none in the top
    // band) must be what the window actually holds.
    const wc = win._mcView && win._mcView.webContents;
    if (wc) {
      const p = wc.executeJavaScript(REDECLARE_DRAG_REGIONS_JS);
      if (p && typeof p.catch === "function") p.catch(() => {});
    }
    return true;
  } catch {
    /* window mid-teardown, or a build without the API */
    return false;
  }
}

module.exports = { applyFocusModeChrome, REDECLARE_DRAG_REGIONS_JS };
