import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  chordMatchesEvent,
  DEFAULT_QUICK_SEARCH_CONFIG,
  displayKeyCap,
  eventKeyToken,
  formatChordKeys,
  formatQuickSearchKeys,
  hasCustomChord,
  isModKEvent,
  isValidChord,
  loadQuickSearchConfig,
  normalizeChord,
  QUICK_SEARCH_SHORTCUT_EVENT,
  QUICK_SEARCH_SHORTCUT_KEY,
  saveQuickSearchConfig,
} from './quickSearchShortcut'

afterEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
})

/** Minimal KeyboardEvent-shaped stub with all-false modifiers by default. */
type KE = Pick<KeyboardEvent, 'code' | 'key' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>
const ke = (o: Partial<KE>): KE => ({
  code: '',
  key: '',
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  ...o,
})

describe('isValidChord', () => {
  it('accepts a chord with a mod, ctrl, or alt modifier', () => {
    expect(isValidChord({ key: 'k', mod: true })).toBe(true)
    expect(isValidChord({ key: 'j', alt: true })).toBe(true)
    expect(isValidChord({ key: '`', ctrl: true })).toBe(true)
  })

  it('rejects a bare key, a Shift-only chord, and empty/absent keys', () => {
    // Shift alone merely types a capital, so it is not a safe activation chord.
    expect(isValidChord({ key: 'k' })).toBe(false)
    expect(isValidChord({ key: 'k', shift: true })).toBe(false)
    expect(isValidChord({ key: '', mod: true })).toBe(false)
    expect(isValidChord(null)).toBe(false)
    expect(isValidChord(undefined)).toBe(false)
  })
})

describe('normalizeChord', () => {
  it('lowercases the key and keeps only truthy modifier flags', () => {
    expect(normalizeChord({ key: 'K', mod: true })).toEqual({ key: 'k', mod: true })
    expect(normalizeChord({ key: 'J', mod: true, alt: false, shift: true })).toEqual({
      key: 'j',
      mod: true,
      shift: true,
    })
    expect(normalizeChord({ key: 'J', ctrl: true })).toEqual({ key: 'j', ctrl: true })
  })
})

describe('load / save round-trip', () => {
  it('returns the mod-k default when nothing is stored', () => {
    expect(loadQuickSearchConfig()).toEqual(DEFAULT_QUICK_SEARCH_CONFIG)
    expect(DEFAULT_QUICK_SEARCH_CONFIG).toEqual({ mode: 'mod-k' })
  })

  it('round-trips the mod-k preset', () => {
    expect(saveQuickSearchConfig({ mode: 'mod-k' })).toBe(true)
    expect(loadQuickSearchConfig()).toEqual({ mode: 'mod-k' })
  })

  it('round-trips a custom chord, normalized', () => {
    saveQuickSearchConfig({ mode: 'custom', custom: { key: 'P', mod: true } })
    expect(loadQuickSearchConfig()).toEqual({ mode: 'custom', custom: { key: 'p', mod: true } })
  })

  it('round-trips a ctrl chord (Control held independently on macOS)', () => {
    saveQuickSearchConfig({ mode: 'custom', custom: { key: 'J', ctrl: true } })
    expect(loadQuickSearchConfig()).toEqual({ mode: 'custom', custom: { key: 'j', ctrl: true } })
  })

  it('drops a stray custom payload when persisting a preset mode', () => {
    saveQuickSearchConfig({ mode: 'double-shift', custom: { key: 'p', mod: true } } as never)
    expect(JSON.parse(localStorage.getItem(QUICK_SEARCH_SHORTCUT_KEY)!)).toEqual({ mode: 'double-shift' })
  })

  it('refuses to persist a custom mode with an invalid chord', () => {
    expect(saveQuickSearchConfig({ mode: 'custom', custom: { key: 'p' } })).toBe(false)
    expect(localStorage.getItem(QUICK_SEARCH_SHORTCUT_KEY)).toBeNull()
  })

  it('falls back to the default on malformed or invalid stored values', () => {
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, 'not json')
    expect(loadQuickSearchConfig()).toEqual(DEFAULT_QUICK_SEARCH_CONFIG)
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'custom' }))
    expect(loadQuickSearchConfig()).toEqual(DEFAULT_QUICK_SEARCH_CONFIG)
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'nonsense' }))
    expect(loadQuickSearchConfig()).toEqual(DEFAULT_QUICK_SEARCH_CONFIG)
  })

  it('broadcasts a change event on save', () => {
    const spy = vi.fn()
    window.addEventListener(QUICK_SEARCH_SHORTCUT_EVENT, spy)
    saveQuickSearchConfig({ mode: 'mod-k' })
    expect(spy).toHaveBeenCalledTimes(1)
    window.removeEventListener(QUICK_SEARCH_SHORTCUT_EVENT, spy)
  })
})

describe('eventKeyToken', () => {
  it('prefers the physical code for letters, digits, and numpad digits', () => {
    expect(eventKeyToken(ke({ code: 'KeyK', key: 'k' }))).toBe('k')
    expect(eventKeyToken(ke({ code: 'Digit1', key: '1' }))).toBe('1')
    expect(eventKeyToken(ke({ code: 'Numpad5', key: '5' }))).toBe('5')
  })

  it('is Shift- and Option-layer stable via the code', () => {
    // Shift+K reports key 'K'; macOS Alt+K reports key '˚'. The code is 'KeyK'
    // either way, so the stored token stays 'k' and the chord remains matchable.
    expect(eventKeyToken(ke({ code: 'KeyK', key: 'K', shiftKey: true }))).toBe('k')
    expect(eventKeyToken(ke({ code: 'KeyK', key: '˚', altKey: true }))).toBe('k')
  })

  it('returns null for a bare modifier keydown', () => {
    for (const key of ['Shift', 'Control', 'Alt', 'Meta']) {
      expect(eventKeyToken(ke({ key }))).toBeNull()
    }
  })

  it('falls back to the key for named keys and when code is absent', () => {
    expect(eventKeyToken(ke({ code: '', key: 'ArrowRight' }))).toBe('ArrowRight')
    expect(eventKeyToken(ke({ code: '', key: 'P' }))).toBe('p')
  })
})

describe('isModKEvent', () => {
  it('accepts ⌘K / Ctrl+K in either case', () => {
    expect(isModKEvent(ke({ key: 'k', metaKey: true }))).toBe(true)
    expect(isModKEvent(ke({ key: 'k', ctrlKey: true }))).toBe(true)
    expect(isModKEvent(ke({ key: 'K', metaKey: true }))).toBe(true)
  })

  it('rejects K with extra modifiers or a different key', () => {
    expect(isModKEvent(ke({ key: 'k', metaKey: true, shiftKey: true }))).toBe(false)
    expect(isModKEvent(ke({ key: 'k', metaKey: true, altKey: true }))).toBe(false)
    expect(isModKEvent(ke({ key: 'j', metaKey: true }))).toBe(false)
    expect(isModKEvent(ke({ key: 'k' }))).toBe(false)
  })
})

describe('chordMatchesEvent', () => {
  const chord = { key: 'p', mod: true } as const

  it('maps mod to Ctrl on Windows/Linux', () => {
    expect(chordMatchesEvent(ke({ code: 'KeyP', ctrlKey: true }), chord, false)).toBe(true)
    // The opposite primary (Meta) must not satisfy a mod chord.
    expect(chordMatchesEvent(ke({ code: 'KeyP', metaKey: true }), chord, false)).toBe(false)
  })

  it('maps mod to Cmd on macOS', () => {
    expect(chordMatchesEvent(ke({ code: 'KeyP', metaKey: true }), chord, true)).toBe(true)
    expect(chordMatchesEvent(ke({ code: 'KeyP', ctrlKey: true }), chord, true)).toBe(false)
  })

  it('requires alt and shift to match exactly', () => {
    const c = { key: 'j', mod: true, shift: true } as const
    expect(chordMatchesEvent(ke({ code: 'KeyJ', ctrlKey: true, shiftKey: true }), c, false)).toBe(true)
    expect(chordMatchesEvent(ke({ code: 'KeyJ', ctrlKey: true }), c, false)).toBe(false)
    expect(chordMatchesEvent(ke({ code: 'KeyJ', ctrlKey: true, shiftKey: true, altKey: true }), c, false)).toBe(false)
  })

  it('rejects a different key', () => {
    expect(chordMatchesEvent(ke({ code: 'KeyX', ctrlKey: true }), chord, false)).toBe(false)
  })

  it('matches a ctrl chord on macOS via ctrlKey alone, never metaKey', () => {
    const c = { key: '`', ctrl: true } as const
    expect(chordMatchesEvent(ke({ code: 'Backquote', key: '`', ctrlKey: true }), c, true)).toBe(true)
    expect(chordMatchesEvent(ke({ code: 'Backquote', key: '`', metaKey: true }), c, true)).toBe(false)
    // Both primaries down is neither a mod chord nor a ctrl chord.
    expect(chordMatchesEvent(ke({ code: 'Backquote', key: '`', metaKey: true, ctrlKey: true }), c, true)).toBe(false)
  })

  it('still rejects ctrlKey for a mod chord on macOS', () => {
    expect(chordMatchesEvent(ke({ code: 'KeyP', ctrlKey: true }), chord, true)).toBe(false)
  })

  it('treats a ctrl chord as Control off macOS, rejecting metaKey', () => {
    const c = { key: '`', ctrl: true } as const
    expect(chordMatchesEvent(ke({ code: 'Backquote', key: '`', ctrlKey: true }), c, false)).toBe(true)
    expect(chordMatchesEvent(ke({ code: 'Backquote', key: '`', metaKey: true }), c, false)).toBe(false)
  })
})

describe('hasCustomChord', () => {
  it('is true only for a custom mode carrying a valid chord', () => {
    expect(hasCustomChord({ mode: 'custom', custom: { key: 'p', mod: true } })).toBe(true)
    expect(hasCustomChord({ mode: 'custom' })).toBe(false)
    expect(hasCustomChord({ mode: 'double-shift' })).toBe(false)
    expect(hasCustomChord({ mode: 'mod-k' })).toBe(false)
  })
})

describe('formatters', () => {
  it('formats the double-shift preset per platform', () => {
    expect(formatQuickSearchKeys({ mode: 'double-shift' }, false)).toEqual(['Shift', 'Shift'])
    expect(formatQuickSearchKeys({ mode: 'double-shift' }, true)).toEqual(['⇧', '⇧'])
  })

  it('formats the mod-k preset per platform', () => {
    expect(formatQuickSearchKeys({ mode: 'mod-k' }, false)).toEqual(['Ctrl', 'K'])
    expect(formatQuickSearchKeys({ mode: 'mod-k' }, true)).toEqual(['⌘', 'K'])
  })

  it('formats a custom chord per platform, modifiers before the key', () => {
    const config = { mode: 'custom', custom: { key: 'j', mod: true, alt: true, shift: true } } as const
    expect(formatQuickSearchKeys(config, false)).toEqual(['Ctrl', 'Alt', 'Shift', 'J'])
    expect(formatQuickSearchKeys(config, true)).toEqual(['⌘', '⌥', '⇧', 'J'])
  })

  it('returns no caps for a custom mode without a recorded chord', () => {
    expect(formatQuickSearchKeys({ mode: 'custom' }, false)).toEqual([])
  })

  it('formatChordKeys and displayKeyCap render the key legibly', () => {
    expect(formatChordKeys({ key: 'p', mod: true }, false)).toEqual(['Ctrl', 'P'])
    expect(displayKeyCap('k')).toBe('K')
    expect(displayKeyCap('ArrowRight')).toBe('ArrowRight')
  })

  it('renders a ctrl chord as ⌃ on macOS and Ctrl elsewhere', () => {
    expect(formatChordKeys({ key: '`', ctrl: true }, true)).toEqual(['⌃', '`'])
    expect(formatChordKeys({ key: '`', ctrl: true }, false)).toEqual(['Ctrl', '`'])
  })

  it('never renders two Ctrl caps when both mod and ctrl are set off macOS', () => {
    expect(formatChordKeys({ key: 'j', mod: true, ctrl: true }, false)).toEqual(['Ctrl', 'J'])
    // On macOS the two flags are distinct keys, so both caps appear.
    expect(formatChordKeys({ key: 'j', mod: true, ctrl: true }, true)).toEqual(['⌃', '⌘', 'J'])
  })
})
