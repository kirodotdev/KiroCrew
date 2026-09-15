/** Constants for the Notes builtin app. */
import type { CSSProperties } from 'react'
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

/**
 * Heading accent chrome.
 *
 * The heading TEXT stays `--text-strong` — the same token the dashboard's own
 * markdown renderer uses — and the active theme's accent appears only as a rule
 * or a rail beside it. That split is deliberate and is what makes the styling
 * safe on every theme: a theme pack may set `--accent` to any value the
 * allowlist in `hooks/themeCss.ts` accepts, including a colour indistinguishable
 * from its own `--bg`. Tinting the text with such an accent yields an unreadable
 * heading, whereas a rule simply fades into faint decoration. Measured across
 * the 36 built-in themes, no single accent ratio clears WCAG AA once arbitrary
 * packs are in scope, so no ratio is applied to text at all.
 *
 * The `color-mix()` expressions behind the three chrome tokens are declared as
 * custom properties on `.mdnb-note` in `styles.ts`; see that file for why they
 * live in the CSS module rather than here.
 */
export const HEADING_FG = 'var(--text-strong)'
/** Rule under h1 — the heaviest accent, on the largest type. */
export const HEADING_RULE_STRONG = 'var(--mdnb-heading-rule-strong)'
/** Rule under h2 — lighter, so the two levels stay distinguishable. */
export const HEADING_RULE_SOFT = 'var(--mdnb-heading-rule-soft)'
/** Vertical rail beside h3–h6, which are too small to carry a rule legibly. */
export const HEADING_RAIL = 'var(--mdnb-heading-rail)'
/** Rail offset into the column gutter, keeping heading text flush with body text. */
export const HEADING_RAIL_INDENT = 10
/** Gap between the rail and the heading text. */
export const HEADING_RAIL_GAP = 8

/** localStorage keys. Renaming one silently resets that preference. */
export const LS = {
  panelOpen: 'mdnb-panel-open',
  panelWidth: 'mdnb-panel-width',
  activeVault: 'mdnb-active-vault',
  openNote: 'mdnb-open-note',
  sort: 'mdnb-sort',
  view: 'mdnb-view',
  autoCommit: 'mdnb-auto-commit',
  syncShortcut: 'mdnb-sync-shortcut',
  fullWidth: 'mdnb-full-width',
} as const

/**
 * Keys that USED to hold auto-sync settings, kept only so an existing choice can
 * be migrated into the backend once (see the settings seed in MdNotebookPage).
 *
 * Auto-sync moved server-side because the backend runs the sync loop itself and
 * cannot read a browser's storage, so a value here could never be honoured — and
 * because "push my notes to a remote on a timer" is a decision about the vault,
 * not about the machine that happened to make it. Deleting these keys outright
 * would have turned auto sync off for everyone who had enabled it, with no
 * message; they are read once and then removed.
 */
export const LEGACY_LS = {
  autoSync: 'mdnb-auto-sync',
  autoSyncMins: 'mdnb-auto-sync-mins',
} as const

/**
 * Pinned notes are a per-vault preference, so the key carries the vault id.
 * Local to this machine on purpose: the pin is a reading aid for THIS device's
 * sidebar, and writing it into the note (frontmatter) would commit a UI
 * preference into the user's git history on the next sync.
 */
export const pinnedKey = (vaultId: string): string => `mdnb-pinned-${vaultId}`

/**
 * Collapsed folder names, per vault for the same reason pins are: two vaults
 * hold different trees, so a name collapsed in one means nothing in the other.
 * Local to this machine because it is view state, not content — writing it into
 * the vault would sync one device's sidebar shape to every other.
 */
export const collapsedKey = (vaultId: string): string => `mdnb-collapsed-${vaultId}`

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

/**
 * Left-rail type ramp — the Sessions list's scale, not a second one.
 *
 * The dashboard declares no global type scale, so the rail borrows the one
 * surface that does declare one: the session-row ramp in `pages/ChatSidebar.tsx`
 * (`ROW_TITLE_CLS` / `ROW_STATUS_CLS` / `ROW_META_CLS`), quantized to a 4px
 * baseline grid, plus that panel's own `text-sm` header. Four sizes carry the
 * whole rail — 14 panel title, 13 row, 11 secondary, 10 meta.
 *
 * 12px is deliberately absent: the sessions list uses no 12px anywhere in a row,
 * a folder header, or a menu item, and every 12px the rail used to carry was a
 * per-component optical nudge rather than a step. A new slot MUST reuse one of
 * these five entries; a literal `fontSize` in the rail is how the ramp drifts.
 *
 * Sizes and leading only — no family. Everything in the rail inherits
 * `FONT_BODY`, so an installed theme pack's sans still wins.
 */
export const RAIL_TYPE: Record<
  'panelTitle' | 'row' | 'secondary' | 'meta' | 'micro',
  CSSProperties
> = {
  /** Panel title — the vault-name trigger. Matches the Sessions panel header,
   *  which is `text-sm` (14px), the one step in the rail above a row. */
  panelTitle: { fontSize: '14px', lineHeight: '20px' },
  /** Any row's headline: a note title, a folder name, the Settings destination,
   *  a menu item. Matches `ROW_TITLE_CLS` and the dashboard's own menu items. */
  row: { fontSize: '13px', lineHeight: '20px' },
  /** Standalone secondary text — a folder's note count, an empty-state line.
   *  Matches `ROW_STATUS_CLS`. */
  secondary: { fontSize: '11px', lineHeight: '16px' },
  /** The line UNDER a headline: folder, relative time. The tightest box in the
   *  ramp, spent on the least important line. Matches `ROW_META_CLS`. */
  meta: { fontSize: '10px', lineHeight: '12px' },
  /** Micro-label inside a chip, where the chip's own padding sets its height, so
   *  the leading collapses instead of adding to it. Same step as `meta`. */
  micro: { fontSize: '10px', lineHeight: 1 },
}

/**
 * Section label inside the rail's own dropdown menus — `VIEW` / `SORT BY`.
 *
 * Deliberately NOT a `RAIL_TYPE` slot. This is a menu element that merely lives
 * in the rail's file, and `RAIL_TYPE` carries no 12px on purpose: removing 12px
 * from the rail is the point of that ramp, so folding a 12px menu size into the
 * same table would quietly contradict it.
 *
 * 12/500 uppercase matches the dashboard's own dropdown section labels
 * (`components/HookSkillsDropdown.tsx` — uppercase, `font-medium`, one size step
 * under its items), NOT Radix's `DropdownMenuLabel`, which is 12/600 and never
 * uppercase. Sitting one step under the 13px items is what keeps it reading as a
 * heading rather than as another choice.
 *
 * Know this before changing it: `textTransform: uppercase` is a no-op for CJK,
 * Korean, Devanagari and Bengali, and those glyphs fill their em box. In those
 * locales only weight and colour separate this label from the items it heads, so
 * the case treatment carries none of the load it carries in Latin and Cyrillic.
 */
export const MENU_SECTION_LABEL: CSSProperties = {
  fontSize: '12px',
  lineHeight: '16px',
  fontWeight: 500,
}

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

/**
 * The reading column: 800px wide with 20px sides.
 *
 * The default, not a rule. A capped measure is what keeps prose readable on a
 * wide display, so it stays on unless the user turns it off — but a note holding
 * wide tables, long code blocks or side-by-side content is unreadable inside it,
 * which is why `LS.fullWidth` can lift the cap per device.
 */
export const COLUMN_MAX_WIDTH = 800
export const COLUMN_PAD_X = 20

/**
 * The document reading base — the size every `em` in the heading ramp resolves
 * against, and the size body prose renders at.
 *
 * Exported because THREE surfaces must carry the same value or a documented
 * promise breaks: the rendered preview, the raw whole-file editor (which matches
 * rendered body metrics so toggling views does not reflow the reading position),
 * and the inline block editor's default (so an edited paragraph keeps the look it
 * had rendered). Each of those used to hardcode its own copy with a comment
 * asserting they agreed, which is a drift waiting to happen.
 */
export const DOC_BODY_PX = 16
export const DOC_BODY_LINE_HEIGHT = 1.55

/**
 * Document heading ramp — Obsidian's stock heading variables.
 *
 * Matched to Obsidian deliberately: a vault is read in both apps, and a note
 * whose hierarchy shifts between them reads as two different documents. Stored as
 * plain ratios so `DOC_H1_PX` below can be derived from them; the renderer spells
 * them as `em`, which resolves against `DOC_BODY_PX` rather than against whatever
 * font-size an enclosing surface happens to set.
 *
 * The ratios are Obsidian's, not a clean modular scale — 1.802 / 1.602 / 1.422 /
 * 1.278 / 1.121 steps by roughly 1.125 but not exactly (1.278 -> 1.121 is 1.140).
 * Do not "correct" them to powers of 1.125: matching Obsidian is the point, and
 * the irregularity is what makes it a match.
 *
 * Weight is a TABLE, not a rule derived from the level: only h1 is 700, and every
 * level below it is 600. That does not coincide with the h1/h2-vs-h3+ split the
 * chrome uses (rule underneath vs left rail), so the two cannot share one
 * `n <= 2` test — a single expression serving both is how they silently drift.
 */
export const DOC_HEADING_EM = [1.802, 1.602, 1.422, 1.278, 1.121, 1]
export const DOC_HEADING_WEIGHTS = [700, 600, 600, 600, 600, 600]

/**
 * h1's size in absolute px, for the inline note title.
 *
 * That title renders in the chrome header, which sets no reading-column
 * font-size, so an `em` there would resolve against the wrong base — it needs a
 * real px value. Derived from the ramp rather than typed as a literal because the
 * two are required to match: a hardcoded copy is what let the title sit 5.8px
 * under h1 the moment the reading base moved from 13px to 16px.
 */
export const DOC_H1_PX = DOC_BODY_PX * DOC_HEADING_EM[0]

/**
 * Monospace inside a document, as a ratio of the reading base.
 *
 * Mono faces run visually larger than the body face at the same nominal size,
 * so code is set slightly below body prose. ONE ratio serves both spellings of
 * code — inline spans use the `em`, and the surfaces that cannot resolve `em`
 * against the reading column (a fenced block's own `<pre>`, the block editor
 * opened on a fence, a mermaid block's source fallback) use the derived px.
 *
 * They are derived from one number because they are the same size: while the
 * fenced sites carried their own literal, raising the reading base moved inline
 * code and left fences behind, which rendered a fenced block SMALLER than the
 * inline code beside it.
 */
export const DOC_CODE_EM = 0.9
export const DOC_CODE_PX = DOC_BODY_PX * DOC_CODE_EM

/** Breathing room between the note title and the floating header controls. */
export const HEADER_CONTROLS_GAP = 12
/**
 * Narrowest the title is allowed to get before it stops sharing a row with the
 * header controls and drops below them instead. The title wraps on any
 * character, so a pane that cannot seat both would otherwise render the
 * filename as a column of single letters.
 */
export const TITLE_MIN_WIDTH = 120
