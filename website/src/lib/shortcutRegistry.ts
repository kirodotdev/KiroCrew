import { safeGetItem } from '../utils/safeStorage'

/**
 * Shortcut registry — the ONE table behind every built-in dashboard chord.
 *
 * Before this module each chord was a literal somewhere: `e.code === 'KeyN' &&
 * e.shiftKey` in the keydown handler, a `{ key: 'n', alt: true, shift: true }`
 * entry in the display list, an accelerator string in the Electron menu. Three
 * copies, no owner, and nothing a rebind UI could read (#4608). The registry
 * gives each shortcut one record — default chord per platform, legacy aliases,
 * where it fires — and the display surfaces (Alt+K modal, Settings → Shortcuts,
 * nav hover hints) and the keydown handler both read it, so they cannot drift.
 *
 * Two dispatch kinds. A `registry` entry is matched here, by
 * {@link matchShortcutEvent}: the handler asks "which id is this keystroke?" and
 * never spells the chord. A `code` entry is a FAMILY the handler matches itself —
 * the digit/letter session jumps (one entry, nine-plus keys, an input gate that
 * depends on which letter), the MRU walk, Alt+arrow, panel navigation, the
 * bracket cycle, the instance switcher. The registry describes those for display
 * and deliberately never claims them, so their contextual gating stays where it
 * is legible. They become registry entries when their modifier is made
 * rebindable (P3 of #4608).
 *
 * Conventional defaults (Joseph Dombroski, Slack; #4608): app-level commands use
 * the platform primary modifier — ⌘ on macOS, Ctrl elsewhere — the way VS Code,
 * Slack, Claude Code and Codex do, and Option/Alt is left to text editing. Every
 * chord that moved keeps the old Option/Alt chord as an ALIAS for one release, so
 * nobody's hands break; aliases render muted in the modal.
 *
 * `browserReserved` marks chords a browser never delivers to the page (⌘N/Ctrl+N,
 * ⌘W/Ctrl+W: new window, close tab). Only the desktop shell can honour them —
 * `electron/app-menu.js` leaves them unclaimed so the keydown reaches the
 * renderer — and a browser host advertises the alias first instead.
 *
 * User overrides ({@link loadShortcutOverrides}) are the #4488 panel-toggle
 * pattern generalized: localStorage holds OVERRIDES only, `null` is a deliberate
 * "unbound", an absent id falls through to the platform default, and a writer
 * broadcasts {@link SHORTCUT_OVERRIDES_EVENT} so live readers refresh. This module
 * ships the READ side only — the writer arrives with the Settings → Keyboard UI
 * (P3) that is its sole caller.
 */

/**
 * Which section of the shortcuts reference an entry belongs to.
 *
 * A STABLE, NON-LOCALISED ID — deliberately not the displayed heading. The value is
 * a discriminant first and a label never: `INSTANCE_SHORTCUTS` and
 * `groupShortcuts()` in ShortcutsModal both select entries by comparing it, so a
 * localised value would make every one of those comparisons miss in every
 * non-English locale and silently render an empty shortcuts modal. The heading text
 * lives in `SHORTCUT_GROUP_LABEL_KEY` (useKeyboardShortcuts) and resolves per render.
 */
export type ShortcutGroup = 'chat-navigation' | 'panel-navigation' | 'actions' | 'remote-crews'

/** Shortcut groups in display order — the canonical id set and ordering. */
export const SHORTCUT_GROUPS: readonly ShortcutGroup[] = [
  'chat-navigation', 'panel-navigation', 'actions', 'remote-crews',
]

export type ShortcutPlatform = 'mac' | 'other'

/**
 * One key combination. POSITIONAL: `key` is derived from `KeyboardEvent.code`
 * (see {@link eventKeyToken}), so a chord keeps working on layouts where the
 * glyph sits elsewhere — the same rule every handler in `useKeyboardShortcuts`
 * has always used.
 */
export interface Chord {
  /**
   * A lowercase letter, a digit, a punctuation glyph in its US-QWERTY position
   * (`,` `/` `[` `]` `` ` ``), or a named key (`Enter`, `Escape`, `ArrowLeft`).
   */
  key: string
  /** The platform primary modifier: ⌘ on macOS, Ctrl on Windows/Linux. */
  mod?: boolean
  /**
   * LITERAL Control on macOS (⌃). Only the two chords that must be Control on a
   * Mac use it (⌃G — the backend prints "ctrl+g"; ⌃1–9 — Option+digit types
   * characters on non-US layouts). Off macOS `ctrl` and `mod` are the same key;
   * an `other` default should say `mod`.
   */
  ctrl?: boolean
  /** Option on macOS, Alt elsewhere. */
  alt?: boolean
  shift?: boolean
}

export type ShortcutDispatch = 'registry' | 'code'

export interface ShortcutEntry {
  /** Stable id, e.g. `new-chat`. The handler, the label table and overrides key on it. */
  id: string
  group: ShortcutGroup
  /** Factory default per platform. `null` = no chord on that platform. */
  defaults: Readonly<Record<ShortcutPlatform, Chord | null>>
  /** Legacy chords still accepted for one release, rendered muted after the primary. */
  aliases?: Readonly<Partial<Record<ShortcutPlatform, readonly Chord[]>>>
  /** See the module doc: `registry` is matched here; `code` is matched by the handler. */
  dispatch: ShortcutDispatch
  /** Browsers never deliver this chord to the page; only the desktop shell can. */
  browserReserved?: boolean
  /**
   * Interpolation count for an indexed label — the N in "Jump to chat {{n}}". Set
   * only on the entries whose label carries a number, so the nine chat-jump chords
   * share ONE catalog key instead of nine near-identical ones.
   */
  n?: number
}

const mac = (chord: Chord | null, other: Chord | null = chord): ShortcutEntry['defaults'] => ({ mac: chord, other })
const both = (chord: Chord): ShortcutEntry['defaults'] => ({ mac: chord, other: chord })
const alias = (...chords: Chord[]): ShortcutEntry['aliases'] => ({ mac: chords, other: chords })

/**
 * The table. Order is display order within a group. Every id here has a label
 * key in `SHORTCUT_LABEL_KEY` (useKeyboardShortcuts) — `shortcutLabelKeys.test`
 * pins the two in step.
 */
export const SHORTCUT_REGISTRY: readonly ShortcutEntry[] = [
  // ---- Chat navigation ------------------------------------------------------
  // Digits use literal Ctrl on macOS: Option+number produces characters on
  // non-US keyboards (UK: ⌥3 → #). Code-driven: the handler owns the
  // Ctrl-vs-Option toggle (MAC_CTRL_DIGITS_KEY) and the letters-for-10+ walk.
  ...[1, 2, 3, 4, 5, 6, 7, 8, 9].map((n): ShortcutEntry => ({
    id: `chat-${n}`, group: 'chat-navigation', dispatch: 'code', n,
    defaults: mac({ key: String(n), ctrl: true }, { key: String(n), alt: true }),
  })),
  // Sessions 10+ get letters (a–z minus letters other chords own — see
  // jumpLetters). One summarizing row instead of ~20 near-identical ones.
  { id: 'chat-letters', group: 'chat-navigation', dispatch: 'code',
    defaults: mac({ key: 'a…z', ctrl: true }, { key: 'a…z', alt: true }) },
  { id: 'chat-prev', group: 'chat-navigation', dispatch: 'code', defaults: both({ key: 'ArrowLeft', alt: true }) },
  { id: 'chat-next', group: 'chat-navigation', dispatch: 'code', defaults: both({ key: 'ArrowRight', alt: true }) },
  { id: 'chat-prev-bracket', group: 'chat-navigation', dispatch: 'code', defaults: both({ key: '[', mod: true }) },
  { id: 'chat-next-bracket', group: 'chat-navigation', dispatch: 'code', defaults: both({ key: ']', mod: true }) },
  { id: 'chat-mru', group: 'chat-navigation', dispatch: 'code', defaults: both({ key: '`', alt: true }) },
  { id: 'chat-mru-back', group: 'chat-navigation', dispatch: 'code', defaults: both({ key: '`', alt: true, shift: true }) },
  // ---- Panel navigation (code-driven: CORE_PANEL_MAP + the extension seam) ---
  { id: 'nav-chat', group: 'panel-navigation', dispatch: 'code', defaults: both({ key: 'c', alt: true }) },
  { id: 'nav-notifications', group: 'panel-navigation', dispatch: 'code', defaults: both({ key: 'n', alt: true }) },
  { id: 'nav-projects', group: 'panel-navigation', dispatch: 'code', defaults: both({ key: 'p', alt: true }) },
  { id: 'nav-schedule', group: 'panel-navigation', dispatch: 'code', defaults: both({ key: 's', alt: true }) },
  // ---- Actions --------------------------------------------------------------
  { id: 'focus-input', group: 'actions', dispatch: 'registry', defaults: both({ key: 'Enter', alt: true }) },
  // The shifted variant of focus-input. It MOVES FOCUS ONLY: see
  // queryPendingApprovalAction for why answering a prompt is not something a
  // chord should be able to do.
  { id: 'focus-approval', group: 'actions', dispatch: 'registry', defaults: both({ key: 'Enter', alt: true, shift: true }) },
  // ⌘N / Ctrl+N — new session, the chord every editor and chat client uses.
  // Was ⌥⇧N; kept as an alias. Browser-reserved: a browser tab opens a new
  // window on it, so there the alias is the working chord.
  { id: 'new-chat', group: 'actions', dispatch: 'registry', browserReserved: true,
    defaults: both({ key: 'n', mod: true }), aliases: alias({ key: 'n', alt: true, shift: true }) },
  // ⌘W / Ctrl+W — close session (the tab-close convention). Was ⌥⇧W; alias.
  // Browser-reserved: closes the tab. On Windows/Linux the desktop shell moves
  // its window-close role to Ctrl+Shift+W so this reaches the renderer.
  { id: 'close-chat', group: 'actions', dispatch: 'registry', browserReserved: true,
    defaults: both({ key: 'w', mod: true }), aliases: alias({ key: 'w', alt: true, shift: true }) },
  // ⌘/ / Ctrl+/ — the shortcuts reference (Slack, Notion, Linear, GitHub). Was
  // ⌥K; alias. Both fire even while shortcuts are globally disabled, so the user
  // can always reach the toggle that re-enables them.
  { id: 'shortcuts-modal', group: 'actions', dispatch: 'registry',
    defaults: both({ key: '/', mod: true }), aliases: alias({ key: 'k', alt: true }) },
  // ⌘, on macOS was already the default (the OS-standard Preferences chord, and
  // the one electron/app-menu.js advertises); Ctrl+, joins on Windows/Linux (VS
  // Code). Option/Alt+, stays an alias on every platform: a Mac browser can
  // claim ⌘, as its own Preferences accelerator before the page sees it, and the
  // desktop shell's menu item handles Ctrl+, itself on Windows/Linux.
  { id: 'open-settings', group: 'actions', dispatch: 'registry',
    defaults: both({ key: ',', mod: true }), aliases: alias({ key: ',', alt: true }) },
  // Route-history Back/Forward (#8258) — ⌘←/⌘→ on macOS, Ctrl+←/→ elsewhere,
  // matching browser convention (⌘[/⌘] being taken by session cycling above).
  // Registry-dispatched, so the P3 rebind UI covers them for free. The handler
  // adds gates the matcher cannot know. Text fields UNCLAIM the hit (the field
  // consumes ⌘←/→ as caret line-start/line-end on macOS, Ctrl+←/→ as word-jump
  // elsewhere; no navigation can result), and terminals keep the PTY. A narrow
  // viewport — where the top-bar arrows are hidden — is different: the chord is
  // CLAIMED and does nothing. On macOS an unclaimed ⌘← is the browser's own
  // Back, which would pop past an unarmed draft trap; claiming-as-no-op is what
  // keeps the two affordances agreeing without opening that hole.
  { id: 'history-back', group: 'actions', dispatch: 'registry', defaults: both({ key: 'ArrowLeft', mod: true }) },
  { id: 'history-forward', group: 'actions', dispatch: 'registry', defaults: both({ key: 'ArrowRight', mod: true }) },
  { id: 'cycle-agent', group: 'actions', dispatch: 'registry', defaults: both({ key: 'a', alt: true, shift: true }) },
  { id: 'cycle-prev-agent', group: 'actions', dispatch: 'registry', defaults: both({ key: 'z', alt: true, shift: true }) },
  { id: 'cycle-reasoning', group: 'actions', dispatch: 'registry', defaults: both({ key: 'd', alt: true, shift: true }) },
  { id: 'cycle-prev-reasoning', group: 'actions', dispatch: 'registry', defaults: both({ key: 'c', alt: true, shift: true }) },
  { id: 'cycle-approval', group: 'actions', dispatch: 'registry', defaults: both({ key: 'f', alt: true, shift: true }) },
  { id: 'cycle-prev-approval', group: 'actions', dispatch: 'registry', defaults: both({ key: 'v', alt: true, shift: true }) },
  { id: 'cycle-model', group: 'actions', dispatch: 'registry', defaults: both({ key: 's', alt: true, shift: true }) },
  { id: 'cycle-prev-model', group: 'actions', dispatch: 'registry', defaults: both({ key: 'x', alt: true, shift: true }) },
  // Deliberately not a macOS dead key. Option+E/I/U/N compose an accent, and
  // keydown.preventDefault() cannot cancel a composed character — this chord
  // fires from inside the composer, so it must not be able to leave one behind.
  { id: 'toggle-focus-mode', group: 'actions', dispatch: 'registry', defaults: both({ key: 'm', alt: true, shift: true }) },
  // Dispatched by ChatInput, not the global handler; listed for the reference.
  { id: 'optimize-prompt', group: 'actions', dispatch: 'code', defaults: both({ key: 'Enter', mod: true, shift: true }) },
  // Literal Ctrl on every platform — see isAgentMonitorChord for why this one
  // does NOT follow the ⌘-on-Mac convention (`ctrl`, not `mod`, on both).
  { id: 'agent-monitor', group: 'actions', dispatch: 'code', defaults: both({ key: 'g', ctrl: true }) },
  // Bare Escape, handled by a capture-phase listener; listed for the reference.
  { id: 'stop-speaking', group: 'actions', dispatch: 'code', defaults: both({ key: 'Escape' }) },
  // ---- Remote instances (useInstanceShortcuts, Electron only) ---------------
  // 1 = Local, 2..6 = the 1st..5th remote instance, matching the InstanceTabBar
  // left-to-right order.
  { id: 'instance-1', group: 'remote-crews', dispatch: 'code', defaults: both({ key: '1', mod: true }) },
  ...[2, 3, 4, 5, 6].map((d): ShortcutEntry => ({
    id: `instance-${d}`, group: 'remote-crews', dispatch: 'code', n: d - 1, defaults: both({ key: String(d), mod: true }),
  })),
]

const BY_ID: ReadonlyMap<string, ShortcutEntry> = new Map(SHORTCUT_REGISTRY.map(e => [e.id, e]))

export function shortcutEntry(id: string): ShortcutEntry | undefined {
  return BY_ID.get(id)
}

// ---- Overrides (P3 writes them; the resolver honours them today) -----------

/** localStorage key holding the JSON-serialized {@link ShortcutOverrides}. */
export const SHORTCUT_OVERRIDES_KEY = 'mc-shortcut-overrides'

/** Window event dispatched after an override changes, so live readers refresh. */
export const SHORTCUT_OVERRIDES_EVENT = 'mc-shortcut-overrides-changed'

/**
 * User overrides by id. A present key wins over the default — including an
 * explicit `null`, which means the user cleared that shortcut to unbound. An
 * absent key falls through to the platform default.
 */
export type ShortcutOverrides = Partial<Record<string, Chord | null>>

/**
 * A chord a user may store: a non-empty key plus at least one non-shift
 * modifier. A bare key or Shift+key would fire mid-typing. `stop-speaking`
 * (bare Escape) is a `code` entry and never reaches this check.
 */
export function isValidChord(c: Partial<Chord> | null | undefined): c is Chord {
  if (!c || typeof c.key !== 'string' || c.key.trim() === '') return false
  return c.mod === true || c.ctrl === true || c.alt === true
}

/** Canonical form: lowercase key, only the modifiers that are on. */
export function normalizeChord(c: Chord): Chord {
  const out: Chord = { key: c.key.length === 1 ? c.key.toLowerCase() : c.key }
  if (c.mod) out.mod = true
  if (c.ctrl) out.ctrl = true
  if (c.alt) out.alt = true
  if (c.shift) out.shift = true
  return out
}

/**
 * Read the stored overrides, dropping any malformed entry rather than throwing.
 * A value survives only if its id is a `registry` entry and it is `null`
 * (unbound) or a valid chord; anything else — a bad key, an unknown or
 * code-driven id, a non-object payload — is discarded so a corrupt or hostile
 * entry degrades to the code default rather than breaking keyboard input.
 */
export function loadShortcutOverrides(): ShortcutOverrides {
  const raw = safeGetItem(SHORTCUT_OVERRIDES_KEY)
  if (!raw) return {}
  const out: ShortcutOverrides = {}
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown> | null
    if (!parsed || typeof parsed !== 'object') return {}
    for (const [id, value] of Object.entries(parsed)) {
      if (BY_ID.get(id)?.dispatch !== 'registry') continue
      if (value === null) out[id] = null
      else if (isValidChord(value as Partial<Chord>)) out[id] = normalizeChord(value as Chord)
    }
  } catch {
    return {}
  }
  return out
}

// No writer here on purpose: nothing in the product records an override until the
// Settings → Keyboard UI (P3 of #4608) exists, and a write API with zero callers
// cannot be tested against a real one. That PR adds the writer beside its form;
// the contract it must honour is the reader above (registry ids only, `null` =
// unbound, valid chords normalized, broadcast SHORTCUT_OVERRIDES_EVENT after
// writing SHORTCUT_OVERRIDES_KEY).

// ---- Resolution -------------------------------------------------------------

/** The chords one shortcut currently answers to. */
export interface ResolvedShortcut {
  id: string
  /** The advertised chord; `null` when unbound (by default or by the user). */
  primary: Chord | null
  /** Legacy chords still accepted; empty once the user has overridden the primary. */
  aliases: readonly Chord[]
}

/**
 * The effective chords for `id` on `platform`: an override if present (even
 * `null`), else the default. A user override REPLACES the alias set too — a
 * rebound shortcut answers to exactly the chord the user chose, so the muted
 * legacy chord cannot keep firing behind their back.
 */
export function resolveShortcut(id: string, overrides: ShortcutOverrides, platform: ShortcutPlatform): ResolvedShortcut {
  const entry = BY_ID.get(id)
  if (!entry) return { id, primary: null, aliases: [] }
  if (entry.dispatch === 'registry' && Object.prototype.hasOwnProperty.call(overrides, id)) {
    return { id, primary: overrides[id] ?? null, aliases: [] }
  }
  return { id, primary: entry.defaults[platform], aliases: entry.aliases?.[platform] ?? [] }
}

/** Every registry entry resolved — the shape display surfaces and the handler hold. */
export function resolveShortcuts(overrides: ShortcutOverrides, platform: ShortcutPlatform): Record<string, ResolvedShortcut> {
  const out: Record<string, ResolvedShortcut> = {}
  for (const e of SHORTCUT_REGISTRY) out[e.id] = resolveShortcut(e.id, overrides, platform)
  return out
}

// ---- Matching ---------------------------------------------------------------

/**
 * `KeyboardEvent.code` → chord key token, for the punctuation the chords use.
 * Positional by design: on layouts where the glyph sits elsewhere (or needs
 * AltGr) the physical US-QWERTY key still works, matching how every chord in
 * `useKeyboardShortcuts` has always been matched. Not mapped: `NumpadEnter`
 * (the handler has always compared `code === 'Enter'`, so the numpad key is not
 * the same chord) and the numpad digits.
 */
const CODE_TOKENS: Readonly<Record<string, string>> = {
  Comma: ',', Period: '.', Slash: '/', Backslash: '\\', BracketLeft: '[', BracketRight: ']',
  Backquote: '`', Minus: '-', Equal: '=', Semicolon: ';', Quote: "'",
}

/**
 * The chord key token a keydown carries, or `null` for a bare modifier. Letters
 * and digits come from `code` (`KeyN` → `n`, `Digit1` → `1`), punctuation from
 * {@link CODE_TOKENS}, and named keys (`Enter`, `Escape`, `ArrowLeft`) are their
 * own code. Falls back to `key` only for an event with no usable code (a
 * synthetic event in a test).
 */
export function eventKeyToken(e: Pick<KeyboardEvent, 'code' | 'key'>): string | null {
  const code = e.code
  if (code) {
    if (/^Key[A-Z]$/.test(code)) return code.slice(3).toLowerCase()
    if (/^Digit\d$/.test(code)) return code.slice(5)
    const punct = CODE_TOKENS[code]
    if (punct) return punct
    return code
  }
  const key = e.key
  if (!key || key === 'Shift' || key === 'Control' || key === 'Alt' || key === 'Meta') return null
  return key.length === 1 ? key.toLowerCase() : key
}

/**
 * True when `e` is EXACTLY `chord` on `platform`. Every modifier must match —
 * a ⌘⌥N miss never satisfies ⌘N, and Shift is significant — and the primary
 * modifier the chord does not name must be up: on macOS `mod` is Meta and
 * `ctrl` is Control, held independently; off macOS both mean Control and Meta
 * (the Windows key) must not be held, mirroring `chordMatchesEvent` in
 * quickSearchShortcut.
 */
export function chordMatchesEvent(
  e: Pick<KeyboardEvent, 'code' | 'key' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>,
  chord: Chord,
  platform: ShortcutPlatform,
): boolean {
  const token = eventKeyToken(e)
  if (token === null) return false
  if ((token.length === 1 ? token.toLowerCase() : token) !== (chord.key.length === 1 ? chord.key.toLowerCase() : chord.key)) return false
  if (!!chord.alt !== e.altKey || !!chord.shift !== e.shiftKey) return false
  if (platform === 'mac') {
    return !!chord.mod === e.metaKey && !!chord.ctrl === e.ctrlKey
  }
  if (e.metaKey) return false
  return (!!chord.mod || !!chord.ctrl) === e.ctrlKey
}

/**
 * The `registry`-dispatched id whose resolved primary OR alias matches this
 * keydown, or `null`. Registry order wins a tie — there is no collision guard
 * yet (P3 adds one at record time), so two ids resolved to one chord give it to
 * the earlier entry. `code` entries are never returned: their handler branches
 * own them.
 */
export function matchShortcutEvent(
  e: Pick<KeyboardEvent, 'code' | 'key' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>,
  resolved: Readonly<Record<string, ResolvedShortcut>>,
  platform: ShortcutPlatform,
): string | null {
  for (const entry of SHORTCUT_REGISTRY) {
    if (entry.dispatch !== 'registry') continue
    const r = resolved[entry.id]
    if (!r) continue
    if (r.primary && chordMatchesEvent(e, r.primary, platform)) return entry.id
    for (const a of r.aliases) if (chordMatchesEvent(e, a, platform)) return entry.id
  }
  return null
}

/**
 * The KEY TOKENS of every non-shift Option/Alt chord the registry's
 * `registry`-dispatched entries consume (primaries and aliases, either platform).
 * `RESERVED_PANEL_CODES` in useKeyboardShortcuts must reserve the `code` of each
 * — an Alt+<letter> panel registered on one would be advertised yet never fire,
 * because the registry match runs before panel routing. `extensionSeams.test`
 * maps these back to codes (via {@link eventKeyToken}) and asserts the containment.
 */
export function registryAltNonShiftKeys(): string[] {
  const out = new Set<string>()
  for (const entry of SHORTCUT_REGISTRY) {
    if (entry.dispatch !== 'registry') continue
    const chords: Chord[] = []
    for (const p of ['mac', 'other'] as const) {
      const d = entry.defaults[p]
      if (d) chords.push(d)
      for (const a of entry.aliases?.[p] ?? []) chords.push(a)
    }
    for (const c of chords) if (c.alt && !c.shift && !c.mod && !c.ctrl) out.add(c.key)
  }
  return [...out]
}
