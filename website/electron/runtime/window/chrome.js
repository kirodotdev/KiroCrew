"use strict";

const { resolveThemeSource } = require("../../native-theme");
const { clampZoomFactor, stepZoomFactor } = require("../../zoom");
const { applyFocusModeChrome } = require("../../focus-chrome");
const { createFocusCursorWatch } = require("../../focus-cursor");
const {
  paintTitleBarOverlay,
  paintAllTitleBarOverlays,
} = require("../../windows-titlebar");

const HEADER_CSS_PX = 42;
const TRAFFIC_LIGHT_NATIVE_H = 12;
const TRAFFIC_LIGHT_Y_NUDGE = -4;

function trafficLightPositionForZoom(zoomFactor) {
  const stripPx = Math.round(HEADER_CSS_PX * zoomFactor);
  return {
    x: Math.round(16 * zoomFactor),
    y: Math.max(
      4,
      Math.round((stripPx - TRAFFIC_LIGHT_NATIVE_H) / 2) + TRAFFIC_LIGHT_Y_NUDGE,
    ),
  };
}

/**
 * The native chrome around every dashboard window: macOS traffic-light
 * placement and focus-mode visibility, the Windows title-bar overlay colours,
 * the process-wide native theme source, dashboard zoom, and the off-window
 * cursor watch a focus-mode reveal needs. Every value derives from the
 * dashboard's 42px header at the current zoom, so the native controls stay
 * centred in it.
 *
 * The facade owns window lookup (windowForWebContents) and injects it, so a
 * renderer request always resolves to its sender's own window.
 */
function createWindowChrome({
  BaseWindow,
  nativeTheme,
  screen,
  store,
  log: glog,
  isMac: IS_MAC,
  isWindows: IS_WINDOWS,
  windowForWebContents,
}) {
  // Reads the MODE PREFERENCE, not only the resolved mode. Setting themeSource
  // to dark/light also overrides prefers-color-scheme in renderers; feeding the
  // resolved value back would freeze the dashboard's Auto mode.
  function syncNativeTheme(view, win) {
    if (win.isDestroyed()) return;
    view.webContents.executeJavaScript(
      `JSON.stringify({`
        + `pref: document.documentElement.dataset.modePref || "",`
        + `mode: document.documentElement.dataset.mode || ""`
        + `})`,
    ).then((raw) => {
      let pref = "";
      let mode = "";
      try {
        const parsed = JSON.parse(raw);
        pref = parsed.pref || "";
        mode = parsed.mode || "";
      } catch {
        return;
      }
      nativeTheme.themeSource = resolveThemeSource(pref, mode);
      if (mode === "dark" || mode === "light") updateWindowsTitleBarOverlay(win, mode);
    }).catch(() => {});
  }

  function updateWindowsTitleBarOverlay(win, mode) {
    if (!IS_WINDOWS) return;
    const resolvedMode = mode || (nativeTheme.shouldUseDarkColors ? "dark" : "light");
    paintTitleBarOverlay(win, resolvedMode, HEADER_CSS_PX);
  }

  function positionTrafficLights(win) {
    if (!IS_MAC || !win || win.isDestroyed()) return;
    try {
      const zoom = win._mcView ? win._mcView.webContents.getZoomFactor() : 1;
      win.setWindowButtonPosition(trafficLightPositionForZoom(zoom));
    } catch {
      // Window is mid-teardown.
    }
  }

  // Chromium applies zoom per-origin, and the header strip scales with it, so
  // the native controls are re-placed after every zoom change.
  function trackZoomChrome(win, view) {
    if (IS_MAC) {
      positionTrafficLights(win);
      view.webContents.on("zoom-changed", () => {
        setTimeout(() => positionTrafficLights(win), 0);
      });
    }
    if (IS_WINDOWS) {
      updateWindowsTitleBarOverlay(win);
      view.webContents.on("zoom-changed", () => {
        setTimeout(() => updateWindowsTitleBarOverlay(win), 0);
      });
    }
  }

  function setThemeAccent(hex) {
    if (
      typeof hex === "string"
      && /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(hex)
    ) {
      store.set("themeAccent", hex);
    }
  }

  function handleFocusMode(sender, visible) {
    if (!IS_MAC) return;
    const win = windowForWebContents(sender);
    if (!win) return;
    // AppKit drops declared drag regions when button visibility mutates;
    // applyFocusModeChrome re-declares them after changing the native chrome.
    applyFocusModeChrome(win, visible, { positionTrafficLights });
  }

  // Off-window cursor distance for a focus-mode reveal. Every platform, unlike
  // handleFocusMode's macOS-only traffic lights: the renderer stops receiving
  // mouse events the moment the pointer crosses a window edge wherever it runs,
  // so the dismissal distance can only be measured here.
  const focusCursorWatch = createFocusCursorWatch({ screen, log: glog });

  function handleWatchFocusCursor(sender, watching) {
    const win = windowForWebContents(sender);
    if (!win) return;
    focusCursorWatch.watch(win, watching);
  }

  function setThemeMode(pref) {
    if (pref === "system" || pref === "dark" || pref === "light") {
      nativeTheme.themeSource = resolveThemeSource(pref, "");
    }
  }

  function setTitlebarMode(mode) {
    if (!IS_WINDOWS) return;
    const resolvedMode = mode === "dark" || mode === "light"
      ? mode
      : (nativeTheme.shouldUseDarkColors ? "dark" : "light");
    // Continue past framed modal windows that cannot accept an overlay; the
    // helper catches per-window so one dialog cannot strand siblings.
    paintAllTitleBarOverlays(
      BaseWindow.getAllWindows(),
      resolvedMode,
      HEADER_CSS_PX,
    );
  }

  function applyZoom(sender, factor) {
    sender.setZoomFactor(factor);
    for (const win of BaseWindow.getAllWindows()) {
      if (win._mcView) positionTrafficLights(win);
    }
    return factor;
  }

  function getZoom(sender) {
    return sender.getZoomFactor();
  }

  function setZoom(sender, factor) {
    return applyZoom(sender, clampZoomFactor(factor));
  }

  function stepZoom(sender, direction) {
    return applyZoom(
      sender,
      stepZoomFactor(sender.getZoomFactor(), direction > 0 ? +1 : -1),
    );
  }

  return {
    syncNativeTheme,
    positionTrafficLights,
    trackZoomChrome,
    setThemeAccent,
    handleFocusMode,
    handleWatchFocusCursor,
    setThemeMode,
    setTitlebarMode,
    getZoom,
    setZoom,
    stepZoom,
  };
}

module.exports = { HEADER_CSS_PX, trafficLightPositionForZoom, createWindowChrome };
