"use strict";
//
// The boot-reveal handshake with loading.html, moved out of
// gateway-supervisor.js whole. Pure apart from the injected ipcMain.

/**
 * Tell the reveal splash the gateway is ready, then wait for its fade before
 * navigating. The timeout covers reduced motion, renderer errors, and loading
 * pages which never send the completion IPC.
 *
 * @param {{on: Function, removeListener: Function}} ipcMain
 * @param {object} webContents
 * @param {number} [timeoutMs]
 * @returns {Promise<void>}
 */
function fadeLoadingScreen(ipcMain, webContents, timeoutMs = 8000) {
  return new Promise((resolve) => {
    if (!webContents || webContents.isDestroyed()) { resolve(); return; }
    let settled = false;
    let timer = null;
    const onComplete = (event) => {
      if (event.sender === webContents) finish();
    };
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      ipcMain.removeListener("boot-complete", onComplete);
      resolve();
    };
    ipcMain.on("boot-complete", onComplete);
    timer = setTimeout(finish, timeoutMs);
    try { webContents.send("boot-ready"); }
    catch { finish(); }
  });
}

module.exports = { fadeLoadingScreen };
