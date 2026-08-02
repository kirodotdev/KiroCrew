/**
 * Static configuration for the Crew Companion builtin page.
 *
 * This page configures a SEPARATE macOS desktop app (Crew Companion) that runs its
 * own HTTP server on 127.0.0.1:7778. The browser cannot read that server directly,
 * so every request goes through the gateway reverse proxy at
 * `/apps/crew-companion/api/<path>` — a same-origin fetch, no CORS opening needed.
 */

/**
 * Candidate proxy paths, tried in order. The gateway documents forwarding
 * `/apps/crew-companion/api/{path}`, but the mount point has moved before, so a
 * failed first guess falls through instead of showing an empty list.
 */
export const REMINDER_PATHS = [
  '/apps/crew-companion/api/reminders',
  '/api/apps/crew-companion/api/reminders',
]

export const STATS_PATHS = [
  '/apps/crew-companion/api/stats',
  '/api/apps/crew-companion/api/stats',
]

/**
 * A browser client with no IPC to the desktop app, so it polls. The desktop app
 * being closed is the ordinary case, not an error — the UI stays visible either way.
 */
export const POLL_MS = 10_000

/**
 * Break-interval choices offered as one-tap presets. The desktop panel renders this
 * same list, so the two surfaces cannot drift.
 */
export const BREAK_PRESETS = [30, 45, 60, 90]

/** Bounds for a custom interval. Below 5 the pet would be a pest; above 8h it would
 *  never fire in a working day. The backend validates the same range. */
export const BREAK_MIN_MINS = 5
export const BREAK_MAX_MINS = 480

/** Default break interval assumed when the backend has not answered yet. */
export const BREAK_DEFAULT_MINS = 45
