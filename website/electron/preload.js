const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("kirocrew", {
  platform: process.platform,
  isElectron: true,
});

contextBridge.exposeInMainWorld("electronAPI", {
  onStatus: (cb) => {
    const handler = (_e, msg) => cb(msg);
    ipcRenderer.on("status", handler);
    return () => ipcRenderer.removeListener("status", handler);
  },
  // Boot-reveal handshake: main.js sends "boot-ready" once the gateway is up;
  // loading.html replies "boot-complete" after its reveal animation fades out.
  onBootReady: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("boot-ready", handler);
    return () => ipcRenderer.removeListener("boot-ready", handler);
  },
  bootComplete: () => ipcRenderer.send("boot-complete"),
  // Persist the user's resolved theme accent (a hex string) so the next launch's
  // boot splash (loading.html) can paint in the user's chosen colour. Read back
  // by main.js and injected as a query param — see showLoadingThenConnect.
  setThemeAccent: (hex) => ipcRenderer.send("theme-accent-changed", String(hex || "")),
  // Dev mode IPC: renderer signals main process to show/hide DevTools menu item.
  setDevMode: (enabled) => ipcRenderer.send("dev-mode-changed", !!enabled),
  // Windows custom titlebar: ask the main process to anchor one of the
  // existing native application submenus below its renderer-side trigger.
  showAppMenu: (id, anchor, mode) => ipcRenderer.send("app-menu:popup", id, anchor, mode),
  // App-menu navigation: main.js sends an in-app path ("/settings",
  // "/settings?tab=about") when the user picks Settings…/About from the
  // native application menu; the SPA routes to it (see App.tsx).
  onNavigate: (cb) => {
    const handler = (_e, path) => cb(path);
    ipcRenderer.on("navigate", handler);
    return () => ipcRenderer.removeListener("navigate", handler);
  },
  onFullScreenChanged: (callback) => {
    const handler = (_event, isFullScreen) => callback(!!isFullScreen);
    ipcRenderer.on("fullscreen-changed", handler);
    return () => ipcRenderer.removeListener("fullscreen-changed", handler);
  },
  // Dock/taskbar badge (RFC notification bus Phase 4): the renderer pushes
  // its unread (critical+default) count; main.js applies app.setBadgeCount.
  // No-op on platforms without badge support (Windows) -- Electron handles it.
  setBadgeCount: (count) => ipcRenderer.send("badge:set", count),
});

// Native zoom bridge for the Settings > Display "Zoom Level" stepper.
// Chromium's per-origin zoom (the thing Cmd/Ctrl +/- changes) is not
// reachable from page JS, so the renderer round-trips through main.js.
// All three calls resolve with the applied zoom factor. Absent in plain
// browsers — the renderer treats a missing bridge as "zoom not controllable"
// and shows a shortcut hint instead of the stepper.
contextBridge.exposeInMainWorld("zoomAPI", {
  get: () => ipcRenderer.invoke("zoom:get"),
  set: (factor) => ipcRenderer.invoke("zoom:set", factor),
  step: (dir) => ipcRenderer.invoke("zoom:step", dir),
});

// Desktop auto-update bridge. Drives the in-app UpdateModal + Settings > About.
// onState pushes update lifecycle events ({state, version, notes, channel});
// check/install/getInfo are promise-based round-trips to the main process.
contextBridge.exposeInMainWorld("updateAPI", {
  onState: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("update-state", handler);
    return () => ipcRenderer.removeListener("update-state", handler);
  },
  check: () => ipcRenderer.invoke("update:check"),
  download: () => ipcRenderer.invoke("update:download"),
  install: () => ipcRenderer.invoke("update:install"),
  getInfo: () => ipcRenderer.invoke("update:get-info"),
  // Channel switcher (Settings > About): "" follows the build stamp,
  // "insider"|"stable" opts the production app onto that lane.
  setChannel: (channel) => ipcRenderer.invoke("update:set-channel", channel),
});
