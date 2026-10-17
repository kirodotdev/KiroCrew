import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer from '../store/dashboardSlice'
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
    sideStop: vi.fn().mockResolvedValue({ ok: true }),
    sideTools: vi.fn().mockResolvedValue({ read_only_tools: true, claude_adapter_outdated: false, config_unavailable: false }),
  },
}))

/** A sidecar mid-turn: `streaming` is what SideChat derives `isBusy` from, so this is
 *  the exact state a hung response leaves behind. */
function busySide() {
  return {
    messages: [{ role: 'user', content: 'side q', ts: new Date().toISOString(), run_id: 'r1' }],
    openedAtTurnCount: 2,
    createdAt: new Date().toISOString(),
    lastRunId: 'r1',
    pending: true,
    streaming: true,
    queue: [],
  }
}

function makeStore(sideState: Record<string, unknown>) {
  const preloaded = {
    chat: {
      activeSlot: 'slot-1',
      messages: [
        { role: 'user', content: 'hi', cls: '', ts: new Date().toISOString() },
        { role: 'assistant', content: 'hello', cls: '', ts: new Date().toISOString() },
      ],
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
      slotSide: { 'slot-1': sideState },
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

describe('SideChat stop control (hung turn escape hatch)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows a stop button while a turn is in flight and clicking it calls api.sideStop', async () => {
    const { api } = await import('../api/client')
    renderWithStore(makeStore(busySide()))
    // The composer renders the armed stop control only while a turn runs and
    // onStop is wired (gap 1: the side composer now wires onStop).
    const stopBtn = await screen.findByTestId('stop-button-armed')
    fireEvent.click(stopBtn)
    await waitFor(() => {
      expect(api.sideStop).toHaveBeenCalledWith('slot-1')
    })
  })

  it('Refresh context is disabled while a turn runs; Stop is the in-flight escape (gap 2)', () => {
    renderWithStore(makeStore(busySide()))
    // A running turn can hold an accepted-but-unconsumed steer with no queue
    // card, so a mid-turn Refresh would silently drop it. The hung-turn escape
    // is the Stop control; Refresh becomes reachable only once the turn settles.
    const refresh = screen.getByText('Refresh context').closest('button')
    expect(refresh).not.toBeNull()
    expect(refresh).toBeDisabled()
    // The greyed Refresh is not silent: its title names the escape so the user
    // who still reaches for the old recovery habit is pointed at Stop.
    expect(refresh).toHaveAttribute(
      'title',
      'Stop the running answer first — refreshing would discard it',
    )
    expect(screen.getByTestId('stop-button-armed')).toBeInTheDocument()
  })

  it('Refresh context stays blocked while a question is queued', () => {
    const side = busySide()
    side.queue = [{ id: 'q1', content: 'queued one', ts: new Date().toISOString() }] as never
    renderWithStore(makeStore(side))
    const refresh = screen.getByText('Refresh context').closest('button')
    expect(refresh).toBeDisabled()
  })

  it('a failed stop surfaces an error notice instead of a silent dead end', async () => {
    const { api } = await import('../api/client')
    ;(api.sideStop as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      Object.assign(new Error('side conversation is not open'), { status: 409 }),
    )
    renderWithStore(makeStore(busySide()))
    const stopBtn = await screen.findByTestId('stop-button-armed')
    fireEvent.click(stopBtn)
    // The rejected mutation feeds the file's ErrorNotice via setLocalError, so the
    // hung turn is not left with no failure feedback.
    await waitFor(() => {
      expect(screen.getByText('Could not stop the side turn — try again')).toBeInTheDocument()
    })
  })
})
