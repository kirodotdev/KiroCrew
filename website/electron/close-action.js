"use strict";
//
// What the main window's close button does, extracted from window-lifecycle.js
// so the decision is unit-testable without Electron (mirrors local-gateway.js).
//
// "background" (the default) hides the window and keeps the app running. "quit"
// ends the app through the Quit menu item's path, so an app-spawned gateway
// stops with it and a gateway the app only connected to keeps running.

/** electron-store key holding the user's choice. */
const CLOSE_ACTION_KEY = "closeAction";
const CLOSE_ACTION_BACKGROUND = "background";
const CLOSE_ACTION_QUIT = "quit";

/**
 * The user's choice, defaulting to "background".
 *
 * Only an exact "quit" quits, so a missing or hand-edited value can never turn
 * closing a window into ending the user's sessions.
 *
 * @param {{get: (key: string) => unknown}} store
 * @returns {"background"|"quit"}
 */
function getCloseAction(store) {
  return store.get(CLOSE_ACTION_KEY) === CLOSE_ACTION_QUIT
    ? CLOSE_ACTION_QUIT
    : CLOSE_ACTION_BACKGROUND;
}

/**
 * Record the user's choice. Anything but an exact "quit" is stored as
 * "background".
 *
 * @param {{set: (key: string, value: unknown) => void}} store
 * @param {unknown} value
 * @returns {"background"|"quit"} the value written
 */
function setCloseAction(store, value) {
  const next = value === CLOSE_ACTION_QUIT ? CLOSE_ACTION_QUIT : CLOSE_ACTION_BACKGROUND;
  // electron-store rewrites the whole file on every set; skip a no-op.
  if (store.get(CLOSE_ACTION_KEY) !== next) store.set(CLOSE_ACTION_KEY, next);
  return next;
}

/**
 * What one `close` event on the main window should do.
 *
 * - "persist": a quit is under way, so let the window close and save its geometry.
 * - "quit": cancel this close and start the app's quit, whose own close then
 *   lands on "persist".
 * - "hide": hide the window and keep the app running.
 *
 * @param {{quitting: boolean, action: string}} o
 * @returns {"persist"|"quit"|"hide"}
 */
function decideClose({ quitting, action }) {
  if (quitting) return "persist";
  return action === CLOSE_ACTION_QUIT ? "quit" : "hide";
}

/**
 * Build the main window's `close` listener from its effects, so the close
 * sequence is testable without a BrowserWindow.
 *
 * The action is read per event, so a change applies to the next close. On
 * "quit", a macOS fullscreen hide still pending from an earlier close is
 * cancelled first so it cannot fire into the quit.
 *
 * @param {object} deps
 * @param {() => boolean} deps.isQuitting
 * @param {() => string} deps.getAction
 * @param {() => void} deps.cancelPendingHide
 * @param {() => void} deps.requestQuit
 * @param {() => void} deps.hide
 * @param {() => void} deps.persist
 * @returns {(event: {preventDefault: () => void}) => "persist"|"quit"|"hide"}
 */
function createCloseHandler({ isQuitting, getAction, cancelPendingHide, requestQuit, hide, persist }) {
  return (event) => {
    const decision = decideClose({ quitting: isQuitting(), action: getAction() });
    if (decision === "quit") {
      event.preventDefault();
      cancelPendingHide();
      // Quitting inside a prevented close cancels the quit, and on macOS the
      // app then lingers windowless with its gateway stopped.
      setImmediate(requestQuit);
    } else if (decision === "hide") {
      event.preventDefault();
      hide();
    } else {
      persist();
    }
    return decision;
  };
}

module.exports = {
  CLOSE_ACTION_KEY,
  CLOSE_ACTION_BACKGROUND,
  CLOSE_ACTION_QUIT,
  getCloseAction,
  setCloseAction,
  decideClose,
  createCloseHandler,
};
