"use strict";

// Decides whether the Linux window should drop the native frame.
//
// On Linux the dashboard's own 42px header would otherwise stack under the
// window manager's native title bar -- two title bars, one duplicating the
// other's controls. Modern Electron honors `frame: false` on Linux, so the
// header can double as the title bar the same way it does on macOS and
// Windows.
//
// The decision is NOT unconditional. Whether a window should draw its own
// decorations is the desktop environment's call, not ours:
//   - GNOME-family desktops expect client-side decorations (GNOME's Wayland
//     compositor does not offer server-side decorations), so a frameless
//     window with an in-app header is the platform-native shape there.
//   - KDE, XFCE, LXQt, and tiling window managers (i3, sway, hyprland, ...)
//     expect server-side decorations. Going frameless there strips the
//     window of WM-provided dragging and edge-resize affordances -- a worse
//     regression than the doubled title bar.
//   - Unknown or headless environments keep the native frame (fail-safe:
//     today's behavior).
//   - X11 sessions keep the native frame even on CSD desktops: Electron's
//     frameless resize affordances (extended resize boundaries, drop shadows)
//     are Wayland-only, so a frameless X11 window loses mouse edge-resize
//     (see isWaylandSession).
//
// The operator can force either shape via the `linuxFrameless` store key
// (true = always frameless, false = always native frame, anything else =
// decide from the desktop environment).

// Desktop-environment names (lowercased XDG tokens) that prefer client-side
// decorations. "ubuntu" appears alone in XDG_CURRENT_DESKTOP on some Ubuntu
// GNOME sessions ("ubuntu:GNOME" splits into both tokens on others).
const CSD_DESKTOPS = new Set([
  "gnome",
  "gnome-classic",
  "gnome-flashback",
  "ubuntu",
  "unity",
  "pantheon",
  "budgie",
  "budgie-desktop",
]);

// Sessions where a tiling window manager or an SSD-only desktop owns the
// decorations. Checked BEFORE the CSD set, because hybrid token sets are real:
// Regolith (i3 on a GNOME session) reports "Regolith:GNOME", and
// gnome-flashback + i3 setups produce the same shape -- the GNOME token must
// not win there.
const SSD_DESKTOPS = new Set([
  "kde",
  "plasma",
  "xfce",
  "lxqt",
  "lxde",
  "mate",
  "x-cinnamon",
  "cinnamon",
  "i3",
  "sway",
  "hyprland",
  "bspwm",
  "dwm",
  "awesome",
  "qtile",
  "regolith",
]);

/**
 * Lowercased desktop-identity tokens from the standard XDG variables.
 * XDG_CURRENT_DESKTOP is colon-separated by spec ("ubuntu:GNOME"); the other
 * two are single values but are folded through the same splitter for
 * uniformity.
 *
 * @param {Record<string, string|undefined>} env
 * @returns {string[]}
 */
function desktopTokens(env) {
  const raw = [env.XDG_CURRENT_DESKTOP, env.XDG_SESSION_DESKTOP, env.DESKTOP_SESSION]
    .filter(Boolean)
    .join(":");
  return raw
    .split(":")
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
}

/**
 * True when the session's desktop environment expects windows to draw their
 * own decorations. SSD/tiling tokens take precedence over CSD tokens (see
 * SSD_DESKTOPS), so a hybrid session like "Regolith:GNOME" stays native.
 *
 * @param {Record<string, string|undefined>} env
 */
function prefersClientSideDecorations(env) {
  const tokens = desktopTokens(env);
  if (tokens.some((t) => SSD_DESKTOPS.has(t))) return false;
  return tokens.some((t) => CSD_DESKTOPS.has(t));
}

/**
 * True only on a Wayland session. Electron guarantees the compensating
 * affordances for a frameless window (GTK drop shadows and extended resize
 * boundaries) on Wayland only; on X11 a frameless window has no non-client
 * area, so the WM draws no resize grips and the window becomes mouse-
 * unresizable -- a worse regression than the doubled title bar. The auto
 * branch therefore never goes frameless on X11.
 *
 * @param {Record<string, string|undefined>} env
 */
function isWaylandSession(env) {
  return String(env.XDG_SESSION_TYPE || "").trim().toLowerCase() === "wayland";
}

/**
 * Normalize the persisted override to strict boolean-or-null. electron-store
 * data is operator-editable JSON, so anything that is not literally true or
 * false means "auto".
 *
 * @param {*} value
 * @returns {boolean|null}
 */
function normalizeFramelessOverride(value) {
  return value === true || value === false ? value : null;
}

/**
 * The frame decision for a Linux window.
 *
 * @param {{env?: Record<string, string|undefined>, override?: *}} [opts]
 * @returns {{frameless: boolean, reason: string}}
 */
function decideLinuxFrame({ env = {}, override = null } = {}) {
  const forced = normalizeFramelessOverride(override);
  if (forced === true) return { frameless: true, reason: "override-frameless" };
  if (forced === false) return { frameless: false, reason: "override-native-frame" };
  if (!isWaylandSession(env)) return { frameless: false, reason: "not-wayland" };
  if (prefersClientSideDecorations(env)) return { frameless: true, reason: "csd-desktop" };
  return { frameless: false, reason: "ssd-or-unknown-desktop" };
}

/**
 * Dispatch one caption-control action onto a window. The frameless Linux
 * window has no OS-provided controls (unlike macOS traffic lights and the
 * Windows titleBarOverlay), so the injected header buttons round-trip through
 * IPC to here. The action vocabulary is a closed allowlist -- anything else
 * (a malformed or forged renderer message) is a no-op that reports false.
 *
 * "close" calls win.close(), NOT destroy(): the main window's close handler
 * hides to tray while quitting is not in progress, and that behavior must be
 * identical to the native close button it replaces.
 *
 * @param {{minimize: Function, maximize: Function, unmaximize: Function,
 *          isMaximized: Function, close: Function, isDestroyed: Function}} win
 * @param {*} action
 * @returns {boolean} whether a control action was applied
 */
function applyWindowControl(win, action) {
  if (!win || typeof win.isDestroyed !== "function" || win.isDestroyed()) return false;
  switch (action) {
    case "minimize":
      win.minimize();
      return true;
    case "maximize-toggle":
      if (win.isMaximized()) win.unmaximize();
      else win.maximize();
      return true;
    case "close":
      win.close();
      return true;
    default:
      return false;
  }
}

module.exports = {
  decideLinuxFrame,
  normalizeFramelessOverride,
  prefersClientSideDecorations,
  isWaylandSession,
  desktopTokens,
  applyWindowControl,
};
