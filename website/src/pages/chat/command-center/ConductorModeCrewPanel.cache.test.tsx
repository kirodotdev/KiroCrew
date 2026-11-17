/**
 * Conductor mode Board: going back to a session paints its last board at once
 * from the cache, past React Query's 5-minute default retention, and the entry
 * read still runs and replaces it. Runs on the dashboard's own client defaults
 * (`newQueryClient`), so the retention under test is the one production gets.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'
import { newQueryClient } from '../../../api/queryClient'
import { CREW_BOARD_GC_MS, crewBoardQueryKey } from '../../../api/crewBoard'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardAction: vi.fn(), sessionSummary: vi.fn(), sendMessage: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel from './ConductorModeCrewPanel'

const board = (root: string, goal: string) => ({ conductor: { schema: 1, slot_key: root, goal, round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', items: [], take_over_available: false })

beforeEach(() => {
  vi.useFakeTimers()
  api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
  api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
})
afterEach(() => { vi.useRealTimers(); vi.clearAllMocks() })

it('paints the cached board on re-entry within the retention, then the entry read replaces it', async () => {
  const qc = newQueryClient()
  const store = createTestStore({ dashboard: { slots: [] } } as never)
  const mount = (root: string) => render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root={root} viewing={root} active /></Provider></QueryClientProvider>)
  const goal = () => screen.getByTestId('conductor-mode-goal').textContent

  api.crewBoard.mockResolvedValueOnce(board('root-1', 'Fix the login bug.'))
  const first = mount('root-1')
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(goal()).toBe('Fix the login bug.')
  first.unmount()

  // Away for half the retention: 15 minutes, far past React Query's 5-minute default.
  await act(async () => { await vi.advanceTimersByTimeAsync(CREW_BOARD_GC_MS / 2) })
  let answer!: (value: unknown) => void
  api.crewBoard.mockReturnValueOnce(new Promise(resolve => { answer = resolve }))
  const again = mount('root-1')
  // The last board is on screen before the entry read answers, and that read was sent.
  expect(goal()).toBe('Fix the login bug.')
  expect(api.crewBoard).toHaveBeenCalledTimes(2)
  await act(async () => { answer(board('root-1', 'Fix the login and signup bugs.')) })
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(goal()).toBe('Fix the login and signup bugs.')
  again.unmount()

  // Another conductor's panel never shows this one's board.
  api.crewBoard.mockReturnValueOnce(new Promise(() => {}))
  const other = mount('root-2')
  expect(goal()).toBe('No goal recorded yet.')
  other.unmount()

  // The retention is bounded: a board nobody shows for that long is dropped.
  await act(async () => { await vi.advanceTimersByTimeAsync(CREW_BOARD_GC_MS) })
  expect(qc.getQueryData(crewBoardQueryKey('root-1'))).toBeUndefined()
})
