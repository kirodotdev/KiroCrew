/**
 * A sub-agent parked on an unanswered spawn approval must not render as running
 * (#7318).
 *
 * The wave chip's header count and its per-agent row both treated `'pending'` as
 * running: the count sat behind a spinning `Loader2` and the row rendered a bare
 * task label with a ticking elapsed timer -- pixel-identical to an agent that had
 * launched a process and was working. The run had in fact launched nothing: it
 * was registered, counted, and blocked on an approval prompt the user had not
 * answered, so the one number the chip exists to publish was asserting the
 * opposite of the truth.
 *
 * These pin the acceptance criteria: the parked run is excluded from the running
 * count, it is reported under its own count instead, its row names the approval,
 * and the chip stays mounted when the parked run is the ONLY member of the wave
 * (excluding it from `running` must not make the surface disappear).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, {
  setActiveSlot, sseSubagentPending, sseSubagentSpawn, sseSubagentStalled,
} from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: { spawnDelete: vi.fn().mockResolvedValue({}), spawnList: vi.fn().mockResolvedValue({ agents: [] }) },
}))

import SubagentProgressBar from '../pages/chat/SubagentProgressBar'

const SLOT = 'test-slot'
const PARKED_LABEL = 'Waiting for your approval to start'

function chip({ parked = 0, running = 0, seed }: {
  parked?: number
  running?: number
  /** Extra frames folded in BEFORE render — a dispatch after render would need
   *  act() and, if forgotten, silently asserts against the first paint. */
  seed?: (dispatch: (a: unknown) => void) => void
} = {}) {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(setActiveSlot(SLOT))
  for (let i = 0; i < parked; i++) {
    // The real producer: useWebSocket routes an `approval` frame whose id is
    // `spawn:<agent_id>` into sseSubagentPending, which is the only writer of
    // status 'pending' and always carries the approval_id.
    store.dispatch(sseSubagentPending({ slot: SLOT, id: `p${i}`, task: `parked ${i}`, approval_id: `spawn:p${i}` }))
  }
  for (let i = 0; i < running; i++) {
    store.dispatch(sseSubagentSpawn({ slot: SLOT, id: `r${i}`, task: `running ${i}`, agent: 'kirocrew' }))
  }
  seed?.(store.dispatch as unknown as (a: unknown) => void)
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const { container } = render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}><SubagentProgressBar slot={SLOT} /></Provider>
    </QueryClientProvider>,
  )
  return { container, store }
}

const text = (el: Element | null) => (el?.textContent ?? '').trim()
const runningCount = (c: HTMLElement) => text(c.querySelector('[data-testid="subagent-running-count"]'))
const awaitingCount = (c: HTMLElement) => text(c.querySelector('[data-testid="subagent-awaiting-count"]'))

beforeEach(() => vi.clearAllMocks())

describe('subagent parked on a spawn approval', () => {
  it('is not counted as running, and is counted as awaiting', () => {
    const { container } = chip({ parked: 1, running: 2 })
    expect(runningCount(container)).toBe('2')
    expect(awaitingCount(container)).toBe('1')
  })

  it('names the approval on its own row instead of leaving it blank', () => {
    const { container } = chip({ parked: 1 })
    const rows = container.querySelectorAll('[data-testid="subagent-row"]')
    expect(rows).toHaveLength(1)
    expect(rows[0].textContent).toContain(PARKED_LABEL)
  })

  it('keeps the chip mounted when the parked run is the whole wave', () => {
    // Regression guard for the fix itself: `running` no longer includes the
    // parked run, so a mount predicate of `running > 0 || queued > 0` would
    // unmount the one surface naming what the wave is blocked on.
    const { container } = chip({ parked: 1 })
    expect(runningCount(container)).toBe('0')
    expect(container.querySelector('[data-testid="subagent-histogram"]')).not.toBeNull()
  })

  it('reports no awaiting count when nothing is parked', () => {
    const { container } = chip({ running: 1 })
    expect(runningCount(container)).toBe('1')
    expect(container.querySelector('[data-testid="subagent-awaiting-count"]')).toBeNull()
  })

  it('prefers the approval over a stall verdict on the same run', () => {
    // The reaper measures an ABSENCE of stream events, which a run that never
    // started produces trivially. Naming the approval is strictly more specific,
    // and it is the only one of the two the user can act on.
    const { container } = chip({
      parked: 1,
      seed: d => d(sseSubagentStalled({ slot: SLOT, id: 'p0', stalled: true, idle_secs: 300 })),
    })
    const row = container.querySelector('[data-testid="subagent-row"]')
    expect(row?.textContent).toContain(PARKED_LABEL)
    expect(row?.textContent).not.toContain('possibly stalled')
  })

  it('leaves a pending entry with no approval_id in the running count', () => {
    // `approval_id` is the discriminator: without one, nothing proves the entry
    // is blocked on the user, so it must keep its previous treatment rather than
    // be reported under a state the user is being asked to resolve.
    const { container } = chip({
      running: 1,
      seed: d => d(sseSubagentPending({ slot: SLOT, id: 'x1', task: 'no approval id', approval_id: '' })),
    })
    expect(runningCount(container)).toBe('2')
    expect(container.querySelector('[data-testid="subagent-awaiting-count"]')).toBeNull()
  })
})
