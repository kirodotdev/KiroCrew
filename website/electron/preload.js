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
  onFullScreenChanged: (callback) => {
    const handler = (_event, isFullScreen) => callback(!!isFullScreen);
    ipcRenderer.on("fullscreen-changed", handler);
    return () => ipcRenderer.removeListener("fullscreen-changed", handler);
  },
  // Reports whether the 32px instance tab strip is the topmost row, so main
  // can center the native traffic lights against the strip (32px) rather than
  // the 52px header. See App.tsx macInstanceBarInset.
  setInstanceBarInset: (on) => ipcRenderer.send("instancebar-inset-changed", !!on),
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
});
