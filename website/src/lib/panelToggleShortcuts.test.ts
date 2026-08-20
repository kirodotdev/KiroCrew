import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  DEFAULT_PANEL_TOGGLE_BINDINGS,
  loadPanelToggleOverrides,
  matchPanelToggleEvent,
  PANEL_TOGGLE_IDS,
  PANEL_TOGGLE_SHORTCUTS_EVENT,
  PANEL_TOGGLE_SHORTCUTS_KEY,
  resolvePanelToggleBinding,
  setPanelToggleBinding,
} from './panelToggleShortcuts'

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

describe('defaults', () => {
  it('binds session panel to Cmd/Ctrl+B and side panel to Cmd/Ctrl+\\', () => {
    expect(DEFAULT_PANEL_TOGGLE_BINDINGS['session-panel']).toEqual({ key: 'b', mod: true })
    expect(DEFAULT_PANEL_TOGGLE_BINDINGS['side-panel']).toEqual({ key: '\\', mod: true })
  })

  it('ships the left sidebar unbound by default (opt-in)', () => {
    expect(DEFAULT_PANEL_TOGGLE_BINDINGS['left-sidebar']).toBeNull()
  })

  it('covers exactly the three known panel ids', () => {
    expect([...PANEL_TOGGLE_IDS].sort()).toEqual(['left-sidebar', 'session-panel', 'side-panel'])
  })
})

describe('resolvePanelToggleBinding', () => {
  it('returns the code default when nothing is overridden', () => {
    expect(resolvePanelToggleBinding('session-panel', {})).toEqual({ key: 'b', mod: true })
  })

  it('returns a custom override in place of the default', () => {
    const override = { 'session-panel': { key: 'j', mod: true } } as const
    expect(resolvePanelToggleBinding('session-panel', override)).toEqual({ key: 'j', mod: true })
  })

  it('treats an explicit null override as unbound (not the default)', () => {
    expect(resolvePanelToggleBinding('session-panel', { 'session-panel': null })).toBeNull()
  })
})

describe('load / save round-trip', () => {
  it('returns an empty override map when nothing is stored', () => {
    expect(loadPanelToggleOverrides()).toEqual({})
  })

  it('round-trips a custom chord, normalized, without touching the other panels', () => {
    expect(setPanelToggleBinding('session-panel', { key: 'J', mod: true })).toBe(true)
    expect(loadPanelToggleOverrides()).toEqual({ 'session-panel': { key: 'j', mod: true } })
  })

  it('persists a null override so a cleared binding survives a reload', () => {
    expect(setPanelToggleBinding('session-panel', null)).toBe(true)
    expect(loadPanelToggleOverrides()).toEqual({ 'session-panel': null })
    // A cleared binding must stay cleared, never silently fall back to the default.
    expect(resolvePanelToggleBinding('session-panel', loadPanelToggleOverrides())).toBeNull()
  })

  it('refuses a non-null chord with no modifier and leaves storage untouched', () => {
    expect(setPanelToggleBinding('session-panel', { key: 'b' })).toBe(false)
    expect(loadPanelToggleOverrides()).toEqual({})
  })

  it('drops a malformed stored value back to no overrides', () => {
    localStorage.setItem(PANEL_TOGGLE_SHORTCUTS_KEY, 'not json')
    expect(loadPanelToggleOverrides()).toEqual({})
  })

  it('drops only the invalid per-panel entries, keeping the valid ones', () => {
    localStorage.setItem(
      PANEL_TOGGLE_SHORTCUTS_KEY,
      JSON.stringify({ 'session-panel': { key: 'b' }, 'side-panel': { key: 'k', mod: true }, bogus: 1 }),
    )
    expect(loadPanelToggleOverrides()).toEqual({ 'side-panel': { key: 'k', mod: true } })
  })

  it('broadcasts a change event on save', () => {
    const spy = vi.fn()
    window.addEventListener(PANEL_TOGGLE_SHORTCUTS_EVENT, spy)
    setPanelToggleBinding('session-panel', { key: 'b', mod: true })
    expect(spy).toHaveBeenCalledTimes(1)
    window.removeEventListener(PANEL_TOGGLE_SHORTCUTS_EVENT, spy)
  })
})

describe('matchPanelToggleEvent', () => {
  it('matches the default session-panel chord (Cmd on macOS, Ctrl on Windows/Linux)', () => {
    expect(matchPanelToggleEvent(ke({ code: 'KeyB', metaKey: true }), {}, true)).toBe('session-panel')
    expect(matchPanelToggleEvent(ke({ code: 'KeyB', ctrlKey: true }), {}, false)).toBe('session-panel')
  })

  it('matches the default side-panel chord on the physical backslash key', () => {
    expect(matchPanelToggleEvent(ke({ code: 'Backslash', key: '\\', metaKey: true }), {}, true)).toBe('side-panel')
  })

  it('does not match the left sidebar by default (unbound), but does once bound', () => {
    expect(matchPanelToggleEvent(ke({ code: 'KeyS', metaKey: true, shiftKey: true }), {}, true)).toBeNull()
    const bound = { 'left-sidebar': { key: 's', mod: true, shift: true } } as const
    expect(matchPanelToggleEvent(ke({ code: 'KeyS', metaKey: true, shiftKey: true }), bound, true)).toBe('left-sidebar')
  })

  it('never matches an unbound (null) panel', () => {
    expect(matchPanelToggleEvent(ke({ code: 'KeyB', metaKey: true }), { 'session-panel': null }, true)).toBeNull()
  })

  it('rejects the opposite primary modifier', () => {
    // A Mac ⌃B must not satisfy a mod chord meant as ⌘B.
    expect(matchPanelToggleEvent(ke({ code: 'KeyB', ctrlKey: true }), {}, true)).toBeNull()
  })
})
