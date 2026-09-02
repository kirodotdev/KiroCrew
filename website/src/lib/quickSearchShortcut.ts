import { isMac } from '../utils/platform'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

/**
 * Configurable activation shortcut for the Search Everywhere command palette.
 *
 * Historically the palette was hard-wired to two gestures in `useCommandPalette`:
 * double-Shift (the IntelliJ "Search Everywhere" gesture) and ⌘K / Ctrl+K. This
 * module makes the *primary* gesture user-selectable.
 *
 * Three presets, mutually exclusive:
 *  - **`mod-k`** (default): ⌘K (macOS) / Ctrl+K (Windows/Linux) toggles the
 *    palette; double-Shift is disabled.
 *  - **`double-shift`** (opt-in): two bare Shift keydowns open the palette. This
 *    was the shipped default until remote-desktop sessions (RDP/ICA/PCoIP) were
 *    found to synthesise spurious repeat Shift events and open the palette on
 *    their own, so it is now opt-in — only users who select it are exposed.
 *  - **`custom`**: a user-recorded chord toggles it; double-Shift is disabled.
 *
 * `⌘K` / `Ctrl+K` stays available as a built-in alias in every mode EXCEPT
 * `custom` with a recorded chord — see {@link isModKEvent} and
 * {@link matchesQuickSearchShortcut}. It is the near-universal command-palette
 * gesture and is page-safe, so it is the escape hatch that guarantees the
 * palette is always reachable from the keyboard until the user deliberately
 * binds their own chord.
 *
 * The preference is persisted to localStorage and read live per keystroke (no
 * listener re-registration), mirroring the `SHORTCUTS_ENABLED_KEY` pattern in
 * `useKeyboardShortcuts`. Writers broadcast {@link QUICK_SEARCH_SHORTCUT_EVENT}
 * so display surfaces (Settings → Shortcuts, the Alt+K modal) re-read without a
 * reload.
 */

/** localStorage key holding the JSON-serialized {@link QuickSearchConfig}. */
export const QUICK_SEARCH_SHORTCUT_KEY = 'mc-quicksearch-shortcut'

/** Window event dispatched after the preference changes, so live readers refresh. */
export const QUICK_SEARCH_SHORTCUT_EVENT = 'mc-quicksearch-shortcut-changed'

export type QuickSearchMode = 'double-shift' | 'mod-k' | 'custom'

/**
 * A normalized, platform-neutral custom chord.
 *
 * `mod` is the platform's primary command modifier — Cmd (`metaKey`) on macOS,
 * Ctrl (`ctrlKey`) on Windows/Linux — so one stored chord means "⌘J" on a Mac
 * and "Ctrl+J" elsewhere, which is how cross-platform apps keep a binding
 * consistent. `key` is a stable physical token (a lowercased letter/digit from
 * `KeyboardEvent.code`, e.g. `KeyJ` → `j`, or a named key like `ArrowRight`),
 * NOT `KeyboardEvent.key` — the latter changes with Shift (`k` vs `K`) and with
 * the macOS Option layer (Alt+K emits `˚`), which would make a chord unmatchable.
 */
export interface QuickSearchChord {
  key: string
  /** Cmd on macOS / Ctrl on Windows/Linux. */
  mod?: boolean
  /** Option on macOS / Alt elsewhere. */
  alt?: boolean
  shift?: boolean
}

export interface QuickSearchConfig {
  mode: QuickSearchMode
  /** Present only for `mode: 'custom'`. */
  custom?: QuickSearchChord
}

/** Default for fresh installs: ⌘K/Ctrl+K (mod-k). Double-Shift is opt-in via Settings. */
export const DEFAULT_QUICK_SEARCH_CONFIG: QuickSearchConfig = { mode: 'mod-k' }

/**
 * A custom chord must carry at least one command/option modifier (`mod` or
 * `alt`). A bare key — or Shift alone, which merely types a capital — would fire
 * mid-typing, so the recorder and the loader both reject it. This keeps a
 * malformed or hostile localStorage value from installing a footgun binding.
 */
export function isValidChord(c: Partial<QuickSearchChord> | null | undefined): c is QuickSearchChord {
  if (!c || typeof c.key !== 'string' || c.key.trim() === '') return false
  return c.mod === true || c.alt === true
}

/** Lowercase the key token so matching is case-insensitive and Shift-stable. */
export function normalizeChord(c: QuickSearchChord): QuickSearchChord {
  const out: QuickSearchChord = { key: c.key.toLowerCase() }
  if (c.mod) out.mod = true
  if (c.alt) out.alt = true
  if (c.shift) out.shift = true
  return out
}

/**
 * Read the stored preference, falling back to the default on any absent,
 * malformed, or invalid value. Never throws — a corrupt entry degrades to the
 * default double-Shift behavior rather than breaking keyboard input.
 */
export function loadQuickSearchConfig(): QuickSearchConfig {
  const raw = safeGetItem(QUICK_SEARCH_SHORTCUT_KEY)
  if (!raw) return DEFAULT_QUICK_SEARCH_CONFIG
  try {
    const parsed = JSON.parse(raw) as Partial<QuickSearchConfig> | null
    const mode = parsed?.mode
    if (mode === 'double-shift' || mode === 'mod-k') return { mode }
    if (mode === 'custom' && isValidChord(parsed?.custom)) {
      return { mode: 'custom', custom: normalizeChord(parsed!.custom!) }
    }
  } catch {
    /* fall through to the default */
  }
  return DEFAULT_QUICK_SEARCH_CONFIG
}

/**
 * Persist the preference and broadcast {@link QUICK_SEARCH_SHORTCUT_EVENT}.
 * The `custom` payload is dropped for preset modes so a later preset switch
 * cannot leave a stale chord behind. A `custom` mode with an invalid chord is
 * refused (returns false) rather than persisting an unusable binding.
 */
export function saveQuickSearchConfig(config: QuickSearchConfig): boolean {
  let toStore: QuickSearchConfig
  if (config.mode === 'custom') {
    if (!isValidChord(config.custom)) return false
    toStore = { mode: 'custom', custom: normalizeChord(config.custom) }
  } else {
    toStore = { mode: config.mode }
  }
  const ok = safeSetItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify(toStore))
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(QUICK_SEARCH_SHORTCUT_EVENT))
  }
  return ok
}

/**
 * The stable physical key token for a keydown, or `null` when the event carries
 * no usable key (a bare modifier press, or a dead key). Prefers
 * `KeyboardEvent.code` for letters/digits so the token is independent of Shift
 * state and the macOS Option layer; falls back to `KeyboardEvent.key` for named
 * keys (`ArrowUp`, `Enter`, punctuation).
 */
export function eventKeyToken(e: Pick<KeyboardEvent, 'code' | 'key'>): string | null {
  const code = e.code
  if (code) {
    if (/^Key[A-Z]$/.test(code)) return code.slice(3).toLowerCase()
    if (/^Digit\d$/.test(code)) return code.slice(5)
    if (/^Numpad\d$/.test(code)) return code.slice(6)
  }
  const key = e.key
  if (!key || key === 'Shift' || key === 'Control' || key === 'Alt' || key === 'Meta') return null
  return key.length === 1 ? key.toLowerCase() : key
}

/**
 * True when `e` is ⌘K (macOS) / Ctrl+K (Windows/Linux) with no other modifier.
 * Platform-agnostic on purpose — it accepts `metaKey || ctrlKey` so the alias
 * works on any OS, matching the original hard-coded behavior.
 */
export function isModKEvent(e: Pick<KeyboardEvent, 'key' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>): boolean {
  return (e.key === 'k' || e.key === 'K') && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey
}

/**
 * True when `e` exactly matches `chord` on the given platform. `mod` maps to
 * `metaKey` on macOS and `ctrlKey` elsewhere; the opposite primary modifier
 * must NOT be held (so a Mac ⌃J never satisfies a `mod` chord meant as ⌘J), and
 * `alt`/`shift` must match exactly. `mac` is injectable (defaulting to the
 * detected platform) so both behaviours are testable without reloading the
 * module — mirroring `isSettingsChord` in useKeyboardShortcuts.
 */
export function chordMatchesEvent(
  e: Pick<KeyboardEvent, 'code' | 'key' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>,
  chord: QuickSearchChord,
  mac: boolean = isMac,
): boolean {
  const token = eventKeyToken(e)
  if (token === null || token.toLowerCase() !== chord.key.toLowerCase()) return false
  const modDown = mac ? e.metaKey : e.ctrlKey
  const crossModDown = mac ? e.ctrlKey : e.metaKey
  if (crossModDown) return false
  if (!!chord.mod !== modDown) return false
  if (!!chord.alt !== e.altKey) return false
  if (!!chord.shift !== e.shiftKey) return false
  return true
}

/**
 * Whether a recorded custom chord is currently the exclusive binding. When true,
 * the ⌘K/Ctrl+K alias is suppressed (the user has taken explicit control);
 * otherwise the alias stays live as the universal escape hatch.
 */
export function hasCustomChord(config: QuickSearchConfig): config is QuickSearchConfig & { custom: QuickSearchChord } {
  return config.mode === 'custom' && isValidChord(config.custom)
}

/** Human keycaps for a config's PRIMARY gesture, platform-aware, for rendering. */
export function formatQuickSearchKeys(config: QuickSearchConfig, mac: boolean = isMac): string[] {
  switch (config.mode) {
    case 'mod-k':
      return mac ? ['⌘', 'K'] : ['Ctrl', 'K']
    case 'custom':
      return config.custom ? formatChordKeys(config.custom, mac) : []
    case 'double-shift':
    default:
      return mac ? ['⇧', '⇧'] : ['Shift', 'Shift']
  }
}

/** Human keycaps for a single chord, platform-aware (modifiers first, then key). */
export function formatChordKeys(chord: QuickSearchChord, mac: boolean = isMac): string[] {
  const caps: string[] = []
  if (chord.mod) caps.push(mac ? '⌘' : 'Ctrl')
  if (chord.alt) caps.push(mac ? '⌥' : 'Alt')
  if (chord.shift) caps.push(mac ? '⇧' : 'Shift')
  caps.push(displayKeyCap(chord.key))
  return caps
}

/** Display form of a stored key token: single letters upper-cased, names as-is. */
export function displayKeyCap(key: string): string {
  return key.length === 1 ? key.toUpperCase() : key
}
