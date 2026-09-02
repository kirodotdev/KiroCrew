import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider, useSelector } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentSpawn, sseSubagentDone } from '../store/chatSlice'
import type { RootState } from '../store'
import { useTipTrigger } from '../components/TipCard'

// Verifies the suppression chain end-to-end using the REAL reducer via
// store.dispatch (the path actual WS events take). Mutating the store DIRECTLY
// (no dispatch) never notifies react-redux subscribers, so the test dispatches
// actions and asserts the suppression chain works end-to-end.

const mockTip = {
  id: 'test-tip',
  feature: 'Cron Jobs',
  title: 'Schedule recurring tasks',
  body: 'Use cron_add to schedule recurring jobs.',
  why: '',
  doc: 'cron-and-scheduling.md',
  cta_prompt: '',
}

vi.mock('../api/client', () => ({
  api: {
    tipsFeedback: vi.fn().mockResolvedValue({ ok: true }),
    tipsNext: vi.fn().mockResolvedValue(null),
    tipsStatus: vi.fn().mockResolvedValue({ enabled_config: true, opted_out: false, cadence_hours: 6 }),
  },
}))

// Mirrors ChatPage's tipSuppressed subagent clause (the part under test)
function Harness() {
  const suppressed = useSelector((s: RootState) =>
    Object.values(s.chat.subagents).some(
      a => a.status === 'running' || a.status === 'tool' || a.status === 'pending',
    ),
  )
  const { tip } = useTipTrigger(true, suppressed, 'slot-a')
  return <div data-testid="tip-out">{tip ? tip.title : 'none'}</div>
}

describe('tip suppression via REAL subagent dispatch (QA v5 S4 adjudication)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.clear()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('dispatching sseSubagentSpawn hides a visible tip; sseSubagentDone restores eligibility', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    const store = configureStore({ reducer: { chat: chatReducer } })
    store.dispatch(setActiveSlot('slot-a'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <Harness />
        </QueryClientProvider>
      </Provider>,
    )

    // Tip becomes visible after the 10s gate
    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    expect(screen.getByTestId('tip-out').textContent).toBe('Schedule recurring tasks')

    // REAL dispatch — the path actual WS subagent events take
    await act(async () => {
      store.dispatch(sseSubagentSpawn({ slot: 'slot-a', id: 'sub-1', task: 't', agent: 'kirocrew' }))
    })
    expect(store.getState().chat.subagents['sub-1']?.status).toBe('running')
    // Suppression must flow: selector -> re-render -> useTipTrigger effect
    expect(screen.getByTestId('tip-out').textContent).toBe('none')

    // Subagent finishes -> suppression lifts (tip stays hidden this turn via
    // shownThisTurnRef, but the suppressed flag itself must clear)
    await act(async () => {
      store.dispatch(sseSubagentDone({ slot: 'slot-a', id: 'sub-1', status: 'done' } as Parameters<typeof sseSubagentDone>[0]))
    })
    expect(
      Object.values(store.getState().chat.subagents).some(a => a.status === 'running'),
    ).toBe(false)
  })
})
