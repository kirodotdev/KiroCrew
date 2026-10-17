/**
 * macOS Option mode (`mc-mac-ctrl-digits = 0`): Option+digit is a typing chord
 * on macOS (Option+3 is # on a UK layout, Option+4 is ¢ on US), so the chat
 * jump must not claim it while a text field has focus. Outside a field the
 * jump still works, and Control mode is unchanged.
 *
 * `IS_MAC` freezes at module load from `navigator.platform`, so the platform is
 * set in `vi.hoisted`, which runs before this file's imports are evaluated.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.hoisted(() => {
  Object.defineProperty(navigator, 'platform', { value: 'MacIntel', configurable: true })
})

import { IS_MAC, MAC_CTRL_DIGITS_KEY, useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { createTestStore, renderHookWithProviders } from './helpers'
import type { RootState } from '../store'

const slots = [
  { key: 'slot-1', title: 'One', messages: 0, running: false },
  { key: 'slot-2', title: 'Two', messages: 0, running: false },
  { key: 'slot-3', title: 'Three', messages: 0, running: false },
]

function setup() {
  const chatInitial = chatReducer(undefined, { type: '@@test/init' })
  const dashInitial = dashboardReducer(undefined, { type: '@@test/init' })
  const store = createTestStore({
    dashboard: { ...dashInitial, slots } as RootState['dashboard'],
    chat: { ...chatInitial, activeSlot: 'slot-1', slotHistory: [] } as RootState['chat'],
  })
  renderHookWithProviders(
    () => useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() }),
    { store },
  )
  return store
}

/** Dispatches a keydown on `target`; true when the handler prevented it. */
function press(target: EventTarget, init: KeyboardEventInit): boolean {
  const ev = new KeyboardEvent('keydown', { cancelable: true, bubbles: true, ...init })
  return !target.dispatchEvent(ev)
}

let field: HTMLTextAreaElement

beforeEach(() => {
  localStorage.clear()
  field = document.createElement('textarea')
  document.body.appendChild(field)
})

afterEach(() => field.remove())

describe('macOS Option mode leaves Option+digit to text fields', () => {
  beforeEach(() => localStorage.setItem(MAC_CTRL_DIGITS_KEY, '0'))

  it('runs as macOS', () => {
    expect(IS_MAC).toBe(true)
  })

  it('Option+digit typed in a focused text field reaches the field', () => {
    const store = setup()
    field.focus()
    expect(press(field, { code: 'Digit3', altKey: true })).toBe(false)
    expect(store.getState().chat.activeSlot).toBe('slot-1')
  })

  it('Option+digit outside a text field still jumps to the session', () => {
    const store = setup()
    expect(press(document.body, { code: 'Digit3', altKey: true })).toBe(true)
    expect(store.getState().chat.activeSlot).toBe('slot-3')
  })
})

describe('macOS Control mode is unchanged', () => {
  it('Control+digit in a focused text field jumps to the session', () => {
    const store = setup()
    field.focus()
    expect(press(field, { code: 'Digit3', ctrlKey: true })).toBe(true)
    expect(store.getState().chat.activeSlot).toBe('slot-3')
  })
})
