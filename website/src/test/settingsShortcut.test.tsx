/**
 * The "Open settings" chord: ⌘+, on macOS, Alt+, on Windows/Linux.
 *
 * macOS reserves ⌘+, for Preferences — the desktop app's own "Settings…" menu
 * item already binds `CmdOrCtrl+,` (electron/app-menu.js) — so the in-page
 * binding and the shortcuts reference must advertise ⌘+, there, not Option+,.
 *
 * `isSettingsChord` takes the platform as an injectable argument, so both
 * behaviours are asserted without reloading the module (IS_MAC is fixed at
 * module load, and this file runs in a non-Mac jsdom).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent } from '@testing-library/react'

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigateSpy }
})

import {
  DEFAULT_SHORTCUTS,
  IS_MAC,
  RESERVED_PANEL_CODES,
  SHORTCUTS_ENABLED_KEY,
  formatShortcut,
  isSettingsChord,
  useKeyboardShortcuts,
} from '../hooks/useKeyboardShortcuts'
import { createTestStore, renderHookWithProviders } from './helpers'
import type { RootState } from '../store'

type Chord = Pick<KeyboardEvent, 'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>

const chord = (over: Partial<Chord> = {}): Chord => ({
  code: 'Comma',
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  ...over,
})

const setPlatform = (val: string) =>
  Object.defineProperty(navigator, 'platform', { value: val, configurable: true })

describe('isSettingsChord — macOS', () => {
  it('accepts ⌘+,', () => {
    expect(isSettingsChord(chord({ metaKey: true }), true)).toBe(true)
  })
  it('still accepts Option+, (unadvertised fallback for Mac browsers that eat ⌘+,)', () => {
    expect(isSettingsChord(chord({ altKey: true }), true)).toBe(true)
  })
  it('rejects ⌘⌥+, — exactly one primary modifier', () => {
    expect(isSettingsChord(chord({ metaKey: true, altKey: true }), true)).toBe(false)
  })
  it('rejects ⌃+, and ⌘⌃+,', () => {
    expect(isSettingsChord(chord({ ctrlKey: true }), true)).toBe(false)
    expect(isSettingsChord(chord({ metaKey: true, ctrlKey: true }), true)).toBe(false)
  })
  it('rejects the shifted form (⌘⇧+, is a different chord)', () => {
    expect(isSettingsChord(chord({ metaKey: true, shiftKey: true }), true)).toBe(false)
  })
  it('rejects a non-comma key', () => {
    expect(isSettingsChord(chord({ code: 'KeyK', metaKey: true }), true)).toBe(false)
  })
})

describe('isSettingsChord — Windows/Linux', () => {
  it('accepts Alt+,', () => {
    expect(isSettingsChord(chord({ altKey: true }), false)).toBe(true)
  })
  it('rejects Ctrl+, / Meta+, (the shell menu owns CmdOrCtrl+, there)', () => {
    expect(isSettingsChord(chord({ ctrlKey: true }), false)).toBe(false)
    expect(isSettingsChord(chord({ metaKey: true }), false)).toBe(false)
  })
  it('rejects a bare comma', () => {
    expect(isSettingsChord(chord(), false)).toBe(false)
  })
})

describe('open-settings registry entry', () => {
  const def = DEFAULT_SHORTCUTS.find(s => s.id === 'open-settings')!

  it('binds the platform primary modifier, never both', () => {
    expect(def.key).toBe(',')
    expect(!!def.meta).toBe(IS_MAC)
    expect(!!def.alt).toBe(!IS_MAC)
    expect(def.shift).toBeUndefined()
  })

  it('renders as ⌘, on Mac and Alt + , elsewhere', () => {
    setPlatform('MacIntel')
    expect(formatShortcut({ ...def, alt: false, meta: true })).toBe('\u2318,')
    setPlatform('Win32')
    expect(formatShortcut({ ...def, alt: true, meta: false })).toBe('Alt + ,')
  })

  it('keeps Comma reserved against downstream panel registration', () => {
    // Still consumed before panel routing on both platforms (Option+, remains
    // bound on Mac), so a downstream panel on Comma would be unreachable.
    expect(RESERVED_PANEL_CODES.has('Comma')).toBe(true)
  })
})

describe('useKeyboardShortcuts — settings navigation', () => {
  function setup(opts: { enabled?: boolean; disabled?: boolean } = {}) {
    if (opts.enabled === false) localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const store = createTestStore({
      dashboard: { slots: [] } as unknown as RootState['dashboard'],
      chat: { activeSlot: null, slotHistory: [] } as unknown as RootState['chat'],
    })
    renderHookWithProviders(
      () => useKeyboardShortcuts({ onToggleShortcutsModal: () => {}, onNewChat: () => {}, disabled: opts.disabled }),
      { store },
    )
  }

  beforeEach(() => {
    localStorage.clear()
    navigateSpy.mockClear()
  })

  it('runs in a non-Mac jsdom, so Alt+, is the live chord here', () => {
    expect(IS_MAC).toBe(false)
  })

  it('Alt+, navigates to /settings', () => {
    setup()
    fireEvent.keyDown(document, { code: 'Comma', altKey: true })
    expect(navigateSpy).toHaveBeenCalledWith('/settings')
  })

  it('Alt+, still navigates when shortcuts are globally disabled', () => {
    // The escape hatch: Settings holds the toggle that re-enables shortcuts.
    setup({ enabled: false })
    fireEvent.keyDown(document, { code: 'Comma', altKey: true })
    expect(navigateSpy).toHaveBeenCalledWith('/settings')
  })

  it('Meta+, does not navigate on a non-Mac platform', () => {
    setup()
    fireEvent.keyDown(document, { code: 'Comma', metaKey: true })
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('Alt+Shift+, does not navigate', () => {
    setup()
    fireEvent.keyDown(document, { code: 'Comma', altKey: true, shiftKey: true })
    expect(navigateSpy).not.toHaveBeenCalled()
  })
})
