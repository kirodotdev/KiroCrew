"use strict";
//
// Pure, injectable helper extracted from main.js so the "a blocking full-page
// prompt must stay dismissable" rule is unit-testable without Electron (mirrors
// gateway-recovery.js / gateway-wait.js / gateway-stop.js).
//
// PROBLEM: the token-required page (token-prompt.html) is loaded into the MAIN
// window's own webContents. When that window is in macOS native fullscreen the
// traffic-light close buttons are hidden and the application menu is out of
// reach, and the page itself offers no exit — so the user is trapped on it and
// the only escape is force-killing the process. The same is true of the other
// immersive modes (simple-fullscreen, kiosk), which also hide window chrome.
//
// FIX: before showing any such in-window blocking prompt, drop every immersive
// mode. That restores the native title bar (Close / minimize) and the app menu
// (Cmd-Q / Cmd-W), so the prompt is always dismissable.
//
// Kept pure and injectable: it takes a window-like object exposing the
// BrowserWindow immersive-mode API and returns what it changed, so a test can
// assert the modes are cleared without a real BrowserWindow.

/**
 * Clear every immersive/chrome-hiding window mode so an in-window blocking
 * prompt cannot trap the user. Best-effort and idempotent: each probe is
 * guarded, a mode already off is left untouched, and a destroyed window is a
 * no-op.
 *
 * @param {object} win  A BrowserWindow-like object. Only the isFullScreen /
 *                      setFullScreen / isSimpleFullScreen / setSimpleFullScreen /
 *                      isKiosk / setKiosk / isDestroyed members are used, each
 *                      optional.
 * @returns {{fullScreen: boolean, kiosk: boolean}}  Which modes were actually
 *                      turned off (for logging / assertions).
 */
function exitImmersiveModes(win) {
  const changed = { fullScreen: false, kiosk: false };
  if (!win) return changed;
  try { if (typeof win.isDestroyed === "function" && win.isDestroyed()) return changed; } catch { return changed; }

  try {
    if (typeof win.isFullScreen === "function" && win.isFullScreen()) {
      win.setFullScreen(false);
      changed.fullScreen = true;
    }
  } catch { /* best effort — never let chrome-restore throw past the caller */ }

  try {
    // macOS "simple" fullscreen (pre-Lion style) is a distinct mode from native
    // fullscreen and also hides the traffic lights; clear it too.
    if (typeof win.isSimpleFullScreen === "function" && win.isSimpleFullScreen()) {
      win.setSimpleFullScreen(false);
      changed.fullScreen = true;
    }
  } catch { /* best effort */ }

  try {
    if (typeof win.isKiosk === "function" && win.isKiosk()) {
      win.setKiosk(false);
      changed.kiosk = true;
    }
  } catch { /* best effort */ }

  return changed;
}

module.exports = { exitImmersiveModes };
