import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import chatReducer from '../store/chatSlice'
import SideChat from '../pages/chat/SideChat'

vi.mock('../api/client', () => ({
  api: {
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: new Date().toISOString() }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
  },
}))

function makeStore(parentMessages: Array<{ role: string; content: string }> = [], sideState?: Record<string, unknown>) {
  const preloaded = {
    chat: {
      activeSlot: 'slot-1',
      messages: parentMessages.map(m => ({ role: m.role, content: m.content, cls: '', ts: new Date().toISOString() })),
      slotRunning: false,
      slotStopping: false,
      slotState: 'idle' as const,
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
      activityTab: 'side' as const,
      focusToolCallId: null,
      slotActivity: {},
      slotSide: sideState ? { 'slot-1': sideState } : {},
      slotSideClosed: {},
      slotHistory: [],
      stopPressedAt: {},
    },
  }
  return configureStore({
    reducer: { chat: chatReducer },
    preloadedState: preloaded as never,
  })
}

function renderWithStore(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <SideChat slot="slot-1" />
      </Provider>
    </QueryClientProvider>
  )
}

describe('SideChat stale-context banner', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('banner shows correct turn count when parent has advanced', () => {
    const store = makeStore(
      [
        { role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1' },
        { role: 'user', content: 'q2' }, { role: 'assistant', content: 'a2' },
        { role: 'user', content: 'q3' }, { role: 'assistant', content: 'a3' },
      ],
      {
        messages: [{ role: 'user', content: 'side q', ts: new Date().toISOString(), run_id: 'r1' }],
        openedAtTurnCount: 2,
        createdAt: new Date().toISOString(),
      }
    )
    const { container } = renderWithStore(store)
    const banner = container.querySelector('span.italic')
    expect(banner).not.toBeNull()
    const text = (banner?.textContent ?? '').replace(/\s+/g, ' ').trim()
    expect(text.toLowerCase()).toContain('context from 4 turns ago')
  })

  it('clicking Refresh context calls api.sideClose AND drops slotSide', async () => {
    const { api } = await import('../api/client')
    const store = makeStore(
      [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'hello' }],
      {
        messages: [{ role: 'user', content: 'side q', ts: new Date().toISOString(), run_id: 'r1' }],
        openedAtTurnCount: 2,
        createdAt: new Date().toISOString(),
      }
    )
    renderWithStore(store)
    fireEvent.click(screen.getByText('Refresh context'))
    await waitFor(() => {
      expect(api.sideClose).toHaveBeenCalledWith('slot-1')
    })
    expect(store.getState().chat.slotSide['slot-1']).toBeUndefined()
  })

  it('rolls back optimistic user bubble when sendMutation rejects', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sideTurn).mockRejectedValueOnce(new Error('side turn already in flight'))
    const store = makeStore(
      [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'hello' }],
    )
    renderWithStore(store)
    const textarea = screen.getByPlaceholderText(/Ask a side question/i)
    fireEvent.change(textarea, { target: { value: 'doomed q' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => {
      expect(store.getState().chat.slotSide['slot-1']?.messages).toHaveLength(1)
    })
    // Rolled back out of the transcript — and handed back to the composer rather
    // than lost, since a rejected submit is a reachable path (e.g. queue full).
    await waitFor(() => {
      expect(store.getState().chat.slotSide['slot-1']?.messages ?? []).toHaveLength(0)
    })
    expect(textarea).toHaveValue('doomed q')
  })
})
