// PopoutFrame is the window shell for a popped-out chat session: it mounts the
// embedded chat, announces itself as a live popout for the `sid` in the query
// string, and mirrors the session title into the OS window title.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { createTestStore, renderWithProviders } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

const { registerPopout, unregister } = vi.hoisted(() => {
  const unregister = vi.fn()
  return { registerPopout: vi.fn(() => unregister), unregister }
})

vi.mock('../utils/chatPopout', () => ({ registerPopout }))

const chatProps: Record<string, unknown>[] = []
vi.mock('../pages/ChatPage', () => ({
  default: (p: Record<string, unknown>) => {
    chatProps.push(p)
    return <div data-testid="chat-page" />
  },
}))

import PopoutFrame from '../pages/PopoutFrame'

function storeWithSlot(key: string, title?: string) {
  const store = createTestStore()
  store.dispatch(sseSlots([{ key, title } as ChatSlot]))
  return store
}

beforeEach(() => {
  registerPopout.mockClear()
  unregister.mockClear()
  chatProps.length = 0
  document.title = ''
})

describe('PopoutFrame', () => {
  it('mounts the single-session chat in popout mode', () => {
    renderWithProviders(<PopoutFrame />, { route: '/popout/chat/x?sid=slot-1' })
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
    expect(chatProps[0]).toMatchObject({ embedded: true, embedMode: 'chat', popout: true })
  })

  it('registers as a live popout for the sid and unregisters on unmount', () => {
    const { unmount } = renderWithProviders(<PopoutFrame />, { route: '/popout/chat/x?sid=slot-1' })
    expect(registerPopout).toHaveBeenCalledWith('slot-1')
    unmount()
    expect(unregister).toHaveBeenCalled()
  })

  it('accepts the legacy slot param as an alias for sid', () => {
    renderWithProviders(<PopoutFrame />, { route: '/popout/chat/x?slot=slot-7' })
    expect(registerPopout).toHaveBeenCalledWith('slot-7')
  })

  it('registers nothing when no session is identified', () => {
    renderWithProviders(<PopoutFrame />, { route: '/popout/chat/x' })
    expect(registerPopout).not.toHaveBeenCalled()
  })

  it('mirrors the session title into the window title', () => {
    renderWithProviders(<PopoutFrame />, {
      route: '/popout/chat/x?sid=slot-1',
      store: storeWithSlot('slot-1', 'zz-title'),
    })
    expect(document.title).toBe('zz-title — Kiro Crew')
  })

  it('falls back to a generic label when the slot has no distinct title', () => {
    renderWithProviders(<PopoutFrame />, {
      route: '/popout/chat/x?sid=slot-1',
      store: storeWithSlot('slot-1', 'slot-1'),
    })
    expect(document.title).toBe('Session — Kiro Crew')
  })

  it('falls back to a generic label when the slot is unknown', () => {
    renderWithProviders(<PopoutFrame />, { route: '/popout/chat/x?sid=ghost' })
    expect(document.title).toBe('Session — Kiro Crew')
  })
})
