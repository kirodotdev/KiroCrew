"use strict";

/**
 * Whether the app's current quit is a relaunch rather than a Quit the user chose.
 *
 * The gateway keeps its YOLO and chat trust across every exit the user did not
 * choose, and the desktop tells it which quit is a user stop (`stopOnQuit`'s
 * `userStop`). A relaunch is a restart: the GPU-crash fallback's automatic one, the
 * update "Restart now", the dashboard's relaunch, a managed update's apply. Each of
 * them calls `app.relaunch()` before it quits, except the "Restart now" of an armed
 * update, which quits for the installer and calls `noteRelaunch()` itself.
 * `watchRelaunch(app)` wraps `app.relaunch` once at startup so every caller is seen.
 */

let relaunching = false;

/** Record that the quit under way is a relaunch. */
function noteRelaunch() {
  relaunching = true;
}

/** Whether a relaunch was asked for in this run of the app. */
function isRelaunching() {
  return relaunching;
}

/** Wrap `app.relaunch` so every call notes the relaunch first. Idempotent. */
function watchRelaunch(app) {
  if (!app || typeof app.relaunch !== "function" || app.relaunch.__notesRelaunch) return;
  const original = app.relaunch.bind(app);
  const wrapped = (...args) => {
    noteRelaunch();
    return original(...args);
  };
  wrapped.__notesRelaunch = true;
  app.relaunch = wrapped;
}

function resetForTests() {
  relaunching = false;
}

module.exports = { noteRelaunch, isRelaunching, watchRelaunch, resetForTests };
