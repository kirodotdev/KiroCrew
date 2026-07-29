"use strict";

// Application menu template for every desktop platform.
//
// macOS gets the conventional app menu (About / Settings… / Services / Hide /
// Quit) — previously this was `{ role: "appMenu" }`, which is only defined on
// darwin and gave no way to add Settings or route About into the dashboard.
// Windows and Linux get the same destinations in their conventional homes:
// File > Settings… and Help > About. Both items navigate the dashboard SPA
// (Settings > About is where "Check for updates" lives), so every platform has
// a discoverable path to version info and updates.
//
// Pure data + injected callbacks: no electron imports, so the template shape
// and the click wiring are unit-testable without a display server.
// Menu.buildFromTemplate stays in main.js.

function buildMenuTemplate(deps) {
  const {
    isMac,
    appName,
    openSettings, // navigate dashboard to /settings
    openAbout, // navigate dashboard to /settings?tab=about (version + updates)
    reload,
    forceReload,
    toggleDevTools,
    zoomActualSize,
    zoomIn,
    zoomOut,
    openNewConnectionWindow,
    renameCurrentWindow,
    promptRemoteHost,
    refreshToken,
    openConfigFile,
  } = deps;

  // Shared destinations. CmdOrCtrl+, is the Settings convention on macOS and
  // the emerging one elsewhere (VS Code, Chrome DevTools, Slack).
  const settingsItem = { label: "Settings…", accelerator: "CmdOrCtrl+,", click: openSettings };
  const aboutItem = { label: `About ${appName}`, click: openAbout };

  return [
    ...(isMac
      ? [
          {
            id: "app-menu",
            label: appName,
            submenu: [
              aboutItem,
              { type: "separator" },
              settingsItem,
              { type: "separator" },
              { role: "services" },
              { type: "separator" },
              { role: "hide" },
              { role: "hideOthers" },
              { role: "unhide" },
              { type: "separator" },
              { role: "quit" },
            ],
          },
        ]
      : [
          {
            id: "file-menu",
            // Windows/Linux home for Settings; the quit role renders as
            // "Exit" on Windows and "Quit" on Linux.
            label: "File",
            submenu: [settingsItem, { type: "separator" }, { role: "quit" }],
          },
        ]),
    { id: "edit-menu", role: "editMenu" },
    {
      id: "view-menu",
      label: "View",
      submenu: [
        // Explicit handlers, not { role: ... }: the roles target the focused
        // window's own webContents, which BaseWindow doesn't have.
        { label: "Reload", accelerator: "CmdOrCtrl+R", click: reload },
        { label: "Force Reload", accelerator: "CmdOrCtrl+Shift+R", click: forceReload },
        { type: "separator" },
        { label: "Actual Size", accelerator: "CmdOrCtrl+0", click: zoomActualSize },
        { label: "Zoom In", accelerator: "CmdOrCtrl+=", click: zoomIn },
        { label: "Zoom Out", accelerator: "CmdOrCtrl+-", click: zoomOut },
        { type: "separator" },
        { role: "togglefullscreen" },
        { type: "separator" },
        {
          label: "Toggle Developer Tools",
          accelerator: "CmdOrCtrl+Shift+I",
          id: "devtools-toggle",
          visible: false, // hidden until dev-mode IPC fires
          click: toggleDevTools,
        },
      ],
    },
    {
      id: "connection-menu",
      label: "Connection",
      submenu: [
        { label: "New Connection Window…", accelerator: "CmdOrCtrl+N", click: openNewConnectionWindow },
        // No accelerator: Cmd+Shift+R is Force Reload (platform standard).
        { label: "Rename Window…", click: renameCurrentWindow },
        { type: "separator" },
        { label: "Set Remote Host…", click: promptRemoteHost },
        { label: "Refresh Token", accelerator: "CmdOrCtrl+Shift+T", click: refreshToken },
        { label: "Open Config File", click: openConfigFile },
      ],
    },
    { id: "window-menu", role: "windowMenu" },
    // Windows/Linux home for About (Help > About <app>).
    ...(isMac ? [] : [{ id: "help-menu", label: "Help", submenu: [aboutItem] }]),
  ];
}

module.exports = { buildMenuTemplate };
