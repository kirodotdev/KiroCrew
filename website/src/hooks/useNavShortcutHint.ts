import { useSyncExternalStore } from 'react'
import {
  DEFAULT_SHORTCUTS,
  IS_MAC,
  SHORTCUTS_ENABLED_EVENT,
  SHORTCUTS_ENABLED_KEY,
  formatShortcut,
  type ShortcutDef,
} from './useKeyboardShortcuts'

/**
 * Per-control shortcut hint for a left-rail nav row, DERIVED from the shortcut
 * registry rather than written next to each row.
 *
 * Why derived and not a string per button: chords are platform-dependent
 * (`IS_MAC`, the macOS Ctrl-vs-Option digit rule) and there is an open ask to
 * make them user-rebindable, so a hardcoded hint per control would be wrong on
 * one platform the day it was written and wrong everywhere the day rebinding
 * lands. Everything here resolves at RENDER from `DEFAULT_SHORTCUTS`, so a
 * registry that grows or changes carries the hints with it and this module needs
 * no edit.
 *
 * Scope is deliberately the rail rows and nothing else. The registry binds many
 * chords whose triggers are composer buttons and menu items; those controls have
 * no shared join key to the registry today, so hinting them is a different
 * change with a different design, not a wider loop over this one.
 */

/**
 * Registry id a route's panel chord is registered under.
 *
 * This is not a convention this module invents — `registerPanelShortcut()` mints
 * exactly this id for a downstream panel (`'nav-' + path.replace(/^\//, '')`),
 * and the four core panel entries are spelled the same way. So the route is
 * ALREADY the join key between a rail row and its chord, for core and
 * downstream panels alike, and a panel registered through that seam gets its
 * hint here for free.
 */
export function navShortcutId(route: string): string {
  return `nav-${route.replace(/^\/+/, '')}`
}

/** The registry entry bound to a rail route, or undefined when it has no chord. */
export function navShortcutDef(route: string): ShortcutDef | undefined {
  const id = navShortcutId(route)
  return DEFAULT_SHORTCUTS.find(s => s.id === id)
}

/**
 * The same chord in the `aria-keyshortcuts` value grammar, which is NOT the
 * display string.
 *
 * `formatShortcut()` renders for a human eye and emits platform glyphs; WAI-ARIA
 * defines its own vocabulary for this attribute, so a glyph string handed to
 * assistive tech announces nothing useful. Modifier order mirrors
 * `formatShortcut()` (meta, ctrl, alt, shift) so the two spellings of one chord
 * cannot drift into different orders, and the spelling follows the one
 * `aria-keyshortcuts` already shipped in this dashboard (`MoveUndoBar` declares
 * `Meta+Z` on macOS and `Control+Z` elsewhere).
 *
 * `meta` means Cmd-on-Mac / Ctrl-elsewhere while `ctrl` is literal Ctrl on every
 * platform, so off macOS the two collapse onto the same name. This deliberately
 * does NOT dedupe them: `DEFAULT_SHORTCUTS` has 0 of 43 entries setting both, and
 * `registerPanelShortcut` — the only other writer into that array — hardcodes
 * `alt` and sets neither, so a guard would fire on nothing constructible. It
 * would also make this function disagree with `formatShortcut()`, which does not
 * dedupe either and would render such a chord `Ctrl + Ctrl + X`.
 *
 * `mac` is injectable for the same reason it is on `isSettingsChord` — `IS_MAC`
 * is fixed at module load, so both platform behaviours need to be testable
 * without reloading the module.
 */
export function ariaKeyshortcutsFor(def: ShortcutDef, mac: boolean = IS_MAC): string {
  const mods: string[] = []
  if (def.meta) mods.push(mac ? 'Meta' : 'Control')
  if (def.ctrl) mods.push('Control')
  if (def.alt) mods.push('Alt')
  if (def.shift) mods.push('Shift')
  // Single characters are announced uppercase (matching the shipped Control+Z);
  // named keys are already DOM `key` values and pass through as themselves.
  const key = def.key.length === 1 ? def.key.toUpperCase() : def.key
  return [...mods, key].join('+')
}

/**
 * Shared view of the global shortcuts on/off toggle.
 *
 * One subscription for the whole rail rather than one per row: every rail row
 * asks the same question, and `useSyncExternalStore` over a module-level
 * snapshot is how this dashboard already shares a cross-component signal (see
 * `useArtifactPopouts`).
 *
 * It listens to `SHORTCUTS_ENABLED_EVENT` and DELIBERATELY NOT to `storage`.
 * The handler in `useKeyboardShortcuts` refreshes its own enabled state from
 * that event alone, so listening to the same single source is what keeps the
 * hint and the live binding in agreement. A `storage` listener here would let
 * another tab erase the hint while the chord in THIS tab still fires, which is
 * worse than not reacting at all: a hint that disagrees with the keyboard is a
 * lie, and a missing update is merely stale until the next same-tab toggle.
 */
/**
 * Live read of the global shortcuts toggle.
 *
 * `useSyncExternalStore` needs `getSnapshot` to be referentially stable across
 * calls that should not re-render. That is satisfied here for free because the
 * snapshot is a BOOLEAN: React compares with `Object.is`, and two reads of the
 * same key give the same primitive. So no cached snapshot is needed -- and
 * without a cache there is nothing to invalidate, which is the point. An earlier
 * version of this file kept a module-level snapshot plus a refcounted listener
 * `Set`, so that one window listener served the whole rail; at the handful of
 * rows that carry a chord that saving is unmeasurable, and it cost a
 * reset-the-cache-on-last-unsubscribe branch that existed ONLY to fix staleness
 * the cache itself introduced. Per-subscriber listeners have no such branch.
 *
 * `storage` is deliberately NOT listened to: it fires only for OTHER tabs, and
 * the live handler in `useKeyboardShortcuts` does not honour it either, so
 * reacting here would let the hint disagree with what the keyboard actually does.
 */
function getShortcutsEnabled(): boolean {
  return localStorage.getItem(SHORTCUTS_ENABLED_KEY) !== '0'
}

function subscribeShortcutsEnabled(onChange: () => void): () => void {
  window.addEventListener(SHORTCUTS_ENABLED_EVENT, onChange)
  return () => window.removeEventListener(SHORTCUTS_ENABLED_EVENT, onChange)
}


export interface NavShortcutHint {
  /** Display chord for the eye, platform-formatted by `formatShortcut()`. */
  chord: string
  /** The same chord in the `aria-keyshortcuts` grammar, for assistive tech. */
  ariaKeyshortcuts: string
}

/**
 * The hint for one rail route, or `null` when the row has no chord or the user
 * has turned shortcuts off.
 *
 * Returning `null` while shortcuts are disabled is the point of reading the
 * toggle at all: advertising a chord that has been switched off would teach a
 * keypress that does nothing.
 */
export function useNavShortcutHint(route: string): NavShortcutHint | null {
  const enabled = useSyncExternalStore(
    subscribeShortcutsEnabled,
    getShortcutsEnabled,
    getShortcutsEnabled,
  )
  if (!enabled) return null
  const def = navShortcutDef(route)
  if (!def) return null
  return { chord: formatShortcut(def), ariaKeyshortcuts: ariaKeyshortcutsFor(def) }
}
