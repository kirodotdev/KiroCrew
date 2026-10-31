/**
 * The grammar of an editor-binding trigger: a key chord or a modifier-click, and
 * how an editor event spells one.
 *
 * Its own module so the editor can read an event without importing the
 * contribution reader, which reaches the API client and, through it, the
 * dashboard's language layer; the editor's memo boundaries would otherwise count
 * as language-dependent for a rule that never shows text. Nothing here renders,
 * fetches or translates.
 */
import { loadShortcutOverrides, resolveShortcuts, type Chord } from '../lib/shortcutRegistry'

/** Modifiers a trigger may require. `primary` is Cmd on macOS, Ctrl elsewhere. */
export type BindingModifier = 'primary' | 'shift' | 'alt'

/** A validated trigger: a key chord or a modifier-click. */
export interface EditorBindingTrigger {
  /** Lowercase key name (`f12`, `o`); empty for a click. */
  key: string
  click: boolean
  /** Sorted, so two spellings of one chord compare equal. */
  modifiers: BindingModifier[]
  /** `primary+shift+f12` or `click:alt`: the spelling the manifest and the POST share. */
  canonical: string
}

/** The modifier state of a key or pointer event, as the editor sees it. */
export interface BindingEvent {
  key?: string
  button?: number
  shiftKey: boolean
  altKey: boolean
  metaKey: boolean
  ctrlKey: boolean
}

/** Mirrors `EDITOR_BINDING_MODIFIERS` in `apps/manifest_contributes.py`. */
export const MODIFIERS: ReadonlySet<BindingModifier> = new Set<BindingModifier>(['primary', 'shift', 'alt'])
/** Mirrors `_EDITOR_BINDING_KEY_RE`: F1-F12, a letter or a digit. */
export const KEY_RE = /^(f([1-9]|1[0-2])|[a-z0-9])$/

/** The canonical spelling of a trigger, as `EditorBindingTrigger.canonical()` writes it. */
export function canonicalTrigger(key: string, click: boolean, modifiers: readonly string[]): string {
  const mods = [...modifiers].sort().join('+')
  if (click) return `click:${mods}`
  return mods ? `${mods}+${key}` : key
}

/**
 * The save key and the dashboard's quick search, refused on every platform. The
 * dashboard's other shortcuts are refused through {@link dashboardShortcutFor};
 * `RESERVED_EDITOR_CHORDS` in Python carries both, for the chords every platform
 * shares.
 */
export const RESERVED_CHORDS: ReadonlySet<string> = new Set(['s', 'k'].map(k => canonicalTrigger(k, false, ['primary'])))

/** Why a trigger is not bindable. */
export type TriggerProblem = 'key-or-click' | 'click-needs-modifier' | 'bad-key' | 'key-needs-modifier' | 'reserved'

/**
 * Why a trigger is not bindable, or null when it is: the rules a manifest's
 * trigger meets, applied the same way to a chord the reader records.
 */
export function triggerProblem(key: string, click: boolean, modifiers: readonly BindingModifier[]): TriggerProblem | null {
  if (!key === !click) return 'key-or-click'
  if (click && modifiers.length === 0) return 'click-needs-modifier'
  if (key && !KEY_RE.test(key)) return 'bad-key'
  if (key && !key.startsWith('f') && modifiers.length === 0) return 'key-needs-modifier'
  if (RESERVED_CHORDS.has(canonicalTrigger(key, click, modifiers))) return 'reserved'
  return null
}

/** A canonical spelling back to a trigger, or null when it is not a bindable one. */
export function parseCanonicalTrigger(canonical: string): EditorBindingTrigger | null {
  if (typeof canonical !== 'string' || !canonical) return null
  let key = ''
  let click = false
  let mods: string[]
  if (canonical.startsWith('click:')) {
    click = true
    mods = canonical.slice('click:'.length).split('+').filter(Boolean)
  } else {
    const parts = canonical.split('+')
    key = parts.pop() ?? ''
    mods = parts
  }
  if (!mods.every(m => MODIFIERS.has(m as BindingModifier))) return null
  const modifiers = [...new Set(mods as BindingModifier[])].sort()
  if (triggerProblem(key, click, modifiers)) return null
  const trigger = { key, click, modifiers, canonical: canonicalTrigger(key, click, modifiers) }
  return trigger.canonical === canonical ? trigger : null
}

/**
 * The key caps a trigger is drawn with, in the order the dashboard's other
 * shortcuts use: ⌘ ⌥ ⇧ on macOS, Ctrl Alt Shift elsewhere, then the key, or
 * `clickLabel` for a modifier-click.
 */
export function triggerCaps(trigger: EditorBindingTrigger, mac: boolean, clickLabel: string): string[] {
  const caps: string[] = []
  if (trigger.modifiers.includes('primary')) caps.push(mac ? '⌘' : 'Ctrl')
  if (trigger.modifiers.includes('alt')) caps.push(mac ? '⌥' : 'Alt')
  if (trigger.modifiers.includes('shift')) caps.push(mac ? '⇧' : 'Shift')
  caps.push(trigger.click ? clickLabel : trigger.key.toUpperCase())
  return caps
}

/**
 * The canonical trigger an editor event spells, or null when it is not a chord or a
 * modifier-click at all. The primary modifier is Cmd on macOS and Ctrl elsewhere, and
 * never both: the same rule the editor applies to its own multi-cursor click. A pointer
 * event is a click only on the left button.
 */
export function triggerOfEvent(e: BindingEvent, mac: boolean): string | null {
  const primary = mac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey
  const other = mac ? e.ctrlKey : e.metaKey
  if (other) return null
  const modifiers: BindingModifier[] = []
  if (primary) modifiers.push('primary')
  if (e.shiftKey) modifiers.push('shift')
  if (e.altKey) modifiers.push('alt')
  if (e.key === undefined) {
    if (e.button !== 0 || modifiers.length === 0) return null
    return canonicalTrigger('', true, modifiers)
  }
  const key = e.key.toLowerCase()
  if (!KEY_RE.test(key)) return null
  if (!key.startsWith('f') && modifiers.length === 0) return null
  return canonicalTrigger(key, false, modifiers)
}

/**
 * The editor triggers a dashboard shortcut chord stands for on this platform.
 * The editor sees an event before the dashboard's own handler does, so a binding
 * on one of these would take that shortcut away while the editor has focus.
 *
 * Literal Ctrl is the primary modifier off macOS and no editor modifier on it
 * (`triggerOfEvent` ignores an event with Ctrl held on a Mac). A chord on a key
 * no binding may use (`/`, `,`, Enter, an arrow) yields nothing. The chat-jump
 * letters (`a…z`) yield every letter: which ones jump depends on how many chats
 * are open.
 */
function chordTriggers(chord: Chord, mac: boolean): string[] {
  if (chord.ctrl && mac) return []
  const modifiers: BindingModifier[] = []
  if (chord.mod || chord.ctrl) modifiers.push('primary')
  if (chord.alt) modifiers.push('alt')
  if (chord.shift) modifiers.push('shift')
  const keys = chord.key === 'a…z'
    ? Array.from({ length: 26 }, (_, i) => String.fromCharCode(97 + i))
    : [chord.key.toLowerCase()]
  return keys
    .filter(key => KEY_RE.test(key) && triggerProblem(key, false, modifiers) === null)
    .map(key => canonicalTrigger(key, false, modifiers))
}

/**
 * Every editor trigger a dashboard shortcut holds on this platform, with the
 * reader's own shortcut changes applied, mapped to the shortcut's registry id.
 */
export function dashboardChordTriggers(mac: boolean): Map<string, string> {
  const out = new Map<string, string>()
  const resolved = resolveShortcuts(loadShortcutOverrides(), mac ? 'mac' : 'other')
  const entries = Object.entries(resolved).flatMap(([id, shortcut]) =>
    [shortcut.primary, ...shortcut.aliases].filter((c): c is Chord => c !== null).map(c => [id, c] as const))
  // A named chord first: the jump-letter range skips the letters other shortcuts own.
  for (const range of [false, true]) {
    for (const [id, chord] of entries) {
      if ((chord.key === 'a…z') !== range) continue
      for (const t of chordTriggers(chord, mac)) if (!out.has(t)) out.set(t, id)
    }
  }
  return out
}

/** The dashboard shortcut (its registry id) that holds `canonical` on this platform, or null. */
export function dashboardShortcutFor(canonical: string, mac: boolean): string | null {
  return dashboardChordTriggers(mac).get(canonical) ?? null
}
