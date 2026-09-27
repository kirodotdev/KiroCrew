"use strict";

const { createBrowserViewManager } = require("../../browser-view");
const {
  canAgentControl,
  isLoopbackUrl,
  mayBootstrapView,
  createControlPlane,
  OWNER,
} = require("../../browser-control");
const { createBrowserOps } = require("../../browser-ops");
const { createAgentCommandChannel } = require("../../browser-agent-channel");
const { attachContextMenu } = require("../../context-menu");
const { openExternalSafely } = require("../../external-scheme");

/**
 * The native browser panels embedded in one dashboard window: one
 * WebContentsView plus one CDP control plane per panel, and the agent command
 * channel that drives them from the gateway. The renderer owns layout; this
 * process owns every WebContents and every privilege.
 *
 * Panels live in a persistent partition that is isolated from the dashboard's
 * cookie jar, and a panel's view is created only by an explicit open or by the
 * one agent verb (navigate) allowed to bootstrap it.
 */

function browserOpsFor(entry) {
  if (entry._ops) return entry._ops;
  entry._ops = createBrowserOps({
    sendCommand: (method, params) => entry.control.send(method, params),
    // The debugger object is stable for one WebContents, so this subscription
    // survives attach/detach and its bounded console buffer persists.
    subscribe: (handler) => {
      const wc = entry.manager.getWebContents();
      const dbg = wc && wc.debugger;
      if (dbg && typeof dbg.on === "function") {
        dbg.on("message", (_event, method, params) => handler(method, params));
      }
    },
  });
  return entry._ops;
}

async function dispatchBrowserOp(entry, op, args) {
  return browserOpsFor(entry).run(op, args);
}

/**
 * Give `win` its panel registry and start its agent command channel. Sets the
 * window properties the rest of the shell reads (`_mcBrowserPanel`,
 * `_mcBrowserPanels`, `_mcDestroyBrowserPanel`, `_mcReachableSessions`,
 * `_mcAgentChannel`) and returns the panel map.
 *
 * @param {object} win   the dashboard BaseWindow
 * @param {object} view  its dashboard WebContentsView
 */
function attachBrowserPanels(win, view, {
  WebContentsView,
  shell,
  partition: BROWSER_PARTITION,
  readInternalSecret,
  isGatewayLocalForWindow,
}) {
  const browserPanels = new Map();

  function browserPanel(panelId, { create = true } = {}) {
    const id = typeof panelId === "string" ? panelId.trim() : "";
    if (!id) return null;
    const existing = browserPanels.get(id);
    if (existing || !create) return existing || null;

    const entry = { id, agentAct: false };
    entry.manager = createBrowserViewManager({
      createView: () => new WebContentsView({
        webPreferences: {
          // Persistent for ordinary browser logins, but isolated from the
          // dashboard's host-scoped mc_token_<port> cookie jar.
          partition: BROWSER_PARTITION,
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
          webviewTag: false,
        },
      }),
      getContentBounds: () => win.getContentBounds(),
      // Keyboard focus belongs to exactly one child view of the BaseWindow.
      // When the embedded page holds it as its view is hidden or released,
      // the dashboard view takes it back — otherwise every text input in the
      // dashboard stays deaf while pointer events keep working.
      focusHost: () => {
        if (!win.isDestroyed() && !view.webContents.isDestroyed()) {
          view.webContents.focus();
        }
      },
      addView: (child) => win.contentView.addChildView(child),
      removeView: (child) => win.contentView.removeChildView(child),
      // Chrome the embedded page needs but the module must not import Electron
      // for: the shared right-click menu (spelling suggestions, cut/copy/paste,
      // Look Up, Copy Link Address). Safe for untrusted content — every item is
      // a plain edit role or a clipboard write, none reaches app state. No
      // origin is passed: an arbitrary site's same-origin pathname that happens
      // to exist on disk is not a local file.
      onCreate: (child) => attachContextMenu(child.webContents),
      onEvent: (name, payload) => {
        if (name === "open-external") {
          if (payload && payload.url) {
            openExternalSafely(
              shell.openExternal,
              payload.url,
              (message) => console.warn(`[browser-panel] ${message}`),
            );
          }
          return;
        }
        if (!view.webContents.isDestroyed()) {
          view.webContents.send(
            `browser:${name}`,
            { ...(payload || {}), panelId: id },
          );
        }
      },
    });

    // Display and CDP ownership are independent. At most one agent owner may
    // hold LIGHT, and all transitions are audited in this process.
    entry.control = createControlPlane({
      getWebContents: () => entry.manager.getWebContents(),
      onAudit: (event, detail) => {
        console.warn(`[browser-control] ${id} ${event} ${JSON.stringify(detail)}`);
      },
    });

    // Browser Mode is the authorization; the native-view existence check is
    // the remaining per-panel precondition. Do not reintroduce a second
    // session consent gate here.
    entry.gate = () => canAgentControl({
      agentActEnabled: true,
      viewOpen: entry.manager.getState().open,
    });

    browserPanels.set(id, entry);
    return entry;
  }

  function destroyBrowserPanel(id) {
    const entry = browserPanels.get(id);
    if (!entry) return;
    browserPanels.delete(id);
    try {
      void entry.control.release();
    } catch {
      // Mid-teardown.
    }
    try {
      entry.manager.close();
    } catch {
      // Mid-teardown.
    }
  }

  win._mcBrowserPanel = browserPanel;
  win._mcBrowserPanels = browserPanels;
  win._mcDestroyBrowserPanel = destroyBrowserPanel;

  // Reachability is distinct from mounted panels: a declared chat slot must
  // be polled so its first navigate can bootstrap the native view.
  const reachableSessions = new Set();
  win._mcReachableSessions = reachableSessions;

  win._mcAgentChannel = createAgentCommandChannel({
    fetchFn: (url, init) => fetch(url, init),
    getGatewayUrl: () => win._mcBackendUrl,
    // Re-read for every call; the secret rotates with each gateway boot.
    getSecret: () => readInternalSecret(),
    // The idle host-presence heartbeat must fire ONLY when the gateway is truly
    // on this machine — see isGatewayLocalForWindow for why loopback alone is
    // not sufficient and why the port must be the window's own.
    isGatewayLocal: () => isGatewayLocalForWindow(win),
    listPanelIds: () => {
      // Preserve the existing predicate exactly. In particular, do not
      // mechanically fold isGatewayLocal into this branch during extraction:
      // that is a policy change, not a module move.
      if (!isLoopbackUrl(win._mcBackendUrl)) return [];
      return [...new Set([...browserPanels.keys(), ...reachableSessions])];
    },
    dispatch: async (sessionKey, op, args) => {
      console.warn(`[browser-cmdbus] dispatch op=${op} session=${sessionKey}`);
      const bootstrapping = op === "navigate";
      const entry = browserPanel(sessionKey, { create: bootstrapping });
      if (!entry) {
        throw new Error(`no native browser panel for session ${sessionKey}`);
      }

      // Navigate is the one op allowed to satisfy an absent-view precondition.
      // The order is essential: opening after acquiring LIGHT would refuse the
      // first command before there was any view to acquire.
      if (bootstrapping && !entry.manager.getWebContents()) {
        const pre = entry.gate();
        if (!mayBootstrapView(pre)) {
          throw new Error(`browser control refused: ${pre.reason}`);
        }
        const opened = entry.manager.navigate(String((args && args.url) || ""));
        if (opened && opened.refused) {
          return {
            ok: false,
            code: "bad_url",
            error: `refused non-web URL: ${args && args.url}`,
          };
        }
        try {
          view.webContents.send("browser:agent-opened", {
            panelId: sessionKey,
            url: (opened && opened.url) || String((args && args.url) || ""),
          });
        } catch {
          // A torn-down dashboard must not fail the navigation itself.
        }
        const takenAfterOpen = await entry.control.setOwner(OWNER.LIGHT, entry.gate());
        if (takenAfterOpen.refused) {
          throw new Error(`browser control refused: ${takenAfterOpen.refused}`);
        }
        return {
          ok: true,
          url: (opened && opened.url) || String((args && args.url) || ""),
        };
      }

      const taken = await entry.control.setOwner(OWNER.LIGHT, entry.gate());
      if (taken.refused) {
        throw new Error(`browser control refused: ${taken.refused}`);
      }
      return dispatchBrowserOp(entry, op, args);
    },
    onError: (error, context) => {
      console.warn(`[browser-agent-channel] ${context}: ${error && error.message}`);
    },
  });
  win._mcAgentChannel.start();
  return browserPanels;
}

module.exports = { attachBrowserPanels, dispatchBrowserOp };
