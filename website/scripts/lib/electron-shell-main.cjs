"use strict";

// Electron main process for the desktop-shell screenshot harness.
//
// It exists because the shell surfaces this repository cannot otherwise
// photograph -- the application menu, its accelerator captions, and the window
// chrome -- are drawn by Electron itself, outside any web page, so the ~546
// Chromium capture scripts under website/scripts/ cannot reach them. See
// capture-electron-shell.mjs for how it is driven and docs/guides/
// worktree-verification-recipes.md for when to reach for it.
//
// It is NOT the product's main process. electron/main.js supervises a Python
// gateway, verifies the bundle, and waits on a token before it shows anything,
// none of which a picture of a menu needs. What this file does instead is import
// the REAL modules that decide the surfaces under test:
//
//   - electron/app-menu.js buildMenuTemplate -- the menu and every caption in it
//   - electron/linux-frame.js decideLinuxFrame -- whether the window is frameless
//
// so the pixels come from product code even though the boot sequence does not.
// Every injected callback is a no-op: a still picture never activates an item,
// and the harness must not be able to reach into the product's behaviour.

const path = require("node:path");
const { app, BrowserWindow, Menu } = require("electron");

const ELECTRON_DIR = path.resolve(__dirname, "..", "..", "electron");
const { buildMenuTemplate } = require(path.join(ELECTRON_DIR, "app-menu.js"));
const { decideLinuxFrame } = require(path.join(ELECTRON_DIR, "linux-frame.js"));

/** Width/height of the harness window, fixed so successive shots diff. */
const WINDOW = { width: 1180, height: 720 };

/** Backdrop behind the chrome under test. */
const BACKDROP = "#0f1115";

// What fills the window body by default, and why it is deliberately empty.
//
// The subjects here are the menu bar, the menu popup and the frame, none of which
// the page draws. The first version of this file loaded the shipped splash
// (electron/loading.html) so the body would be real product pixels, and that
// made the shot undiffable: the splash places its ghosts with Math.random() and
// churns them on randomised timers, so two runs differ in the body while the
// chrome is identical. A flat backdrop is honest about being the harness's own,
// and it keeps a diff reporting only the chrome.
//
// SHELL_HARNESS_URL overrides it with a file path or a URL, for an author whose
// subject really is the frame around a particular page.
const BLANK_PAGE = `data:text/html,<body style="margin:0;background:${encodeURIComponent(BACKDROP)}"></body>`;

const noop = () => {};

/**
 * The dependency object buildMenuTemplate destructures. Named explicitly rather
 * than built from a key list, so adding a dependency to the template shows up
 * here as an obviously missing line instead of an undefined click handler.
 *
 * @param {boolean} isMac
 */
function menuDeps(isMac) {
  return {
    isMac,
    appName: "Kiro Crew",
    openSettings: noop,
    openAbout: noop,
    reload: noop,
    forceReload: noop,
    toggleDevTools: noop,
    zoomActualSize: noop,
    zoomIn: noop,
    zoomOut: noop,
    alwaysOnTop: false,
    toggleAlwaysOnTop: noop,
    openNewSessionWindow: noop,
    openNewConnectionWindow: noop,
    renameCurrentWindow: noop,
    promptRemoteHost: noop,
    refreshToken: noop,
    openConfigFile: noop,
  };
}

// Which platform's menu to build. The harness runs on Linux, so `darwin` is the
// only value that needs forcing: it lets a Linux run render the macOS menu
// TEMPLATE (the app menu and its Cmd+, caption) even though only macOS renders
// it in the system menu bar.
const isMac = (process.env.SHELL_HARNESS_PLATFORM || process.platform) === "darwin";

app.whenReady().then(async () => {
  Menu.setApplicationMenu(Menu.buildFromTemplate(buildMenuTemplate(menuDeps(isMac))));

  const { frameless } = decideLinuxFrame({ env: process.env });
  const win = new BrowserWindow({
    ...WINDOW,
    show: true,
    frame: !frameless,
    backgroundColor: BACKDROP,
    webPreferences: { sandbox: false, nodeIntegration: false, contextIsolation: true },
  });
  const url = process.env.SHELL_HARNESS_URL;
  if (!url) await win.loadURL(BLANK_PAGE);
  else if (/^[a-z][a-z0-9+.-]*:/i.test(url)) await win.loadURL(url);
  else await win.loadFile(path.resolve(url));
});
