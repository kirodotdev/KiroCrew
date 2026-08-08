/**
 * Static configuration for the Crew Companion builtin page.
 *
 * The backend runs IN-PROCESS inside the gateway (see
 * `src/kiro_crew/apps/builtins/crew_companion/backend/routes.py`), so these are
 * ordinary same-origin paths on the dashboard's own origin — no reverse proxy, no
 * second process, no CORS.
 *
 * This replaced a two-candidate probe (`/apps/crew-companion/api/...` then
 * `/api/apps/crew-companion/api/...`) that existed because the page was reaching a
 * SEPARATE macOS app on 127.0.0.1:7778 through the gateway's proxy, and that mount
 * point had moved before. A single owned route needs no fallback: if it 404s, that
 * is a routing bug to fix loudly rather than paper over — which is exactly what the
 * long-term reviewer asked for when it flagged the probe.
 */

/** Where this app's own routes are mounted. Must match `_BASE` in routes.py. */
export const API_BASE = '/api/apps/crew-companion'

export const REMINDERS_PATH = `${API_BASE}/reminders`
export const STATS_PATH = `${API_BASE}/stats`
/** Turn the companion on. The app manager's generic per-app enable route. */
export const ENABLE_PATH = `${API_BASE}/enable`
/**
 * Ask the always-running overlay to open one of its Electron windows. The
 * dashboard page has no bridge to the desktop main process, so it records the
 * intent here and the overlay acts on it when it next drains `/pending`. Body:
 * `{ target: 'panel' | 'gallery' }`.
 */
export const WINDOW_PATH = `${API_BASE}/window`
/** Fires waiting to be drawn as bubbles. Cursor-based; see `drain` in store.py. */
export const PENDING_PATH = `${API_BASE}/pending`
/** The overlay reporting the user is there, so break nudges are not sent to an
 *  empty chair. Must be pinged more often than PRESENCE_TTL_SECONDS (90s). */
export const PRESENCE_PATH = `${API_BASE}/presence`
/** A guided breathing exercise was completed (not merely suggested). */
export const BREATHING_DONE_PATH = `${API_BASE}/breathing-done`

/**
 * How often the page re-reads reminders and stats.
 *
 * The page polls rather than subscribing because the gateway exposes no
 * server-push channel to an app's own UI. Ten seconds is unnoticeable for a list
 * that changes when the user changes it, and writes update optimistically anyway.
 */
export const POLL_MS = 10_000

/**
 * Break-interval choices offered as one-tap presets. The panel renders this same
 * list, and `BREAK_PRESETS` in reminders.py holds the same values for the backend,
 * so the surfaces cannot drift.
 */
export const BREAK_PRESETS = [30, 45, 60, 90]

/** Bounds for a custom interval. Below 5 the companion would be a pest; above 8h
 *  it would never fire in a working day. The backend clamps to the same range. */
export const BREAK_MIN_MINS = 5
export const BREAK_MAX_MINS = 480

/** Default break interval assumed before the backend has answered. */
export const BREAK_DEFAULT_MINS = 45

/** Panel mutations. The panel is the only surface that writes reminders. */
export const ADD_PATH = `${API_BASE}/reminders/add`
export const REMOVE_PATH = `${API_BASE}/reminders/remove`
export const SKIP_PATH = `${API_BASE}/reminders/skip`
export const CONFIG_PATH = `${API_BASE}/reminders/config`

/**
 * The companion's size, verbatim from PET_W/PET_H in the desktop app's
 * shared/constants.ts. The drag grip centres on these and the bubble is placed
 * from them, so they are a contract, not a styling choice.
 */
export const PET_W = 128
export const PET_H = 128

/** Appearance packs — the avatar library. */
export const APPEARANCES_PATH = `${API_BASE}/appearances`
export const APPEARANCE_DETAIL_PATH = `${API_BASE}/appearances/detail`
export const APPEARANCE_COLOURS_PATH = `${API_BASE}/appearances/colours`
export const APPEARANCE_EXPORT_PATH = `${API_BASE}/appearances/export`
export const APPEARANCE_IMPORT_PATH = `${API_BASE}/appearances/import`
export const APPEARANCE_SAVE_SPRITE_PATH = `${API_BASE}/appearances/save-sprite`
export const PETDEX_FETCH_PATH = `${API_BASE}/petdex/fetch`
export const APPEARANCE_SAVE_PATH = `${API_BASE}/appearances/save`
export const APPEARANCE_DELETE_PATH = `${API_BASE}/appearances/delete`

/** Transparent gutter around the gallery card. Must match GALLERY_PAD in galleryWindow.js. */
export const GALLERY_PAD = 24
