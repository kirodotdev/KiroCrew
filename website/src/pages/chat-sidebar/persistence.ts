/** The sidebar's browser-persisted view preferences: storage keys, their readers and
 *  the two migrations (the lane boolean, the superseded conductor fold set). Keys are
 *  user state, so none may be renamed. The four status-filter keys live on
 *  `SESSION_FILTERS` in ./filters. */
import { DEFAULT_RECENT_WINDOW_MS } from '../recentWindow'
import { DEFAULT_STALE_COLLAPSE_MS } from '../staleCollapse'
import type { SidebarLane } from './types'

// Recency window for the "Recent" filter: surfaces sessions whose last activity
// is within the selected window (default one hour), keyed off the same
// last-activity timestamp the date sort uses. The window is user-selectable
// (presets + custom) and persisted under RECENT_WINDOW_LS_KEY. The pure window
// math lives in ../recentWindow so it can be unit-tested without a render.
export const RECENT_WINDOW_LS_KEY = 'mc-session-recent-window-ms'

/** Read the persisted Recent window (ms), falling back to the default. Runs in
 *  a useState initializer during render, so a throwing localStorage (private
 *  mode / disabled storage) must not crash the component — fall back instead. */
export function readStoredRecentWindow(): number {
  try {
    const saved = Number(localStorage.getItem(RECENT_WINDOW_LS_KEY))
    return Number.isFinite(saved) && saved > 0 ? saved : DEFAULT_RECENT_WINDOW_MS
  } catch {
    return DEFAULT_RECENT_WINDOW_MS
  }
}

/** Folders excluded from the flat lane (see `filterHiddenFolders`). Stored as a JSON
 *  array of folder ids under this key. */
export const HIDDEN_FOLDERS_LS_KEY = 'mc-flat-hidden-folders'

// Stale-session collapse threshold (ms), persisted. 0 = off. Presets live in
// the filter menu's display section; the pure split math lives in
// ../staleCollapse so it can be unit-tested without a render.
export const STALE_COLLAPSE_LS_KEY = 'mc-session-stale-collapse-ms'

/** Read the persisted stale-collapse threshold (ms). A stored "0" means the
 *  user turned the feature off and must survive reloads, so only a missing or
 *  invalid value falls back to the default. Runs in a useState initializer, so
 *  a throwing localStorage must not crash the component. */
export function readStoredStaleCollapse(): number {
  try {
    const raw = localStorage.getItem(STALE_COLLAPSE_LS_KEY)
    if (raw === null) return DEFAULT_STALE_COLLAPSE_MS
    const saved = Number(raw)
    return Number.isFinite(saved) && saved >= 0 ? saved : DEFAULT_STALE_COLLAPSE_MS
  } catch {
    return DEFAULT_STALE_COLLAPSE_MS
  }
}

/** Whether the filter menu's Folders section is rolled up to its heading. */
export const FOLDERS_SHELVED_LS_KEY = 'mc-filter-folders-shelved'
// A status chip's HIDE state ('1' = drop its matching rows). One per chip that
// can hide; referenced from that chip's `SESSION_FILTERS` entry in ./filters.
export const UNREAD_HIDDEN_LS_KEY = 'mc-session-unread-hidden'
export const RUNNING_HIDDEN_LS_KEY = 'mc-session-running-hidden'
export const PINNED_HIDDEN_LS_KEY = 'mc-session-pinned-hidden'

/** Read the persisted hidden-folder ids. Runs in a useState initializer during
 *  render, so a throwing localStorage (private mode / disabled storage) or a
 *  hand-corrupted value must fall back to "nothing hidden", never crash. */
export function readStoredHiddenFolders(): Set<string> {
  try {
    const raw = localStorage.getItem(HIDDEN_FOLDERS_LS_KEY)
    if (!raw) return new Set()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((id): id is string => typeof id === 'string'))
  } catch {
    return new Set()
  }
}

/** Tag ids the list is filtered DOWN TO, as a JSON array under this key.
 *
 *  Inclusive, unlike the folder filter above, which stores the ids it HIDES.
 *  The asymmetry is deliberate and follows what a new item should do by default:
 *  a newly created folder must stay visible, whereas a newly created tag must
 *  not silently start narrowing the list. So empty here means "no tag filter",
 *  and selecting Blocked means "show only Blocked". */
export const TAG_FILTER_LS_KEY = 'mc-session-tag-filter'

/** Read the persisted tag-filter ids. Runs in a useState initializer during
 *  render, so a throwing localStorage (private mode / disabled storage) or a
 *  hand-corrupted value must fall back to "no filter", never crash. */
export function readStoredTagFilter(): Set<string> {
  try {
    const raw = localStorage.getItem(TAG_FILTER_LS_KEY)
    if (!raw) return new Set()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((id): id is string => typeof id === 'string'))
  } catch {
    return new Set()
  }
}

/** Flat view ("explode chats out of folders") persistence key.
 *
 *  LEGACY. Superseded by `SIDEBAR_LANE_LS_KEY`, and still read once at mount so a
 *  user who had flat view on keeps it: see `readStoredLane`. Still WRITTEN by the
 *  folder-create path, which turns flat view off, because a build that rolls back
 *  must not strand that user in a lane they were moved out of.
 */
export const FLAT_VIEW_LS_KEY = 'mc-sidebar-flat-view'

/** Lane preference. Replaces the `FLAT_VIEW_LS_KEY` boolean. */
export const SIDEBAR_LANE_LS_KEY = 'mc-sidebar-lane'

/** Which conductor rows the user has OPENED, as a JSON array of row keys. A row absent
 *  from it is collapsed, which is what makes one crew read as one row: a conductor
 *  with fourteen workers is a line with a count on it, not fifteen lines, until the
 *  person asks for the workers. */
export const CONDUCTOR_EXPANDED_LS_KEY = 'mc-sidebar-conductor-expanded'
/** The key the one above supersedes, kept only so it can be removed from storage. */
const CONDUCTOR_SUPERSEDED_COLLAPSED_LS_KEY = 'mc-sidebar-conductor-collapsed'

/**
 * The persisted lane, migrating the boolean this replaced.
 *
 * A stored `'1'` under the old key was flat view ON, so that user opens in `flat`
 * rather than being silently reset to the tree. The new key wins whenever it holds a
 * value this build recognises: an unknown string is treated as absent rather than
 * refused, because the only honest reading of a lane name from a future build is
 * "not one of mine".
 */
export function readStoredLane(): SidebarLane {
  const stored = localStorage.getItem(SIDEBAR_LANE_LS_KEY)
  if (stored === 'tree' || stored === 'flat' || stored === 'conductor') return stored
  return localStorage.getItem(FLAT_VIEW_LS_KEY) === '1' ? 'flat' : 'tree'
}

/** The conductor rows the user has OPENED, or an empty set when the value is
 *  unusable. Opened rather than collapsed is what makes COLLAPSED the default: a
 *  conductor this build has never seen has no entry here, so it renders as one row
 *  carrying its child count and its subtree's badges, and nothing under it is on
 *  screen until the person asks.
 *
 *  The set this supersedes held the OPPOSITE sense, one row per conductor the user had
 *  closed, and there is no reading of it that produces this one: a row it names was
 *  closed, which is now the default, and a row it omits was open only because nobody
 *  had touched it. So it is dropped rather than converted -- left in place it would sit
 *  in storage for the life of the browser profile, meaning nothing to any build. */
export function readConductorExpanded(): Set<string> {
  try {
    localStorage.removeItem(CONDUCTOR_SUPERSEDED_COLLAPSED_LS_KEY)
  } catch {
    // Storage that refuses a write still answers reads, so the fold state below is
    // worth reading; an undeletable stale key costs nothing but the bytes.
  }
  try {
    const raw = localStorage.getItem(CONDUCTOR_EXPANDED_LS_KEY)
    if (!raw) return new Set()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((k): k is string => typeof k === 'string' && k !== ''))
  } catch {
    // Collapsed-by-default is the documented default, so an unreadable value costs the
    // user one re-open per crew rather than an error they cannot act on.
    return new Set()
  }
}

export const SIDEBAR_LS_KEY = 'mc-sidebar-width'
/** The width the user had before a board auto-widen, so switching back to list
 *  view restores it instead of stranding the automatic value. */
export const SIDEBAR_PRE_BOARD_LS_KEY = 'mc-sidebar-width-pre-board'

/** The Older Sessions pane's height (px); ./history clamps what it reads back. */
export const HISTORY_HEIGHT_LS_KEY = 'mc-history-height'
