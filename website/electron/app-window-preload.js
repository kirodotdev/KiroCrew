"use strict";

/**
 * app-window-preload.js — the bridge a hosted app window's page gets.
 *
 * Loaded only into windows app-window-host.js creates, which run sandboxed with
 * context isolation and no Node. It exposes `window.kirocrewDesktopWindow`: a few
 * fire-and-forget calls, one answered call (`showSession`), a few events and (level
 * 2) the capability calls, nothing else. Every capability call is answered only for
 * a window whose app declares and holds that capability; elsewhere a reveal does
 * nothing and the others resolve to `false` or `null`. The identity and
 * `capabilities` it reports are
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
  closeAll: "app-window:close-all",
  send: "app-window:send",
  broadcast: "app-window:broadcast",
  message: "app-window:message",
  windowChange: "app-window:window-change",
  placed: "app-window:placed",
  beginDrag: "app-window:begin-drag",
  endDrag: "app-window:end-drag",
  active: "app-window:active",
  drag: "app-window:drag",
  showSession: "app-window:show-session",
  openExternal: "app-window:open-external",
  revealFile: "app-window:reveal-file",
  openImage: "app-window:open-image",
  listGateways: "app-window:list-gateways",
  chooseGateway: "app-window:choose-gateway",
};
const CAPABILITIES = ["screenCapture", "localFiles", "gatewayChoice"];

/** Subscribe *cb* to one host event; *map* turns its arguments into the callback's. */
function subscribe(ipc, channel, cb, map) {
  if (typeof cb !== "function") return () => {};
  const listener = (_event, ...args) => {
    const out = map(...args);
    if (out) cb(...out);
  };
  ipc.on(channel, listener);
  return () => ipc.removeListener(channel, listener);
}

function box(r) {
  return {
    x: Number(r && r.x), y: Number(r && r.y),
    width: Number(r && r.width), height: Number(r && r.height),
  };
}

/** `--kc-app-window=<app>/<id>/<kind>/<level>[/<capability>,...]`, set by the host on this window. */
function identityFrom(argv) {
  const prefix = "--kc-app-window=";
  const arg = (Array.isArray(argv) ? argv : []).find((a) => typeof a === "string" && a.startsWith(prefix));
  const [app = "", id = "", kind = "", level = "0", caps = ""] = arg ? arg.slice(prefix.length).split("/") : [];
  const listed = caps.split(",");
  return {
    app,
    window: id,
    kind,
    level: Number.parseInt(level, 10) || 0,
    capabilities: CAPABILITIES.filter((cap) => listed.includes(cap)),
  };
}

function createBridge(ipc, identity) {
  return Object.freeze({
    level: identity.level,
    app: identity.app,
    window: identity.window,
    kind: identity.kind,
    /** The capabilities this window was opened with (level 2). */
    capabilities: Object.freeze([...(identity.capabilities || [])]),
    /** Reveal this window. Call once the first frame is painted. */
    ready: () => ipc.send(CHANNELS.ready),
    /** Close this window, or another window this app declares, by its id. */
    close: (windowId) => (windowId === undefined
      ? ipc.send(CHANNELS.close)
      : ipc.send(CHANNELS.close, String(windowId))),
    /** Close every window of this app (it reopens what it declares `openOnEnable` while enabled). */
    closeAll: () => ipc.send(CHANNELS.closeAll),
    /**
     * Open another window this app declares, by its id. `{ anchor }`, a box in this
     * page's pixels, opens a panel beside it.
     */
    open: (windowId, options) => (options && options.anchor
      ? ipc.send(CHANNELS.open, String(windowId), { anchor: box(options.anchor) })
      : ipc.send(CHANNELS.open, String(windowId))),
    /** Overlay only: the boxes, in page pixels, that take the pointer. The rest clicks through. */
    setHitRegions: (regions) => ipc.send(
      CHANNELS.hitRegions,
      Array.isArray(regions) ? regions.map(box) : [],
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
    /** Send a JSON message to another window this app declares (to the avatar's copy of a spanning overlay). */
    send: (windowId, message) => ipc.send(CHANNELS.send, String(windowId), message),
    /** Send a JSON message to every other open window of this app. */
    broadcast: (message) => ipc.send(CHANNELS.broadcast, message),
    /** Called with (fromWindowId, message) for a message another of this app's windows sent. */
    onMessage: (cb) => subscribe(ipc, CHANNELS.message, cb, (from, message) => (
      typeof from === "string" ? [from, message] : null
    )),
    /** Called with (windowId, open) when another of this app's windows opens or closes. */
    onWindowChange: (cb) => subscribe(ipc, CHANNELS.windowChange, cb, (id, open) => (
      typeof id === "string" ? [id, open === true] : null
    )),
    /** Panel only: called with `{ side }` when the panel was placed beside an anchor. */
    onPlaced: (cb) => subscribe(ipc, CHANNELS.placed, cb, (placement) => (
      placement && typeof placement === "object" ? [{ side: placement.side === "left" ? "left" : "right" }] : null
    )),
    /**
     * Spanning overlay only: called with `{ active, x?, y?, dragging? }` when this
     * copy starts or stops hosting the avatar; `x`/`y` place it on a hand-off.
     */
    onActive: (cb) => subscribe(ipc, CHANNELS.active, cb, (state) => (
      state && typeof state === "object" ? [{ ...state, active: state.active === true }] : null
    )),
    /**
     * Spanning overlay only, from the copy hosting the avatar: the user started
     * dragging it. `offsetX`/`offsetY` are the cursor's offset into the avatar box
     * of `width` x `height` page pixels.
     */
    beginDrag: (drag) => ipc.send(CHANNELS.beginDrag, {
      offsetX: Number(drag && drag.offsetX), offsetY: Number(drag && drag.offsetY),
      width: Number(drag && drag.width), height: Number(drag && drag.height),
    }),
    /** Spanning overlay only, from any copy: the drag was released. */
    endDrag: () => ipc.send(CHANNELS.endDrag),
    /**
     * Spanning overlay only: called with ("start") on every copy when a drag
     * starts, and with ("move", x, y) / ("end", x, y) on the copy hosting it.
     */
    onDrag: (cb) => subscribe(ipc, CHANNELS.drag, cb, (phase, x, y) => (
      typeof phase === "string" ? [phase, x, y] : null
    )),
    /**
     * Raise the dashboard on a session (or just raise it, for ""). Resolves true
     * only when the dashboard is there to show it. Answered only while the user
     * is acting in this window, never from a background window.
     */
    showSession: (slotKey) => ipc.invoke(CHANNELS.showSession, String(slotKey || "")),
    /** Open an https link in the user's browser; the same rule as `showSession`. */
    openExternal: (url) => ipc.send(CHANNELS.openExternal, String(url)),
    /** `localFiles`: show an existing local file in the system file manager. */
    revealFile: (filePath) => ipc.send(CHANNELS.revealFile, String(filePath)),
    /** `localFiles`: open a local image in the system viewer. Resolves whether it opened. */
    openImage: (filePath) => ipc.invoke(CHANNELS.openImage, String(filePath)).then((ok) => ok === true, () => false),
    /**
     * `gatewayChoice`: the operator's crews, as `{state, gateways, chosen, showing}`,
     * each gateway `{id, name, state, connected, appEnabled}`. Resolves null when refused.
     */
    listGateways: () => ipc.invoke(CHANNELS.listGateways).catch(() => null),
    /**
     * `gatewayChoice`: load this app's windows from crew *id* ("self" for this
     * computer's gateway). Resolves `{ok, chosen, showing}` once the windows moved.
     */
    chooseGateway: (id) => ipc.invoke(CHANNELS.chooseGateway, String(id)).catch(() => ({ ok: false })),
  });
}

if (contextBridge && ipcRenderer) {
  contextBridge.exposeInMainWorld("kirocrewDesktopWindow", createBridge(ipcRenderer, identityFrom(process.argv)));
}

// A sandboxed preload need not have `module`; the guard keeps the tests' handle.
if (typeof module !== "undefined") module.exports = { CHANNELS, createBridge, identityFrom };
