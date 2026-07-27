import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentSpawn, sseSubagentPending, sseSubagentQueued, sseSubagentDone } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
import { api } from '../api/client'

const SLOT = 'test-slot'

/** Build a store with `running` running agents + optionally one pending agent, all in SLOT. */
function makeStore(running: string[], pending?: string) {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  running.forEach(id => store.dispatch(sseSubagentSpawn({ slot: SLOT, id, task: `task ${id}`, agent: `agent-${id}` })))
  if (pending) store.dispatch(sseSubagentPending({ slot: SLOT, id: pending, task: `task ${pending}`, approval_id: `appr-${pending}` }))
  return store
}

function renderBar(store: ReturnType<typeof makeStore>) {
  return render(
    <Provider store={store}>
      <SubagentProgressBar slot={SLOT} />
    </Provider>,
  )
}

describe('SubagentProgressBar — in-chat stop controls', () => {
  beforeEach(() => vi.clearAllMocks())

  it('stops a single running agent from its per-row button (excludes pending from stop-all)', () => {
    // 1 running + 1 pending: only the running agent is stoppable.
    renderBar(makeStore(['a1'], 'p1'))
    // Header reflects the total active count (running + pending).
    // Exactly one per-row stop button (the running agent, not the pending one).
    const rowStops = screen.getAllByLabelText(/^Stop subagent/)
    expect(rowStops).toHaveLength(1)
    fireEvent.click(rowStops[0])
    expect(api.spawnDelete).toHaveBeenCalledTimes(1)
    expect(api.spawnDelete).toHaveBeenCalledWith('a1')
  })

  it('"Stop all" cancels every running agent but never a pending one', () => {
    renderBar(makeStore(['a1', 'a2'], 'p1'))
    fireEvent.click(screen.getByLabelText('Stop all running subagents'))
    expect(api.spawnDelete).toHaveBeenCalledTimes(2)
    expect(api.spawnDelete).toHaveBeenCalledWith('a1')
    expect(api.spawnDelete).toHaveBeenCalledWith('a2')
    expect(api.spawnDelete).not.toHaveBeenCalledWith('p1')
  })

  it('labels the header stop control "Stop" (not "Stop all") when exactly one agent is stoppable', () => {
    renderBar(makeStore(['a1']))
    expect(screen.getByLabelText('Stop running subagent')).toBeInTheDocument()
    expect(screen.queryByLabelText('Stop all running subagents')).not.toBeInTheDocument()
  })

  it('renders no stop controls when every active agent is pending (stoppableCount === 0)', () => {
    renderBar(makeStore([], 'p1'))
    // The pending agent still shows in the header, but offers no stop affordance.
    expect(screen.queryByLabelText(/^Stop/)).toBeNull()
    expect(api.spawnDelete).not.toHaveBeenCalled()
  })
})

describe('SubagentProgressBar — queued / waiting count', () => {
  beforeEach(() => vi.clearAllMocks())

  it('mounts on a queued-only wave (no running agents yet) and shows the waiting count', () => {
    // Nothing has started (no subagent_spawn) — the wave is only queued.
    // The chip must still appear so the user gets an immediate signal.
    const store = makeStore([])
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 5 }))
    renderBar(store)
    const queued = screen.getByTestId('subagent-queued-count')
    expect(queued).toBeInTheDocument()
    expect(queued.textContent).toContain('5')
    // running count is present and zero
    expect(screen.getByTestId('subagent-running-count').textContent).toContain('0')
  })

  it('stays mounted across the staggered ramp when running momentarily hits zero but agents remain queued', () => {
    const store = makeStore(['a1'])
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 3 }))
    // The only running agent finishes, but 3 are still queued.
    store.dispatch(sseSubagentDone({ slot: SLOT, id: 'a1', elapsed: 2, outcome: 'completed' }))
    renderBar(store)
    // Chip is still present (did not unmount) and shows the waiting count.
    expect(screen.getByTestId('subagent-histogram')).toBeInTheDocument()
    expect(screen.getByTestId('subagent-queued-count').textContent).toContain('3')
  })

  it('hides the waiting segment once the queue drains to zero', () => {
    const store = makeStore(['a1'])
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 2 }))
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 0 }))
    renderBar(store)
    expect(screen.queryByTestId('subagent-queued-count')).toBeNull()
  })

  it('unmounts entirely when nothing is running and nothing is queued', () => {
    const store = makeStore([])
    const { container } = renderBar(store)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('sseSubagentQueued reducer', () => {
  function freshStore() {
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    store.dispatch(setActiveSlot(SLOT))
    return store
  }

  it('stores the queued count keyed by slot', () => {
    const store = freshStore()
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 4 }))
    expect(store.getState().chat.subagentQueued[SLOT]).toBe(4)
  })

  it('deletes the entry when count reaches zero (keeps the map clean)', () => {
    const store = freshStore()
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 4 }))
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 0 }))
    expect(store.getState().chat.subagentQueued[SLOT]).toBeUndefined()
  })

  it('clamps negative / garbage payloads to a non-negative integer', () => {
    const store = freshStore()
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: -3 as unknown as number }))
    expect(store.getState().chat.subagentQueued[SLOT]).toBeUndefined()
    store.dispatch(sseSubagentQueued({ slot: SLOT, queued: 2.9 }))
    expect(store.getState().chat.subagentQueued[SLOT]).toBe(2)
  })
})
