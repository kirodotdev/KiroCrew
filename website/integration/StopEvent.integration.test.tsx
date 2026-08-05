import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { createTestStore, renderWithProviders } from './helpers'
import StopEventCard from '../src/pages/chat/StopEventCard'
import type { ChatMessage } from '../src/types'
import type { RootState } from '../src/store'
import { sseChatMessage } from '../src/store/chatSlice'
import { act } from '@testing-library/react'

function makeMsg(state: string, id = 'stop-abc123'): ChatMessage {
  return {
    role: 'system',
    content: '',
    cls: '',
    kind: 'stop_event',
    meta: { kind: 'stop_event', id, state, ts_start: 1714000000, outcome: state === 'stopping' ? null : state === 'stopped' ? 'soft' : 'hard' },
  }
}

describe('StopEventCard', () => {
  it('test stopping state renders red pulse', () => {
    renderWithProviders(<StopEventCard message={makeMsg('stopping')} />)

    const card = screen.getByTestId('stop-event-card')
    expect(card).toBeInTheDocument()
    expect(card.textContent).toContain('Stopping')
    // Deterministic state assertion (opacity would be a no-op — Framer Motion does
    // not produce inline styles reliably in jsdom).
    expect(card.getAttribute('data-state')).toBe('stopping')
  })

  it('test stopped state renders [Stopped]', () => {
    renderWithProviders(<StopEventCard message={makeMsg('stopped')} />)

    const card = screen.getByTestId('stop-event-card')
    expect(card).toBeInTheDocument()
    expect(card.textContent).toContain('[Stopped]')
  })

  it('test stop_failed_reset renders [Stop Failed, Session Reset]', () => {
    renderWithProviders(<StopEventCard message={makeMsg('stop_failed_reset')} />)

    const card = screen.getByTestId('stop-event-card')
    expect(card).toBeInTheDocument()
    expect(card.textContent).toContain('[Stop Failed, Session Reset]')
  })

  it('test card replaces itself in place (same key)', () => {
    const SLOT = 'test-slot'
    const store = createTestStore({
      chat: {
        activeSlot: SLOT,
        messages: [],
        slotRunning: true,
        slotStopping: false,
        slotState: 'streaming',
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
        activityOpen: false,
        activityTab: 'tools',
        slotActivity: {},
        stopPressedAt: {},
      } as RootState['chat'],
    })

    // Insert a stopping event
    act(() => {
      store.dispatch(sseChatMessage({
        slot: SLOT,
        role: 'system',
        content: '',
        kind: 'stop_event',
        meta: { kind: 'stop_event', id: 'stop-xyz', state: 'stopping', ts_start: 1714000000 },
      }))
    })

    let msgs = store.getState().chat.messages
    expect(msgs).toHaveLength(1)
    expect(msgs[0].meta?.state).toBe('stopping')

    // Update the same event to stopped — should replace in place
    act(() => {
      store.dispatch(sseChatMessage({
        slot: SLOT,
        role: 'system',
        content: '',
        kind: 'stop_event',
        meta: { kind: 'stop_event', id: 'stop-xyz', state: 'stopped', ts_end: 1714000002, outcome: 'soft' },
      }))
    })

    msgs = store.getState().chat.messages
    expect(msgs).toHaveLength(1) // Still 1 message — replaced in place
    expect(msgs[0].meta?.state).toBe('stopped')
    expect(msgs[0].meta?.id).toBe('stop-xyz')
  })
})
