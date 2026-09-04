import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import reducer, { sseSideResult } from '../store/chatSlice'
import { renderWithProviders, createTestStore } from './helpers'

vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      const fn = vi.fn().mockResolvedValue(
        prop === 'sideOpen' ? { ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }
          : prop === 'sideTurn' ? { ok: true, run_id: 'r1', messages: 1 }
            : prop === 'sideClose' ? { ok: true, was_open: true }
              : prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
                : {},
      )
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'

const SLOT = 'test-slot-1'

describe('SideChat Thinking indicator', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('user frame sets pending=true; assistant frame clears it', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    expect(state.slotSide[SLOT].pending).toBe(true)
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'a' }))
    expect(state.slotSide[SLOT].pending).toBe(false)
  })

  it('renders Thinking indicator when pending=true', () => {
    const store = createTestStore({
      chat: {
        ...initial,
        activeSlot: SLOT,
        slotSide: {
          [SLOT]: {
            messages: [{ role: 'user' as const, content: 'q', ts: '2026-05-20T00:00:00Z', run_id: 'r1' }],
            lastRunId: 'r1',
            pending: true,
          },
        },
      },
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    expect(screen.getByText('Thinking…')).toBeInTheDocument()
  })
})
