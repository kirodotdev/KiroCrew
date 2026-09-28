/**
 * WorkflowProgressBar — the band between the transcript and the composer.
 *
 * The behaviour under test is a LAYOUT invariant, not a class name for its own
 * sake: the band is not a shrinkable flex item, so an unbounded expanded body
 * (phase tree + result + View source, easily taller than the viewport) grows it
 * until the composer is clipped out of view. jsdom has no layout engine, so the
 * bound is asserted through the capping styles the same way the other
 * responsive/overflow suites in this directory do.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import WorkflowProgressBar from '../pages/chat/WorkflowProgressBar'
import chatReducer from '../store/chatSlice'
import type { WorkflowRunProgress } from '../store/chatSlice'

const SLOT = 'chat-1'

const run = (over: Partial<WorkflowRunProgress> = {}): WorkflowRunProgress => ({
  run_id: 'wf_000025',
  name: 'kirocrew-perf-investigation',
  phase: 'critique',
  lastLog: 'Starting Kiro Crew performance investigation',
  status: 'running',
  sessionKey: `dashboard:${SLOT}`,
  ...over,
})

function renderBar(runs: WorkflowRunProgress[]) {
  const store = configureStore({
    reducer: { chat: chatReducer },
    preloadedState: {
      chat: {
        activeSlot: SLOT,
        workflowRuns: Object.fromEntries(runs.map(r => [r.run_id, r])),
      },
    } as never,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <WorkflowProgressBar slot={SLOT} />
      </QueryClientProvider>
    </Provider>,
  )
}

const bar = () => screen.getByTestId('workflow-progress-bar')

describe('WorkflowProgressBar height bound', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ run_id: 'wf_000025', status: 'running', events: [], source: 'x = 1\n' }),
      })),
    )
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders nothing when no run belongs to this slot', () => {
    renderBar([run({ sessionKey: 'dashboard:chat-9' })])
    expect(screen.queryByTestId('workflow-progress-bar')).toBeNull()
  })

  it('does not scroll while collapsed — the one-liner needs no cap', () => {
    renderBar([run()])
    expect(bar().className).toContain('overflow-hidden')
    expect(bar().className).not.toContain('overflow-y-auto')
    expect(bar().className).not.toContain('max-h-')
  })

  it('caps its height and scrolls internally once a run is expanded', async () => {
    const user = userEvent.setup()
    renderBar([run()])
    await user.click(screen.getByRole('button', { expanded: false }))

    // Bounded: the band can no longer outgrow the viewport and clip the composer.
    expect(bar().className).toContain('max-h-[45vh]')
    expect(bar().className).toContain('overflow-y-auto')
    // Scrolling the expanded body must not chain into the transcript behind it.
    expect(bar().className).toContain('overscroll-contain')
  })

  it('drops the cap again when the run is collapsed', async () => {
    const user = userEvent.setup()
    renderBar([run()])
    const row = screen.getByRole('button', { expanded: false })
    await user.click(row)
    expect(bar().className).toContain('max-h-[45vh]')
    await user.click(screen.getByRole('button', { expanded: true }))
    expect(bar().className).not.toContain('max-h-[45vh]')
    expect(bar().className).toContain('overflow-hidden')
  })

  it('caps the whole band, not each row, so several expanded runs stay bounded', async () => {
    const user = userEvent.setup()
    renderBar([run(), run({ run_id: 'wf_000026', name: 'second-run' })])
    const rows = screen.getAllByRole('button', { expanded: false })
    await user.click(rows[0])
    await user.click(screen.getAllByRole('button', { expanded: false })[0])
    expect(screen.getAllByRole('button', { expanded: true })).toHaveLength(2)
    // One cap on the shared container — two expanded rows cannot sum to 90vh.
    expect(bar().className).toContain('max-h-[45vh]')
  })
})
