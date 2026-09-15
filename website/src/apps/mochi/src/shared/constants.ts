/** Shared layout constants — single source of truth for pet dimensions */
export const PET_W = 128
export const PET_H = 128
export const BUBBLE_W = 240

/** Bubble fade-out animation duration (ms) — used by useBubble renderer hook. */
export const BUBBLE_FADE_MS = 300

/** Delay between consecutive bubble notifications (ms) — derived from fade duration + buffer. */
export const BUBBLE_DRAIN_DELAY_MS = BUBBLE_FADE_MS + 100

/** Agent spawn timeout — shared between QueuePoller and WatchlistService */
export const AGENT_SPAWN_TIMEOUT_MS = 5 * 60_000  // 5 minutes

/** Agent name for background spawns (watch checks, reminders, planning). */
export const BG_AGENT_NAME = 'mochi-bg'

/**
 * Bottom margin (px) to keep the pet above the macOS Dock / taskbar.
 * workArea already excludes the menu bar, but the pet sprite can still
 * land behind the Dock if Y is too close to workArea.height.
 */
export const PET_BOTTOM_MARGIN = 140

/** Watchlist side panel width in pixels — shared between IPC handler and renderer. */
export const WATCHLIST_PANEL_WIDTH = 280

/** Pinned files side panel width in pixels — shared between IPC handler and renderer. */
export const PINNED_PANEL_WIDTH = 180
