"use strict";

/**
 * Hold the app's quit until the gateway's graceful stop has settled.
 *
 * Electron ends a quit as soon as every `before-quit` listener has returned. On
 * Windows the gateway is a non-detached child, so it sits in the app's
 * kill-on-close job object: when the main process exits, Windows terminates the
 * gateway in the middle of its own shutdown, its cleanup hooks never run, and
 * the children it started (app backends, ssh forwarders) are left running.
 *
 * So the first quit is prevented, `teardown` runs and starts the stop, and the
 * quit is asked for again once the stop settles. Every quit before that is
 * prevented too, except the installer's own quit for an update (`isUpdateQuit`):
 * the update lanes stop the gateway themselves before they hand off, and holding
 * that quit would hold the install. `stopGatewayGracefully` bounds itself;
 * `capMs` only keeps a stop that never settles from keeping the app open.
 *
 * @param {object} opts
 * @param {{ quit: () => void }} opts.app
 * @param {() => (Promise<void>|void)} opts.teardown the quit's own teardown; returns the stop
 * @param {() => boolean} [opts.isUpdateQuit] whether the installer is quitting the app
 * @param {number} [opts.capMs]
 * @param {(message: string) => void} [opts.log]
 * @returns {(event: { preventDefault: () => void }) => void} the `before-quit` listener
 */
function createQuitHold({
  app,
  teardown,
  isUpdateQuit = () => false,
  capMs = 30000,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  log = () => {},
}) {
  let stopping = null;
  let settled = false;
  return function onBeforeQuit(event) {
    if (settled) return;
    if (!isUpdateQuit()) event.preventDefault();
    if (stopping) return;
    let stop;
    try {
      stop = teardown();
    } catch (error) {
      log("quit: teardown failed — " + (error && error.message));
    }
    let timer = null;
    const cap = new Promise((resolve) => {
      timer = setTimeoutFn(() => {
        log(`quit: gateway stop still pending after ${capMs}ms — quitting anyway`);
        resolve();
      }, capMs);
    });
    stopping = Promise.race([Promise.resolve(stop).catch(() => {}), cap]).then(() => {
      clearTimeoutFn(timer);
      settled = true;
      app.quit();
    });
  };
}

module.exports = { createQuitHold };
