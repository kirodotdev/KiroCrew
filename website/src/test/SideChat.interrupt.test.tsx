import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer from '../store/dashboardSlice'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import chatReducer, { sseSideResult } from '../store/chatSlice'
import SideChat from '../pages/chat/SideChat'

vi.mock('../api/client', () => ({
  api: {
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: new Date().toISOString() }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
    sideInterrupt: vi.fn().mockResolvedValue({ ok: true, interrupted: true, run_id: 'r1' }),
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: '' } }),
  },
}))

function makeStore(sideState?: Record<string, unknown>) {
  const preloaded = {
    chat: {
      activeSlot: 'slot-1',
      messages: [],
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
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
    preloadedState: {
      ...(preloaded as object),
      dashboard: { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true },
    } as never,
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

/** A side turn in flight: one streamed answer, streaming+pending set. The empty
 *  composer is the hung-answer shape — nothing typed to steer or queue. */
function busySideState() {
  return {
    messages: [
      { role: 'user', content: 'side q', ts: new Date().toISOString(), run_id: 'r1' },
      { role: 'assistant', content: 'partial…', ts: new Date().toISOString(), run_id: 'r1' },
    ],
    openedAtTurnCount: 0,
    createdAt: new Date().toISOString(),
    lastRunId: 'r1',
    pending: true,
    streaming: true,
  }
}

describe('SideChat interrupt', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows a Stop control while a turn is in flight', () => {
    renderWithStore(makeStore(busySideState()))
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
  })

  it('clicking Stop calls api.sideInterrupt for the slot', async () => {
    const { api } = await import('../api/client')
    renderWithStore(makeStore(busySideState()))
    fireEvent.click(screen.getByTestId('stop-button-armed'))
    await waitFor(() => {
      expect(api.sideInterrupt).toHaveBeenCalledWith('slot-1')
    })
  })

  it('shows a failed interrupt in the error notice', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sideInterrupt).mockRejectedValueOnce(new Error('interrupt failed'))
    renderWithStore(makeStore(busySideState()))
    fireEvent.click(screen.getByTestId('stop-button-armed'))
    expect(await screen.findByText('interrupt failed')).toBeInTheDocument()
  })

  it('settles the panel from the HTTP reply when no WebSocket frame arrives', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.sideInterrupt).mockResolvedValueOnce({ ok: true, interrupted: true, run_id: 'r1', content: '(answer stopped)' })
    const store = makeStore(busySideState())
    renderWithStore(store)
    fireEvent.click(screen.getByTestId('stop-button-armed'))
    await waitFor(() => {
      const side = store.getState().chat.slotSide['slot-1']
      expect(side.pending).toBe(false)
      expect(side.streaming).toBe(false)
    })
    const rows = store.getState().chat.slotSide['slot-1'].messages.filter(m => m.is_error)
    expect(rows).toHaveLength(1)
    expect(rows[0].content).toBe('(answer stopped)')
  })

  it('ignores the duplicate WebSocket stop frame, even after the next turn started', () => {
    const store = makeStore(busySideState())
    const stop = { slot: 'slot-1', run_id: 'r1', role: 'assistant' as const, content: '(answer stopped)', is_error: true, final: true }
    store.dispatch(sseSideResult(stop))
    store.dispatch(sseSideResult({ slot: 'slot-1', run_id: 'r2', role: 'user', content: 'next q' }))
    store.dispatch(sseSideResult(stop))
    const side = store.getState().chat.slotSide['slot-1']
    expect(side.messages.filter(m => m.is_error)).toHaveLength(1)
    expect(side.pending).toBe(true)
    expect(side.streaming).toBe(true)
  })

  it('ignores a late stop row for an older run once a newer run has started', () => {
    const store = makeStore(busySideState())
    // The WebSocket lost r1's stop frame but delivered r2's first frame; r1's
    // HTTP reply then arrives late.
    store.dispatch(sseSideResult({ slot: 'slot-1', run_id: 'r2', role: 'user', content: 'next q' }))
    store.dispatch(sseSideResult({ slot: 'slot-1', run_id: 'r1', role: 'assistant', content: '(answer stopped)', is_error: true, final: true }))
    const side = store.getState().chat.slotSide['slot-1']
    expect(side.pending).toBe(true)
    expect(side.streaming).toBe(true)
    expect(side.lastRunId).toBe('r2')
    expect(side.messages.filter(m => m.is_error)).toHaveLength(0)
  })

  it('shows no Stop control when idle', () => {
    renderWithStore(
      makeStore({
        messages: [
          { role: 'user', content: 'side q', ts: new Date().toISOString(), run_id: 'r1' },
          { role: 'assistant', content: 'done', ts: new Date().toISOString(), run_id: 'r1' },
        ],
        openedAtTurnCount: 0,
        createdAt: new Date().toISOString(),
        lastRunId: 'r1',
        pending: false,
        streaming: false,
      })
    )
    expect(screen.queryByTestId('stop-button-armed')).not.toBeInTheDocument()
  })
})
