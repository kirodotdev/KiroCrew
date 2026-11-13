"use strict";

/**
 * Tell every dashboard view that the OS session has locked.
 *
 * A page cannot see a screen lock: the tab stays visible and keeps running, so a
 * dictation started before the person walked away keeps the microphone open in
 * an empty room. Only the main process hears the lock, through `powerMonitor`'s
 * `lock-screen` event, so it relays it to each dashboard view and the page stops
 * its own recording.
 *
 * `lock-screen` is emitted on macOS and Windows. Electron does not emit it on
 * Linux, so there the relay never fires and a recording runs until the person
 * stops it, as before.
 *
 * The message carries no data. What to do with it belongs to the page.
 */

const SESSION_LOCKED_CHANNEL = "os-session-locked";

/** The live dashboard views among `windows`: the ones with a `_mcView`. */
function dashboardContents(windows) {
  const out = [];
  for (const win of windows || []) {
    try {
      if (!win || win.isDestroyed() || !win._mcView) continue;
      const wc = win._mcView.webContents;
      if (wc && !wc.isDestroyed()) out.push(wc);
    } catch {
      // A window mid-teardown is skipped, not fatal to the relay.
    }
  }
  return out;
}

/**
 * Relay `lock-screen` to the views `listWindows()` returns at the moment of the
 * lock, so a window opened after this is armed is reached too. Returns the
 * function that stops the relay.
 */
function watchSessionLock(powerMonitor, listWindows, log = () => {}) {
  const onLock = () => {
    let sent = 0;
    for (const wc of dashboardContents(listWindows())) {
      try {
        wc.send(SESSION_LOCKED_CHANNEL);
        sent += 1;
      } catch {
        // One view failing to take the message must not stop the others.
      }
    }
    log(`power: session locked; told ${sent} dashboard view(s)`);
  };
  powerMonitor.on("lock-screen", onLock);
  return () => powerMonitor.removeListener("lock-screen", onLock);
}

module.exports = { SESSION_LOCKED_CHANNEL, dashboardContents, watchSessionLock };
