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

    // Advance past the 30s reconciliation interval
    await act(async () => { vi.advanceTimersByTime(31_000) })

    await waitFor(() => {
      expect(mockSpawnList).toHaveBeenCalled()
    })

    // After reconciliation, the phantom agent should be cleared
    await waitFor(() => {
      const state = store.getState().chat.subagents['phantom1']
      expect(state?.status).toBe('error')
      expect(state?.error).toContain('reconciliation')
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
