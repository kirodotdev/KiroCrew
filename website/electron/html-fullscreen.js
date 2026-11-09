// Track who raised the window when a page element goes DOM-fullscreen.
//
// When a page element calls requestFullscreen(), Electron itself raises the host
// window (the dashboard is a WebContentsView under a BaseWindow), keeps it
// raised if the user had already fullscreened it, and lowers it again when
// Chromium leaves DOM fullscreen. This module does NOT call `setFullScreen`: a
// second caller racing Electron's own exit leaves the window out of fullscreen
// while the element stays `document.fullscreenElement`, so a <video> keeps
// covering the whole view and Escape no longer reaches it.
//
// What the shell still needs is OWNERSHIP, to know whether the window is
// fullscreen because a page asked or because the user chose it:
//
//   - The persisted window state. window-lifecycle.js persists `fullScreen` on
//     every geometry change, and a video's fullscreen is not a window preference:
//     a quit or crash mid-playback must not relaunch into a fullscreen Space the
//     user never chose (see window-state.js's "blacked out" note).
//
// A page-raised window is claimed explicitly, when `enter-html-full-screen`
// fires, and only if the user had not already fullscreened the window. Persistence
// reads `raisedWindow()` synchronously inside the window's own `enter-full-screen`
// emit, so a fullscreen the user just chose has to read as unclaimed before this
// module has decided anything about it: nothing is claimed by default.
//
// The user's choice is read from the window's own transitions, not from the order
// of Electron's events. Electron raises the window and then emits
// `enter-html-full-screen` in the same call, but the window's `enter-full-screen`
// lands before that emit where the raise is synchronous (Windows, Linux) and
// after it where it is not (macOS). Each native emit is its own callback scope,
// so a window `enter-full-screen` is settled on the next event-loop turn, after
// both: it counts as the user's only if no page claimed it by then. The claim is
// therefore not readable inside the window's own `enter-full-screen` emit in
// every order; window-lifecycle.js persists that event after a debounce instead.
//
// The claim ends when the WINDOW has actually left fullscreen, not when the page
// says it is done. The exit is asynchronous on macOS (see hide-to-tray.js, which
// waits on the same transition), so the claim holds across `leave-html-full-screen`
// and a persist landing in that gap still sees the transient `true`.
//
// Extracted into its own module rather than inlined in window-lifecycle.js so a
// test can drive the wiring with stubs.
"use strict";

/**
 * Attach the DOM-fullscreen ownership tracker to one window/WebContents pair.
 *
 * Both objects are duck-typed so tests need no Electron: `win` supplies
 * `isDestroyed`, `isFullScreen` and `on`; `webContents` supplies `on`.
 *
 * @param {object} args
 * @param {{ isDestroyed: () => boolean, isFullScreen: () => boolean,
 *           on: (event: string, listener: () => void) => void }} args.win
 * @param {{ on: (event: string, listener: () => void) => void }} args.webContents
 * @returns {{ raisedWindow: () => boolean }} ownership predicate, read by
 *          window-lifecycle.js's persistence to suppress a transient fullscreen
 */
function attachHtmlFullScreen({ win, webContents }) {
  // The window was fullscreen when this was attached, or a settled transition the
  // user started took it there.
  let userFullScreen = win.isFullScreen();
  // A page element, not the user, is why the window is fullscreen.
  let pageRaised = false;

  webContents.on("enter-html-full-screen", () => {
    pageRaised = !userFullScreen;
  });

  win.on("enter-full-screen", () => {
    setImmediate(() => {
      if (!pageRaised) userFullScreen = true;
    });
  });
  win.on("leave-full-screen", () => {
    userFullScreen = false;
    pageRaised = false;
  });

  return {
    raisedWindow: () => !win.isDestroyed() && win.isFullScreen() && pageRaised,
  };
}

module.exports = { attachHtmlFullScreen };
