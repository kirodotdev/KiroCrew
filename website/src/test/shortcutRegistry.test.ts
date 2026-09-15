import { describe, it, expect, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  SHORTCUT_OVERRIDES_KEY,
  SHORTCUT_REGISTRY,
  chordMatchesEvent,
  eventKeyToken,
  isValidChord,
  loadShortcutOverrides,
  matchShortcutEvent,
  normalizeChord,
  registryAltNonShiftKeys,
  resolveShortcut,
  resolveShortcuts,
  shortcutEntry,
  type Chord,
} from '../lib/shortcutRegistry'
import { SHORTCUT_LABEL_KEY } from '../hooks/useKeyboardShortcuts'

type Ev = Pick<KeyboardEvent, 'code' | 'key' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>
const ev = (code: string, mods: Partial<Ev> = {}): Ev =>
  ({ code, key: '', metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...mods })

describe('shortcutRegistry — table invariants', () => {
  it('ids are unique', () => {
    const ids = SHORTCUT_REGISTRY.map(e => e.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('every id has a label catalog key (the display table and the registry cannot drift)', () => {
    const missing = SHORTCUT_REGISTRY.map(e => e.id).filter(id => !(id in SHORTCUT_LABEL_KEY))
    expect(missing).toEqual([])
    const orphaned = Object.keys(SHORTCUT_LABEL_KEY).filter(id => !shortcutEntry(id))
    expect(orphaned).toEqual([])
  })

  it('every registry-dispatched id has a handler action in useKeyboardShortcuts (drift guard)', () => {
    // The handler dispatches registry hits through an `actions` map keyed by id.
    // A `registry` entry without a branch there would be claimed and dropped —
    // parse the source for the keys the map declares and assert containment
    // both ways. FAIL-CLOSED: the map's shape is asserted so a refactor that
    // renames it fails here rather than passing vacuously.
    const src = readFileSync(resolve(process.cwd(), 'src/hooks/useKeyboardShortcuts.ts'), 'utf-8')
    const start = src.indexOf('const actions: Record<string, () => void> = {')
    expect(start).toBeGreaterThan(-1)
    const end = src.indexOf('const action = Object.prototype.hasOwnProperty.call(actions, hit)', start)
    expect(end).toBeGreaterThan(start)
    const declared = new Set([...src.slice(start, end).matchAll(/^\s*'([a-z-]+)': \(\) =>/gm)].map(m => m[1]))
    // `shortcuts-modal` and `open-settings` are dispatched by their own branches
    // ahead of the map because they fire even while shortcuts are disabled.
    for (const id of ['shortcuts-modal', 'open-settings']) {
      expect(src).toContain(`hit === '${id}'`)
      declared.add(id)
    }
    const registryIds = SHORTCUT_REGISTRY.filter(e => e.dispatch === 'registry').map(e => e.id)
    expect(registryIds.filter(id => !declared.has(id))).toEqual([])
    expect([...declared].filter(id => shortcutEntry(id)?.dispatch !== 'registry')).toEqual([])
  })

  it('every default and alias chord is a valid chord (a modifier that is not Shift), except bare Escape', () => {
    for (const e of SHORTCUT_REGISTRY) {
      for (const p of ['mac', 'other'] as const) {
        const d = e.defaults[p]
        if (d && e.id !== 'stop-speaking') expect(isValidChord(d), `${e.id}/${p}`).toBe(true)
        for (const a of e.aliases?.[p] ?? []) expect(isValidChord(a), `${e.id}/${p} alias`).toBe(true)
      }
    }
  })

  it('no two registry-dispatched entries resolve to the same chord on either platform (defaults + aliases)', () => {
    for (const p of ['mac', 'other'] as const) {
      const seen = new Map<string, string>()
      const resolved = resolveShortcuts({}, p)
      for (const e of SHORTCUT_REGISTRY) {
        if (e.dispatch !== 'registry') continue
        const r = resolved[e.id]
        for (const c of [r.primary, ...r.aliases]) {
          if (!c) continue
          const sig = JSON.stringify(normalizeChord(c))
          expect(seen.get(sig), `${p}: ${sig} claimed by ${seen.get(sig)} and ${e.id}`).toBeUndefined()
          seen.set(sig, e.id)
        }
      }
    }
  })

  it('the conventional defaults are what #4608 / the design comment say they are', () => {
    expect(resolveShortcut('new-chat', {}, 'mac')).toEqual({ id: 'new-chat', primary: { key: 'n', mod: true }, aliases: [{ key: 'n', alt: true, shift: true }] })
    expect(resolveShortcut('close-chat', {}, 'other')).toEqual({ id: 'close-chat', primary: { key: 'w', mod: true }, aliases: [{ key: 'w', alt: true, shift: true }] })
    expect(resolveShortcut('shortcuts-modal', {}, 'mac').primary).toEqual({ key: '/', mod: true })
    expect(resolveShortcut('shortcuts-modal', {}, 'mac').aliases).toEqual([{ key: 'k', alt: true }])
    expect(resolveShortcut('open-settings', {}, 'other').primary).toEqual({ key: ',', mod: true })
    expect(resolveShortcut('open-settings', {}, 'other').aliases).toEqual([{ key: ',', alt: true }])
    expect(shortcutEntry('new-chat')?.browserReserved).toBe(true)
    expect(shortcutEntry('close-chat')?.browserReserved).toBe(true)
    expect(shortcutEntry('shortcuts-modal')?.browserReserved).toBeUndefined()
  })

  it('keeps the literal-Ctrl chords literal: ⌃G on every platform, ⌃1–9 on macOS (Alt+1–9 elsewhere)', () => {
    expect(shortcutEntry('agent-monitor')?.defaults).toEqual({ mac: { key: 'g', ctrl: true }, other: { key: 'g', ctrl: true } })
    expect(shortcutEntry('chat-1')?.defaults).toEqual({ mac: { key: '1', ctrl: true }, other: { key: '1', alt: true } })
  })

  it('registryAltNonShiftKeys lists exactly the Option/Alt chords the handler now claims pre-panel', () => {
    expect(registryAltNonShiftKeys().sort()).toEqual([',', 'Enter', 'k'])
  })
})

describe('shortcutRegistry — key tokens', () => {
  it('derives letters and digits from code, punctuation from the US-QWERTY position, names verbatim', () => {
    expect(eventKeyToken(ev('KeyN'))).toBe('n')
    expect(eventKeyToken(ev('Digit3'))).toBe('3')
    expect(eventKeyToken(ev('Comma'))).toBe(',')
    expect(eventKeyToken(ev('Slash'))).toBe('/')
    expect(eventKeyToken(ev('BracketLeft'))).toBe('[')
    expect(eventKeyToken(ev('Backquote'))).toBe('`')
    expect(eventKeyToken(ev('Enter'))).toBe('Enter')
    expect(eventKeyToken(ev('ArrowLeft'))).toBe('ArrowLeft')
    // NumpadEnter is not Enter — the handler has always compared code === 'Enter'.
    expect(eventKeyToken(ev('NumpadEnter'))).toBe('NumpadEnter')
  })

  it('is positional: Option+, on a Mac (key "≤") is still the comma chord', () => {
    expect(eventKeyToken(ev('Comma', { key: '≤' }))).toBe(',')
  })

  it('falls back to key only when there is no code, and ignores bare modifiers', () => {
    expect(eventKeyToken({ code: '', key: 'N' })).toBe('n')
    expect(eventKeyToken({ code: '', key: 'Shift' })).toBeNull()
  })
})

describe('shortcutRegistry — chordMatchesEvent', () => {
  const modN: Chord = { key: 'n', mod: true }

  it('mod is Meta on macOS and Ctrl elsewhere, and the other primary must be up', () => {
    expect(chordMatchesEvent(ev('KeyN', { metaKey: true }), modN, 'mac')).toBe(true)
    expect(chordMatchesEvent(ev('KeyN', { ctrlKey: true }), modN, 'mac')).toBe(false)
    expect(chordMatchesEvent(ev('KeyN', { metaKey: true, ctrlKey: true }), modN, 'mac')).toBe(false)
    expect(chordMatchesEvent(ev('KeyN', { ctrlKey: true }), modN, 'other')).toBe(true)
    expect(chordMatchesEvent(ev('KeyN', { metaKey: true }), modN, 'other')).toBe(false)
    expect(chordMatchesEvent(ev('KeyN', { ctrlKey: true, metaKey: true }), modN, 'other')).toBe(false)
  })

  it('every modifier is significant — a superset chord is a miss', () => {
    expect(chordMatchesEvent(ev('KeyN', { metaKey: true, shiftKey: true }), modN, 'mac')).toBe(false)
    expect(chordMatchesEvent(ev('KeyN', { metaKey: true, altKey: true }), modN, 'mac')).toBe(false)
    expect(chordMatchesEvent(ev('KeyN'), modN, 'mac')).toBe(false)
  })

  it('literal ctrl is Control on macOS, independent of Meta; and collapses onto mod elsewhere', () => {
    const ctrlG: Chord = { key: 'g', ctrl: true }
    expect(chordMatchesEvent(ev('KeyG', { ctrlKey: true }), ctrlG, 'mac')).toBe(true)
    expect(chordMatchesEvent(ev('KeyG', { metaKey: true }), ctrlG, 'mac')).toBe(false)
    expect(chordMatchesEvent(ev('KeyG', { ctrlKey: true }), ctrlG, 'other')).toBe(true)
  })

  it('alt/shift chords match exactly', () => {
    const altShiftN: Chord = { key: 'n', alt: true, shift: true }
    expect(chordMatchesEvent(ev('KeyN', { altKey: true, shiftKey: true }), altShiftN, 'mac')).toBe(true)
    expect(chordMatchesEvent(ev('KeyN', { altKey: true }), altShiftN, 'mac')).toBe(false)
    expect(chordMatchesEvent(ev('KeyN', { altKey: true, shiftKey: true, metaKey: true }), altShiftN, 'mac')).toBe(false)
  })
})

describe('shortcutRegistry — matchShortcutEvent', () => {
  it('returns the id for a primary and for an alias, and null for a code-driven family chord', () => {
    const r = resolveShortcuts({}, 'other')
    expect(matchShortcutEvent(ev('KeyN', { ctrlKey: true }), r, 'other')).toBe('new-chat')
    expect(matchShortcutEvent(ev('KeyN', { altKey: true, shiftKey: true }), r, 'other')).toBe('new-chat')
    expect(matchShortcutEvent(ev('Slash', { ctrlKey: true }), r, 'other')).toBe('shortcuts-modal')
    expect(matchShortcutEvent(ev('KeyK', { altKey: true }), r, 'other')).toBe('shortcuts-modal')
    expect(matchShortcutEvent(ev('Comma', { ctrlKey: true }), r, 'other')).toBe('open-settings')
    // Families: Alt+1 (chat jump), ⌘[ (bracket cycle), Alt+C (panel nav), Ctrl+1 (instance)
    expect(matchShortcutEvent(ev('Digit1', { altKey: true }), r, 'other')).toBeNull()
    expect(matchShortcutEvent(ev('BracketLeft', { ctrlKey: true }), r, 'other')).toBeNull()
    expect(matchShortcutEvent(ev('KeyC', { altKey: true }), r, 'other')).toBeNull()
    expect(matchShortcutEvent(ev('Digit1', { ctrlKey: true }), r, 'other')).toBeNull()
  })

  it('on macOS the ⌘ chords match Meta and the Option aliases match Alt', () => {
    const r = resolveShortcuts({}, 'mac')
    expect(matchShortcutEvent(ev('KeyW', { metaKey: true }), r, 'mac')).toBe('close-chat')
    expect(matchShortcutEvent(ev('KeyW', { altKey: true, shiftKey: true }), r, 'mac')).toBe('close-chat')
    expect(matchShortcutEvent(ev('KeyW', { ctrlKey: true }), r, 'mac')).toBeNull()
    expect(matchShortcutEvent(ev('Comma', { metaKey: true }), r, 'mac')).toBe('open-settings')
    expect(matchShortcutEvent(ev('Comma', { altKey: true }), r, 'mac')).toBe('open-settings')
  })
})

describe('shortcutRegistry — overrides', () => {
  beforeEach(() => localStorage.clear())

  it('loads nothing for an absent, malformed, or non-object payload', () => {
    expect(loadShortcutOverrides()).toEqual({})
    localStorage.setItem(SHORTCUT_OVERRIDES_KEY, 'not json')
    expect(loadShortcutOverrides()).toEqual({})
    localStorage.setItem(SHORTCUT_OVERRIDES_KEY, '[1,2]')
    expect(loadShortcutOverrides()).toEqual({})
  })

  it('keeps null (unbound) and valid chords for registry ids; drops bad chords, unknown ids, and code-driven ids', () => {
    localStorage.setItem(SHORTCUT_OVERRIDES_KEY, JSON.stringify({
      'new-chat': null,
      'close-chat': { key: 'Q', mod: true },
      'cycle-model': { key: 'm' },            // no modifier
      'nope': { key: 'x', mod: true },        // unknown id
      'chat-1': { key: '1', mod: true },      // code-driven family — not rebindable yet
      'focus-input': 'Enter',                 // not an object
    }))
    expect(loadShortcutOverrides()).toEqual({ 'new-chat': null, 'close-chat': { key: 'q', mod: true } })
  })

  it('normalizes a stored chord on read (upper-case key, false modifiers dropped)', () => {
    // The reader is the contract a future writer (P3) must honour; until then the
    // stored shape is whatever a hand-edited or migrated payload holds.
    localStorage.setItem(SHORTCUT_OVERRIDES_KEY, JSON.stringify({ 'new-chat': { key: 'J', mod: true, shift: true, alt: false } }))
    expect(loadShortcutOverrides()).toEqual({ 'new-chat': { key: 'j', mod: true, shift: true } })
  })

  it('an override replaces the default and its aliases; null unbinds; a code id ignores overrides', () => {
    expect(resolveShortcut('new-chat', { 'new-chat': { key: 'j', mod: true } }, 'mac'))
      .toEqual({ id: 'new-chat', primary: { key: 'j', mod: true }, aliases: [] })
    expect(resolveShortcut('new-chat', { 'new-chat': null }, 'mac'))
      .toEqual({ id: 'new-chat', primary: null, aliases: [] })
    expect(resolveShortcut('chat-1', { 'chat-1': { key: '9', mod: true } }, 'mac').primary).toEqual({ key: '1', ctrl: true })
    expect(resolveShortcut('unknown-id', {}, 'mac')).toEqual({ id: 'unknown-id', primary: null, aliases: [] })
  })

  it('isValidChord / normalizeChord', () => {
    expect(isValidChord({ key: 'n', mod: true })).toBe(true)
    expect(isValidChord({ key: 'g', ctrl: true })).toBe(true)
    expect(isValidChord({ key: 'n', shift: true })).toBe(false)
    expect(isValidChord({ key: ' ', mod: true })).toBe(false)
    expect(isValidChord(null)).toBe(false)
    expect(normalizeChord({ key: 'N', mod: true, shift: false, alt: undefined })).toEqual({ key: 'n', mod: true })
    expect(normalizeChord({ key: 'Enter', alt: true })).toEqual({ key: 'Enter', alt: true })
  })
})
