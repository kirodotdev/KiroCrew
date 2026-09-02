// Native zoom ladder — the single source of truth for how KiroCrew steps
// Chromium's per-origin zoom factor.
//
// Every zoom mutation in the app funnels through this module: the View-menu
// items (Cmd/Ctrl +, -, 0), the ctrl+wheel gesture handler, and the Settings
// "Zoom Level" stepper (via the zoom:* IPC handlers in main.js). Chromium
// itself persists the resulting factor per-origin in the persistent session,
// so there is no bespoke storage — quit and relaunch restores the same zoom.
//
// The ladder mirrors Chrome's preset zoom stops (trimmed to a sane dashboard
// range) so stepping always lands on familiar round values instead of the
// 1.2^0.5 ≈ 109.5% values that Electron's `setZoomLevel(level ± 0.5)` role
// behavior produces.

const ZOOM_LADDER = [0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
const ZOOM_MIN = ZOOM_LADDER[0];
const ZOOM_MAX = ZOOM_LADDER[ZOOM_LADDER.length - 1];

// Float tolerance when comparing an arbitrary current factor (e.g. one
// restored by Chromium or set by a ctrl+wheel gesture) against ladder stops.
const EPS = 1e-3;

/** Clamp an arbitrary factor into the supported range. Non-finite input
 *  (NaN/Infinity from a malformed IPC payload) resets to 1. */
function clampZoomFactor(factor) {
  if (typeof factor !== "number" || !Number.isFinite(factor)) return 1;
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, factor));
}

/** Next ladder stop from `current` in direction `dir` (+1 zooms in, -1 out).
 *  Off-ladder values snap to the nearest stop in that direction; the ends
 *  saturate. */
function stepZoomFactor(current, dir) {
  const f = clampZoomFactor(current);
  if (dir > 0) {
    for (const v of ZOOM_LADDER) if (v > f + EPS) return v;
    return ZOOM_MAX;
  }
  for (let i = ZOOM_LADDER.length - 1; i >= 0; i--) {
    if (ZOOM_LADDER[i] < f - EPS) return ZOOM_LADDER[i];
  }
  return ZOOM_MIN;
}

module.exports = { ZOOM_LADDER, ZOOM_MIN, ZOOM_MAX, clampZoomFactor, stepZoomFactor };
