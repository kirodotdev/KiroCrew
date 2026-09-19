import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import reducer from '../store/chatSlice'
import { renderWithProviders, createTestStore } from './helpers'

vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      const fn = vi.fn().mockResolvedValue(
        prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
      )
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'

const SLOT = 'test-slot-1'
const initial = reducer(undefined, { type: '@@INIT' })

function storeWith(sideOver: Record<string, unknown>) {
  return createTestStore({
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user' as const, content: 'q', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
            { role: 'assistant' as const, content: 'Remove the per-service limiter.', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
          ...sideOver,
        },
      },
    },
  })
}

describe('SideChat send to main chat', () => {
  it('offers the button on a settled assistant answer', () => {
    renderWithProviders(<SideChat slot={SLOT} />, { store: storeWith({}) })
    expect(screen.getByTestId('side-chat-send-to-main')).toBeInTheDocument()
  })

  it('clicking stages the answer into the main composer (append, not send)', () => {
    const store = storeWith({})
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    fireEvent.click(screen.getByTestId('side-chat-send-to-main'))
    expect(store.getState().chat.mainComposerAppend).toBe('Remove the per-service limiter.')
  })

  it('is hidden while the answer is still streaming', () => {
    renderWithProviders(<SideChat slot={SLOT} />, { store: storeWith({ streaming: true }) })
    expect(screen.queryByTestId('side-chat-send-to-main')).not.toBeInTheDocument()
  })

  it('is hidden when the last answer is an error', () => {
    const store = createTestStore({
      chat: {
        ...initial,
        activeSlot: SLOT,
        slotSide: {
          [SLOT]: {
            messages: [
              { role: 'user' as const, content: 'q', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
              { role: 'assistant' as const, content: 'boom', ts: '2026-05-20T00:00:01Z', run_id: 'r1', is_error: true },
            ],
            lastRunId: 'r1',
          },
        },
      },
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    expect(screen.queryByTestId('side-chat-send-to-main')).not.toBeInTheDocument()
  })
})
