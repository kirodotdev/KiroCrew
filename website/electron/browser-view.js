// Native browser panel — the embedded Chromium view behind the dashboard's
// Browser side panel.
//
// Instead of mirroring a headless browser as JPEG screenshots, the panel hosts a
// real `WebContentsView` positioned over the side-panel rectangle. The view *is*
// the panel: native paint, real events, downloads, video, no letterboxing.
//
// Two things about this are load-bearing and were confirmed empirically by the
// design spike (docs/system-specs/modules/browser.md §10–§12):
//
//   1. A view does NOT have to fill the window. `setBounds()` honours an
//      arbitrary rectangle, which is what makes an in-panel embed possible.
//      Every other view in main.js is full-window, so this is the first
//      partial-rect view in the app.
//   2. A native view is composited ABOVE the SPA — it is not in the React tree
//      and cannot be layered by CSS. Anything the dashboard draws over the panel
//      rect (modal, dropdown, toast, drag preview) would be occluded, so the
//      renderer tells us when an overlay is up and we hide the view for its
//      duration. `computeVisible` is that decision, kept pure and tested.
//
// Security posture: this view renders arbitrary untrusted web content, so it is
// deliberately the most locked-down webContents in the app.
//   • NO preload. The dashboard's preload exposes `kirocrew`/`electronAPI`/
//     `zoomAPI` bridges; attaching it here would hand those to any website.
//   • sandbox on, contextIsolation on, nodeIntegration off, webviewTag off.
//   • Navigation is restricted to http(s) — `normalizeUrl` rejects file:,
//     data:, javascript: and friends, so a compromised renderer cannot point
//     the view at local files.
//   • Popups are denied; the host decides whether to open them externally.
//
// The view uses the SHARED default session (per the design decision): cookies
// and logged-in state carry across, matching normal browser expectations.
// Cookie isolation remains per-origin, so sharing the partition does not expose
// the dashboard's own cookies to page content.

/** Schemes the embedded view may ever navigate to. */
const ALLOWED_PROTOCOLS = new Set(["http:", "https:"]);
const BLANK_URL = "about:blank";

// Every webContents that renders UNTRUSTED web content, i.e. the embedded
// browser views. A WeakSet so entries vanish with the view.
//
// This exists because the app's permission handlers are installed on
// `session.defaultSession` (main.js) and the embedded view SHARES that session
// by design. Those handlers grant the microphone to any `localhost` origin,
// which was a sound proxy for "the dashboard" back when the dashboard was the
// only thing that ever loaded in this session. A general-purpose browser breaks
// that assumption: browsing to any `http://localhost:<port>` page would inherit
// the dashboard's grant. Registering the view here lets the permission handlers
// refuse it by identity, before any origin heuristic runs.
const untrustedContents = new WeakSet();

/** True when `wc` renders untrusted web content (an embedded browser view). */
function isUntrustedContents(wc) {
  return !!wc && untrustedContents.has(wc);
}

/**
 * Coerce renderer-supplied input into a URL the embedded view may load.
 * Returns the normalized absolute URL, or `null` when it must be refused.
 *
 * Bare hosts are accepted and upgraded to https so the panel behaves like a
 * browser address bar ("example.com" works). Everything that is not
 * http/https/about:blank is refused — see the security note above.
 */
function normalizeUrl(input) {
  if (typeof input !== "string") return null;
  const raw = input.trim();
  if (!raw) return null;
  if (raw === BLANK_URL) return BLANK_URL;

  const parse = (candidate) => {
    try {
      return new URL(candidate);
    } catch {
      return null;
    }
  };

  let url = parse(raw);
  // No scheme at all ("example.com", "localhost:5173/x") — try https.
  // `new URL` also mis-parses "localhost:5173" as protocol "localhost:", so
  // treat a non-allowed scheme with no slashes as scheme-less too.
  if (!url || (!ALLOWED_PROTOCOLS.has(url.protocol) && !raw.includes("//"))) {
    const upgraded = parse(`https://${raw}`);
    if (upgraded && ALLOWED_PROTOCOLS.has(upgraded.protocol)) return upgraded.href;
    return null;
  }
  return ALLOWED_PROTOCOLS.has(url.protocol) ? url.href : null;
}

/**
 * Clamp a renderer-reported panel rectangle into the window's content area.
 *
 * The renderer measures the panel in CSS pixels and can report stale or
 * malformed values (mid-resize, a detached node, a NaN from a layout race), so
 * every field is validated. Returns integer bounds, or `null` when the rect has
 * no drawable area — the caller treats that as "hide".
 */
function normalizeBounds(rect, contentBounds) {
  if (!rect || typeof rect !== "object") return null;
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : NaN);
  const maxW = Math.max(0, Math.trunc(num(contentBounds && contentBounds.width) || 0));
  const maxH = Math.max(0, Math.trunc(num(contentBounds && contentBounds.height) || 0));
  if (maxW <= 0 || maxH <= 0) return null;

  const x = num(rect.x);
  const y = num(rect.y);
  const w = num(rect.width);
  const h = num(rect.height);
  if ([x, y, w, h].some(Number.isNaN)) return null;

  const left = Math.min(Math.max(0, Math.round(x)), maxW);
  const top = Math.min(Math.max(0, Math.round(y)), maxH);
  const width = Math.min(Math.max(0, Math.round(w)), maxW - left);
  const height = Math.min(Math.max(0, Math.round(h)), maxH - top);
  if (width <= 0 || height <= 0) return null;
  return { x: left, y: top, width, height };
}

/** True when two bounds are identical — used to skip redundant setBounds churn
 *  during a resize drag, which fires continuously. */
function boundsEqual(a, b) {
  if (!a || !b) return a === b;
  return a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;
}

/**
 * CSS pixels → device-independent pixels.
 *
 * The renderer measures the panel with `getBoundingClientRect()`, which is in
 * CSS pixels, but `view.setBounds()` takes DIP relative to the window's content
 * area. Those differ whenever the dashboard's Chromium zoom factor is not 1 (the
 * app ships a zoom ladder — see zoom.js), so a zoomed dashboard would otherwise
 * place the native view at the wrong offset and size.
 *
 * The scale is derived rather than passed: the renderer reports its own viewport
 * size in CSS px, and the main process knows the same area's DIP size, so their
 * ratio *is* the zoom factor. Falls back to 1 when either side is unusable, and
 * refuses implausible ratios so a bad report cannot fling the view off-window.
 */
function deriveScale(viewport, contentBounds) {
  const vw = viewport && typeof viewport.width === "number" ? viewport.width : NaN;
  const cw = contentBounds && typeof contentBounds.width === "number" ? contentBounds.width : NaN;
  if (!Number.isFinite(vw) || !Number.isFinite(cw) || vw <= 0 || cw <= 0) return 1;
  const scale = cw / vw;
  if (!Number.isFinite(scale) || scale < 0.25 || scale > 4) return 1;
  return scale;
}

/** Apply a scale factor to a rect, leaving validation to normalizeBounds. */
function scaleRect(rect, scale) {
  if (!rect || typeof rect !== "object" || scale === 1) return rect;
  const mul = (v) => (typeof v === "number" && Number.isFinite(v) ? v * scale : v);
  return { x: mul(rect.x), y: mul(rect.y), width: mul(rect.width), height: mul(rect.height) };
}

/**
 * Should the native view be on screen right now?
 *
 * Hidden whenever the SPA has an overlay over the panel rect, because the
 * native layer would paint on top of it (see header note 2). Also hidden while
 * the panel is INACTIVE (its side-panel tab is not the visible one) and with no
 * usable bounds, which is how "panel collapsed" arrives.
 *
 * Note `inactive` hides but deliberately does NOT destroy: switching side-panel
 * tabs away and back must not lose the page's unsaved form input, scroll
 * position or history. Only an explicit close (or unmount) releases the view.
 */
function computeVisible(state) {
  const s = state || {};
  if (!s.open) return false;
  if (s.overlayActive) return false;
  if (s.inactive) return false;
  if (!s.bounds) return false;
  return true;
}

/**
 * Build the per-window manager for the embedded browser view.
 *
 * Dependencies are injected so the module stays unit-testable without a live
 * Electron window:
 *   createView()      -> a WebContentsView-like object
 *   getContentBounds() -> { width, height } of the host window's content area
 *   addView(view) / removeView(view) -> attach/detach from the window
 *   onEvent(name, payload) -> notify the host (navigation state, denied popups)
 */
function createBrowserViewManager(deps) {
  const {
    createView,
    getContentBounds,
    addView,
    removeView,
    onEvent = () => {},
    onCreate = () => {},
  } = deps || {};

  let view = null;
  let bounds = null;
  let open = false;
  let overlayActive = false;
  let inactive = false;
  let currentUrl = BLANK_URL;
  let appliedBounds = null;
  // Last raw rect + viewport the renderer reported, kept so bounds can be
  // re-derived after a window resize or a zoom change with no new report.
  let lastReport = null;

  /** Re-derive DIP bounds from the last renderer report against the window's
   *  current content area. */
  function resolveBounds() {
    if (!lastReport) return null;
    const content = getContentBounds();
    const scale = deriveScale(lastReport.viewport, content);
    return normalizeBounds(scaleRect(lastReport.rect, scale), content);
  }

  const emit = (name, payload) => {
    try {
      onEvent(name, payload);
    } catch {
      /* a listener must never break view lifecycle */
    }
  };

  function state() {
    return { open, overlayActive, inactive, bounds, url: currentUrl, visible: isVisible() };
  }

  function isVisible() {
    return computeVisible({ open, overlayActive, inactive, bounds });
  }

  /** Apply visibility + geometry. Cheap and idempotent: called on every bounds
   *  report, overlay toggle and window resize. */
  function sync() {
    if (!view) return;
    const visible = isVisible();
    // Prefer setVisible (present on the Electron versions we ship); fall back
    // to detaching the child view on anything older.
    if (typeof view.setVisible === "function") {
      view.setVisible(visible);
    } else if (!visible) {
      removeView(view);
    } else {
      addView(view);
    }
    if (visible && bounds && !boundsEqual(bounds, appliedBounds)) {
      view.setBounds(bounds);
      appliedBounds = bounds;
    }
  }

  function ensureView() {
    if (view) return view;
    view = createView();
    appliedBounds = null;
    addView(view);
    const wc = view.webContents;
    if (wc) {
      // Mark BEFORE anything can load, so the permission handlers can never see
      // this webContents as trusted (see `untrustedContents` above).
      untrustedContents.add(wc);
      // Popups: never open a second native window for page-driven
      // window.open — hand the URL to the host instead.
      if (typeof wc.setWindowOpenHandler === "function") {
        wc.setWindowOpenHandler(({ url }) => {
          const safe = normalizeUrl(url);
          if (safe) emit("open-external", { url: safe });
          return { action: "deny" };
        });
      }
      // Refuse any navigation that is not http(s) — including ones initiated by
      // page content rather than by us.
      if (typeof wc.on === "function") {
        wc.on("will-navigate", (event, url) => {
          if (!normalizeUrl(url)) event.preventDefault();
        });
        wc.on("did-navigate", (_event, url) => {
          currentUrl = url;
          emit("did-navigate", { url, title: safeTitle(wc) });
        });
        wc.on("page-title-updated", (_event, title) => {
          emit("title-updated", { url: currentUrl, title });
        });
      }
    }
    // Host-side chrome that needs real Electron APIs (context menu, and
    // anything else that must not be imported into this module to keep it
    // testable without a live session).
    onCreate(view);
    return view;
  }

  function safeTitle(wc) {
    try {
      return wc.getTitle();
    } catch {
      return "";
    }
  }

  return {
    /** Open (creating the view on first use) and navigate. Returns state. */
    open(url) {
      const target = normalizeUrl(url) || BLANK_URL;
      ensureView();
      open = true;
      currentUrl = target;
      sync();
      view.webContents.loadURL(target);
      return state();
    },

    /** Navigate an already-open view. Refuses disallowed URLs without
     *  disturbing the current page. */
    navigate(url) {
      const target = normalizeUrl(url);
      if (!target) return { ...state(), refused: true };
      if (!open) return this.open(target);
      currentUrl = target;
      view.webContents.loadURL(target);
      return state();
    },

    /** The renderer reports the panel rectangle (CSS px, viewport-relative)
     *  together with its own viewport size, from which the zoom scale is
     *  derived. Both are retained so a later window resize or zoom change can
     *  re-derive without another report. */
    setPanelBounds(rect, viewport) {
      lastReport = { rect, viewport };
      bounds = resolveBounds();
      sync();
      return state();
    },

    /** The renderer reports whether an overlay covers the panel rect. */
    setOverlayActive(active) {
      overlayActive = Boolean(active);
      sync();
      return state();
    },

    /**
     * The renderer reports whether this panel is the visible side-panel tab.
     *
     * Distinct from close(): going inactive HIDES the view and keeps the page
     * alive, so switching tabs away and back preserves unsaved form input,
     * scroll position and history. Closing destroys it.
     */
    setInactive(value) {
      inactive = Boolean(value);
      sync();
      return state();
    },

    /** Re-clamp against the current window size — call on resize/move/
     *  fullscreen, where the content area changes under us. */
    refreshBounds() {
      bounds = resolveBounds();
      sync();
      return state();
    },

    /** Close the panel and release the view entirely (the browser should not
     *  keep running once the user closes it). */
    close() {
      open = false;
      overlayActive = false;
      inactive = false;
      appliedBounds = null;
      bounds = null;
      lastReport = null;
      currentUrl = BLANK_URL;
      if (view) {
        const doomed = view;
        view = null;
        try {
          removeView(doomed);
        } catch { /* window may be tearing down */ }
        try {
          if (doomed.webContents) doomed.webContents.close();
        } catch { /* already gone */ }
      }
      return state();
    },

    getState: state,
    /** The live view's webContents, or null when the panel was never opened.
     *  This is the control plane's handle onto the page (browser-control.js). */
    getWebContents: () => (view ? view.webContents : null),
    /** Test seam: the live view, or null when the panel was never opened. */
    _view: () => view,
  };
}

module.exports = {
  ALLOWED_PROTOCOLS,
  BLANK_URL,
  isUntrustedContents,
  normalizeUrl,
  normalizeBounds,
  boundsEqual,
  deriveScale,
  scaleRect,
  computeVisible,
  createBrowserViewManager,
};
