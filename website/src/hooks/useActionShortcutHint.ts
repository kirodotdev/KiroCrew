import { shortcutDefFromChord, shortcutPlatform, useShortcutBindings } from './useKeyboardShortcuts'
import { ariaKeyshortcutsFor, useKeyboardLayout, useShortcutsEnabled } from './useNavShortcutHint'
import { keyCapGlyph, normalizeChord, shortcutEntry } from '../lib/shortcutRegistry'

/**
 * Live advertisement of one registry action chord, for the control that the
 * chord drives (the composer agent chip advertises `cycle-agent`).
 *
 * Two readings, because they serve two audiences:
 *
 * - `ariaKeyshortcuts` is derived from the LIVE binding, so assistive tech
 *   always hears the chord that actually fires, rebound or not.
 * - `isFactory` says whether that live binding is still the platform default
 *   and its keycap on the user's layout is the US glyph. A visible tooltip
 *   spells its chord inside a catalog string (so a locale can rename
 *   modifiers and the pseudolocale gate sees no raw Latin), and a catalog
 *   string can only spell the factory chord with its US glyph. Callers show the
 *   chord-carrying tooltip only while this is true.
 *
 * `null` when shortcuts are globally off or the action is unbound: advertising
 * a chord the keydown handler will ignore teaches a keypress that does nothing.
 */
export interface ActionShortcutHint {
  ariaKeyshortcuts: string
  isFactory: boolean
}

export function useActionShortcutHint(id: string): ActionShortcutHint | null {
  useKeyboardLayout()
  const enabled = useShortcutsEnabled()
  const bindings = useShortcutBindings()
  if (!enabled) return null
  const chord = bindings[id]?.primary ?? null
  if (!chord) return null
  const factory = shortcutEntry(id)?.defaults[shortcutPlatform()]
  const isFactory = !!factory
    && JSON.stringify(normalizeChord(chord)) === JSON.stringify(normalizeChord(factory))
    && keyCapGlyph(chord.key, !!chord.alt && shortcutPlatform() === 'mac') === chord.key.toUpperCase()
  const def = shortcutDefFromChord({ id, group: 'actions' }, chord)
  return { ariaKeyshortcuts: ariaKeyshortcutsFor(def), isFactory }
}
