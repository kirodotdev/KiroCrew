/**
 * Whether the platform's primary command modifier is held — Cmd on macOS,
 * Ctrl elsewhere.
 *
 * Exactly one of the two, never both. Cmd+Ctrl together forms a chord macOS
 * reserves for itself: Control+Command+F toggles full screen and
 * Control+Command+D opens Look Up. Shortcut handlers `preventDefault()` on a
 * match, so a test that accepted both modifiers would consume those chords
 * before the application menu could act on them.
 *
 * Accepts anything carrying the two flags, so DOM and React synthetic keyboard
 * events both satisfy it.
 */
export const hasCommandModifier = (e: Pick<KeyboardEvent, 'metaKey' | 'ctrlKey'>): boolean =>
  e.metaKey !== e.ctrlKey
