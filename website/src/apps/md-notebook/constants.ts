/** Constants for the Notes builtin app. */
import type { Note, Shortcut } from './types'
import { compareText } from '../../i18n/format'

/** The gateway proxies this prefix to the app's own backend. */
export const API_BASE = '/apps/md-notebook/api'

// Theme integration: these CSS custom properties resolve against the active
// dashboard theme and update live when the user changes theme or font settings.
export const ACCENT = 'var(--accent)'
export const ACCENT_BG = 'var(--accent-subtle)'
export const ACCENT_FG = 'var(--accent-fg)'
export const FONT_BODY = 'var(--font-body)'
export const FONT_MONO = 'var(--mono)'

/** localStorage keys. Renaming one silently resets that preference. */
export const LS = {
  panelOpen: 'mdnb-panel-open',
  panelWidth: 'mdnb-panel-width',
  activeVault: 'mdnb-active-vault',
  openNote: 'mdnb-open-note',
  sort: 'mdnb-sort',
  view: 'mdnb-view',
  autoSync: 'mdnb-auto-sync',
  autoSyncMins: 'mdnb-auto-sync-mins',
  autoCommit: 'mdnb-auto-commit',
  syncShortcut: 'mdnb-sync-shortcut',
} as const

/**
 * Pinned notes are a per-vault preference, so the key carries the vault id.
 * Local to this machine on purpose: the pin is a reading aid for THIS device's
 * sidebar, and writing it into the note (frontmatter) would commit a UI
 * preference into the user's git history on the next sync.
 */
export const pinnedKey = (vaultId: string): string => `mdnb-pinned-${vaultId}`

/**
 * Sort options for the notes list. Keys are persisted, so renaming one resets
 * the user's choice to the default. Labels live in `labels.ts` (`sortLabel`),
 * keyed by these same ids, so the i18n key-reference gate can verify them.
 */
export const SORTS: Record<string, { cmp: (a: Note, b: Note) => number }> = {
  'modified-desc': { cmp: (a, b) => b.modifiedAt - a.modifiedAt },
  'modified-asc': { cmp: (a, b) => a.modifiedAt - b.modifiedAt },
  'created-desc': { cmp: (a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0) },
  'created-asc': { cmp: (a, b) => (a.createdAt ?? 0) - (b.createdAt ?? 0) },
  'name-asc': { cmp: (a, b) => compareText(a.title, b.title) },
  'name-desc': { cmp: (a, b) => compareText(b.title, a.title) },
}
export const DEFAULT_SORT = 'name-asc'

/** Manual-sync shortcut. Cmd+S by default (Ctrl+S on non-Mac keyboards). */
export const DEFAULT_SYNC_SHORTCUT: Shortcut = {
  key: 's',
  meta: true,
  ctrl: false,
  alt: false,
  shift: false,
}
export const DEFAULT_AUTO_SYNC_MINS = 10
export const MIN_AUTO_SYNC_MINS = 1
export const MAX_AUTO_SYNC_MINS = 1440
/**
 * Auto-commit is ON by default; auto-sync is not. A commit is local, private and
 * reversible, so doing it unasked costs the user nothing — whereas an unasked
 * push sends their notes to a remote, which has to stay a deliberate choice.
 */
export const DEFAULT_AUTO_COMMIT = true
/**
 * Autosave cadence. Not user-configurable: a local commit is cheap and private,
 * and a knob here would only invite a value that loses history. The editor's own
 * 1s debounce is what protects the file on disk — this interval only decides how
 * finely that work is sliced into git history.
 */
export const AUTO_COMMIT_MINS = 5

/** Left panel drag bounds. */
export const PANEL_MIN_WIDTH = 180
export const PANEL_MAX_WIDTH = 420
export const PANEL_DEFAULT_WIDTH = 260

/** Debounce before a note edit is persisted. */
export const SAVE_DEBOUNCE_MS = 1000
/** External-change poll interval. */
export const CHANGES_POLL_MS = 4000

// Indent measurement. A tab advances to the next 4-column stop, so one nesting
// level is 32px — counting raw characters would score a tab the same as a
// single space and render real nesting shallower than a stray two-space indent.
export const TAB_STOP = 4
export const INDENT_COL_PX = 8
/** One nesting level, inserted by the Tab gesture. Matches what Obsidian writes. */
export const LIST_INDENT = '\t'
/** Offset that lands a nesting rail on the parent's checkbox or bullet centre. */
export const RAIL_X = 7

/** The reading column: 800px wide with 20px sides. */
export const COLUMN_MAX_WIDTH = 800
export const COLUMN_PAD_X = 20
