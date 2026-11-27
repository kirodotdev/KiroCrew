"use strict";

/**
 * The window a start-time update apply shows while its command runs.
 *
 * The apply runs before the main window, the tray and the IPC bridges exist, so
 * this is the boot splash in a window of its own with no preload (preload.js
 * asks a synchronous question that only a dashboard view answers) and a fixed
 * status line in its query.
 */
function createInstallSplash({ BrowserWindow, page, log, isQuitting = () => false }) {
  let win = null;
  let applying = false;

  function show(version) {
    applying = true;
    try {
      win = new BrowserWindow({
        width: 640,
        height: 440,
        resizable: false,
        backgroundColor: "#0f1117",
        title: "Kiro Crew",
        webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
      });
      // Windows and Linux would otherwise show Electron's default menu: the app
      // menu does not exist yet.
      win.removeMenu();
      // Closing never cancels the update command, and an unprevented close of
      // the only window would quit the app mid-apply through window-all-closed.
      // A quit already under way goes through: before-quit has run its teardown,
      // and a prevented close would cancel the quit with the app half stopped.
      win.on("close", (event) => {
        if (!applying || isQuitting()) return;
        event.preventDefault();
        win.hide();
      });
      win.loadFile(page, { query: { status: `Installing update ${version}. Kiro Crew opens when it is done.` } });
    } catch (err) {
      log(`[update] could not open the install window: ${(err && err.message) || err}`);
    }
    return () => {
      applying = false;
      if (win && !win.isDestroyed()) win.hide();
    };
  }

  function raise() {
    if (!applying) return false;
    log("[update] a launch arrived during the start-time apply");
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    }
    return true;
  }

  function close() {
    if (win && !win.isDestroyed()) win.destroy();
    win = null;
  }

  return { show, raise, close };
}

module.exports = { createInstallSplash };
