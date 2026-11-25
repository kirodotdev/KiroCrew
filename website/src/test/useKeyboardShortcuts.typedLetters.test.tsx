/**
 * The keydown handler reads a key that types a Latin letter as that letter for
 * its code families (chat jumps, panel navigation), so a remapped Latin layout
 * gets the chord its keys type. Dvorak types `c` on QWERTY's I position.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { Provider } from 'react-redux'
import { useKeyboardShortcuts } from '../hooks/useKeyboardShortcuts'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { createTestStore } from './helpers'
import type { RootState } from '../store'

const slots = Array.from({ length: 13 }, (_, i) => ({
  key: `slot-${i + 1}`, title: `S${i + 1}`, messages: 0, running: false,
}))

function Probe() {
  useKeyboardShortcuts({ onToggleShortcutsModal: vi.fn(), onNewChat: vi.fn() })
  return <div data-testid="loc">{useLocation().pathname}</div>
}

describe('code families read the letter the layout types', () => {
  it('on Dvorak, Alt + the key that types c opens Chat, not the jump on its QWERTY position', () => {
    // By position, KeyI is the 13th session's jump letter.
    const store = createTestStore({
      dashboard: {
        ...dashboardReducer(undefined, { type: '@@test/init' }), slots, sidebarOrder: slots.map(s => s.key),
      } as RootState['dashboard'],
      chat: { ...chatReducer(undefined, { type: '@@test/init' }), activeSlot: null, slotHistory: [] } as RootState['chat'],
    })
    render(<Provider store={store}><MemoryRouter initialEntries={['/settings']}><Probe /></MemoryRouter></Provider>)
    fireEvent.keyDown(document, { code: 'KeyI', key: 'c', altKey: true })
    expect(store.getState().chat.activeSlot).toBeNull()
    expect(screen.getByTestId('loc').textContent).toBe('/chat')
  })
})
