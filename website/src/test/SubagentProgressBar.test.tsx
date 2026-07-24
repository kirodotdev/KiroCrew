import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentSpawn, sseSubagentPending } from '../store/chatSlice'
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
