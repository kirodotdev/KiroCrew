"use strict";

const { createBrowserViewManager } = require("../../browser-view");
const { attachContextMenu } = require("../../context-menu");
const { openExternalSafely } = require("../../external-scheme");

/**
 * The native Preview panels embedded in one dashboard window: one
 * WebContentsView per panel, showing the person's own local dev server. The
 * renderer owns layout; this process owns every WebContents and every
 * privilege. Nothing here is driven by the agent: the agent's browser lives
 * behind the gateway and is shown in the chat, not in this view.
 *
 * Panels live in a persistent partition that is isolated from the dashboard's
 * cookie jar, and a panel's view is created only by an explicit open.
 */

/**
 * Give `win` its panel registry. Sets the window properties the rest of the
 * shell reads (`_mcBrowserPanel`, `_mcBrowserPanels`, `_mcDestroyBrowserPanel`)
 * and returns the panel map.
 *
 * @param {object} win   the dashboard BaseWindow
 * @param {object} view  its dashboard WebContentsView
 */
function attachBrowserPanels(win, view, {
  WebContentsView,
  shell,
  partition: BROWSER_PARTITION,
}) {
  const browserPanels = new Map();

  function browserPanel(panelId, { create = true } = {}) {
    const id = typeof panelId === "string" ? panelId.trim() : "";
    if (!id) return null;
    const existing = browserPanels.get(id);
    if (existing || !create) return existing || null;

    const entry = { id };
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
      // When the embedded page holds it as its view is hidden or released, or
      // takes it while already hidden, the dashboard view takes it back —
      // otherwise every text input in the dashboard stays deaf while pointer
      // events keep working.
      focusHost: () => {
        if (!win.isDestroyed() && !view.webContents.isDestroyed()) {
          view.webContents.focus();
        }
      },
      // The already-hidden case runs only while this window is the active
      // one, so a background load never brings the window forward.
      hostActive: () => !win.isDestroyed() && win.isFocused(),
      // One line per hand-back, naming the view that held focus and the
      // trigger, so a recurrence of the deaf-composer symptom is decisive.
      log: (message) => console.warn(message),
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

    browserPanels.set(id, entry);
    return entry;
  }

  function destroyBrowserPanel(id) {
    const entry = browserPanels.get(id);
    if (!entry) return;
    browserPanels.delete(id);
    try {
      entry.manager.close();
    } catch {
      // Mid-teardown.
    }
  }

  win._mcBrowserPanel = browserPanel;
  win._mcBrowserPanels = browserPanels;
  win._mcDestroyBrowserPanel = destroyBrowserPanel;
  return browserPanels;
}

module.exports = { attachBrowserPanels };
