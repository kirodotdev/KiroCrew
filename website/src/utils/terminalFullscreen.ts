/** Pure rules for the terminal's find and fullscreen affordances (the layout
 *  rules, the per-platform find/fullscreen chord match and the keycaps the
 *  menu shows), kept out of the components so they unit-test without
 *  rendering. */

/** Whether a keydown is the F of the find/fullscreen chord. Matches the letter
 *  TYPED (`key`), so on a remapped Latin layout such as Dvorak, Ctrl+U on
 *  QWERTY's F position still reaches the shell as kill-line, and the key that
 *  types `f` opens find. Falls back to the physical position (`code === 'KeyF'`)
 *  only when the typed key is not a Latin letter, so non-Latin layouts (and
 *  macOS Option glyphs) keep a working chord. */
export function isFindChordKey(key: string, code: string): boolean {
  const k = key.toLowerCase()
  if (k.length === 1 && k >= 'a' && k <= 'z') return k === 'f'
  return code === 'KeyF'
}

/** What a keydown in a focused terminal (or its find bar) asks for, if
 *  anything. Per platform, because the shell owns bare Ctrl+F everywhere
 *  (readline forward-char, page-forward in less/man/vim):
 *  - macOS: Cmd+F finds, Cmd+Shift+F toggles fullscreen. Ctrl+F is the shell's.
 *  - Linux/Windows: Ctrl+Shift+F finds. Bare Ctrl+F is the shell's and passes
 *    through unchanged. Fullscreen has no chord there; its overflow-menu item
 *    is the way in.
 *  Alt, and Cmd+Ctrl together, never match. */
export type TerminalChord = 'find' | 'fullscreen' | null

export function terminalChord(
  e: { key: string; code: string; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; altKey: boolean },
  mac: boolean,
): TerminalChord {
  if (e.altKey || !isFindChordKey(e.key, e.code)) return null
  if (mac) {
    if (!e.metaKey || e.ctrlKey) return null
    return e.shiftKey ? 'fullscreen' : 'find'
  }
  if (e.ctrlKey && !e.metaKey && e.shiftKey) return 'find'
  return null
}

/** The keycap the overflow menu shows next to Find. Keycap data, not copy. */
export function findKeycap(mac: boolean): string {
  return mac ? '⌘F' : 'Ctrl+Shift+F'
}

/** The keycap the overflow menu shows next to Fullscreen, or null where the
 *  platform has no fullscreen chord (Linux/Windows). */
export function fullscreenKeycap(mac: boolean): string | null {
  return mac ? '⇧⌘F' : null
}

/** Whether the dock terminal may go fullscreen. Fullscreen fills the dashboard
 *  content area, whose stacking context only exists on desktop; on a mobile
 *  viewport the shell's nav drawer and scrims must paint over the content, so a
 *  maximized terminal there would cover them. Desktop only. */
export function isTerminalFullscreenAllowed(isMobile: boolean): boolean {
  return !isMobile
}

/** The stacking-context class `<main>` carries. `isolate` ONLY when the dock
 *  terminal is actually rendered AND maximized — the maximized panel is a later
 *  sibling of `<main>` and must paint above the in-content chat layers it covers.
 *  It tracks the dock's own render condition (`terminalEnabled && !terminalPoppedOut`),
 *  not the bare fullscreen flag: the flag persists across a pop-out and a disable,
 *  but no panel is on screen then, so isolating `<main>` would trap a page-level
 *  inline modal inside it for nothing. Docked-but-not-maximized, popped out,
 *  disabled, or closed: no stacking context, so the modal paints over the dock. */
export function mainStackingClass(opts: {
  isTerminalFullscreen: boolean
  terminalEnabled: boolean
  terminalPoppedOut: boolean
}): string {
  const maximizedPanelOnScreen =
    opts.isTerminalFullscreen && opts.terminalEnabled && !opts.terminalPoppedOut
  return maximizedPanelOnScreen ? 'isolate' : ''
}
