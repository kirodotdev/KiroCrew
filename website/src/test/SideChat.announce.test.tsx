/**
 * The side-question panel feeds the transcript's one live region. Its rows are
 * drawn by the shared, virtualized `ChatMessageList`, whose scroller is not a
 * live region, so without this a side answer arrives in silence.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { renderWithProviders, createTestStore } from './helpers'
import { sseSideResult } from '../store/chatSlice'

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<PropertyKey, unknown>, {
    get: (_t, prop) => {
      if (Object.prototype.hasOwnProperty.call(_t, prop)) return _t[prop]
      const fn = vi.fn().mockResolvedValue(
        prop === 'sideOpen' ? { ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }
          : prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
            : prop === 'acpBackends' ? { backends: [{ id: '', capabilities: [{ id: 'side_chat_tools', available: true }] }] }
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

function storeWithHistory() {
  return createTestStore({
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
            { role: 'user', content: 'first question', ts: '2026-05-19T10:00:00Z', run_id: 'r1' },
            { role: 'assistant', content: 'an old answer', ts: '2026-05-19T10:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
        },
      },
      slotHistory: [SLOT],
      stopPressedAt: {},
    } as unknown as RootState['chat'],
  })
}

const liveRegion = (c: HTMLElement) => c.querySelector('[aria-live="polite"][aria-atomic="true"]')

describe('SideChat live region', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => { vi.restoreAllMocks() })

  it('stays silent for the side history it opens with, and speaks a new side answer', async () => {
    const store = storeWithHistory()
    const { container } = renderWithProviders(<SideChat slot={SLOT} />, { store })
    await waitFor(() => expect(liveRegion(container)).not.toBeNull())
    expect(liveRegion(container)?.textContent).toBe('')
    act(() => {
      store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'assistant', content: 'the new answer', ts: Date.parse('2026-05-19T10:05:00Z') / 1000, final: true, message_id: 'm2' }))
    })
    await waitFor(() => expect(liveRegion(container)?.textContent).toContain('the new answer'))
  })
})
