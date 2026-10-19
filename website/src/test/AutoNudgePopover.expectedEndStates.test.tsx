import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'
import { EXPECTED_END_STATES } from '../components/expectedEndStates'

// The popover's failure classification must DEFER to the shared closed list
// (`expectedEndStates.ts`), not keep a private copy of which reasons are benign.
// `extraExpected` lets one test widen the list: if the popover consulted only
// its own failure set, a listed reason would still render as an error.
const extraExpected = vi.hoisted(() => ({ states: [] as string[] }))
vi.mock('../components/expectedEndStates', async importOriginal => {
  const actual = await importOriginal<typeof import('../components/expectedEndStates')>()
  return {
    ...actual,
    isExpectedEndState: (state: unknown) =>
      actual.isExpectedEndState(state) || (typeof state === 'string' && extraExpected.states.includes(state)),
  }
})

const SLOT = 'chat-1-100'

const paused = (stopped_reason: string): AutoNudgeLoop => ({
  id: 'l1', slot_key: SLOT, message: 'goal', idle_secs: 90, max_cycles: 3, cycle_count: 3,
  active: false, last_fire_ts: 0, next_due_ts: 0, stopped_reason,
})

function renderPaused(reason: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  render(
    <QueryClientProvider client={qc}>
      <AutoNudgePopover slotKey={SLOT} loop={paused(reason)} open onOpenChange={() => {}} onChange={() => {}} onFired={() => {}} />
    </QueryClientProvider>,
  )
  return screen.getByTestId('auto-nudge-status')
}

describe('AutoNudgePopover routes its status through the shared expected-end-state list', () => {
  beforeEach(() => {
    extraExpected.states = []
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ loop: null }) })) as unknown as typeof fetch)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('an interrupted cycle is on the list and renders as a status line, not an error', () => {
    expect(EXPECTED_END_STATES).toContain('interrupted_cycle')
    const status = renderPaused('interrupted_cycle')
    expect(status.getAttribute('role')).not.toBe('alert')
    expect(status.className).toMatch(/\bbg-warn-subtle\b/)
    expect(screen.queryByTestId('auto-nudge-status-box')).toBeNull()
  })

  it('a failure NOT on the list still renders the error notice', () => {
    const status = renderPaused('structural_terminal')
    expect(status.getAttribute('role')).toBe('alert')
    expect(screen.getByTestId('auto-nudge-status-box')).toBeTruthy()
  })

  it('the list decides: a failure reason the list names renders as status', () => {
    // Fails if the popover classifies from its private failure set alone.
    extraExpected.states = ['structural_terminal']
    const status = renderPaused('structural_terminal')
    expect(status.getAttribute('role')).not.toBe('alert')
    expect(status.className).toMatch(/\bbg-warn-subtle\b/)
    expect(screen.queryByTestId('auto-nudge-status-box')).toBeNull()
  })
})
