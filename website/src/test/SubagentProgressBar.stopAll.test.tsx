import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseSubagentSpawn, sseSubagentPending, sseSubagentQueued } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    spawnDelete: vi.fn().mockResolvedValue({}),
    spawnStopAll: vi.fn().mockResolvedValue({ ok: true, cancelled: 0, unqueued: 0 }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
import { api } from '../api/client'

const SLOT = 'test-slot'

/** Store with `running` running agents, `queued` waiting-to-start count, and
 *  optionally one approval-parked pending agent — all in SLOT. */
function makeStore(running: string[], queued = 0, pending?: string) {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  running.forEach(id => store.dispatch(sseSubagentSpawn({ slot: SLOT, id, task: `task ${id}`, agent: `agent-${id}` })))
  if (queued > 0) store.dispatch(sseSubagentQueued({ slot: SLOT, queued }))
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

describe('SubagentProgressBar — server-side Stop all (#8270)', () => {
  beforeEach(() => vi.clearAllMocks())

  it('"Stop all" makes ONE stop-all call with the slot instead of a per-id loop', () => {
    renderBar(makeStore(['a1', 'a2']))
    fireEvent.click(screen.getByLabelText('Stop all running subagents'))
    expect(api.spawnStopAll).toHaveBeenCalledTimes(1)
    expect(api.spawnStopAll).toHaveBeenCalledWith(SLOT)
    // The per-id loop is only the fallback path — never fired on success.
    expect(api.spawnDelete).not.toHaveBeenCalled()
  })

  it('stays visible and stoppable with a queued-only wave (nothing running yet)', () => {
    // Members accepted behind the stagger/concurrency gate exist only as an
    // aggregate count — the window where the old running-only control was
    // absent and the wave could not be stopped before it started draining.
    // The aria-label is the STATUS-NEUTRAL copy: announcing "running
    // subagents" to a screen reader while 0 are running would misstate the
    // exact scope this fix widened past.
    renderBar(makeStore([], 3))
    expect(screen.getByTestId('subagent-queued-count')).toHaveTextContent('3')
    expect(screen.queryByLabelText('Stop all running subagents')).not.toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Stop all subagents'))
    expect(api.spawnStopAll).toHaveBeenCalledWith(SLOT)
  })

  it('falls back to the per-id loop when the stop-all request fails', async () => {
    vi.mocked(api.spawnStopAll).mockRejectedValueOnce(new Error('boom'))
    renderBar(makeStore(['a1', 'a2']))
    fireEvent.click(screen.getByLabelText('Stop all running subagents'))
    await waitFor(() => expect(api.spawnDelete).toHaveBeenCalledTimes(2))
    expect(api.spawnDelete).toHaveBeenCalledWith('a1')
    expect(api.spawnDelete).toHaveBeenCalledWith('a2')
  })

  it('the fallback targets the ids snapshotted at CLICK time, not at rejection time', async () => {
    // The rejection callback runs later; reading the live ref then would
    // target whatever the ref holds at that moment (e.g. a different slot
    // after a chat switch) — cancelling another session's work. Agents that
    // appear between click and rejection must not be touched.
    vi.mocked(api.spawnStopAll).mockRejectedValueOnce(new Error('boom'))
    const store = makeStore(['a1', 'a2'])
    renderBar(store)
    fireEvent.click(screen.getByLabelText('Stop all running subagents'))
    // A new agent lands after the click but before the rejection resolves.
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: 'late1', task: 'task late1', agent: 'agent-late1' }))
    await waitFor(() => expect(api.spawnDelete).toHaveBeenCalledTimes(2))
    expect(api.spawnDelete).toHaveBeenCalledWith('a1')
    expect(api.spawnDelete).toHaveBeenCalledWith('a2')
    expect(api.spawnDelete).not.toHaveBeenCalledWith('late1')
  })

  it('a failed stop-all on a queued-only wave surfaces an ErrorNotice (no silent no-op)', async () => {
    // The per-id fallback has nothing to send for queued-only state, so the
    // notice is the only trace of a stop that never landed; the chip stays
    // mounted with the live queued count and the control remains for a retry.
    vi.mocked(api.spawnStopAll).mockRejectedValueOnce(new Error('boom'))
    renderBar(makeStore([], 3))
    fireEvent.click(screen.getByLabelText('Stop all subagents'))
    await waitFor(() => expect(screen.getByTestId('subagent-stopall-error')).toBeInTheDocument())
    expect(api.spawnDelete).not.toHaveBeenCalled()
    expect(screen.getByTestId('subagent-queued-count')).toBeInTheDocument()
    // A retry press starts clean: the stale notice is cleared before the call.
    fireEvent.click(screen.getByLabelText('Stop all subagents'))
    await waitFor(() => expect(screen.queryByTestId('subagent-stopall-error')).not.toBeInTheDocument())
    expect(api.spawnStopAll).toHaveBeenCalledTimes(2)
  })

  it('a single QUEUED member gets the neutral Stop all copy, not "Stop running subagent"', () => {
    renderBar(makeStore([], 1))
    expect(screen.getByLabelText('Stop all subagents')).toBeInTheDocument()
    expect(screen.queryByLabelText('Stop running subagent')).not.toBeInTheDocument()
  })

  it('an approval-parked wave alone shows no stop control (approval card owns it)', () => {
    renderBar(makeStore([], 0, 'p1'))
    expect(screen.queryByLabelText(/^Stop all/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Stop running subagent')).not.toBeInTheDocument()
  })
})
