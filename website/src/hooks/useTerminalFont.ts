import { useSyncExternalStore } from 'react'
import { safeSetItem } from '../utils/safeStorage'

/* ── Built-in terminal appearance preference ────────────────────────────────
 * A single app-wide preference for the xterm.js terminals (activity-bar tabs
 * AND the docked bottom panel), controlling the font family, cell size and
 * cursor shape xterm renders with. It is a per-CLIENT rendering choice — the
 * font must be installed on the machine doing the viewing, and the cursor is a
 * local render too — so it is persisted in localStorage rather than server-side
 * config, and is intentionally NOT synced across devices (a font present on
 * your laptop may be absent on a remote you connect from). This mirrors
 * useBottomTerminal's module-level + localStorage store so terminals pick it up
 * without a redux round-trip.
 *
 * The font support's primary motivation is Powerline/Nerd Font glyphs: the
 * built-in terminal hard-coded a font that lacks the private-use-area glyphs
 * prompt themes rely on, so those rendered as tofu. Pointing it at an installed
 * Nerd Font fixes that. The cursor style lets a reader swap the default
 * blinking block — which obscures the character it sits on — for a thin bar or
 * underline, matching most text editors. */

/**
 * Default terminal font, shaped as a `fontFamily` style value (the historical
 * hard-coded stack) plus the historical 13px cell size. Kept as a style-shaped
 * object on purpose: the CSS font-stack string is then an excluded `fontFamily`
 * property value (a stylesheet declaration) rather than translatable UI copy.
 */
const terminalFontDefaults = {
  fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
  fontSize: 13,
}
/** Fallback stack when the user has not chosen a family. */
export const DEFAULT_TERMINAL_FONT_FAMILY = terminalFontDefaults.fontFamily
/** Default cell font size in px (the historical hard-coded value). */
export const DEFAULT_TERMINAL_FONT_SIZE = terminalFontDefaults.fontSize

// `BUNDLED_MONO_FONTS` lives in utils/fontFamilyOptions.ts alongside the other
// font-name literals so the ESLint i18n exemption stays scoped to a single
// module. Consumers (DisplayPanel's terminal font picker) should import from
// there directly.
/** Font-size bounds — below 8px the cell is unreadable, above 32px one line fills the pane. */
export const MIN_TERMINAL_FONT_SIZE = 8
export const MAX_TERMINAL_FONT_SIZE = 32

/**
 * The xterm cursor shapes we expose, a subset of xterm's `cursorStyle` union.
 * `block` is the historical hard-coded default, so defaulting to it keeps every
 * existing terminal unchanged until the user asks. `bar` and `underline` are
 * the thin-cursor shapes the request asked for (and most editors use).
 */
export type TerminalCursorStyle = 'block' | 'bar' | 'underline'
export const TERMINAL_CURSOR_STYLES: readonly TerminalCursorStyle[] = ['block', 'bar', 'underline']
/** Default cursor shape (the historical hard-coded value). */
export const DEFAULT_TERMINAL_CURSOR_STYLE: TerminalCursorStyle = 'block'

const isCursorStyle = (v: unknown): v is TerminalCursorStyle =>
  typeof v === 'string' && (TERMINAL_CURSOR_STYLES as readonly string[]).includes(v)

export interface TerminalFontState {
  /** Raw user-entered font family. Empty string means "use the default stack". */
  fontFamily: string
  /** Cell font size in px, always clamped to [MIN, MAX]. */
  fontSize: number
  /** xterm cursor shape — block (default), bar, or underline. */
  cursorStyle: TerminalCursorStyle
}

const STORAGE_KEY = 'mc-terminal-font'

const clampSize = (n: number): number =>
  Math.max(MIN_TERMINAL_FONT_SIZE, Math.min(MAX_TERMINAL_FONT_SIZE, Math.round(n)))

function loadPersisted(): TerminalFontState {
  const base: TerminalFontState = {
    fontFamily: '',
    fontSize: DEFAULT_TERMINAL_FONT_SIZE,
    cursorStyle: DEFAULT_TERMINAL_CURSOR_STYLE,
  }
  if (typeof localStorage === 'undefined') return base
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return base
    const p = JSON.parse(raw) as Partial<TerminalFontState> | null
    if (!p || typeof p !== 'object') return base
    return {
      fontFamily: typeof p.fontFamily === 'string' ? p.fontFamily : '',
      fontSize: typeof p.fontSize === 'number' ? clampSize(p.fontSize) : DEFAULT_TERMINAL_FONT_SIZE,
      cursorStyle: isCursorStyle(p.cursorStyle) ? p.cursorStyle : DEFAULT_TERMINAL_CURSOR_STYLE,
    }
  } catch {
    return base
  }
}

let state: TerminalFontState = loadPersisted()
const listeners = new Set<() => void>()

function emit(): void { for (const cb of listeners) cb() }

function set(next: TerminalFontState): void {
  if (
    next.fontFamily === state.fontFamily &&
    next.fontSize === state.fontSize &&
    next.cursorStyle === state.cursorStyle
  ) return
  state = next
  emit()
  try { safeSetItem(STORAGE_KEY, JSON.stringify(state)) } catch { /* quota / locked storage */ }
}

/* ── Actions (module functions so the settings panel AND non-React callers can
 *    drive the preference) ── */

export function setTerminalFontFamily(fontFamily: string): void {
  set({ ...state, fontFamily })
}

export function setTerminalFontSize(px: number): void {
  set({ ...state, fontSize: clampSize(px) })
}

export function setTerminalCursorStyle(cursorStyle: TerminalCursorStyle): void {
  set({ ...state, cursorStyle })
}

export function resetTerminalFont(): void {
  set({ fontFamily: '', fontSize: DEFAULT_TERMINAL_FONT_SIZE, cursorStyle: DEFAULT_TERMINAL_CURSOR_STYLE })
}

/** Current preference — for non-React callers (xterm construction in CliPanel). */
export function getTerminalFont(): TerminalFontState { return state }

/**
 * Build a valid CSS/xterm `fontFamily` string from the raw user input.
 *
 * xterm passes `fontFamily` straight into a canvas `font` shorthand, so a
 * multi-word family name MUST be quoted (`JetBrainsMonoNL Nerd Font Mono` is
 * three unquoted family tokens otherwise) and there MUST be a generic
 * `monospace` fallback so a typo'd or uninstalled family degrades to the
 * platform monospace instead of a proportional font. Empty input falls back to
 * the historical default stack.
 */
export function resolveTerminalFontFamily(input: string): string {
  const raw = input.trim()
  if (!raw) return DEFAULT_TERMINAL_FONT_FAMILY
  const tokens = raw.split(',').map(t => t.trim()).filter(Boolean)
  if (tokens.length === 0) return DEFAULT_TERMINAL_FONT_FAMILY
  const quoted = tokens.map(t => {
    // Leave an already-quoted token or a single unspaced token (family or generic) as-is.
    if (/^['"].*['"]$/.test(t)) return t
    return /\s/.test(t) ? `'${t}'` : t
  })
  const hasGenericFallback = tokens.some(t => t.toLowerCase() === 'monospace')
  if (!hasGenericFallback) quoted.push('monospace')
  return quoted.join(', ')
}

/* ── React binding ── */

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}
function getSnapshot(): TerminalFontState { return state }

export function useTerminalFont(): TerminalFontState {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot)
}

/**
 * Subscribe to preference changes from a non-React module (CliPanel pushes the
 * new font onto every live xterm instance). Returns an unsubscribe fn.
 */
export function subscribeTerminalFont(cb: () => void): () => void {
  return subscribe(cb)
}

/** Test-only: reset the module store and its persisted copy. */
export function __resetTerminalFontStore(): void {
  state = { fontFamily: '', fontSize: DEFAULT_TERMINAL_FONT_SIZE, cursorStyle: DEFAULT_TERMINAL_CURSOR_STYLE }
  emit()
  if (typeof localStorage !== 'undefined') {
    try { localStorage.removeItem(STORAGE_KEY) } catch { /* ignore */ }
  }
}
