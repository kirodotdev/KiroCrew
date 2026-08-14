const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("kirocrew", {
  platform: process.platform,
  isElectron: true,
  // Absolute filesystem path for a File the OS handed the renderer (drag-drop,
  // file input). Browsers deliberately hide real paths, and Electron removed
  // File.path, so webUtils in the preload is the only remaining bridge. Returns
  // "" when no path can be resolved (synthetic File) so callers can treat any
  // falsy result as "no path available" and keep their browser fallback.
  getPathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file) || "";
    } catch {
      return "";
    }
  },
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
  // Report the user's dark/light mode PREFERENCE ("system" | "dark" | "light")
  // so main.js can set `nativeTheme.themeSource` to match. Must be the
  // preference, not the resolved mode: pinning themeSource to dark/light also
  // pins `prefers-color-scheme`, which is what Auto resolves through.
  setThemeMode: (pref) => ipcRenderer.send("theme-mode-changed", String(pref || "")),
  // Report the RESOLVED dark/light mode ("dark" | "light") so main.js can sync
  // the Windows titleBarOverlay colors. Separate from setThemeMode because that
  // carries the preference (system/dark/light) while this carries the outcome.
  setTitleBarOverlayTheme: (mode) => ipcRenderer.send("titlebar-overlay-theme", String(mode || "")),
  // Dev mode IPC: renderer signals main process to show/hide DevTools menu item.
  setDevMode: (enabled) => ipcRenderer.send("dev-mode-changed", !!enabled),
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
  // Mic-denial recovery. The renderer only ever sees getUserMedia's
  // NotAllowedError — it cannot tell "the OS refused" from "Electron refused",
  // and it cannot open System Settings itself. So when a mic capture is denied
  // it pings the main process, which checks the real OS status and shows the
  // Privacy-pane dialog only if macOS is actually the one saying no. Without
  // this the toast is a dead end: macOS never re-prompts after a denial.
  reportMicDenied: () => ipcRenderer.send("mic:denied"),
  // The system-wide summon hotkey as ACTUALLY bound by main.js (registration
  // can degrade to the default or to nothing when a key is taken), so the
  // shortcuts UI advertises what really works. Resolves
  // { accelerator, default } — accelerator is "" when nothing is bound.
  getGlobalHotkey: () => ipcRenderer.invoke("global-hotkey:get"),
});

// Local-gateway switch for the Settings > Developer toggle. The choice lives in
// the app's own config, which page JS cannot read or write, so the renderer
// round-trips through main.js. Both calls resolve with the stored value.
// Absent in plain browsers and in the PWA — the renderer hides the toggle when
// the bridge is missing, since a browser tab has no local gateway to manage.
contextBridge.exposeInMainWorld("localGatewayAPI", {
  get: () => ipcRenderer.invoke("local-gateway:get"),
  set: (enabled) => ipcRenderer.invoke("local-gateway:set", !!enabled),
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

// Native browser panel bridge. The Browser side panel is a real embedded
// Chromium view owned by the main process, composited over the panel rect —
// not an iframe and not in the React tree. So the renderer has to (a) report
// the rectangle it laid out, and (b) report when one of its own overlays covers
// that rectangle, since the native layer paints above the SPA and would occlude
// it. Every call resolves with the view's state ({open, visible, bounds, url}).
//
// Every call takes a `panelId` — the dashboard renders one Browser panel per
// chat session, and each gets its own view, control plane and agent-act
// authorization. A single per-window slot let two sessions clobber each other.
//
// Absent in a plain browser — the renderer treats a missing bridge as "no native
// panel available" and falls back to the streamed-frame transport.
contextBridge.exposeInMainWorld("browserAPI", {
  open: (panelId, url) => ipcRenderer.invoke("browser:open", panelId, url),
  navigate: (panelId, url) => ipcRenderer.invoke("browser:navigate", panelId, url),
  setBounds: (panelId, rect, viewport) =>
    ipcRenderer.invoke("browser:set-bounds", panelId, rect, viewport),
  setOverlayActive: (panelId, active) =>
    ipcRenderer.invoke("browser:set-overlay", panelId, !!active),
  // Hides the view without destroying the page — for a panel whose tab is not
  // currently visible. `close` is the destructive one.
  setInactive: (panelId, value) =>
    ipcRenderer.invoke("browser:set-inactive", panelId, !!value),
  close: (panelId) => ipcRenderer.invoke("browser:close", panelId),
  getState: (panelId) => ipcRenderer.invoke("browser:get-state", panelId),
  // Agent control. `setAgentAct` mirrors the dashboard's general "let the agent
  // act" authorization into the main process, which evaluates the control gate
  // itself on every owner transition. `control` takes a CLOSED verb set, never a
  // raw CDP method — the surface cannot be widened from here.
  setAgentAct: (panelId, enabled) =>
    ipcRenderer.invoke("browser:set-agent-act", panelId, !!enabled),
  setControlOwner: (panelId, owner) =>
    ipcRenderer.invoke("browser:set-control-owner", panelId, owner),
  getControl: (panelId) => ipcRenderer.invoke("browser:get-control", panelId),
  control: (panelId, op, args) => ipcRenderer.invoke("browser:control", panelId, op, args),
  // Declares that a chat session may host a browser panel, so the agent command
  // channel polls for it even before the Browser tab is ever opened. Grants no
  // authorization — authorization to drive the built-in browser is Browser Mode
  // (the Settings toggle), and the gate is just the view precondition. This is
  // the ONLY session-declaration channel: reachability is simply the active slots
  // the renderer declares here.
  trackSession: (panelId, tracked) =>
    ipcRenderer.invoke("browser:track-session", panelId, !!tracked),
  // Events carry their own panelId so a listener can ignore other panels'.
  // `onAgentOpened` fires when the AGENT bootstrapped a native view for a
  // session whose Browser panel may not be mounted yet — the dashboard must
  // surface that panel so it can report bounds, or the page would render to
  // nowhere.
  onAgentOpened: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("browser:agent-opened", handler);
    return () => ipcRenderer.removeListener("browser:agent-opened", handler);
  },
  onDidNavigate: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("browser:did-navigate", handler);
    return () => ipcRenderer.removeListener("browser:did-navigate", handler);
  },
  onTitleUpdated: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("browser:title-updated", handler);
    return () => ipcRenderer.removeListener("browser:title-updated", handler);
  },
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
