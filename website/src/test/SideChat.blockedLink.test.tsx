import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import type { RootState } from '../store'
import { renderWithProviders, createTestStore } from './helpers'
import chatReducer, { sseSideResult } from '../store/chatSlice'

/* A finished side answer is drawn by the shared bubble, which reads a main chat
 * row's blocked-link records from `meta.link_blocks`. Its records arrive on the
 * final `chat.side_result` frame (or the restore read) and ride in the side row,
 * so a flagged link in a side answer draws as the Blocked link chip and never as
 * a live link, while the text keeps the address as written. */

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<PropertyKey, unknown>, {
    get: (_t, prop) => {
      if (Object.prototype.hasOwnProperty.call(_t, prop)) return _t[prop]
      const fn = vi.fn().mockResolvedValue(
        prop === 'sideOpen' ? { ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }
          : prop === 'sideTurn' ? { ok: true, run_id: 'r1', messages: 1 }
            : prop === 'sideClose' ? { ok: true, was_open: true }
              : prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
                : prop === 'acpBackends' ? { backends: [{ id: '', capabilities: [{ id: 'side_chat_tools', available: true }] }] }
                  : prop === 'revealPath' ? { ok: true }
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
const URL = 'https://collect.example.com/report?q=' + 'x'.repeat(260)
const RECORD = { url: URL, domain: 'collect.example.com', rule: 'exfil_query_length', query_chars: 262, withheld: null }

function storeWithReply(content: string, linkBlocks?: unknown) {
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
            { role: 'user', content: 'where are the notes?', ts: '2026-05-19T10:00:00Z', run_id: 'r1' },
            { role: 'assistant', content, ts: '2026-05-19T10:00:01Z', run_id: 'r1', id: 'a1', ...(linkBlocks !== undefined ? { link_blocks: linkBlocks } : {}) },
          ],
          lastRunId: 'r1',
        },
      },
      slotHistory: [SLOT],
      stopPressedAt: {},
    } as unknown as RootState['chat'],
  })
}

describe('SideChat draws a blocked link in a finished answer as its chip', () => {
  it('a side answer with a record shows the chip, not a live link', async () => {
    const { container } = renderWithProviders(<SideChat slot={SLOT} />, { store: storeWithReply(`See ${URL} for it.`, [RECORD]) })
    expect(await screen.findByTestId('blocked-link-chip')).toBeTruthy()
    expect(container.querySelector(`a[href="${URL}"]`)).toBeNull()
  })

  it('the same answer with no record renders its link live', async () => {
    const { container } = renderWithProviders(<SideChat slot={SLOT} />, { store: storeWithReply(`See ${URL} for it.`) })
    await screen.findByText(/for it/)
    expect(screen.queryByTestId('blocked-link-chip')).toBeNull()
    expect(container.querySelector(`a[href="${URL}"]`)).toBeTruthy()
  })

  it('the final frame brings the records onto the answer it settles, and a delta brings none', () => {
    // The whole initial chat state, so every map the reducer reads is there.
    const store = createTestStore({
      chat: {
        ...chatReducer(undefined, { type: '@@INIT' }),
        activeSlot: SLOT,
        slotSide: { [SLOT]: { messages: [{ role: 'user', content: 'q', ts: '2026-05-19T10:00:00Z', run_id: 'r1', id: 'q1' }], lastRunId: 'r1' } },
      } as unknown as RootState['chat'],
    })
    const side = () => store.getState().chat.slotSide[SLOT]
    store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'user', content: 'again?', message_id: 'q2' }))
    store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'assistant', content: 'See ', link_blocks: [RECORD] }))
    expect(side().messages.at(-1)?.link_blocks).toBeUndefined()
    store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'assistant', content: `See ${URL}`, final: true, message_id: 'a2', link_blocks: [RECORD] }))
    const settled = side().messages.at(-1)
    expect(settled?.content).toBe(`See ${URL}`)
    expect(settled?.link_blocks).toEqual([RECORD])
  })
})
