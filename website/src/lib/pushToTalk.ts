/**
 * Push-to-talk binding: what key starts voice input, and what press/release means.
 *
 * The industry-convergent shape (VoiceInk, superwhisper, Wispr Flow all land here):
 * hold ONE bare modifier, and let the user pick WHICH one. A bare modifier is the
 * only key class that produces no character while held, emits no auto-repeat, and
 * cannot collide with an editor chord — which matters because the composer almost
 * always has focus while dictating.
 *
 * Stored BROWSER-LOCAL (localStorage), not in the server-side STT config, for the
 * same reason `getPreferredMicId` is: the right key depends on the keyboard in
 * front of you, and one account reaches the dashboard from several machines. A
 * server-side default would push the wrong key onto every other device.
 */
import { safeSetItem } from '../utils/safeStorage'
import { i18nT } from '../i18n/t'

export type PttMode = 'toggle' | 'ptt' | 'hybrid'

/**
 * A binding, keyed by `KeyboardEvent.code` so it is POSITIONAL — the physical key
 * matters, not the glyph the current layout paints on it (the same convention
 * `useKeyboardShortcuts` uses).
 *
 * Two shapes share this one type:
 * - **bare modifier** — `code` is itself a modifier (`AltRight`) and every flag is
 *   unset. Held alone; this is the default and the recommended shape.
 * - **chord** — `code` is a normal key (`Space`) plus the required modifier flags.
 *   The Windows/Linux default, and whatever a user records as a custom binding.
 */
export interface PttBinding {
  code: string
  alt?: boolean
  ctrl?: boolean
  meta?: boolean
  shift?: boolean
}

export interface PttConfig {
  mode: PttMode
  binding: PttBinding
  /** Hold duration that separates a tap from a hold, in ms. Hybrid mode only. */
  holdMs: number
}

export const PTT_STORAGE_KEY = 'mc-ptt-config'
export const PTT_CHANGED_EVENT = 'mc-ptt-config-changed'

/** Hold-threshold bounds. 500ms is what VoiceInk and superwhisper both ship. */
export const HOLD_MS_DEFAULT = 500
export const HOLD_MS_MIN = 200
export const HOLD_MS_MAX = 1500
export const HOLD_MS_STEP = 100

/**
 * Hard ceiling on one hold, in ms. A release we never hear about (focus stolen
 * mid-hold, OS grabbing the chord, a modal seizing the window) would otherwise
 * leave the mic open indefinitely — the single worst failure mode of a
 * hold-to-talk binding. The watchdogs in `usePushToTalk` catch the common cases;
 * this is the backstop for the ones they don't.
 */
export const MAX_HOLD_MS = 120_000

/** Every modifier code, by the modifier FAMILY it belongs to. */
const MODIFIER_FAMILY: Record<string, 'alt' | 'ctrl' | 'meta' | 'shift'> = {
  AltLeft: 'alt', AltRight: 'alt',
  ControlLeft: 'ctrl', ControlRight: 'ctrl',
  MetaLeft: 'meta', MetaRight: 'meta',
  ShiftLeft: 'shift', ShiftRight: 'shift',
}

/** True on macOS. Mirrors `useKeyboardShortcuts.IS_MAC` rather than importing it,
 *  so this module stays free of React/router imports and is trivially testable. */
export const IS_MAC =
  typeof navigator !== 'undefined' &&
  (/Mac|iPhone|iPad/.test(navigator.platform ?? '') || /Macintosh/.test(navigator.userAgent ?? ''))

/**
 * Platform default.
 *
 * macOS gets bare right Option — the common intersection of what VoiceInk,
 * superwhisper and Wispr Flow offer, and free of any system binding.
 *
 * Windows/Linux CANNOT have it: on most layouts there the right Alt key is
 * AltGr, which reports `ctrlKey && altKey` and composes characters, and a lone
 * left Alt reveals the window menu. Those platforms get the ⌥⇧Space chord, which
 * is unclaimed on all three OSes (plain Alt+Space is the Windows system menu;
 * adding Shift takes it out of that path).
 */
export function defaultBinding(mac: boolean = IS_MAC): PttBinding {
  return mac ? { code: 'AltRight' } : { code: 'Space', alt: true, shift: true }
}

export function defaultConfig(mac: boolean = IS_MAC): PttConfig {
  return { mode: 'hybrid', binding: defaultBinding(mac), holdMs: HOLD_MS_DEFAULT }
}

/**
 * The bare-modifier keys offered in the Settings dropdown, in recommendation
 * order. Left Control is absent on purpose: it is the one modifier macOS itself
 * leans on for emacs-style text bindings (⌃A / ⌃E / ⌃K all work in any text
 * field), so binding it would break editing for the users most likely to notice.
 */
export const SELECTABLE_BARE_CODES = [
  'AltRight', 'AltLeft', 'MetaRight', 'ControlRight', 'ShiftRight',
] as const

/** True when this binding is a lone modifier held by itself. */
export function isBareModifier(b: PttBinding): boolean {
  return !!MODIFIER_FAMILY[b.code] && !b.alt && !b.ctrl && !b.meta && !b.shift
}

type KeyLike = Pick<KeyboardEvent, 'code' | 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'>

/**
 * Does this keystroke arm the binding?
 *
 * A bare-modifier binding is matched on `code` PLUS the requirement that its own
 * family is the ONLY active one. Without that second half, holding ⌃⌥ would arm
 * an `AltRight` binding: the AltRight keydown carries `altKey` regardless of what
 * else is down, so `code` alone cannot tell "Option held alone" from "Option held
 * as part of something bigger". Same reasoning as the single-primary-modifier
 * tests in `useKeyboardShortcuts`.
 */
export function matchesBinding(e: KeyLike, b: PttBinding): boolean {
  if (e.code !== b.code) return false
  const family = MODIFIER_FAMILY[b.code]
  if (family && isBareModifier(b)) {
    return (family === 'alt' || !e.altKey)
      && (family === 'ctrl' || !e.ctrlKey)
      && (family === 'meta' || !e.metaKey)
      && (family === 'shift' || !e.shiftKey)
  }
  return e.altKey === !!b.alt && e.ctrlKey === !!b.ctrl
    && e.metaKey === !!b.meta && e.shiftKey === !!b.shift
}

/**
 * Is the binding's key STILL physically down, according to a later event?
 *
 * The reconciliation half of the stuck-mic defence: a KeyboardEvent's modifier
 * flags report live hardware state, so any subsequent keystroke can prove a
 * release we never received.
 *
 * Reads the flags rather than `getModifierState(name)`: for the four modifier
 * families they carry identical information, and the name form would need a
 * literal `'Alt'`/`'Control'`/`'Meta'`/`'Shift'` table -- DOM-API argument
 * strings that the i18n lint (which reads inside module constants) cannot tell
 * apart from UI copy. Only answerable for a bare modifier -- a chord's primary
 * key has no flag, so a chord binding leans on blur + the hard cap instead, and
 * this returns `true` ("no evidence it is up").
 */
export function stillHeld(
  e: Pick<KeyboardEvent, 'altKey' | 'ctrlKey' | 'metaKey' | 'shiftKey'>,
  b: PttBinding,
): boolean {
  const family = MODIFIER_FAMILY[b.code]
  if (!family || !isBareModifier(b)) return true
  return family === 'alt' ? e.altKey
    : family === 'ctrl' ? e.ctrlKey
      : family === 'meta' ? e.metaKey
        : e.shiftKey
}

/** Keycap labels for display, e.g. `['⌥','⇧','Space']` or `['右 ⌥']`-shaped ids. */
export function bindingCaps(b: PttBinding, mac: boolean = IS_MAC): string[] {
  if (isBareModifier(b)) return [bareCap(b.code, mac)]
  const caps: string[] = []
  if (b.meta) caps.push(mac ? '\u2318' : 'Ctrl')
  if (b.ctrl) caps.push(mac ? '\u2303' : 'Ctrl')
  if (b.alt) caps.push(mac ? '\u2325' : 'Alt')
  if (b.shift) caps.push(mac ? '\u21e7' : 'Shift')
  caps.push(displayKey(b.code))
  return caps
}

function bareCap(code: string, mac: boolean): string {
  const side = code.endsWith('Right') ? 'R' : 'L'
  const family = MODIFIER_FAMILY[code]
  const glyph = family === 'alt' ? (mac ? '\u2325' : 'Alt')
    : family === 'ctrl' ? (mac ? '\u2303' : 'Ctrl')
      : family === 'meta' ? (mac ? '\u2318' : 'Win')
        : (mac ? '\u21e7' : 'Shift')
  return `${side} ${glyph}`
}

/** `KeyboardEvent.code` → the label a keycap shows. */
export function displayKey(code: string): string {
  if (code.startsWith('Key')) return code.slice(3)
  if (code.startsWith('Digit')) return code.slice(5)
  if (code.startsWith('Numpad')) {
    return i18nT('pages.settings.sttSettings.ptt_key_numpad', { n: code.slice(6) })
  }
  if (code === 'ArrowLeft') return '\u2190'
  if (code === 'ArrowRight') return '\u2192'
  if (code === 'ArrowUp') return '\u2191'
  if (code === 'ArrowDown') return '\u2193'
  return code
}

/**
 * Catalog key for a modifier's spelled-out name, e.g. "Right Option ⌥".
 *
 * Covers all EIGHT modifier codes, not just the five the dropdown offers: the
 * test strip has to be able to NAME whatever the user actually pressed, and
 * "you pressed Left Control" is the whole point of the wrong-key state.
 *
 * Full literal keys in a flat Record — the only shape
 * `scripts/check-i18n-keys.mjs` resolves statically. Lives under the panel that
 * renders them (`lib` is not an i18n namespace).
 */
export const BARE_CODE_LABEL_KEY: Record<string, string> = {
  AltRight: 'pages.settings.sttSettings.ptt_key_right_option',
  AltLeft: 'pages.settings.sttSettings.ptt_key_left_option',
  MetaRight: 'pages.settings.sttSettings.ptt_key_right_command',
  MetaLeft: 'pages.settings.sttSettings.ptt_key_left_command',
  ControlRight: 'pages.settings.sttSettings.ptt_key_right_control',
  ControlLeft: 'pages.settings.sttSettings.ptt_key_left_control',
  ShiftRight: 'pages.settings.sttSettings.ptt_key_right_shift',
  ShiftLeft: 'pages.settings.sttSettings.ptt_key_left_shift',
}

/**
 * The same eight names as a Windows/Linux keyboard prints them.
 *
 * Needed because spelling the names out — the fix for an unreadable `R ⌥`
 * abbreviation — silently shipped Mac vocabulary everywhere: a Windows user was
 * offered "Right Option ⌥" and "Right Command ⌘" for keys their keyboard labels
 * Alt and Win. `bareCap` was already platform-aware; the spelled-out names were
 * not.
 */
/**
 * The panel's own prose is platform-specific: the mac default is a BARE right
 * Option, the non-mac default is the Alt+Shift+Space chord, and neither
 * sentence is true on the other platform. Rendering the mac copy everywhere
 * put a false claim ("Right Option works out of the box") directly above a
 * test strip that says "Press Alt + Shift + Space" — the same class of defect
 * already fixed for the key NAMES via BARE_CODE_LABEL_KEY_OTHER. Kept as a
 * module-level const because the i18n dynamic-key gate only resolves direct
 * indexing of one of these at the i18nT() call site.
 */
export const PTT_COPY_KEY = {
  headingDescMac: 'pages.settings.sttSettings.ptt_heading_desc',
  headingDescOther: 'pages.settings.sttSettings.ptt_heading_desc_other',
  keyDescMac: 'pages.settings.sttSettings.ptt_key_desc',
  keyDescOther: 'pages.settings.sttSettings.ptt_key_desc_other',
} as const

export const BARE_CODE_LABEL_KEY_OTHER: Record<string, string> = {
  AltRight: 'pages.settings.sttSettings.ptt_key_right_alt',
  AltLeft: 'pages.settings.sttSettings.ptt_key_left_alt',
  MetaRight: 'pages.settings.sttSettings.ptt_key_right_win',
  MetaLeft: 'pages.settings.sttSettings.ptt_key_left_win',
  ControlRight: 'pages.settings.sttSettings.ptt_key_right_ctrl',
  ControlLeft: 'pages.settings.sttSettings.ptt_key_left_ctrl',
  ShiftRight: 'pages.settings.sttSettings.ptt_key_right_shift_win',
  ShiftLeft: 'pages.settings.sttSettings.ptt_key_left_shift_win',
}

/** The label table for this platform's keyboard vocabulary. */
export function bareCodeLabelKeys(mac: boolean = IS_MAC): Record<string, string> {
  return mac ? BARE_CODE_LABEL_KEY : BARE_CODE_LABEL_KEY_OTHER
}

/**
 * A binding's SPELLED-OUT name, resolved at render time — "Right Option ⌥",
 * not the "R ⌥" keycap.
 *
 * The distinction is load-bearing, not cosmetic. A first-run review of the
 * abbreviated form found it unreadable: a new user has to infer that R means
 * Right, on top of ⌥ already being the least-recognised glyph on a Mac
 * keyboard — and the abbreviation was worst exactly where the copy most needed
 * to be unmistakable, in the "this is not the key you picked" warning. Use this
 * in PROSE; keep {@link bindingCaps} for the keycap chips.
 *
 * A chord (or any non-modifier key) has no name to spell out, so it falls back
 * to its keycap sequence joined the way the platform writes chords.
 */
export function bindingLabel(b: PttBinding, mac: boolean = IS_MAC): string {
  if (isBareModifier(b) && Object.prototype.hasOwnProperty.call(bareCodeLabelKeys(mac), b.code)) {
    // Each map is indexed DIRECTLY at the i18nT() call rather than through an
    // alias or a function return — that literal form is what
    // `scripts/check-i18n-keys.mjs` can resolve statically, so these stay out of
    // the dynamic-key population the gate cannot verify.
    return mac ? i18nT(BARE_CODE_LABEL_KEY[b.code]) : i18nT(BARE_CODE_LABEL_KEY_OTHER[b.code])
  }
  return bindingCaps(b, mac).join(mac ? '' : ' + ')
}

/** A hold duration in whole-tenths of a second — "0.5", "1.2". Milliseconds are
 *  a developer unit; every user-facing duration string takes seconds. */
export function toSeconds(ms: number): string {
  return (Math.round(ms / 100) / 10).toFixed(1)
}

/** Clamp a hold threshold into range, rejecting non-finite input. */
export function clampHoldMs(ms: unknown): number {
  const n = typeof ms === 'number' && Number.isFinite(ms) ? Math.round(ms) : HOLD_MS_DEFAULT
  return Math.min(HOLD_MS_MAX, Math.max(HOLD_MS_MIN, n))
}

const MODES: readonly PttMode[] = ['toggle', 'ptt', 'hybrid']

/**
 * Coerce anything (a parsed localStorage blob, a partial patch) into a valid
 * config, field by field. Per-field rather than all-or-nothing so one corrupt
 * value doesn't silently reset the other two.
 */
export function normalizeConfig(raw: unknown, mac: boolean = IS_MAC): PttConfig {
  const base = defaultConfig(mac)
  if (!raw || typeof raw !== 'object') return base
  const o = raw as Record<string, unknown>
  const mode = MODES.includes(o.mode as PttMode) ? (o.mode as PttMode) : base.mode
  return { mode, binding: normalizeBinding(o.binding, mac), holdMs: clampHoldMs(o.holdMs) }
}

/**
 * Coerce a stored binding. A binding whose `code` is missing or not a string is
 * unusable, so it falls back to the platform default rather than to "no binding"
 * — a config that silently binds nothing looks identical to a broken keyboard.
 */
export function normalizeBinding(raw: unknown, mac: boolean = IS_MAC): PttBinding {
  if (!raw || typeof raw !== 'object') return defaultBinding(mac)
  const o = raw as Record<string, unknown>
  if (typeof o.code !== 'string' || o.code === '') return defaultBinding(mac)
  const b: PttBinding = { code: o.code }
  if (o.alt === true) b.alt = true
  if (o.ctrl === true) b.ctrl = true
  if (o.meta === true) b.meta = true
  if (o.shift === true) b.shift = true
  // A non-modifier key with no modifiers at all would bind a bare character key
  // (Space, a letter) and make the composer untypable. Refuse it.
  if (!MODIFIER_FAMILY[b.code] && !b.alt && !b.ctrl && !b.meta && !b.shift) return defaultBinding(mac)
  return b
}

export function loadPttConfig(mac: boolean = IS_MAC): PttConfig {
  try {
    const raw = localStorage.getItem(PTT_STORAGE_KEY)
    return normalizeConfig(raw ? JSON.parse(raw) : null, mac)
  } catch {
    return defaultConfig(mac)
  }
}

/** Persist and broadcast, so every mounted consumer re-reads without prop drilling. */
export function savePttConfig(cfg: PttConfig): void {
  safeSetItem(PTT_STORAGE_KEY, JSON.stringify(cfg))
  try {
    window.dispatchEvent(new Event(PTT_CHANGED_EVENT))
  } catch {
    /* no window (SSR/test env) — persistence already happened */
  }
}
