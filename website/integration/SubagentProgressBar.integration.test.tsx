import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import SubagentProgressBar from '../src/pages/chat/SubagentProgressBar'
import { createTestStore, renderWithProviders } from './helpers'
import { sseSubagentSpawn, sseSubagentDone, sseSubagentTool } from '../src/store/chatSlice'
import type { RootState } from '../src/store'

// Mock api.spawnList for reconciliation tests
vi.mock('../src/api/client', () => ({
  api: {
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
}))
import { api } from '../src/api/client'
const mockSpawnList = vi.mocked(api.spawnList)

const SLOT = 'chat-test-slot'

function makeAgent(id: string, task: string, overrides?: Partial<Parameters<typeof sseSubagentSpawn>[0]>) {
  return { slot: SLOT, id, task, agent: 'kirocrew', ...overrides }
}

function storeWithActiveSlot(): ReturnType<typeof createTestStore> {
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
    } as RootState['chat'],
  })
}

describe('SubagentProgressBar', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockSpawnList.mockReset().mockResolvedValue({ agents: [] })
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders nothing when no subagents are active', () => {
    const store = storeWithActiveSlot()
    const { container } = renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })
    expect(container.innerHTML).toBe('')
  })

  it('shows agent count when subagents are running', () => {
    const store = storeWithActiveSlot()
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('a1', 'Search codebase'))) })

    expect(screen.getByTestId('subagent-running-count').textContent?.trim()).toBe('1')
  })

  it('shows plural count for multiple agents', () => {
    const store = storeWithActiveSlot()
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => {
      store.dispatch(sseSubagentSpawn(makeAgent('a1', 'Search codebase')))
      store.dispatch(sseSubagentSpawn(makeAgent('a2', 'Read CHANGELOG')))
      store.dispatch(sseSubagentSpawn(makeAgent('a3', 'Count lines')))
    })

    expect(screen.getByTestId('subagent-running-count').textContent?.trim()).toBe('3')
  })

  it('shows task previews without an extra expansion step', () => {
    const store = storeWithActiveSlot()
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('a1', 'Search the entire codebase for uses of SessionManager'))) })

    expect(screen.getByText(/Search the entire codebase/)).toBeInTheDocument()
    expect(screen.getAllByTestId('subagent-row')).toHaveLength(1)
  })

  it('opens the subagents sidebar when an agent row is clicked', async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    const store = storeWithActiveSlot()
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('a1', 'Search codebase'))) })

    expect(screen.queryByText('View agents')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /open search codebase in subagents sidebar/i }))

    expect(store.getState().chat.activityOpen).toBe(true)
    expect(store.getState().chat.activityTab).toBe('subagents')
  })

  it('shows one summary row for each active agent', () => {
    const store = storeWithActiveSlot()
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => {
      store.dispatch(sseSubagentSpawn(makeAgent('a1', 'Task one')))
      store.dispatch(sseSubagentSpawn(makeAgent('a2', 'Task two')))
    })

    expect(screen.getAllByTestId('subagent-row')).toHaveLength(2)
    expect(screen.getByText('Task one')).toBeInTheDocument()
    expect(screen.getByText('Task two')).toBeInTheDocument()
  })

  it('shows current tool when agent is using a tool', () => {
    const store = storeWithActiveSlot()
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => {
      store.dispatch(sseSubagentSpawn(makeAgent('a1', 'Search codebase')))
      store.dispatch(sseSubagentTool({ slot: SLOT, id: 'a1', tool: 'readFile' }))
    })

    expect(screen.getByText('→ readFile')).toBeInTheDocument()
  })

  it('hides when all agents complete', () => {
    const store = storeWithActiveSlot()
    const { container } = renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('a1', 'Search codebase'))) })
    expect(screen.getByTestId('subagent-running-count').textContent?.trim()).toBe('1')

    act(() => { store.dispatch(sseSubagentDone({ slot: SLOT, id: 'a1', elapsed: 5 })) })
    expect(container.innerHTML).toBe('')
  })

  it('decrements count as agents finish', () => {
    const store = storeWithActiveSlot()
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => {
      store.dispatch(sseSubagentSpawn(makeAgent('a1', 'Task one')))
      store.dispatch(sseSubagentSpawn(makeAgent('a2', 'Task two')))
    })
    expect(screen.getByTestId('subagent-running-count').textContent?.trim()).toBe('2')

    act(() => { store.dispatch(sseSubagentDone({ slot: SLOT, id: 'a1', elapsed: 3 })) })
    expect(screen.getByTestId('subagent-running-count').textContent?.trim()).toBe('1')
  })

  it('reconciliation clears phantom agents after 30s', async () => {
    mockSpawnList.mockResolvedValue({ agents: [] })
    const store = storeWithActiveSlot()
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('phantom1', 'Phantom task'))) })
    expect(screen.getByTestId('subagent-running-count').textContent?.trim()).toBe('1')

    // Advance past the reconcile grace period (15s) + one adaptive poll tick (5s)
    await act(async () => { vi.advanceTimersByTime(21_000) })

    await waitFor(() => {
      expect(mockSpawnList).toHaveBeenCalled()
    })

    // After reconciliation, the phantom agent is cleared. The run-boundary
    // eviction (hasActive true→false) may have already deleted the record,
    // so we check either: status='error' (before eviction) or deleted (after).
    await waitFor(() => {
      const state = store.getState().chat.subagents['phantom1']
      // Either marked error, or already evicted by clearTerminalSubagents
      expect(state?.status === 'error' || state === undefined).toBe(true)
    })
  })

  it('reconciliation preserves agents still running on backend', async () => {
    mockSpawnList.mockResolvedValue({
      agents: [{ id: 'real1', done: false, parent: `dashboard:${SLOT}` }],
    })
    const store = storeWithActiveSlot()
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('real1', 'Real task'))) })
    expect(screen.getByTestId('subagent-running-count').textContent?.trim()).toBe('1')

    await act(async () => { vi.advanceTimersByTime(31_000) })

    await waitFor(() => { expect(mockSpawnList).toHaveBeenCalled() })

    // Agent should still be running — not cleared
    expect(screen.getByTestId('subagent-running-count').textContent?.trim()).toBe('1')
    expect(store.getState().chat.subagents['real1']?.status).toBe('running')
  })

  // Asserting on final store state is unreliable here: the run-boundary
  // eviction (hasActive true→false) can delete the record before the assertion
  // runs, so `state === undefined` satisfies any disjunction and the test
  // passes whether or not the outcome was forwarded. Assert on the dispatched
  // ACTION instead — that is the thing under test.
  function recordDispatches(store: ReturnType<typeof createTestStore>) {
    const seen: { type: string; payload: Record<string, unknown> }[] = []
    const orig = store.dispatch.bind(store)
    // @ts-expect-error test-only dispatch spy
    store.dispatch = (action: { type: string; payload: Record<string, unknown> }) => {
      if (action && typeof action.type === 'string') seen.push({ type: action.type, payload: action.payload })
      return orig(action as never)
    }
    return seen
  }

  it('does NOT resurrect evicted agents from a reconcile that was already in flight', async () => {
    // Run-boundary eviction (hasActive true->false) clears terminal agents. A
    // reconcile request that STARTED before that boundary lands afterwards; if
    // it is applied, its snapshot re-spawns the very agents that were just
    // cleared. The request's own start time is compared against the eviction
    // watermark for exactly this reason -- `dataUpdatedAt` cannot substitute,
    // because the response always ARRIVES after the eviction.
    let release: (v: { agents: unknown[] }) => void = () => {}
    mockSpawnList.mockImplementation(
      () => new Promise((res) => { release = res as typeof release }),
    )
    const store = storeWithActiveSlot()
    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('ghost1', 'evicted task'))) })
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    // The mount fetch is now in flight (started BEFORE the boundary below).
    await waitFor(() => { expect(mockSpawnList).toHaveBeenCalled() })

    // Cross the run boundary: the agent goes terminal, then hasActive falls to
    // false, which evicts the terminal record.
    const seen = recordDispatches(store)
    await act(async () => {
      store.dispatch(sseSubagentDone({ slot: SLOT, id: 'ghost1', elapsed: 1 }))
    })
    await waitFor(() => { expect(store.getState().chat.subagents['ghost1']).toBeUndefined() })

    // Now let the pre-boundary response land.
    await act(async () => {
      release({ agents: [{ id: 'ghost1', done: false, parent: `dashboard:${SLOT}` }] })
      await vi.runAllTimersAsync()
    })

    // The stale snapshot must not re-spawn the evicted agent.
    const resurrected = seen.filter(
      (d) => d.type === sseSubagentSpawn.type && d.payload?.id === 'ghost1',
    )
    expect(resurrected).toHaveLength(0)
    expect(store.getState().chat.subagents['ghost1']).toBeUndefined()
  })

  it('does NOT resurrect evicted terminal agents from a poll that started AFTER eviction', async () => {
    // The request-level guard (startedAt <= evictedAtRef) handles polls that
    // were IN FLIGHT when eviction fired. But a NEWER poll (startedAt > eviction)
    // passes that guard. The backfill must independently reject terminal records
    // whose backend `started` timestamp predates the eviction watermark.
    //
    // Simulate: zombie1 runs and finishes (eviction fires), then a later poll
    // returns it as done with a backend started time before the eviction.
    let resolve: (v: { agents: unknown[] }) => void = () => {}
    mockSpawnList.mockImplementation(
      () => new Promise((r) => { resolve = r as typeof resolve }),
    )
    const store = storeWithActiveSlot()
    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('zombie1', 'old task'))) })
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })
    // Let mount fetch begin
    await waitFor(() => { expect(mockSpawnList).toHaveBeenCalled() })
    // Resolve the mount fetch with zombie1 running (not yet done)
    await act(async () => {
      resolve({ agents: [{ id: 'zombie1', done: false, parent: `dashboard:${SLOT}`, started: Date.now() / 1000 - 10 }] })
      await vi.advanceTimersByTimeAsync(100)
    })

    // Now zombie1 finishes and eviction fires (hasActive->false)
    await act(async () => {
      store.dispatch(sseSubagentDone({ slot: SLOT, id: 'zombie1', elapsed: 5 }))
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(store.getState().chat.subagents['zombie1']).toBeUndefined()

    // Start a new run so hasActive goes true and polling resumes.
    // The backend still reports zombie1 as done (not yet pruned server-side).
    const zombieStartedEpoch = Date.now() / 1000 - 11 // definitely before eviction
    mockSpawnList.mockImplementation(
      () => new Promise((r) => { resolve = r as typeof resolve }),
    )
    const seen = recordDispatches(store)
    await act(async () => {
      store.dispatch(sseSubagentSpawn(makeAgent('alive1', 'new run')))
      await vi.advanceTimersByTimeAsync(3000)
    })

    // Resolve the new poll (startedAt is NOW > eviction, passes request guard)
    await act(async () => {
      resolve({ agents: [
        { id: 'alive1', done: false, parent: `dashboard:${SLOT}`, started: Date.now() / 1000 },
        { id: 'zombie1', done: true, parent: `dashboard:${SLOT}`, started: zombieStartedEpoch, elapsed: 5 },
      ] })
      await vi.advanceTimersByTimeAsync(100)
    })

    // zombie1 must not reappear
    const resurrected = seen.filter(
      (d) => d.type === sseSubagentSpawn.type && d.payload?.id === 'zombie1',
    )
    expect(resurrected).toHaveLength(0)
    expect(store.getState().chat.subagents['zombie1']).toBeUndefined()
  })

  it('reconciliation forwards the terminal OUTCOME, not just terminality', async () => {
    // The backfill dispatches sseSubagentDone for an agent the backend reports
    // as finished. Passing only {slot,id,elapsed} makes the reducer classify
    // every such agent as 'done' — so a run that FAILED on the backend is
    // reconciled into a success. /api/spawn returns error/stopped/outcome for
    // exactly this reason (see api_spawn_list), so they must be forwarded.
    mockSpawnList.mockResolvedValue({
      agents: [{
        id: 'failed1', done: true, parent: `dashboard:${SLOT}`,
        outcome: 'failed', error: 'boom', elapsed: 4,
      }],
    })
    const store = storeWithActiveSlot()
    const seen = recordDispatches(store)
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('failed1', 'Task that fails'))) })
    await act(async () => { vi.advanceTimersByTime(21_000) })
    await waitFor(() => { expect(mockSpawnList).toHaveBeenCalled() })

    await waitFor(() => {
      const done = seen.filter(a => a.type.endsWith('sseSubagentDone') && a.payload?.id === 'failed1')
      expect(done.length).toBeGreaterThan(0)
      // Dropping these fields silently turns a failure into a success in the
      // chip, the sidebar and the run card at once.
      expect(done.some(a => a.payload.outcome === 'failed')).toBe(true)
      expect(done.some(a => a.payload.error === 'boom')).toBe(true)
    })
  })

  it('reconciliation forwards a user-stopped outcome', async () => {
    mockSpawnList.mockResolvedValue({
      agents: [{
        id: 'stopped1', done: true, parent: `dashboard:${SLOT}`,
        outcome: 'stopped', stopped: true, elapsed: 2,
      }],
    })
    const store = storeWithActiveSlot()
    const seen = recordDispatches(store)
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('stopped1', 'Task that is cancelled'))) })
    await act(async () => { vi.advanceTimersByTime(21_000) })
    await waitFor(() => { expect(mockSpawnList).toHaveBeenCalled() })

    await waitFor(() => {
      const done = seen.filter(a => a.type.endsWith('sseSubagentDone') && a.payload?.id === 'stopped1')
      expect(done.length).toBeGreaterThan(0)
      expect(done.some(a => a.payload.outcome === 'stopped')).toBe(true)
      expect(done.some(a => a.payload.stopped === true)).toBe(true)
    })
  })

  it('two panes share ONE reconcile request instead of polling independently', async () => {
    // The chip used to call api.spawnList() from its own setInterval, so every
    // split pane issued its own identical request against the same fleet-wide
    // endpoint. The poll is now a React Query useQuery on a slot-free key, so
    // concurrent panes dedupe onto a single in-flight fetch.
    mockSpawnList.mockResolvedValue({ agents: [{ id: 'x1', done: false, parent: `dashboard:${SLOT}` }] })
    const store = storeWithActiveSlot()
    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('x1', 'shared'))) })

    renderWithProviders(
      <>
        <SubagentProgressBar slot={SLOT} />
        <SubagentProgressBar slot={SLOT} />
      </>,
      { store },
    )
    // Wait for BOTH panes to be on screen, then let the microtask/timer queue
    // drain, so a second pane's independent fetch would have had its chance to
    // fire. Asserting straight after the first call would race it and pass even
    // when each pane polls separately.
    await waitFor(() => {
      expect(screen.getAllByTestId('subagent-running-count').length).toBe(2)
    })
    await waitFor(() => { expect(mockSpawnList).toHaveBeenCalled() })
    await act(async () => { vi.advanceTimersByTime(1_000) })
    // Two mounted panes, one request — not two.
    expect(mockSpawnList.mock.calls.length).toBe(1)
  })

  it('cold backfill preserves failure state instead of converting to success', async () => {
    // GPT 5.6 finding: cold mount after failure/stop -> absent entry dispatches
    // sseSubagentSpawn with done=true but loses outcome/error -> reducer records
    // as success. Fix: follow up with sseSubagentDone carrying the terminal fields.
    mockSpawnList.mockResolvedValue({
      agents: [{
        id: 'fail1', done: true, error: 'timeout', outcome: 'failed' as const,
        parent: `dashboard:${SLOT}`, task: 'research', agent: 'default', elapsed: 30,
      }],
    })
    const store = storeWithActiveSlot()
    // No prior sseSubagentSpawn — simulates a cold mount
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })
    await waitFor(() => { expect(mockSpawnList).toHaveBeenCalled() })
    await act(async () => { vi.advanceTimersByTime(500) })
    // The agent must be recorded as error, not done
    const state = store.getState()
    const agent = state.chat.subagents['fail1']
    expect(agent).toBeDefined()
    expect(agent.status).toBe('error')
    expect(agent.error).toBe('timeout')
  })

  it('truncates task preview to 80 characters', () => {
    const store = storeWithActiveSlot()
    renderWithProviders(<SubagentProgressBar slot={SLOT} />, { store })

    const longTask = 'A'.repeat(100)
    act(() => { store.dispatch(sseSubagentSpawn(makeAgent('a1', longTask))) })

    // Should show 80 chars + ellipsis
    const preview = screen.getByText(/^A+…$/)
    expect(preview.textContent).toBe('A'.repeat(80) + '…')
  })
})
