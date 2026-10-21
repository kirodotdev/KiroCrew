import { describe, expect, it } from 'vitest'
import { isFindChordKey, isTerminalFullscreenAllowed, mainStackingClass, terminalChord, findKeycap, fullscreenKeycap } from './terminalFullscreen'

describe('isFindChordKey', () => {
  it('matches the typed letter f, with or without Shift', () => {
    expect(isFindChordKey('f', 'KeyF')).toBe(true)
    expect(isFindChordKey('F', 'KeyF')).toBe(true)
  })
  it('leaves a remapped Latin letter on the F position to the shell (Dvorak Ctrl+U)', () => {
    expect(isFindChordKey('u', 'KeyF')).toBe(false)
  })
  it('matches the key that types f wherever it sits on the layout', () => {
    // Dvorak: the key that types `f` is QWERTY's Y position.
    expect(isFindChordKey('f', 'KeyY')).toBe(true)
  })
  it('falls back to the physical F position when the typed key is not a Latin letter', () => {
    expect(isFindChordKey('а', 'KeyF')).toBe(true) // Cyrillic layout
    expect(isFindChordKey('ƒ', 'KeyF')).toBe(true) // macOS Option glyph
    expect(isFindChordKey('а', 'KeyA')).toBe(false)
  })
})

describe('isTerminalFullscreenAllowed', () => {
  it('allows fullscreen on desktop', () => {
    expect(isTerminalFullscreenAllowed(false)).toBe(true)
  })
  it('forbids fullscreen on a mobile viewport', () => {
    expect(isTerminalFullscreenAllowed(true)).toBe(false)
  })
})

describe('mainStackingClass', () => {
  const docked = { terminalEnabled: true, terminalPoppedOut: false }

  it('isolates <main> only when a maximized dock panel is actually on screen', () => {
    expect(mainStackingClass({ isTerminalFullscreen: true, ...docked })).toBe('isolate')
  })
  it('adds no stacking class when the terminal is not fullscreen', () => {
    // Docked but not maximized: a page-level inline modal must paint over the dock.
    expect(mainStackingClass({ isTerminalFullscreen: false, ...docked })).toBe('')
  })
  it('adds no stacking class when the maximized terminal is popped out', () => {
    // The fullscreen flag persists across pop-out, but no panel is on screen, so
    // isolating <main> would trap a page modal inside it for nothing.
    expect(mainStackingClass({ isTerminalFullscreen: true, terminalEnabled: true, terminalPoppedOut: true })).toBe('')
  })
  it('adds no stacking class when the terminal is disabled while the flag is still set', () => {
    expect(mainStackingClass({ isTerminalFullscreen: true, terminalEnabled: false, terminalPoppedOut: false })).toBe('')
  })
})

describe('terminalChord', () => {
  const k = (mods: Partial<{ ctrlKey: boolean; metaKey: boolean; shiftKey: boolean; altKey: boolean }>, key = 'f') =>
    ({ key, code: 'KeyF', ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, ...mods })

  it('Linux/Windows: bare Ctrl+F is the shell\'s, never a terminal chord', () => {
    expect(terminalChord(k({ ctrlKey: true }), false)).toBeNull()
  })
  it('Linux/Windows: Ctrl+Shift+F finds and nothing toggles fullscreen', () => {
    expect(terminalChord(k({ ctrlKey: true, shiftKey: true }, 'F'), false)).toBe('find')
    expect(terminalChord(k({ metaKey: true, shiftKey: true }, 'F'), false)).toBeNull()
    expect(terminalChord(k({ metaKey: true }), false)).toBeNull()
  })
  it('macOS: Cmd+F finds, Cmd+Shift+F toggles fullscreen, Ctrl+F stays with the shell', () => {
    expect(terminalChord(k({ metaKey: true }), true)).toBe('find')
    expect(terminalChord(k({ metaKey: true, shiftKey: true }, 'F'), true)).toBe('fullscreen')
    expect(terminalChord(k({ ctrlKey: true }), true)).toBeNull()
    expect(terminalChord(k({ ctrlKey: true, shiftKey: true }, 'F'), true)).toBeNull()
  })
  it('Alt, Cmd+Ctrl together, or another letter never match', () => {
    expect(terminalChord(k({ ctrlKey: true, shiftKey: true, altKey: true }), false)).toBeNull()
    expect(terminalChord(k({ metaKey: true, altKey: true }), true)).toBeNull()
    expect(terminalChord(k({ metaKey: true, ctrlKey: true }), true)).toBeNull()
    expect(terminalChord(k({ ctrlKey: true, shiftKey: true }, 'g'), false)).toBeNull()
  })
})

describe('menu keycaps', () => {
  it('shows the find chord each platform actually answers', () => {
    expect(findKeycap(true)).toBe('⌘F')
    expect(findKeycap(false)).toBe('Ctrl+Shift+F')
  })
  it('shows a fullscreen chord on macOS only', () => {
    expect(fullscreenKeycap(true)).toBe('⇧⌘F')
    expect(fullscreenKeycap(false)).toBeNull()
  })
})
