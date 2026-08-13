import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import reducer, { sseSideResult } from '../store/chatSlice'
import type { RootState } from '../store'
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

const SLOT = 'parent-slot-1'

describe('Side multi-turn conversation', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('accumulates 3 sequential turns with distinct run_ids', () => {
    let state = initial
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'a1' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r2', role: 'user', content: 'q2' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r2', role: 'assistant', content: 'a2' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'user', content: 'q3' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r3', role: 'assistant', content: 'a3' }))

    const msgs = state.slotSide[SLOT].messages
    expect(msgs).toHaveLength(6)
    expect(msgs.map(m => m.run_id)).toEqual(['r1', 'r1', 'r2', 'r2', 'r3', 'r3'])
    expect(state.slotSide[SLOT].lastRunId).toBe('r3')
  })

  describe('component', () => {
    beforeEach(() => {
      vi.restoreAllMocks()
    })

    it('renders all messages from a multi-turn side conversation', () => {
      const store = createTestStore({
        chat: {
          activeSlot: SLOT,
          messages: [],
          slotRunning: false,
          slotStopping: false,
          slotState: 'idle',
          slotStatusDetail: {},
          slotHasMore: false,
          slotOldestIndex: 0,
          loadingOlder: false,
          lastChunkSeq: undefined,
          _wsChunkedDuringFetch: false,
          history: [],
          historyHasMore: false,
          historyOffset: 0,
          pendingInput: null,
          slotContextPct: {},
          voicePlaying: false,
          voiceAudio: null,
          subagents: {},
          toolLog: [],
          activityOpen: true,
          activityTab: 'side',
          focusToolCallId: null,
          slotActivity: {},
          slotSide: {
            [SLOT]: {
              messages: [
                { role: 'user', content: 'Turn 1 q', ts: '2026-05-19T10:00:00Z', run_id: 'r1' },
                { role: 'assistant', content: 'Turn 1 a', ts: '2026-05-19T10:00:01Z', run_id: 'r1' },
                { role: 'user', content: 'Turn 2 q', ts: '2026-05-19T10:01:00Z', run_id: 'r2' },
                { role: 'assistant', content: 'Turn 2 a', ts: '2026-05-19T10:01:01Z', run_id: 'r2' },
              ],
              lastRunId: 'r2',
            },
          },
          slotHistory: [SLOT],
          stopPressedAt: {},
        } as unknown as RootState['chat'],
      })
      renderWithProviders(<SideChat slot={SLOT} />, { store })
      expect(screen.getByText('Turn 1 q')).toBeInTheDocument()
      expect(screen.getByText('Turn 2 a')).toBeInTheDocument()
    })
  })
})
