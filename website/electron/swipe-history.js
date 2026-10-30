"use strict";

/**
 * Relay macOS swipe gestures as one history step.
 *
 * With Logitech's Options software, and utilities like SensibleSideButtons, a
 * mouse's Back and Forward side buttons reach apps as AppKit swipe events, not
 * as buttons 4 and 5, so the page never sees a button. The trackpad's
 * three-finger page swipe sends the same events. Chrome turns them into history
 * Back/Forward; Electron performs no navigation and only emits the window's
 * `swipe` event (macOS only, never fired elsewhere). Without a listener the
 * press is dropped.
 *
 * The dashboard's step is SENT to it rather than performed here with
 * `navigationHistory.goBack()`: the dashboard takes it through the same guarded
 * step as its top-bar arrows and ⌘←/⌘→, which asks before an unsaved draft is
 * discarded. A native Back is caught by that prompt only best-effort.
 *
 * Raw buttons 4 and 5 need nothing here: Chromium walks the history of the page
 * under the pointer itself, which is why splash-history.js prunes the boot
 * splash. A swipe carries no position, so the cursor stands in for one: over a
 * visible native browser panel the step goes to that page's own history, the
 * way Chromium routes the buttons.
 */

/** Main → dashboard channel; the preload's `onHistoryStep` listens on it. */
const HISTORY_STEP_CHANNEL = "history:step";

// Electron names the direction after the event's deltaX: +1 is "left", which
// Chrome reads as Back. Vertical swipes carry no history meaning.
const HISTORY_STEP_BY_SWIPE = Object.freeze({ left: -1, right: 1 });

/** -1 for Back, 1 for Forward, 0 for any other direction. */
function historyStepForSwipe(direction) {
  return Object.hasOwn(HISTORY_STEP_BY_SWIPE, direction) ? HISTORY_STEP_BY_SWIPE[direction] : 0;
}

/**
 * The webContents of the visible native browser panel under `cursor`, or null.
 * `cursor` is in screen DIP; a panel's bounds are DIP relative to the window's
 * content area (browser-view.js), so the cursor is moved into that space first.
 */
function browserPanelUnderCursor(win, cursor) {
  const panels = win._mcBrowserPanels;
  if (!panels || !cursor) return null;
  const content = win.getContentBounds();
  const x = cursor.x - content.x;
  const y = cursor.y - content.y;
  for (const entry of panels.values()) {
    const { visible, bounds } = entry.manager.getState();
    if (!visible || !bounds) continue;
    const inside = x >= bounds.x && x < bounds.x + bounds.width
      && y >= bounds.y && y < bounds.y + bounds.height;
    if (!inside) continue;
    const pageWc = entry.manager.getWebContents();
    return pageWc && !pageWc.isDestroyed() ? pageWc : null;
  }
  return null;
}

/** Step a page's own history, as Chromium does for buttons 4 and 5: nothing
 *  when there is nowhere to go. */
function stepPageHistory(pageWc, step) {
  const nav = pageWc.navigationHistory;
  if (!(step < 0 ? nav.canGoBack() : nav.canGoForward())) return;
  if (step < 0) nav.goBack();
  else nav.goForward();
}

/**
 * Arm the relay on a dashboard window. Call ONCE per window, next to
 * armSplashHistoryClear: the listener lives as long as the window.
 *
 * `isAlive` follows the window-lifecycle convention of checking for teardown
 * before touching a webContents. `panelUnderCursor` returns the webContents of
 * the browser panel the cursor is over, or null for the dashboard. Returns the
 * handler (for tests).
 */
function armSwipeHistory(win, wc, { isAlive = () => true, panelUnderCursor = () => null } = {}) {
  const onSwipe = (_event, direction) => {
    const step = historyStepForSwipe(direction);
    if (step === 0) return;
    try {
      if (!isAlive()) return;
      const pageWc = panelUnderCursor();
      if (pageWc) stepPageHistory(pageWc, step);
      else wc.send(HISTORY_STEP_CHANNEL, step);
    } catch {
      /* window may be mid-teardown: same tolerance as the other window helpers */
    }
  };
  win.on("swipe", onSwipe);
  return onSwipe;
}

module.exports = { armSwipeHistory, browserPanelUnderCursor, historyStepForSwipe, HISTORY_STEP_CHANNEL };
