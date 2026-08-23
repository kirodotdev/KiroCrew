// Bridge DOM fullscreen to real window fullscreen for the Kiro Crew shell.
//
// Why this exists: the dashboard renders inside a WebContentsView whose bounds
// are clamped to the host window's content rect (main.js updateViewBounds). When
// a page element calls requestFullscreen(), Chromium makes it :fullscreen and
// sizes it to THAT rect — so an inline <video>'s fullscreen button appeared to do
// nothing: the element was fullscreen inside a window that was not. The window
// itself only ever went fullscreen from the native control, which fires
// `enter-full-screen`; DOM fullscreen fires a different pair of events on the
// WebContents (`enter-html-full-screen` / `leave-html-full-screen`) that nothing
// was listening to.
//
// The subtle half is OWNERSHIP. Two things depend on knowing whether the window
// is fullscreen because a page asked or because the user did:
//
//   - The restore. Exiting DOM fullscreen must not drop a window the user had
//     ALREADY put in fullscreen before pressing play, so the bridge only lowers
//     what it raised.
//   - The persisted window state. main.js persists `fullScreen` on every
//     geometry change, and a video's fullscreen is not a window preference: a
//     quit or crash mid-playback must not relaunch into a fullscreen Space the
//     user never chose (see window-state.js's "blacked out" note).
//
// Ownership therefore ends when the WINDOW has actually left fullscreen, not
// when the page says it is done. `setFullScreen(false)` is asynchronous on macOS
// (see hide-to-tray.js, which waits on the same transition), so clearing the
// flag inline would leave a window in which `isFullScreen()` is still true while
// the bridge no longer claims it — and a persist landing there would write the
// transient `true` to disk, which is the whole thing this flag exists to prevent.
// The window's own `leave-full-screen` is the only signal that the transition
// completed, so that is what clears it.
//
// Known bounded residual: re-entering DOM fullscreen DURING that exit animation
// reads `isFullScreen() === true` and so does not re-raise the window. That
// needs a toggle inside a sub-second transition, and its worst outcome is a
// window that fails to enlarge (the original cosmetic symptom) — never a wrong
// value on disk. Not worth a state machine.
//
// Extracted into its own module rather than inlined in main.js because the bug
// lived in the WIRING, not in any single call: a test that reconstructed the
// closure by hand would pass whether or not main.js ever attached it. Here the
// wiring is a named function a test can drive with stubs.
"use strict";

/**
 * Attach the DOM-fullscreen bridge to one window/WebContents pair.
 *
 * Both objects are duck-typed so tests need no Electron: `win` supplies
 * `isDestroyed`, `isFullScreen`, `setFullScreen` and `on`; `webContents`
 * supplies `on`.
 *
 * @param {object} args
 * @param {{ isDestroyed: () => boolean, isFullScreen: () => boolean,
 *           setFullScreen: (v: boolean) => void,
 *           on: (event: string, listener: () => void) => void }} args.win
 * @param {{ on: (event: string, listener: () => void) => void }} args.webContents
 * @returns {{ raisedWindow: () => boolean }} ownership predicate, read by
 *          main.js's persistMainWindowState() to suppress a transient fullscreen
 */
function attachHtmlFullScreen({ win, webContents }) {
  // True while THIS bridge is responsible for the window being fullscreen.
  // Never set when the window was already fullscreen when the page asked.
  let raisedWindow = false;

  webContents.on("enter-html-full-screen", () => {
    // A window can be torn down between Chromium dispatching the event and this
    // listener running; every other main.js listener guards the same way.
    if (win.isDestroyed()) return;
    raisedWindow = !win.isFullScreen();
    if (raisedWindow) win.setFullScreen(true);
  });

  webContents.on("leave-html-full-screen", () => {
    if (win.isDestroyed()) return;
    // Only lower what this bridge raised. The flag is deliberately NOT cleared
    // here — see the header: the window is still fullscreen until the (possibly
    // async) transition completes, and dropping the claim early would let a
    // persist write that transient state.
    if (raisedWindow) win.setFullScreen(false);
  });

  // The transition completed. This also covers the user exiting a bridge-raised
  // fullscreen with the native control while the video is still :fullscreen —
  // ownership ends, so the later leave-html-full-screen correctly does nothing.
  win.on("leave-full-screen", () => { raisedWindow = false; });

  return { raisedWindow: () => raisedWindow };
}

module.exports = { attachHtmlFullScreen };
