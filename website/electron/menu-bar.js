"use strict";

// Menu-bar visibility control. Deliberately Linux-only in behaviour:
// Windows keeps the native menu bar hard-hidden because the custom
// titlebar carries the hamburger menus already (Design Review of PR
// #10247 flagged the Windows regression risk — reversing the hard-hide
// commit `9c32ce965` would produce two stacked bars on Windows and this
// PR ships without a Windows test path). macOS has no per-window menu
// bar to toggle. The helpers accept a `platform` parameter (defaulting to
// `process.platform`) so tests can exercise each branch without mocking
// `process.platform` globally.
//
// Kept as a sibling of window-lifecycle.js / app-menu.js so the unit tests
// can pass plain window/store stubs — no Electron import here.
//
// NOTE: menu-bar visibility is a window-chrome concern, NOT a display
// preference. It deliberately does NOT live under `display-preferences/` —
// coupling an unrelated feature to a module named after font-size would
// mislead the next reader. If more window-chrome slots land (title-bar
// style, always-on-top default), they belong alongside this file.

// electron-store key. Kept top-level rather than nested because it's a
// single leaf and there is no `windowChrome.*` namespace yet; the day a
// second slot arrives the migration is one rename + one key in
// store-rename.js.
const STORE_KEY_MENU_BAR_VISIBLE = "menuBarVisible";

// Return a click handler for the F10 accelerator. On Linux the handler
// flips the current visibility, persists the new state, and re-negotiates
// the content bounds so Chromium re-measures the viewport (see the docs
// note below for the full rationale). On Windows and macOS the handler
// is a no-op — Windows keeps the hard-hidden bar per the Design Review
// decision on PR #10247, and macOS has no per-window bar to toggle.
//
// autoHideMenuBar is a persistent BrowserWindow property Electron re-
// applies after each menu interaction. It must move together with
// setMenuBarVisibility, or the bar will hide itself again after any menu
// click. Turn autoHide OFF when the user asks the bar to stay visible
// (F10 from hidden); turn it ON when hiding (F10 from visible) so the
// Alt-peek contract still applies. Order: autoHide BEFORE visibility, so
// the state a visibility listener observes is already consistent.
// Docs: https://www.electronjs.org/docs/api/browser-window#winsetautohidemenubarhide
//
// Third call — setContentBounds(getContentBounds()) — forces Chromium to
// renegotiate the client area. On Linux GTK, setMenuBarVisibility changes
// the window's frame layout but does not reliably fire a resize event, so
// the SPA renders at the previous viewport height (bare strip below the
// app when hiding; content clipped past the bottom when showing). A no-op
// setContentBounds writes the (updated) bounds back to itself, which is
// enough to trigger the re-measure. Same pattern as browser-view.js.
// See: https://github.com/electron/electron/issues/26073
function makeToggleMenuBar(win, store, platform = process.platform) {
  if (platform !== "linux") {
    // No-op on Windows (hard-hidden by construction) and macOS
    // (OS-managed menu bar). Return an empty function so callers can
    // still bind it unconditionally to a menu item's click handler; the
    // accelerator is separately gated to Linux-only in app-menu.js so
    // this path shouldn't fire in practice.
    return function noopToggleMenuBar() {};
  }
  return function toggleMenuBar() {
    // Defensive guards on every method: the main window is a BaseWindow
    // (see window-lifecycle.js), and while Electron's BaseWindow in the
    // versions we ship DOES expose setMenuBarVisibility / setAutoHideMenuBar
    // / setContentBounds, the pre-PR code kept `typeof … === "function"`
    // checks as defense-in-depth (see the `crash-data-loss-corruption`
    // review flag on 2f2a18865). Preserved here for the same reason: a
    // Windows-side BaseWindow that lacked these would crash every boot
    // with no fallback. Cheap insurance.
    if (typeof win.isMenuBarVisible !== "function") return;
    const next = !win.isMenuBarVisible();
    if (typeof win.setAutoHideMenuBar === "function") win.setAutoHideMenuBar(!next);
    if (typeof win.setMenuBarVisibility === "function") win.setMenuBarVisibility(next);
    store.set(STORE_KEY_MENU_BAR_VISIBLE, next);
    if (typeof win.getContentBounds === "function" && typeof win.setContentBounds === "function") {
      win.setContentBounds(win.getContentBounds());
    }
  };
}

// Called at window creation for the main dashboard and connection
// windows. Platform-aware: Linux restores the persisted visibility from
// the store; Windows unconditionally hard-hides the menu bar (matching
// the pre-PR behaviour installed at commit `9c32ce965` to avoid the
// stacked-bar UX defect where the native strip and the custom titlebar
// both render); macOS is a no-op because the menu bar is app-level, not
// per-window.
//
// The Linux path applies an explicit `false` even when it matches the
// default because the call is idempotent for autohide-hidden bars and
// removes ambiguity when reading the flow later.
//
// autoHideMenuBar moves with visibility for the same reason as in
// makeToggleMenuBar — a persisted "visible" state is only STICKY if
// autoHide is off. Without this, Behaviour #12 (menu bar persists across
// restart) would show the bar for a frame then hide it on first paint.
//
// setContentBounds pairs with the pair above so Chromium re-measures the
// client area after the boot-time restore — see makeToggleMenuBar for
// the full rationale.
function applyMenuBarVisibilityFromStore(win, store, platform = process.platform) {
  if (platform === "win32") {
    // Windows: hard-hide is the design (custom titlebar owns menu
    // discoverability). Ignore any stored value — the toggle is Linux-
    // only, so a stored `true` here would only arrive from a Linux user
    // whose profile got copied to a Windows machine, in which case the
    // hard-hide is still correct.
    //
    // Defensive `typeof` guards: this fires at every Windows boot on a
    // BaseWindow, and a BaseWindow that lacked either method would crash
    // startup with no fallback (see PR #10247 GPT 5.6 + First Principles
    // review of 2f2a18865). Present-Electron BaseWindow DOES expose both,
    // but the pre-PR code kept these guards deliberately and we preserve
    // that stance rather than trust the API surface across minor version
    // bumps.
    if (typeof win.setAutoHideMenuBar === "function") win.setAutoHideMenuBar(true);
    if (typeof win.setMenuBarVisibility === "function") win.setMenuBarVisibility(false);
    return;
  }
  if (platform !== "linux") {
    // macOS and any future platform: no-op. The menu bar is either OS-
    // managed (macOS) or has undefined semantics for a shell we haven't
    // validated on.
    return;
  }
  // Linux path.
  const persisted = store.get(STORE_KEY_MENU_BAR_VISIBLE);
  if (persisted === undefined || persisted === null) return;
  const visible = Boolean(persisted);
  if (typeof win.setAutoHideMenuBar === "function") win.setAutoHideMenuBar(!visible);
  if (typeof win.setMenuBarVisibility === "function") win.setMenuBarVisibility(visible);
  if (typeof win.getContentBounds === "function" && typeof win.setContentBounds === "function") {
    win.setContentBounds(win.getContentBounds());
  }
}

module.exports = {
  makeToggleMenuBar,
  applyMenuBarVisibilityFromStore,
  STORE_KEY_MENU_BAR_VISIBLE,
};
