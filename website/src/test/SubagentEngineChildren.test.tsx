import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentSpawn, sseSubagentDone, sseSubagentSnapshot, sseSubagentTool } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { SubagentActivity } from '../types'

// jsdom polyfill: the Subagents panel's controls use ResizeObserver.
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

vi.mock('../api/client', () => ({
  api: {
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnStopAll: vi.fn().mockResolvedValue({}),
    spawnRetry: vi.fn().mockResolvedValue({}),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
  },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
import ActivityViewer from '../pages/chat/ActivityViewer'
import { api } from '../api/client'

/**
 * A KAS engine sub-agent (the `kas_subagents` preview on) is a `native:*` card the
 * gateway marks `engine: true`. The chat's own turn runs it, so the wave chip
 * lists it; nothing stops it alone and the engine reports no credits or model
 * for it, so the chip and the Subagents card say both in words. A `native:*`
 * card without the mark keeps the path it had.
 */

const SLOT = 'test-slot'
const E1 = 'native:11111111-aaaa'
const E2 = 'native:22222222-bbbb'

function makeStore() {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  return store
}
type Store = ReturnType<typeof makeStore>

const engine = (store: Store, id: string, task: string) =>
  store.dispatch(sseSubagentSpawn({ slot: SLOT, id, task, agent: 'kirocrew-helper', engine: true }))
const managed = (store: Store, id: string) =>
  store.dispatch(sseSubagentSpawn({ slot: SLOT, id, task: `task ${id}`, agent: 'kirocrew' }))

function renderBar(store: Store) {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <SubagentProgressBar slot={SLOT} />
      </Provider>
    </QueryClientProvider>,
  )
}

function renderPanel(subagents: Record<string, SubagentActivity>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={makeStore()}>
      <QueryClientProvider client={qc}>
        <ActivityViewer subagents={subagents} toolLog={[]} open onToggle={vi.fn()} slot={SLOT} view="subagents" />
      </QueryClientProvider>
    </Provider>,
  )
}

const card = (over: Partial<SubagentActivity>): SubagentActivity => ({
  id: E1, task: 'Run sleep 20; echo one', agent: 'kirocrew-helper', status: 'running',
  streaming: '', lastTool: '', startedAt: 0, elapsed: 0, ...over,
})

describe('wave chip: engine sub-agents', () => {
  beforeEach(() => vi.clearAllMocks())

  it('lists one row per engine sub-agent, with its task and its live tool', () => {
    const store = makeStore()
    engine(store, E1, 'Run sleep 20; echo one')
    engine(store, E2, 'Run sleep 35; echo two')
    store.dispatch(sseSubagentTool({ slot: SLOT, id: E1, tool: 'Wait 20 seconds then print one', tool_call_id: 'c1', tool_count: 1 }))
    renderBar(store)

    expect(screen.getAllByTestId('subagent-row')).toHaveLength(2)
    expect(screen.getByTestId('subagent-running-count')).toHaveTextContent('2')
    expect(screen.getByText('Run sleep 20; echo one')).toBeInTheDocument()
    expect(screen.getByText('Run sleep 35; echo two')).toBeInTheDocument()
    expect(screen.getByText('→ Wait 20 seconds then print one')).toBeInTheDocument()
  })

  it('offers no Stop of any kind when only engine sub-agents run, and says what stops them', () => {
    const store = makeStore()
    engine(store, E1, 'one')
    engine(store, E2, 'two')
    renderBar(store)

    expect(screen.queryByRole('button', { name: /stop/i })).not.toBeInTheDocument()
    expect(screen.getByTestId('subagent-engine-note')).toHaveTextContent(
      'Sub-agents that run inside the chat stop when you stop the answer.',
    )
    expect(api.spawnStopAll).not.toHaveBeenCalled()
    expect(api.spawnDelete).not.toHaveBeenCalled()
  })

  it('names the answer on Stop all while an engine sub-agent runs beside a managed one', async () => {
    // One managed run alone reads "Stop"; with an engine sub-agent open the same
    // press ends that sub-agent and the answer, so the control says so.
    const store = makeStore()
    managed(store, 'a1')
    engine(store, E1, 'one')
    renderBar(store)

    const stop = screen.getByTestId('subagent-stop-all')
    expect(stop).toHaveAccessibleName('Stop all and the answer')
    expect(stop).toHaveTextContent('Stop all and the answer')
    // The managed row keeps its own Stop; the engine row has none.
    expect(screen.getAllByLabelText(/^Stop subagent/)).toHaveLength(1)
    fireEvent.click(stop)
    await waitFor(() => expect(api.spawnStopAll).toHaveBeenCalledWith(SLOT))
  })

  it('keeps the plain label once the engine sub-agent has ended', () => {
    const store = makeStore()
    managed(store, 'a1')
    engine(store, E1, 'one')
    store.dispatch(sseSubagentDone({ slot: SLOT, id: E1, elapsed: 3, engine: true }))
    renderBar(store)

    expect(screen.getByTestId('subagent-stop-all')).toHaveAccessibleName('Stop running subagent')
    expect(screen.queryByTestId('subagent-engine-note')).not.toBeInTheDocument()
    expect(screen.getByTestId('subagent-done-count')).toHaveTextContent('1')
  })

  it('counts a failed engine sub-agent and offers no retry for it', () => {
    const store = makeStore()
    engine(store, E1, 'one')
    engine(store, E2, 'two')
    store.dispatch(sseSubagentDone({ slot: SLOT, id: E1, elapsed: 3, error: 'Sub-agent execution was cancelled', engine: true }))
    renderBar(store)

    expect(screen.getByTestId('subagent-failed-count')).toHaveTextContent('1')
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument()
  })

  it('leaves an unmarked native card out of the chip, as before', () => {
    const store = makeStore()
    managed(store, 'a1')
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'native:sess-x', task: 'nested', agent: 'x' }))
    renderBar(store)

    expect(screen.getAllByTestId('subagent-row')).toHaveLength(1)
    expect(screen.getByTestId('subagent-stop-all')).toHaveAccessibleName('Stop running subagent')
    expect(screen.queryByTestId('subagent-engine-note')).not.toBeInTheDocument()
  })

  describe('the 30 s inventory sweep', () => {
    beforeEach(() => vi.useFakeTimers())
    afterEach(() => vi.useRealTimers())

    it('does not end an engine row the spawn inventory never lists', async () => {
      const store = makeStore()
      engine(store, E1, 'one')
      renderBar(store)

      await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })

      expect(api.spawnList).toHaveBeenCalled()
      expect(store.getState().chat.subagents[E1].status).toBe('running')
    })
  })
})

describe('store: the engine mark', () => {
  it('is kept by a done frame and by a replayed snapshot, and never invented', () => {
    const store = makeStore()
    engine(store, E1, 'one')
    store.dispatch(sseSubagentDone({ slot: SLOT, id: E1, elapsed: 2 }))
    expect(store.getState().chat.subagents[E1].engine).toBe(true)

    // A card the replay creates (a tab opened mid-turn) carries the mark too.
    store.dispatch(sseSubagentSnapshot({ id: E2, slot: SLOT, task: 'two', agent: 'kirocrew-helper', streaming: '', last_tool: '', started: 1, engine: true }))
    expect(store.getState().chat.subagents[E2].engine).toBe(true)
    // And one a done frame creates (a tab opened after it ended).
    store.dispatch(sseSubagentDone({ slot: SLOT, id: 'native:33333333', elapsed: 2, engine: true, result: 'r' }))
    expect(store.getState().chat.subagents['native:33333333'].engine).toBe(true)

    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'native:sess-x', task: 'nested', agent: 'x' }))
    expect(store.getState().chat.subagents['native:sess-x'].engine).toBeUndefined()
  })
})

describe('Subagents card: engine sub-agents', () => {
  it('shows no Cancel on a running engine card and says what stops it', () => {
    renderPanel({ [E1]: card({ engine: true }) })

    expect(screen.queryByTestId('subagent-cancel-btn')).not.toBeInTheDocument()
    expect(screen.getByTestId('subagent-engine-note')).toHaveTextContent(
      'Sub-agents that run inside the chat stop when you stop the answer.',
    )
    expect(screen.getByText('Run sleep 20; echo one')).toBeInTheDocument()
  })

  it('keeps Cancel on a running native card that is not marked', () => {
    renderPanel({ [E1]: card({}) })

    expect(screen.getByTestId('subagent-cancel-btn')).toBeInTheDocument()
    expect(screen.queryByTestId('subagent-engine-note')).not.toBeInTheDocument()
  })

  it('says in words that credits and model are not reported, instead of a figure', () => {
    renderPanel({ [E1]: card({ engine: true, status: 'done', elapsed: 26, result: 'one' }) })
    fireEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByTestId('subagent-engine-usage')).toHaveTextContent(
      "Credits and model are not reported for a sub-agent that runs inside the chat. Its credits are part of the answer's total.",
    )
    expect(screen.queryByTestId('subagent-credit-usage')).not.toBeInTheDocument()
    expect(screen.queryByTestId('subagent-model')).not.toBeInTheDocument()
  })

  it('says "(no output)" for an engine sub-agent that ended with none', () => {
    renderPanel({ [E1]: card({ engine: true, status: 'error', elapsed: 0, error: 'The user rejected this tool call.' }) })
    fireEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByTestId('subagent-output-body')).toHaveTextContent('(no output)')
    expect(screen.queryByText('(output shown in chat)')).not.toBeInTheDocument()
    expect(screen.getByText('The user rejected this tool call.')).toBeInTheDocument()
  })

  it('keeps "(output shown in chat)" and no usage line on a finished unmarked native card', () => {
    renderPanel({ [E1]: card({ status: 'done', elapsed: 4 }) })
    fireEvent.click(screen.getByRole('button', { expanded: false }))

    expect(screen.getByTestId('subagent-output-body')).toHaveTextContent('(output shown in chat)')
    expect(screen.queryByTestId('subagent-engine-usage')).not.toBeInTheDocument()
  })
})
