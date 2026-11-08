"use strict";

/**
 * app-window-preload.js — the bridge a hosted app window's page gets.
 *
 * Loaded only into windows app-window-host.js creates, which run sandboxed with
 * context isolation and no Node. It exposes `window.kirocrewDesktopWindow`: a few
 * fire-and-forget calls and one event, nothing else. The identity it reports is
 * informational — the host resolves every message back to the window that sent
 * it and acts only within that window's app declaration, so a page that lies
 * about who it is changes nothing.
 */

const { contextBridge, ipcRenderer } = require("electron");

const CHANNELS = {
  ready: "app-window:ready",
  close: "app-window:close",
  open: "app-window:open",
  hitRegions: "app-window:set-hit-regions",
  focusable: "app-window:set-focusable",
  shortcut: "app-window:shortcut",
};

/** `--kc-app-window=<app>/<id>/<kind>/<level>`, set by the host on this window. */
function identityFrom(argv) {
  const prefix = "--kc-app-window=";
  const arg = (Array.isArray(argv) ? argv : []).find((a) => typeof a === "string" && a.startsWith(prefix));
  const [app = "", id = "", kind = "", level = "0"] = arg ? arg.slice(prefix.length).split("/") : [];
  return { app, window: id, kind, level: Number.parseInt(level, 10) || 0 };
}

function createBridge(ipc, identity) {
  return Object.freeze({
    level: identity.level,
    app: identity.app,
    window: identity.window,
    kind: identity.kind,
    /** Reveal this window. Call once the first frame is painted. */
    ready: () => ipc.send(CHANNELS.ready),
    /** Close this window. */
    close: () => ipc.send(CHANNELS.close),
    /** Open another window this app declares, by its id. */
    open: (windowId) => ipc.send(CHANNELS.open, String(windowId)),
    /** Overlay only: the boxes, in page pixels, that take the pointer. The rest clicks through. */
    setHitRegions: (regions) => ipc.send(
      CHANNELS.hitRegions,
      Array.isArray(regions)
        ? regions.map((r) => ({
          x: Number(r && r.x), y: Number(r && r.y),
          width: Number(r && r.width), height: Number(r && r.height),
        }))
        : [],
    ),
    /** Overlay only: take keyboard focus (true) or hand it back (false). */
    setFocusable: (focusable) => ipc.send(CHANNELS.focusable, focusable === true),
    /** Called with a declared shortcut's id when the user presses it. Returns an unsubscribe. */
    onShortcut: (cb) => {
      if (typeof cb !== "function") return () => {};
      const listener = (_event, id) => {
        if (typeof id === "string") cb(id);
      };
      ipc.on(CHANNELS.shortcut, listener);
      return () => ipc.removeListener(CHANNELS.shortcut, listener);
    },
  });
}

if (contextBridge && ipcRenderer) {
  contextBridge.exposeInMainWorld("kirocrewDesktopWindow", createBridge(ipcRenderer, identityFrom(process.argv)));
}

// A sandboxed preload need not have `module`; the guard keeps the tests' handle.
if (typeof module !== "undefined") module.exports = { CHANNELS, createBridge, identityFrom };
