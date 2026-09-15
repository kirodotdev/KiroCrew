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

const { TIERS } = require("./display-preferences");

function buildMenuTemplate(deps) {
  const {
    isMac,
    appName,
    openSettings, // navigate dashboard to /settings
    openAbout, // navigate dashboard to /settings/about (version + updates)
    reload,
    forceReload,
    toggleDevTools,
    zoomActualSize,
    zoomIn,
    zoomOut,
    alwaysOnTop, // initial checked state for Keep on Top (restored preference)
    toggleAlwaysOnTop,
    openNewSessionWindow,
    openNewConnectionWindow,
    renameCurrentWindow,
    promptRemoteHost,
    refreshToken,
    openConfigFile,
    currentFontSize, // current fontSize px, used to check the matching radio item
    setFontSize, // click handler for View > Content Text Size > <tier>
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
        // Content Text Size ladder — flat rather than nested. The Windows custom
        // titlebar popup renders only one level (see windows-menu-model.js /
        // windows-titlebar-contract.test.js), so a `Content Text Size` submenu
        // would appear as a dead item there. Kept flat for now; if the
        // titlebar renderer ever teaches nesting, this can collapse into a submenu.
        //
        // The label reads "Content Text Size" rather than "Font Size" —
        // Fable First-Principles review pointed out that "Font Size" implies
        // universal scope (chrome + content), which this feature can't
        // deliver: 4096 px-pinned text-[NNpx] literals across website/src
        // ignore Chromium's defaultFontSize. Renaming to "Content Text Size"
        // is the honest scope: this ladder grows ambient DOM text (chat
        // messages, markdown, dialog copy) — not sidebars, tab strips,
        // buttons, or menu chrome. Internal API names (`fontSize`,
        // `changeFontSize`, `defaultFontSize`) unchanged — those speak
        // Chromium's own vocabulary at the technical layer.
        //
        // Each item carries an id (`font-size-<tier.name>`) so an in-process
        // change (via updateFontSizeChecks in window-lifecycle.js) can reach
        // into the built menu and update the `checked` state after the fact.
        // Menu-driven clicks auto-update the radio via Electron.
        ...TIERS.map((tier) => ({
          id: `font-size-${tier.name}`,
          label: `Content Text Size: ${tier.label}`,
          type: "radio",
          checked: currentFontSize === tier.px,
          click: () => setFontSize(tier.px),
        })),
        // No F10 "Toggle Menu Bar" item on any platform. Framed Linux
        // (X11, KDE Wayland+SSD) shows the OS-drawn menu bar via the WM's
        // frame; Wayland/CSD Linux and Windows both reach menu items
        // through the in-app WindowsTitlebarMenu (App.tsx:3353 gates it
        // on isWinElectron || isLinuxFramelessElectron). No user path
        // needs a toggle affordance.
        { type: "separator" },
        { role: "togglefullscreen" },
        // Checkable, no accelerator: there is no cross-platform convention for
        // always-on-top, and inventing one risks colliding with an existing
        // binding. `checked` seeds from the restored preference; main.js
        // reconciles it with the window's ACTUAL state after every toggle.
        {
          label: "Keep on Top",
          type: "checkbox",
          id: "keep-on-top",
          checked: !!alwaysOnTop,
          click: toggleAlwaysOnTop,
        },
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
        ...(isMac
          ? [
              { label: "New Window", accelerator: "Cmd+Shift+N", click: openNewSessionWindow },
              { type: "separator" },
            ]
          : []),
        // NOT CmdOrCtrl+N. Cmd+N is "new session" in the renderer (the chord every
        // editor and chat client uses — see src/lib/shortcutRegistry.ts, #4608),
        // and a menu accelerator would take the keystroke before the page saw it.
        // A new connection window is a rare, dialog-opening action; it gets the
        // Alt-shifted variant so it stays reachable from the keyboard.
        { label: "New Connection Window…", accelerator: "CmdOrCtrl+Alt+N", click: openNewConnectionWindow },
        // No accelerator: Cmd+Shift+R is Force Reload (platform standard).
        { label: "Rename Window…", click: renameCurrentWindow },
        { type: "separator" },
        { label: "Set Remote Host…", click: promptRemoteHost },
        { label: "Refresh Token", accelerator: "CmdOrCtrl+Shift+T", click: refreshToken },
        { label: "Open Config File", click: openConfigFile },
      ],
    },
    // macOS keeps the stock Window menu (Minimize, Zoom, Front — no Close entry,
    // so Cmd+W reaches the renderer). Windows/Linux write it out: the stock role
    // puts "Close" on Ctrl+W, which is "close session" in the renderer, so the
    // window close moves to Ctrl+Shift+W — the VS Code / Chrome convention (Ctrl+W
    // closes a tab, Ctrl+Shift+W the window; Alt+F4 still works).
    isMac
      ? { id: "window-menu", role: "windowMenu" }
      : {
          id: "window-menu",
          label: "Window",
          submenu: [{ role: "minimize" }, { role: "zoom" }, { role: "close", accelerator: "Ctrl+Shift+W" }],
        },
    // Windows/Linux home for About (Help > About <app>).
    ...(isMac ? [] : [{ id: "help-menu", label: "Help", submenu: [aboutItem] }]),
    // Note: no F10 "Toggle Menu Bar" accelerator exists on any platform in
    // this PR's final shape — the Linux-only in-View item that PR #10247
    // originally added was deleted in the Fable First-Principles response
    // (zero-option cost: framed Linux already shows the bar, Wayland/CSD
    // has nowhere to render it, so the toggle serves no user).
  ];
}

module.exports = { buildMenuTemplate };
