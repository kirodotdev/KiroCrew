/**
 * The macOS "Open settings" chords with keyboard shortcuts turned off (#37193).
 *
 * ⌘, is the primary and keeps opening Settings with shortcuts off. Option+, is
 * a Mac alias, and on a Mac layout it types a character (≤ on US, " on Neo2),
 * so with shortcuts off it must reach the text field like every other
 * shortcut does.
 *
 * `IS_MAC` freezes at module load from `navigator.platform`, so the platform is
 * set in `vi.hoisted`, which runs before this file's imports are evaluated.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.hoisted(() => {
  Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true })
})

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigateSpy }
})

import { IS_MAC, SHORTCUTS_ENABLED_KEY, useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import { SHORTCUT_OVERRIDES_KEY } from '../lib/shortcutRegistry'
import { createTestStore, renderHookWithProviders } from './helpers'
import type { RootState } from '../store'

function setup() {
  const store = createTestStore({
    dashboard: { slots: [] } as unknown as RootState['dashboard'],
    chat: { activeSlot: null, slotHistory: [] } as unknown as RootState['chat'],
  })
  renderHookWithProviders(
    () => useKeyboardShortcuts({ onToggleShortcutsModal: () => {}, onNewChat: () => {} }),
    { store },
  )
}

/** Dispatches a comma keydown on the focused field; true when the handler prevented it. */
function pressComma(init: KeyboardEventInit): boolean {
  const ev = new KeyboardEvent('keydown', { cancelable: true, bubbles: true, code: 'Comma', ...init })
  return !field.dispatchEvent(ev)
}

let field: HTMLTextAreaElement

beforeEach(() => {
  localStorage.clear()
  navigateSpy.mockClear()
  field = document.createElement('textarea')
  document.body.appendChild(field)
  field.focus()
})

afterEach(() => field.remove())

describe('macOS open-settings chords', () => {
  it('runs with the macOS platform frozen in', () => {
    expect(IS_MAC).toBe(true)
  })

  it('shortcuts off: Option+, is not prevented and does not navigate', () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    setup()
    expect(pressComma({ key: '≤', altKey: true })).toBe(false)
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('shortcuts off: ⌘, still navigates to /settings', () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    setup()
    expect(pressComma({ key: ',', metaKey: true })).toBe(true)
    expect(navigateSpy).toHaveBeenCalledWith('/settings')
  })

  it('shortcuts on: Option+, still navigates to /settings', () => {
    setup()
    expect(pressComma({ key: '≤', altKey: true })).toBe(true)
    expect(navigateSpy).toHaveBeenCalledWith('/settings')
  })

  it('shortcuts off with a user override of ⌘,: ⌘, navigates and Option+, types', () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    localStorage.setItem(SHORTCUT_OVERRIDES_KEY, '{"open-settings":{"key":",","mod":true}}')
    setup()
    expect(pressComma({ key: '≤', altKey: true })).toBe(false)
    expect(navigateSpy).not.toHaveBeenCalled()
    expect(pressComma({ key: ',', metaKey: true })).toBe(true)
    expect(navigateSpy).toHaveBeenCalledWith('/settings')
  })
})
